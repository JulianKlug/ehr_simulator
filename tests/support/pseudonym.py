"""Pseudonymisation test secret."""

from __future__ import annotations

from ehr_simulator.pseudonym import SECRET_BYTES

#: Fixed secret for tests that build bundles in memory.
TEST_SECRET = bytes(range(SECRET_BYTES))
