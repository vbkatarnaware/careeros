"""Tests for careeros/providers/_apify_actor_common.py's `run_actor` — v1.3
additions specifically: (1) per-token rotation is silent (no alarming
"token index N failed" print for a normal, recoverable multi-key rotation),
(2) a token that already failed this billing cycle is cached by fingerprint
(`budget.apify_tokens.json`) and skipped on the next call instead of being
retried, and (3) the all-exhausted error message clearly names the fix path.
No real Apify/network call is ever made — `ApifyClient` is mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apify_client.errors import ApifyApiError, ApifyClientError

from careeros import budget
from careeros.providers._apify_actor_common import run_actor
from careeros.providers.base import ProviderError


class _FakeApifyApiError(ApifyApiError):
    """A stand-in raised in place of a real ApifyApiError — the real one
    needs a live HttpResponse and dispatches to a status-code subclass via a
    custom __new__, both overkill for tests. `except ApifyApiError` still
    catches this (it's a real subclass); `run_actor` only ever inspects it
    via `str(e)`."""

    def __new__(cls, message: str):
        return ApifyClientError.__new__(cls)

    def __init__(self, message: str) -> None:
        Exception.__init__(self, message)


def _fake_api_error(message: str) -> ApifyApiError:
    return _FakeApifyApiError(message)


def _client_that_fails_then_succeeds(fail_tokens: set[str], items=None):
    """A fake ApifyClient(token) constructor: tokens in fail_tokens raise on
    .actor(...).call(...); any other token succeeds with a fake run."""
    items = items if items is not None else [{"title": "x"}]

    def _make_client(token):
        client = MagicMock()
        actor_mock = MagicMock()
        if token in fail_tokens:
            actor_mock.call.side_effect = _fake_api_error(f"budget exhausted for {token}")
        else:
            actor_mock.call.return_value = {"defaultDatasetId": "ds1", "usageTotalUsd": 0.01}
            dataset_mock = MagicMock()
            dataset_mock.iterate_items.return_value = iter(items)
            client.dataset.return_value = dataset_mock
        client.actor.return_value = actor_mock
        return client

    return _make_client


def test_rotation_is_silent_no_alarming_print_on_recoverable_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APIFY_TOKENS", "tok-bad,tok-good")
    with patch(
        "careeros.providers._apify_actor_common.ApifyClient",
        side_effect=_client_that_fails_then_succeeds({"tok-bad"}),
    ):
        result = run_actor("fake-provider", {}, "some/actor", {}, careeros_dir=tmp_path)

    assert len(result.items) == 1
    captured = capsys.readouterr()
    assert "token index" not in captured.out
    assert "failed" not in captured.out.lower()


def test_successful_first_token_needs_no_careeros_dir(monkeypatch):
    """Backward-compatible: careeros_dir is optional, and a normal successful
    call never touches it."""
    monkeypatch.setenv("APIFY_TOKENS", "tok-good")
    with patch(
        "careeros.providers._apify_actor_common.ApifyClient",
        side_effect=_client_that_fails_then_succeeds(set()),
    ):
        result = run_actor("fake-provider", {}, "some/actor", {})
    assert len(result.items) == 1


def test_exhausted_token_is_persisted_by_fingerprint_not_raw_value(monkeypatch, tmp_path):
    monkeypatch.setenv("APIFY_TOKENS", "tok-bad,tok-good")
    with patch(
        "careeros.providers._apify_actor_common.ApifyClient",
        side_effect=_client_that_fails_then_succeeds({"tok-bad"}),
    ):
        run_actor("fake-provider", {}, "some/actor", {}, careeros_dir=tmp_path)

    state = budget.load_apify_tokens_state(tmp_path, "2026-07-15")
    assert budget.is_token_exhausted(state, "tok-bad") is True
    assert budget.is_token_exhausted(state, "tok-good") is False
    raw_file = (tmp_path / budget.APIFY_TOKENS_FILENAME).read_text()
    assert "tok-bad" not in raw_file


def test_known_exhausted_token_is_skipped_without_being_tried_again(monkeypatch, tmp_path):
    monkeypatch.setenv("APIFY_TOKENS", "tok-bad,tok-good")
    # Pre-populate the cache as if tok-bad already exhausted earlier this month.
    state = budget.load_apify_tokens_state(tmp_path, "2026-07-15")
    budget.mark_token_exhausted(state, "tok-bad")
    budget.save_apify_tokens_state(tmp_path, state)

    make_client = _client_that_fails_then_succeeds({"tok-bad"})
    calls_made = []

    def _tracking_make_client(token):
        calls_made.append(token)
        return make_client(token)

    with patch(
        "careeros.providers._apify_actor_common.ApifyClient",
        side_effect=_tracking_make_client,
    ):
        result = run_actor("fake-provider", {}, "some/actor", {}, careeros_dir=tmp_path)

    assert "tok-bad" not in calls_made  # skipped entirely, never even constructed a client
    assert calls_made == ["tok-good"]
    assert len(result.items) == 1


def test_all_tokens_already_known_exhausted_raises_before_trying_any(monkeypatch, tmp_path):
    monkeypatch.setenv("APIFY_TOKENS", "tok-a,tok-b")
    state = budget.load_apify_tokens_state(tmp_path, "2026-07-15")
    budget.mark_token_exhausted(state, "tok-a")
    budget.mark_token_exhausted(state, "tok-b")
    budget.save_apify_tokens_state(tmp_path, state)

    calls_made = []

    def _tracking_make_client(token):
        calls_made.append(token)
        raise AssertionError("should never construct a client for an already-exhausted token")

    with patch(
        "careeros.providers._apify_actor_common.ApifyClient",
        side_effect=_tracking_make_client,
    ):
        try:
            run_actor("fake-provider", {}, "some/actor", {}, careeros_dir=tmp_path)
            assert False, "expected ProviderError"
        except ProviderError as e:
            assert "already known exhausted" in str(e)
            assert "APIFY_TOKENS" in str(e)

    assert calls_made == []


def test_all_tokens_exhausted_live_raises_clear_fix_path_message(monkeypatch, tmp_path):
    monkeypatch.setenv("APIFY_TOKENS", "tok-a,tok-b")
    with patch(
        "careeros.providers._apify_actor_common.ApifyClient",
        side_effect=_client_that_fails_then_succeeds({"tok-a", "tok-b"}),
    ):
        try:
            run_actor("fake-provider", {}, "some/actor", {}, careeros_dir=tmp_path)
            assert False, "expected ProviderError"
        except ProviderError as e:
            msg = str(e)
            assert "exhausted this billing cycle" in msg
            assert "APIFY_TOKENS" in msg
            assert "raise your Apify plan" in msg

    # Both tokens should now be cached as exhausted for next time.
    state = budget.load_apify_tokens_state(tmp_path, "2026-07-15")
    assert budget.is_token_exhausted(state, "tok-a") is True
    assert budget.is_token_exhausted(state, "tok-b") is True
