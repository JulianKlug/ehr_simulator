"""DB-layer exceptions.

:class:`DbError` is raised by DAO functions when they wrap a
:class:`sqlite3.IntegrityError` the caller is expected to handle (e.g., a
foreign-key violation on an ``events`` insert with a bogus ``session_id``).
``sqlite3.OperationalError`` is not wrapped — boot-time / connection errors
propagate verbatim so lifespan failures surface unambiguously.
"""

from __future__ import annotations


class DbError(Exception):
    """Raised by DAO functions on integrity violations the caller handles."""
