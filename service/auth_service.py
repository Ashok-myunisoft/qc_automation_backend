"""QC login using the ERP's own login service.

How it works
------------
1. The user types user code + database + password on the QC login screen.
2. This server calls the ERP login address (ERP_LOGIN_URL in .env) ONCE to check them.
3. If the ERP says OK, we give the browser a QC token (valid for TOKEN_HOURS).
4. Every /api/* call and the WebSocket must carry that token.

The ERP password and the ERP's own tokens are NEVER stored or logged.
The login address is only for login. The Test Environment form (baseUrl / db / user / password)
that people fill in to run tests is a separate thing and is not touched here.

.env settings
-------------
ERP_LOGIN_URL = https://api-live.goodbookserp.in
QC_JWT_SECRET = <a long random text, at least 32 characters - keep it secret>
AUTH_REQUIRED = false      # keep false until the new login screen is in; then set true
TOKEN_HOURS   = 8          # optional
"""
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque

import jwt  # pip install PyJWT
import requests
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

load_dotenv()  # read .env even if this module is imported before anything else does

logger = logging.getLogger(__name__)

ERP_AUTH_PATH = "/fws/Authentication/AuthenticateUserNative"
_ALGO = "HS256"

# ---- settings are read at call time so a changed .env is picked up after a restart ----


def _erp_url() -> str:
    url = (os.getenv("ERP_LOGIN_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("ERP_LOGIN_URL is not set in .env")
    return url + ERP_AUTH_PATH


def _secret() -> str:
    secret = os.getenv("QC_JWT_SECRET") or ""
    if len(secret) < 32:
        raise RuntimeError("QC_JWT_SECRET is missing or too short (use at least 32 random characters)")
    return secret


def auth_required() -> bool:
    return (os.getenv("AUTH_REQUIRED") or "false").strip().lower() in ("1", "true", "yes")


def _token_hours() -> float:
    try:
        return float(os.getenv("TOKEN_HOURS") or 8)
    except ValueError:
        return 8.0


# ---- 1. ask the ERP "is this a real user?" ----

class ErpLoginError(Exception):
    """Wrong user / password / database, or the ERP could not be reached."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


def erp_authenticate(user_code: str, password: str, database: str) -> dict:
    """Returns the few user details we keep. Raises ErpLoginError when the login is not accepted."""
    try:
        resp = requests.post(
            _erp_url(),
            json={
                "UserCode": user_code,
                "Password": password,
                "ConnectionName": database,
                "PushNotificationToken": "",
            },
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "Mozilla/5.0 (QC-Automation-Server)",
            },
            timeout=20,
        )
    except requests.RequestException as e:
        logger.warning("ERP login service not reachable: %s", type(e).__name__)
        raise ErpLoginError("Could not reach the ERP login service. Try again in a moment.", 502)

    try:
        data = resp.json()
    except ValueError:
        logger.warning("ERP login returned a non-JSON answer (HTTP %s)", resp.status_code)
        raise ErpLoginError("The ERP login service gave an unexpected answer.", 502)

    # A wrong password still comes back as HTTP 200, with "Status": 400 inside the JSON.
    if data.get("Status") != 200:
        body = data.get("Body")
        message = body.strip() if isinstance(body, str) and body.strip() else "Login failed."
        # the body is plain text like "Incorrect password.." on failure - safe to show
        raise ErpLoginError(message[:200], 401)

    try:
        inner = json.loads(data.get("Body") or "{}")
    except ValueError:
        raise ErpLoginError("The ERP login service gave an unexpected answer.", 502)

    code = inner.get("UserCode")
    if not code:
        raise ErpLoginError("Login failed.", 401)

    # Keep only what the QC tool needs. No ERP tokens, no password.
    return {
        "userCode": code,
        "userName": inner.get("UserName") or code,
        "database": inner.get("DatabaseName") or database,
        "email": inner.get("UserPrimaryMailId") or "",
        "isAdmin": bool(inner.get("IsAdmin")),
        "erpUserId": inner.get("UserId"),
    }


# ---- 2. our own QC token ----

def make_token(user: dict) -> str:
    now = int(time.time())
    payload = {**user, "iat": now, "exp": now + int(_token_hours() * 3600)}
    return jwt.encode(payload, _secret(), algorithm=_ALGO)


def decode_token(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return jwt.decode(token, _secret(), algorithms=[_ALGO])
    except jwt.PyJWTError:
        return None


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def request_user(request: Request) -> dict | None:
    """The logged-in user of an HTTP request (None when not logged in)."""
    return getattr(request.state, "user", None)


def ws_user(websocket) -> dict | None:
    """The logged-in user of a WebSocket. The browser sends the token as ?token=...
    While AUTH_REQUIRED is false, everybody is 'anonymous' so nothing breaks during roll-out."""
    if not auth_required():
        return {"userCode": "anonymous", "userName": "Anonymous", "database": "", "email": "", "isAdmin": False}
    return decode_token(websocket.query_params.get("token"))


# ---- 3. simple protection against password guessing ----

_FAILS: dict[str, deque] = defaultdict(deque)
_MAX_FAILS = 5
_WINDOW_SECONDS = 300


def _too_many_fails(key: str) -> bool:
    now = time.time()
    q = _FAILS[key]
    while q and now - q[0] > _WINDOW_SECONDS:
        q.popleft()
    return len(q) >= _MAX_FAILS


# ---- 4. the login / me routes ----

router = APIRouter()


class LoginBody(BaseModel):
    userCode: str
    database: str
    password: str


@router.post("/api/auth/login")
def api_login(body: LoginBody):
    user_code = body.userCode.strip()
    database = body.database.strip()
    if not user_code or not database or not body.password:
        raise HTTPException(status_code=400, detail="Enter user code, database and password.")

    key = f"{user_code.lower()}|{database.lower()}"
    if _too_many_fails(key):
        raise HTTPException(status_code=429, detail="Too many wrong attempts. Wait 5 minutes and try again.")

    try:
        user = erp_authenticate(user_code, body.password, database)
    except ErpLoginError as e:
        if e.status == 401:
            _FAILS[key].append(time.time())
        raise HTTPException(status_code=e.status, detail=e.message)
    except RuntimeError as e:  # a missing .env setting - show it in the server log, not to the user
        logger.error("login is not configured: %s", e)
        raise HTTPException(status_code=500, detail="Login is not configured on the server.")

    _FAILS.pop(key, None)
    logger.info("QC login OK: %s (%s)", user["userCode"], user["database"])  # no password, no tokens
    try:  # remember the name for the Admin screen - in the background, so a slow database never delays a login
        from service.roles_service import remember_user
        threading.Thread(target=remember_user, args=(user,), daemon=True).start()
    except Exception:  # noqa: BLE001
        pass
    return {"token": make_token(user), "user": user, "expiresInHours": _token_hours()}


@router.get("/api/auth/me")
def api_me(request: Request):
    user = request_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return {"user": user}


# ---- 5. one call in app.py switches everything on ----

_OPEN_PATHS = {"/api/auth/login", "/docs", "/openapi.json", "/redoc"}


def install_auth(app) -> None:
    """Call once, BEFORE the CORS middleware is added in app.py."""
    app.include_router(router)

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path in _OPEN_PATHS or not path.startswith("/api/"):
            return await call_next(request)

        user = decode_token(_bearer(request))
        request.state.user = user
        if user is None and auth_required():
            return JSONResponse({"detail": "Please log in."}, status_code=401)
        if user is None:  # roll-out mode: let the request through as anonymous
            request.state.user = {"userCode": "anonymous", "userName": "Anonymous"}
        return await call_next(request)