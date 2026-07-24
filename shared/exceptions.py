"""
shared/exceptions.py

Named domain errors. Each carries the HTTP status it should become, so
api/routes.py can map the whole family in one clause:

    except ServicingError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc))

Nothing in the codebase catches bare Exception.
"""


class ServicingError(Exception):
    http_status = 400


class PolicyNotFound(ServicingError):
    http_status = 404


class IdempotencyConflict(ServicingError):
    http_status = 409


class LedgerIntegrityError(ServicingError):
    http_status = 500


class MemberNotFound(ServicingError):
    http_status = 404


class InsufficientData(ServicingError):
    http_status = 422
