"""Unit tests for polymarket.env project-.env discovery. Filesystem only, no network."""

import os

from polymarket import env as env_mod


def _fresh(monkeypatch):
    monkeypatch.setattr(env_mod, "_loaded", False)


def test_env_found_walking_up_from_subdir(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PMT_TEST_WALKUP=from_dotenv\n")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    _fresh(monkeypatch)
    try:
        env_mod.load_project_env()
        assert os.environ.get("PMT_TEST_WALKUP") == "from_dotenv"
    finally:
        os.environ.pop("PMT_TEST_WALKUP", None)


def test_env_nearest_file_wins(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PMT_TEST_NEAREST=outer\n")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / ".env").write_text("PMT_TEST_NEAREST=inner\n")
    monkeypatch.chdir(inner)
    _fresh(monkeypatch)
    try:
        env_mod.load_project_env()
        assert os.environ.get("PMT_TEST_NEAREST") == "inner"
    finally:
        os.environ.pop("PMT_TEST_NEAREST", None)


def test_env_never_overrides_exported_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("PMT_TEST_EXPORTED", "exported")
    (tmp_path / ".env").write_text("PMT_TEST_EXPORTED=from_dotenv\n")
    monkeypatch.chdir(tmp_path)
    _fresh(monkeypatch)
    env_mod.load_project_env()
    assert os.environ["PMT_TEST_EXPORTED"] == "exported"


def test_env_idempotent_second_call_is_noop(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PMT_TEST_FIRST=1\n")
    monkeypatch.chdir(tmp_path)
    _fresh(monkeypatch)
    try:
        env_mod.load_project_env()
        assert os.environ.get("PMT_TEST_FIRST") == "1"
        (tmp_path / ".env").write_text("PMT_TEST_SECOND=2\n")
        env_mod.load_project_env()  # already loaded → no re-read
        assert "PMT_TEST_SECOND" not in os.environ
    finally:
        os.environ.pop("PMT_TEST_FIRST", None)
        os.environ.pop("PMT_TEST_SECOND", None)


def test_env_noop_when_no_file_found(tmp_path, monkeypatch):
    # Ancestors of tmp_path may hold unrelated .env files, but never our sentinel.
    monkeypatch.chdir(tmp_path)
    _fresh(monkeypatch)
    env_mod.load_project_env()
    assert "PMT_TEST_ABSENT" not in os.environ
