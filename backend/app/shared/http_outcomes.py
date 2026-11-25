"""Contract-shaped error adapter for the C11/C12 HTTP boundary."""

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.infrastructure.errors import APIError, correlation_id
from app.shared.result import Failure


def outcome(request, result):
    if not isinstance(result, Failure):
        return result.value
    return JSONResponse(
        status_code={
            "Invalid": 422,
            "Forbidden": 403,
            "NotFound": 404,
            "Conflict": 409,
            "Unavailable": 503,
            "Unauthenticated": 401,
        }.get(result.kind, 503),
        content={
            "error": {
                "code": result.kind,
                "message": result.message,
                "correlationId": correlation_id(request),
            }
        },
        headers={"Cache-Control": "no-store"},
    )


class ContractRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except APIError as error:
                code = {401: "Unauthenticated", 403: "Forbidden", 422: "Invalid"}.get(
                    error.status, "Unavailable"
                )
                return outcome(request, Failure(code, error.message))
            except (RequestValidationError, ValidationError):
                return outcome(request, Failure("Invalid", "Invalid request fields"))
            except IntegrityError:
                return outcome(
                    request,
                    Failure("Conflict", "Concurrent command; retry with the same key"),
                )
            except SQLAlchemyError:
                return outcome(
                    request,
                    Failure("Unavailable", "Recorded data is temporarily unavailable"),
                )

        return handle
