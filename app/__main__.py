"""Production entrypoint:  python -m app

Reads PORT from the environment (Railway/Render inject it) and falls back to
APP_PORT / 8000. Doing this in Python means the start command never depends on
shell expansion such as ``${PORT:-8000}`` — Railway custom start commands are
executed without a shell, so that syntax reaches uvicorn as a literal string
and the container crash-loops with "'${PORT:-8000}' is not a valid integer".
"""
from __future__ import annotations

import os

import uvicorn


def _port() -> int:
    raw = os.environ.get("PORT") or os.environ.get("APP_PORT") or "8000"
    try:
        return int(raw)
    except ValueError:
        return 8000


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("APP_HOST", "0.0.0.0"),
        port=_port(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
