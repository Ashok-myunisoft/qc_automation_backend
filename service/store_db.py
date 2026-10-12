"""PostgreSQL connection for the History / Jobs stores.

Kept separate from db_service.py on purpose: db_service.py talks to the real
application SQL Server (DATABASE_URL) for the business-context agent, using
SQL Server-only queries. History and Jobs live in their own PostgreSQL
database, configured with HISTORY_DATABASE_URL, e.g.

    HISTORY_DATABASE_URL=postgresql+psycopg2://user:password@host:5432/qc_db
"""
import logging
import os
from contextlib import contextmanager
from urllib.parse import unquote, urlparse

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class StoreDbError(Exception):
    pass


def _config() -> dict:
    url = os.getenv("HISTORY_DATABASE_URL")
    if not url:
        raise StoreDbError(
            "missing env var: HISTORY_DATABASE_URL — set a PostgreSQL URL in .env "
            "(postgresql+psycopg2://user:password@host:5432/dbname)"
        )
    parsed = urlparse(url)
    cfg = {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/") or None,
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
    }
    missing = [k for k in ("host", "dbname", "user", "password") if not cfg[k]]
    if missing:
        raise StoreDbError(f"invalid HISTORY_DATABASE_URL — missing {', '.join(missing)}")
    return cfg


@contextmanager
def connection():
    """Yield a psycopg2 connection whose cursors return dict rows.
    Callers commit explicitly, same as the old SQL Server code."""
    cfg = _config()
    conn = psycopg2.connect(
        host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
        user=cfg["user"], password=cfg["password"],
        connect_timeout=10,
        options="-c statement_timeout=30000",
        cursor_factory=RealDictCursor,
    )
    try:
        yield conn
    finally:
        conn.close()