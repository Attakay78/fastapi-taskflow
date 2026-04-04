"""
Authentication for TaskAdmin.

Supports three auth configurations passed to TaskAdmin(auth=...):

* ``("user", "pass")``                  – single credential pair
* ``[("user", "pass"), ...]``           – multiple credential pairs
* ``TaskAuthBackend`` instance          – custom backend

Tokens are HMAC-SHA256 signed, no external dependencies required.
"""

from __future__ import annotations

import abc
import base64
import hashlib
import hmac
import json
import time
from typing import Union

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

COOKIE_NAME = "taskflow_token"

_FAVICON = (
    "data:image/svg+xml,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'>"
    "<rect width='200' height='200' rx='40' ry='40' fill='%23111111'/>"
    "<polyline points='20,100 55,100 80,145 120,55 145,100 180,100' "
    "fill='none' stroke='white' stroke-width='18' "
    "stroke-linecap='round' stroke-linejoin='round'/></svg>"
)


# ---------------------------------------------------------------------------
# Public backend ABC
# ---------------------------------------------------------------------------


class TaskAuthBackend(abc.ABC):
    """
    Base class for custom authentication backends.

    Subclass this and pass an instance to ``TaskAdmin(auth=...)``::

        class MyBackend(TaskAuthBackend):
            def authenticate_user(self, username: str, password: str) -> bool:
                return check_db(username, password)

        TaskAdmin(app, task_manager, auth=MyBackend())
    """

    @abc.abstractmethod
    def authenticate_user(self, username: str, password: str) -> bool:
        """Return ``True`` if *username* / *password* are valid credentials."""


# ---------------------------------------------------------------------------
# Internal simple backend
# ---------------------------------------------------------------------------


class _SimpleAuthBackend(TaskAuthBackend):
    def __init__(self, credentials: list[tuple[str, str]]) -> None:
        self._store = {u: p for u, p in credentials}

    def authenticate_user(self, username: str, password: str) -> bool:
        expected = self._store.get(username)
        if expected is None:
            return False
        return hmac.compare_digest(expected, password)


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------


def resolve_backend(
    auth: Union[tuple, list, TaskAuthBackend, None],
) -> TaskAuthBackend | None:
    if auth is None:
        return None
    if isinstance(auth, TaskAuthBackend):
        return auth
    if isinstance(auth, tuple) and len(auth) == 2:
        return _SimpleAuthBackend([auth])
    if isinstance(auth, list):
        return _SimpleAuthBackend(auth)
    raise TypeError(
        "auth must be a (username, password) tuple, a list of such tuples, "
        "or a TaskAuthBackend instance."
    )


# ---------------------------------------------------------------------------
# Token helpers (HMAC-SHA256, stdlib only)
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (padding % 4))


def create_token(secret_key: str, expiry: int) -> str:
    header = _b64url_encode(b'{"alg":"HS256","typ":"JWT"}')
    body = _b64url_encode(
        json.dumps({"exp": int(time.time()) + expiry}).encode()
    )
    signing_input = f"{header}.{body}"
    sig = hmac.new(
        secret_key.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url_encode(sig)}"


def verify_token(secret_key: str, token: str) -> bool:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        signing_input = f"{parts[0]}.{parts[1]}"
        expected = _b64url_encode(
            hmac.new(
                secret_key.encode(), signing_input.encode(), hashlib.sha256
            ).digest()
        )
        if not hmac.compare_digest(expected, parts[2]):
            return False
        payload = json.loads(_b64url_decode(parts[1]))
        return int(payload.get("exp", 0)) > int(time.time())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# FastAPI dependency factory
# ---------------------------------------------------------------------------


def make_api_guard(secret_key: str):
    """Returns a FastAPI ``Depends`` that raises 401 for invalid/missing tokens."""

    def _guard(request: Request) -> None:
        token = request.cookies.get(COOKIE_NAME, "")
        if not verify_token(secret_key, token):
            raise HTTPException(status_code=401, detail="Unauthorized")

    return Depends(_guard)


# ---------------------------------------------------------------------------
# Login page HTML
# ---------------------------------------------------------------------------


def _login_html(action: str, error: str = "") -> str:
    error_block = (
        f'<div class="error">{error}</div>' if error else ""
    )
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sign in \u00b7 fastapi-taskflow</title>
  <link rel="icon" type="image/svg+xml" href="{_FAVICON}">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Geist', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f5f5f5;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      -webkit-font-smoothing: antialiased;
    }}
    .card {{
      background: #fff;
      border: 1px solid #e5e7eb;
      border-radius: 12px;
      padding: 2rem;
      width: 100%;
      max-width: 360px;
    }}
    .logo-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 1.75rem;
    }}
    .logo-title {{
      font-size: 15px;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: #111;
    }}
    label {{
      display: block;
      font-size: 13px;
      font-weight: 500;
      color: #374151;
      margin-bottom: 5px;
    }}
    input[type=text], input[type=password] {{
      width: 100%;
      padding: 8px 12px;
      border: 1px solid #d1d5db;
      border-radius: 7px;
      font-size: 14px;
      font-family: inherit;
      color: #111;
      background: #fff;
      outline: none;
      transition: border-color .15s;
      margin-bottom: 1rem;
    }}
    input:focus {{ border-color: #111; box-shadow: 0 0 0 3px rgba(0,0,0,.06); }}
    button {{
      width: 100%;
      padding: 9px;
      background: #111;
      color: #fff;
      border: none;
      border-radius: 7px;
      font-size: 14px;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      transition: opacity .15s;
    }}
    button:hover {{ opacity: 0.82; }}
    .error {{
      background: #fef2f2;
      border: 1px solid #fecaca;
      color: #dc2626;
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 13px;
      margin-bottom: 1rem;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo-row">
      <svg width="28" height="28" viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
        <rect width="200" height="200" rx="40" ry="40" fill="#111111"/>
        <polyline points="20,100 55,100 80,145 120,55 145,100 180,100"
          fill="none" stroke="white" stroke-width="18"
          stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span class="logo-title">fastapi-taskflow</span>
    </div>
    {error_block}
    <form method="post" action="{action}">
      <label for="username">Username</label>
      <input id="username" name="username" type="text"
             autocomplete="username" autofocus required>
      <label for="password">Password</label>
      <input id="password" name="password" type="password"
             autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Auth router
# ---------------------------------------------------------------------------


def create_auth_router(
    backend: TaskAuthBackend,
    secret_key: str,
    token_expiry: int,
    prefix: str,
) -> APIRouter:
    router = APIRouter(prefix=prefix, include_in_schema=False)
    login_path = f"{prefix}/auth/login"
    dashboard_path = f"{prefix}/dashboard"

    @router.get("/auth/login", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        return HTMLResponse(_login_html(action=login_path))

    @router.post("/auth/login", response_class=HTMLResponse)
    async def login(
        username: str = Form(...),
        password: str = Form(...),
    ):
        if backend.authenticate_user(username, password):
            token = create_token(secret_key, token_expiry)
            response = RedirectResponse(url=dashboard_path, status_code=302)
            response.set_cookie(
                COOKIE_NAME,
                token,
                httponly=True,
                max_age=token_expiry,
                samesite="lax",
            )
            return response
        return HTMLResponse(
            _login_html(action=login_path, error="Invalid username or password."),
            status_code=401,
        )

    @router.get("/auth/logout")
    def logout():
        response = RedirectResponse(url=login_path, status_code=302)
        response.delete_cookie(COOKIE_NAME)
        return response

    return router
