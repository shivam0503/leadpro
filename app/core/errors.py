from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", None)
        logger.exception("Unhandled error (request_id={}): {}", rid, exc)
        # Avoid leaking internals in prod
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "internal_server_error", "request_id": rid},
        )
