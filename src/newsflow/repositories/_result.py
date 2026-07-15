"""Typing helpers for SQLAlchemy Core result objects."""

from typing import Any, cast

from sqlalchemy import CursorResult, Result


def rowcount(result: Result[Any]) -> int:
    """Rows affected by a DML (UPDATE/DELETE) statement.

    ``AsyncSession.execute()`` is typed to return ``Result[Any]``, but a DML
    statement yields a ``CursorResult`` at runtime — ``.rowcount`` lives there,
    not on the base ``Result``. Casting keeps the call sites honest (and int-typed)
    without scattering ``# type: ignore`` across every repository.
    """
    return cast("CursorResult[Any]", result).rowcount
