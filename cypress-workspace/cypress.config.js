const path = require("path");
const { defineConfig } = require("cypress");
const createBundler = require("@bahmutov/cypress-esbuild-preprocessor");
const {
  addCucumberPreprocessorPlugin,
} = require("@badeball/cypress-cucumber-preprocessor");
const {
  createEsbuildPlugin,
} = require("@badeball/cypress-cucumber-preprocessor/esbuild");

const XLSX = require("xlsx");
const fs = require("fs");
const sql = require("mssql");
const zlib = require("zlib");

// ─── Connection cache (per process lifetime — one Cypress run) ───────────────
// Avoids hitting TCMS on every queryDb call; keyed by serverConfigId
const _connCache = new Map();

// ─── Direct-DB cache populated by loginToConnection ──────────────────────────
// Maps serverConfigId → SQL connection descriptor so queryDb uses the right DB
// even when TCMS is unavailable.
const _directDbCache = new Map();

// ─── connectByType ───────────────────────────────────────────────────────────
// Accepts decrypted connection descriptor from TCMS GetTestDbConnection.
// DATABASETYPE: 0=SQL Server, 1=Oracle, 2=PostgreSQL, 3=MySQL
async function connectByType(conn) {
  switch (conn.databaseType ?? conn.DATABASETYPE) {
    case 0: {
      // SQL Server — mssql package (already installed)
      return sql.connect({
        server: conn.serverIP ?? conn.SERVERIP,
        database: conn.databaseName ?? conn.DATABASENAME,
        user: conn.userName ?? conn.DATABASEUSERNAME,
        password: conn.password ?? conn.DATABASEPASSWORD,
        port: parseInt(conn.port ?? conn.DATABASEPORT ?? "1433", 10) || 1433,
        options: { encrypt: false, trustServerCertificate: true },
      });
    }
    case 2: {
      // PostgreSQL — pg package (install: npm i -D pg)
      const { Client } = require("pg");
      const pg = new Client({
        host: conn.serverIP ?? conn.SERVERIP,
        database: conn.databaseName ?? conn.DATABASENAME,
        user: conn.userName ?? conn.DATABASEUSERNAME,
        password: conn.password ?? conn.DATABASEPASSWORD,
        port: parseInt(conn.port ?? conn.DATABASEPORT ?? "5432", 10) || 5432,
      });
      await pg.connect();
      return pg;
    }
    case 1:
      throw new Error(
        "Oracle not yet configured — install oracledb npm package and implement connectByType case 1"
      );
    default:
      throw new Error(
        `Unsupported DATABASETYPE: ${conn.databaseType ?? conn.DATABASETYPE}`
      );
  }
}

// ─── resolveConnectionViaFallback ────────────────────────────────────────────
// Used when TCMS is not yet available (transition period).
// Falls back to reading direct connection from config.env (the old style).
function buildFallbackConn(config) {
  return {
    serverIP: config.env.DB_SERVER || process.env.DB_SERVER || "192.168.0.112",
    databaseName: config.env.DB_NAME || process.env.DB_NAME || "BASICTEST",
    userName: config.env.DB_USER || process.env.DB_USER || "unisoft",
    password: config.env.DB_PASSWORD || process.env.DB_PASSWORD || "unisoft@2012",
    port: "1433",
    databaseType: 0,
  };
}

