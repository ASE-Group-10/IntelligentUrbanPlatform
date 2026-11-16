"""Shared ``Result<T>`` outcome type used by every component's service API."""

from dataclasses import dataclass
from typing import Literal

FailureKind = Literal[
    "Invalid",
    "Forbidden",
    "NotFound",
    "Conflict",
    "Unavailable",
    "Unconfirmed",
    "Infeasible",
    "Partial",
]


@dataclass(frozen=True)
class Ok[T]:
    """Successful outcome carrying ``value``."""

    value: T


@dataclass(frozen=True)
class Failure:
    """Expected failure with one of the agreed kinds and a human-readable reason."""

    kind: FailureKind
    message: str


type Result[T] = Ok[T] | Failure
