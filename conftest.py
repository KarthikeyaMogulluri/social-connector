"""Safety rails for the whole test suite.

1. Every test gets its own throwaway .env, so no test can ever write to your real one.
2. At the end of the session, the real .env must be exactly as it was before (or still absent).
"""
import hashlib
from pathlib import Path

import pytest

import envstore

REAL_ENV = Path(envstore.ENV)


def _fingerprint():
    return hashlib.sha256(REAL_ENV.read_bytes()).hexdigest() if REAL_ENV.exists() else None


@pytest.fixture(scope="session", autouse=True)
def real_env_untouched():
    before = _fingerprint()
    yield
    assert _fingerprint() == before, f"a test modified the real settings file: {REAL_ENV}"


@pytest.fixture(autouse=True)
def isolated_env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(envstore, "ENV", tmp_path / "isolated.env")
