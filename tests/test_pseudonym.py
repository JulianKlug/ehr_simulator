"""Export pseudonyms: HMAC-SHA256(secret, clinician_id) under an operator secret.

::

    --pseudonym-secret FILE ─► load_or_create_secret ─► 32 bytes (mode 600)
    clinician_id (sha256(name)[:16]) ─► pseudonymize ─► 16 hex, unlinkable
                                                         without the secret
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from ehr_simulator.pseudonym import (
    SECRET_BYTES,
    PseudonymSecretError,
    load_or_create_secret,
    pseudonymize,
)
from tests.support.pseudonym import TEST_SECRET

PSEUDONYM_HEX_LENGTH = 16
DB_ID = hashlib.sha256(b"dr. alice").hexdigest()[:16]


def test_same_secret_gives_stable_pseudonyms() -> None:
    assert pseudonymize(TEST_SECRET, DB_ID) == pseudonymize(TEST_SECRET, DB_ID)


def test_different_secret_gives_different_pseudonyms() -> None:
    other = bytes(reversed(TEST_SECRET))
    assert pseudonymize(TEST_SECRET, DB_ID) != pseudonymize(other, DB_ID)


def test_pseudonym_is_not_the_db_id() -> None:
    pseudonym = pseudonymize(TEST_SECRET, DB_ID)
    assert pseudonym != DB_ID
    assert len(pseudonym) == PSEUDONYM_HEX_LENGTH
    int(pseudonym, 16)


def test_missing_secret_is_created_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "pseudonym.secret"

    secret = load_or_create_secret(path)

    assert len(secret) == SECRET_BYTES
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_secret(path) == secret


def test_secret_creation_ignores_a_loose_umask(tmp_path: Path) -> None:
    previous = os.umask(0o000)
    try:
        path = tmp_path / "pseudonym.secret"
        load_or_create_secret(path)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_secret_with_loose_mode_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "pseudonym.secret"
    load_or_create_secret(path)
    path.chmod(0o644)

    with pytest.raises(PseudonymSecretError, match="0600"):
        load_or_create_secret(path)


def test_secret_with_wrong_length_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "pseudonym.secret"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, b"short")
    os.close(fd)

    with pytest.raises(PseudonymSecretError, match="32 bytes"):
        load_or_create_secret(path)


def test_secret_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PseudonymSecretError):
        load_or_create_secret(tmp_path)
