"""Export pseudonyms: keyed, so a staff roster cannot reverse them.

The DB ``clinician_id`` is ``sha256(casefold(name))[:16]``: anyone with a
roster recomputes it. Exports therefore carry
``HMAC-SHA256(secret, clinician_id)[:16]`` instead::

    --pseudonym-secret FILE ─► load_or_create_secret ─► 32 random bytes
         (created once, mode 0600, never inside a bundle)
    clinician_id ─► pseudonymize(secret, ·) ─► 16 hex in every exported table

The same secret gives the same pseudonyms across exports; a new secret
unlinks them. The secret never leaves this module's caller.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from collections.abc import Sequence
from pathlib import Path

__all__ = [
    "SECRET_BYTES",
    "PseudonymSecretError",
    "load_or_create_secret",
    "pseudonymize",
    "require_outside_outputs",
]

SECRET_BYTES = 32
SECRET_MODE = 0o600

#: Same width as the DB id, so exported columns keep their shape.
PSEUDONYM_HEX_LENGTH = 16


class PseudonymSecretError(ValueError):
    """The pseudonym secret file is missing a safety property."""


def pseudonymize(secret: bytes, clinician_id: str) -> str:
    """``HMAC-SHA256(secret, clinician_id)`` truncated to 16 hex characters."""
    digest = hmac.new(secret, clinician_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:PSEUDONYM_HEX_LENGTH]


def require_outside_outputs(
    secret: Path, *, directories: Sequence[Path] = (), files: Sequence[Path] = ()
) -> None:
    """Refuse a secret that would be published with, or overwritten by, an export.

    Raises:
        PseudonymSecretError: ``secret`` lies inside one of ``directories``
            or is one of ``files``.
    """
    resolved = Path(secret).resolve()
    inside = any(
        resolved == d.resolve() or d.resolve() in resolved.parents for d in map(Path, directories)
    )
    clashes = any(resolved == Path(f).resolve() for f in files)
    if inside or clashes:
        raise PseudonymSecretError(
            f"the pseudonym secret {secret} must lie outside --out-dir and differ from "
            "every export file: the bundle must not carry its own key"
        )


def load_or_create_secret(path: Path) -> bytes:
    """Read the 32-byte, mode-0600 secret at ``path``; create it when absent.

    Raises:
        PseudonymSecretError: not POSIX, not a regular file, a mode other
            than 0600, or a length other than 32 bytes.
    """
    if os.name != "posix":
        raise PseudonymSecretError(
            f"cannot keep a pseudonym secret on {os.name}: mode 0600 is not available"
        )

    path = Path(path)
    if not path.exists():
        return _create(path)
    return _read(path)


def _create(path: Path) -> bytes:
    """Exclusive create: a concurrent creator makes this call fail, never overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(SECRET_BYTES)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, SECRET_MODE)
    try:
        os.fchmod(fd, SECRET_MODE)  # the umask may have narrowed it
        os.write(fd, secret)
        os.fsync(fd)
    finally:
        os.close(fd)
    return secret


def _read(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise PseudonymSecretError(f"cannot open pseudonym secret {path}: {exc}") from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PseudonymSecretError(f"pseudonym secret {path} is not a regular file")

        mode = stat.S_IMODE(info.st_mode)
        if mode != SECRET_MODE:
            raise PseudonymSecretError(
                f"pseudonym secret {path} has mode {mode:#o}, not 0600; chmod 600 it"
            )

        secret = os.read(fd, SECRET_BYTES + 1)
    finally:
        os.close(fd)

    if len(secret) != SECRET_BYTES:
        raise PseudonymSecretError(f"pseudonym secret {path} must hold exactly 32 bytes")
    return secret
