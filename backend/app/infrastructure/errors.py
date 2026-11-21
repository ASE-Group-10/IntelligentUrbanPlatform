from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", str(uuid4()))


async def api_error_handler(request: Request, error: APIError):
    return JSONResponse(
        status_code=error.status,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "correlation_id": correlation_id(request),
                **error.extra,
            }
        },
    )
