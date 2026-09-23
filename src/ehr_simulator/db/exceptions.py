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


class StudyIdentityError(ValueError):
    """S11a: a database and a study_id disagree about identity.

    Raised when a database is already bound to a *different* study, when
    an unbound-but-populated database would be silently adopted by the
    strict (serve) path, or when the ``study_id`` itself is malformed.
    Subclasses :class:`ValueError` so generic handlers catch it.
    """
