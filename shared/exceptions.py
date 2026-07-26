"""shared/exceptions.py — domain-specific exceptions.

Routes translate these into HTTP status codes. Nothing in the codebase
catches bare Exception; every error has a name and a handler.

Imported by: everything.
"""
from __future__ import annotations


class ServicingError(Exception):
    """Base class so routes can catch the whole domain in one clause."""

    http_status: int = 400


class PolicyNotFound(ServicingError):
    """Requested policy ID does not exist in /policies."""

    http_status = 404


class PolicyNotBound(ServicingError):
    """No approved policy is registered for this intent — fail closed, hand off."""

    http_status = 422


class EffectUnverified(ServicingError):
    """A write was submitted but its effect could not be confirmed.

    Not a failure and not a success. Reconcile; never retry the write.
    """

    http_status = 503


class IdempotencyConflict(ServicingError):
    """Duplicate idempotency key submitted with different inputs."""

    http_status = 409


class LedgerIntegrityError(ServicingError):
    """Hash-chain verification failed."""

    http_status = 500


class MemberNotFound(ServicingError):
    """No account for the given member ID."""

    http_status = 404


class InsufficientData(ServicingError):
    """Policy cannot evaluate because required account facts are missing."""

    http_status = 422
