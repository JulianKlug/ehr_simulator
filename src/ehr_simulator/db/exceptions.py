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


class ConfigurationError(Exception):
    """S11b: base for configuration version-history/provenance failures."""


class ConfigurationActivationError(ConfigurationError):
    """An explicit activation was refused.

    Raised for malformed ``config_version`` / activation metadata,
    colliding or reused version labels, a dataset change within one
    study, and ambiguous S11a provenance that a first activation cannot
    backfill. Nothing is written when this is raised.
    """


class ConfigurationProvenanceError(ConfigurationError):
    """Case-configuration provenance disagrees with the pinned case.

    Raised when a stored row (answer, session, progress, assignment) carries
    a ``config_version``/``config_hash`` that does not match the case's
    registered history, a row missing provenance is read after history
    exists, or a write would omit provenance the schema now requires. An
    operator integrity condition — never guessed, never repaired in place.
    """


class StaleConfigurationError(ConfigurationError):
    """The running server's active configuration no longer matches the DB.

    An external activation landed after this process started; creating a new
    case is refused until the server is restarted (existing pinned cases may
    keep using their historical snapshots).
    """


class RandomisationError(Exception):
    """S11c: a randomisation schedule cannot be generated or stored.

    Raised for a study without ``randomisation`` settings, an allocation
    state that does not cover the configured patients, or a schedule
    requested under a configuration that is not the active one.
    """


class RandomisationIntegrityError(RandomisationError):
    """A stored schedule disagrees with the one being written.

    A clinician holds at most one immutable schedule; a different schedule
    for the same clinician is refused, never overwritten.
    """