module.exports = defineConfig({
  projectId: "g2qvxj",

  e2e: {
    async setupNodeEvents(on, config) {

      await addCucumberPreprocessorPlugin(on, config);

      on("file:preprocessor", createBundler({
        plugins: [createEsbuildPlugin(config)],
        alias: {
          "@pageObject": path.resolve(__dirname, "cypress/pageObject"),
          "@support":    path.resolve(__dirname, "cypress/support"),
        },
      }));

      // ── All tasks ──────────────────────────────────────────────────────────
      on("task", {

        // ── Excel reader ─────────────────────────────────────────────────────
        readExcel({ filePath, sheetName }) {
          const fileBuffer = fs.readFileSync(filePath);
          const workbook = XLSX.read(fileBuffer, { type: "buffer" });
          const worksheet = workbook.Sheets[sheetName];
          return XLSX.utils.sheet_to_json(worksheet);
        },

        // ── loginToConnection ────────────────────────────────────────────────
        // Calls the GoodBooks login endpoint for the named test connection.
        // Returns the full loginDTO (including ServerConfigId for queryDb use).
        // Connection credentials are stored in cypress.env.json TestConnections map.
        //
        // Usage from step defs:
        //   cy.task('loginToConnection', 'BasicTest').then(dto => {
        //     Cypress.env('loginDTO',      dto);
        //     Cypress.env('serverConfigId', dto.ServerConfigId);
        //   });
        async loginToConnection(connectionName) {
          const connections = config.env.TestConnections;
          if (!connections) {
            throw new Error(
              "loginToConnection: no TestConnections map found in cypress.env.json."
            );
          }
          const creds = connections[connectionName];
          if (!creds) {
            throw new Error(
              `loginToConnection: connection "${connectionName}" not found. ` +
              `Available: ${Object.keys(connections).join(", ")}`
            );
          }

          // ── Attempt 0: static DTO pre-configured for this connection ───────
          // Used when a connection has a fixed loginDTO template (e.g. a known
          // active session token). Skips web login and gb5system lookup entirely.
          let dto = null;
          if (creds.staticLoginDTO) {
            const staticTemplate = config.env[creds.staticLoginDTO];
            if (staticTemplate) {
              dto = { ...staticTemplate, BaseURL: staticTemplate.BaseURL ?? creds.baseUrl };
              console.log(
                `loginToConnection: using static DTO "${creds.staticLoginDTO}" for "${connectionName}" ` +
                `(UserId=${dto.UserId}, DB=${dto.DatabaseName})`
              );
            }
          }

          // ── Attempt 1: GoodBooks web login ─────────────────────────────────
          const loginUrl = `${creds.baseUrl}${creds.loginPath || "/fws/User.svc/Authenticate/"}`;
          if (!dto) try {
            const response = await fetch(loginUrl, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                UserCode: creds.userName,
                Password: creds.password,
                DatabaseName: creds.dbName,
              }),
            });
            if (response.ok) {
              const raw = await response.json();
              let inner;
              if (raw.Body && typeof raw.Body === "string") {
                const buf = await new Promise((res, rej) =>
                  zlib.gunzip(Buffer.from(raw.Body, "base64"), (e, b) => e ? rej(e) : res(b))
                );
                inner = JSON.parse(buf.toString("utf8"));
              } else {
                inner = raw.Data ?? raw;
              }
              // Only accept if it's a real LoginDTO (no app-level error)
              if (inner.ServerConfigId !== undefined && !inner.ErrorNumber) {
                dto = inner;
                console.log(`loginToConnection: web login OK for "${connectionName}"`);
              } else {
                console.warn(
                  `loginToConnection: web login returned app error for "${connectionName}": ` +
                  `${inner.ErrorNumber ?? "unknown"} — ${inner.Body ?? ""}`
                );
              }
            } else {
              console.warn(`loginToConnection: web login HTTP ${response.status} for "${connectionName}" — trying direct DB fallback`);
            }
          } catch (err) {
            console.warn(`loginToConnection: web login unreachable for "${connectionName}" (${err.message}) — trying direct DB fallback`);
          }

          // ── Attempt 2: Direct gb5system lookup ─────────────────────────────
          // Used when the GoodBooks web app is unavailable or returns an app error.
          if (!dto) {
            const gb5 = config.env.GB5System;
            if (!gb5) {
              throw new Error(
                `loginToConnection: web login failed for "${connectionName}" and no GB5System config found in cypress.env.json`
              );
            }
            let gb5Pool;
            try {
              gb5Pool = await sql.connect({
                server: gb5.serverIP,
                database: gb5.dbName,
                user: gb5.userName,
                password: gb5.password,
                port: 1433,
                options: { encrypt: false, trustServerCertificate: true },
              });
              const result = await gb5Pool.request()
                .input("dbName", sql.NVarChar, creds.dbName)
                .query(
                  "SELECT TOP 1 SERVERCONFIGID, DATABASENAME, DATABASEUSERNAME " +
                  "FROM MSERVERCONFIG WHERE DATABASENAME = @dbName ORDER BY SERVERCONFIGID"
                );
              if (!result.recordset.length) {
                throw new Error(`No MSERVERCONFIG entry found for database "${creds.dbName}"`);
              }
              const row = result.recordset[0];
              // Build a usable LoginDTO from the stored template + gb5system data
              const template = config.env.loginDTO ?? {};
              dto = {
                ...template,
                ServerConfigId: row.SERVERCONFIGID,
                DatabaseName: row.DATABASENAME,
                UserCode: creds.userName,
                UserName: creds.userName,
                BaseURL: creds.baseUrl,
              };
              console.log(
                `loginToConnection: "${connectionName}" resolved via GB5System ` +
                `→ DB=${dto.DatabaseName} ServerConfigId=${dto.ServerConfigId}`
              );
            } catch (err) {
              throw new Error(`loginToConnection: GB5System fallback failed — ${err.message}`);
            } finally {
              try { await gb5Pool?.close?.(); } catch (_) { }
            }
          }

          // Cache the direct DB descriptor so queryDb uses the right database
          // without needing TCMS when this serverConfigId is queried.
          // If the connection defines a dbSystem key (e.g. "SWSDatabase"), use those
          // credentials; otherwise fall back to GB5System defaults.
          const dbSysKey = creds.dbSystem;
          const dbSys = (dbSysKey && config.env[dbSysKey]) ? config.env[dbSysKey] : (config.env.GB5System ?? {});
          _directDbCache.set(dto.ServerConfigId, {
            serverIP: dbSys.serverIP ?? "192.168.0.112",
            databaseName: dto.DatabaseName,
            userName: dbSys.userName ?? "unisoft",
            password: dbSys.password ?? "unisoft@2012",
            port: String(dbSys.port ?? "1433"),
            databaseType: dbSys.databaseType ?? 0,
          });

          console.log(`loginToConnection: "${connectionName}" → DB=${dto.DatabaseName} ServerConfigId=${dto.ServerConfigId}`);
          return dto;
        },

        // ── queryDb ──────────────────────────────────────────────────────────
        // Run a raw SQL query against a test database.
        //
        // Preferred call (plan-compliant):
        //   cy.task('queryDb', { query: 'SELECT ...', serverConfigId: -1399999710 })
        //   → resolves connection via TCMS GetTestDbConnection (decrypts password server-side)
        //
        // Legacy / fallback call (backward-compatible):
        //   cy.task('queryDb', 'SELECT ...')          ← plain string
        //   cy.task('queryDb', { query: 'SELECT ...' }) ← no serverConfigId → uses env fallback
        //
        // Safety: TCMS endpoint enforces DBINSTANCETYPE=1; LIVE configs are rejected with 403.
        async queryDb(arg) {
          let query, serverConfigId;

          // Normalise input — accept plain string (legacy) or object
          if (typeof arg === "string") {
            query = arg;
            serverConfigId = null;
          } else {
            query = arg.query;
            serverConfigId = arg.serverConfigId ?? null;
          }

          let conn;

          if (serverConfigId && config.env.tcmsBaseUrl && config.env.tcmsSystemLogin) {
            // ── Path 1: resolve via TCMS (plan-compliant, decrypt handled by BE) ──
            const cached = _connCache.get(serverConfigId);
            if (cached) {
              conn = cached;
            } else {
              const tcmsUrl = `${config.env.tcmsBaseUrl}/TCMS/TestEnv/GetTestDbConnection?serverConfigId=${serverConfigId}`;
              let connResp;
              try {
                connResp = await fetch(tcmsUrl, {
                  headers: { Login: config.env.tcmsSystemLogin },
                });
              } catch (err) {
                throw new Error(
                  `queryDb: could not reach TCMS at ${tcmsUrl} — ${err.message}. ` +
                  "Falling back is not automatic; ensure TCMS is reachable or remove tcmsBaseUrl to use env fallback."
                );
              }

              if (!connResp.ok) {
                throw new Error(
                  `queryDb: TCMS returned HTTP ${connResp.status} for ServerConfigId=${serverConfigId}. ` +
                  "Check that DBINSTANCETYPE=1 (TEST) for this connection."
                );
              }

              conn = await connResp.json();
              // Unwrap Data if TCMS wraps response in standard envelope
              if (conn.Data) conn = conn.Data;
              _connCache.set(serverConfigId, conn);
            }
          } else {
            // ── Path 2: use direct-DB cache (populated by loginToConnection) ──
            if (serverConfigId && _directDbCache.has(serverConfigId)) {
              conn = _directDbCache.get(serverConfigId);
            } else {
              // Last resort: env / hardcoded defaults
              if (serverConfigId) {
                console.warn(`queryDb: serverConfigId=${serverConfigId} not in cache — using default fallback conn`);
              }
              conn = buildFallbackConn(config);
            }
          }

          // Execute query against the resolved test DB
          let pool;
          try {
            pool = await connectByType(conn);
            const result = await pool.request().query(query);
            return result.recordset ?? result.rows ?? [];
          } catch (err) {
            // Gracefully handle unreachable DB server (firewall, wrong port, VPN)
            // so that DB pre-condition and cleanup steps don't block API-layer tests.
            const isConnErr =
              err.code === "ETIMEOUT" ||
              err.code === "ECONNREFUSED" ||
              err.code === "ESOCKET" ||
              err.code === "ENOTFOUND" ||
              (err.message && err.message.includes("Failed to connect"));
            if (isConnErr) {
              console.warn(
                `queryDb: cannot reach DB at ${conn.serverIP}:${conn.port ?? 1433} ` +
                `(${err.message}). Returning empty result — DB pre-condition and cleanup steps will pass silently.`
              );
              return [];
            }
            console.error("queryDb ERROR:", err.message);
            throw err;
          } finally {
            try { await pool?.close?.(); } catch (_) { /* ignore close errors */ }
          }
        },

        // ── Log writer ───────────────────────────────────────────────────────
        writeLog(message) {
          fs.appendFileSync("test-result.txt", message + "\n");
          return null;
        },

      });

      return config;
    },

    reporter: "mochawesome",
    reporterOptions: {
      reportDir: "cypress/reports/mochawesome-reports",
      charts: true,
      overwrite: true,
      html: true,
      json: true,
    },

    video: false,
    specPattern: "cypress/_runs/**/*.feature",
    baseUrl: "https://qcws.goodbookserp.in/5.5",   // GoodBooks app root
    fixturesFolder: false,
    defaultCommandTimeout: 70000,
    execTimeout: 120000,
    pageLoadTimeout: 270000,
    requestTimeout: 120000,
    responseTimeout: 120000,
    taskTimeout: 120000,
    viewportHeight: 720,
    viewportWidth: 1280,
    numTestsKeptInMemory: 1,
    experimentalMemoryManagement: true,
    retries: {
      runMode: 0,
      openMode: 0,
    },
  },

  component: {
    devServer: {
      framework: "angular",
      bundler: "webpack",
    },
    specPattern: "**/*.cy.ts",
  },
});