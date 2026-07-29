"""Token model and storage tests."""

from __future__ import annotations

import json
import time

import pytest

from myq.tokens import FileTokenStore, Tokens


def make_tokens(**kw) -> Tokens:
    base = dict(
        access_token="at",
        refresh_token="rt",
        expires_at=time.time() + 1800,
        account_id="acct",
        device_serial="SN1",
    )
    base.update(kw)
    return Tokens(**base)


def test_file_store_round_trip(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    original = make_tokens()
    store.save(original)
    loaded = store.load()
    assert loaded.access_token == "at"
    assert loaded.refresh_token == "rt"
    assert loaded.account_id == "acct"
    assert loaded.device_serial == "SN1"


def test_file_store_missing_file_returns_none(tmp_path):
    assert FileTokenStore(tmp_path / "nope.json").load() is None


def test_file_store_ignores_corrupt_file(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json")
    assert FileTokenStore(path).load() is None


def test_file_store_rejects_payload_without_access_token(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"refresh_token": "rt"}))
    assert FileTokenStore(path).load() is None


def test_file_store_is_owner_only(tmp_path):
    """Tokens on disk open a garage door — they must not be world readable."""
    path = tmp_path / "tokens.json"
    FileTokenStore(path).save(make_tokens())
    assert path.stat().st_mode & 0o077 == 0


def test_from_dict_accepts_expires_in_from_a_proxy_capture():
    """Payloads pasted straight from a capture carry relative expiry."""
    tokens = Tokens.from_dict(
        {"access_token": "at", "refresh_token": "rt", "expires_in": 1800}
    )
    assert 1700 < tokens.expires_at - time.time() <= 1800


def test_from_dict_accepts_legacy_token_scope_key():
    tokens = Tokens.from_dict(
        {"access_token": "at", "expires_in": 60, "token_scope": "custom scope"}
    )
    assert tokens.scope == "custom scope"


def test_from_dict_requires_access_token():
    with pytest.raises(ValueError):
        Tokens.from_dict({"refresh_token": "rt"})


def test_is_expired_respects_skew():
    tokens = make_tokens(expires_at=time.time() + 100)
    assert tokens.is_expired() is False
    assert tokens.is_expired(skew=300) is True


def test_redacted_never_exposes_secrets():
    summary = make_tokens().redacted()
    assert "at" not in summary.values()
    assert "rt" not in summary.values()
    assert summary["has_refresh_token"] is True
