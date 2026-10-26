from __future__ import annotations

import warnings

import requests
import urllib3

from watchable.providers.base import ProviderError, _is_retryable, looks_like_stale_target
from watchable.providers.plex import PlexClient


def _provider_error_from(cause: BaseException) -> ProviderError:
    try:
        raise ProviderError("wrapped") from cause
    except ProviderError as exc:
        return exc


def _http_error(status_code: int) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    return requests.exceptions.HTTPError(response=response)


def test_is_retryable_true_for_connection_error():
    assert _is_retryable(_provider_error_from(requests.exceptions.ConnectionError())) is True


def test_is_retryable_true_for_timeout():
    assert _is_retryable(_provider_error_from(requests.exceptions.Timeout())) is True


def test_is_retryable_true_for_401():
    # Observed in production: a push 401'd immediately after a bulk pull
    # succeeded with the exact same credentials -- a momentary blip, not
    # bad credentials, so this is worth a quick retry.
    assert _is_retryable(_provider_error_from(_http_error(401))) is True


def test_is_retryable_true_for_5xx_and_429():
    for status in (429, 500, 502, 503, 504):
        assert _is_retryable(_provider_error_from(_http_error(status))) is True


def test_is_retryable_false_for_400_and_404():
    # Not transient -- retrying a malformed request or a missing item
    # wastes time without any chance of succeeding.
    for status in (400, 404):
        assert _is_retryable(_provider_error_from(_http_error(status))) is False


def test_is_retryable_false_for_non_provider_error():
    assert _is_retryable(ValueError("not a ProviderError")) is False


def test_is_retryable_false_for_provider_error_without_cause():
    assert _is_retryable(ProviderError("no cause set")) is False


def test_looks_like_stale_target_true_for_401_403_404():
    for status in (401, 403, 404):
        assert looks_like_stale_target(_provider_error_from(_http_error(status))) is True


def test_looks_like_stale_target_false_for_5xx_and_429():
    # Server-side/rate-limit trouble, not a sign the cached item id itself
    # is wrong -- clearing the cache here would just cause an unnecessary
    # re-lookup next cycle without addressing the real problem.
    for status in (429, 500, 502, 503, 504):
        assert looks_like_stale_target(_provider_error_from(_http_error(status))) is False


def test_looks_like_stale_target_false_for_connection_error():
    assert looks_like_stale_target(_provider_error_from(requests.exceptions.ConnectionError())) is False


def test_looks_like_stale_target_false_for_non_provider_error():
    assert looks_like_stale_target(ValueError("not a ProviderError")) is False


def test_looks_like_stale_target_false_for_provider_error_without_cause():
    assert looks_like_stale_target(ProviderError("no cause set")) is False


def test_verify_tls_false_suppresses_insecure_request_warning(caplog):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        with caplog.at_level("WARNING", logger="watchable.providers"):
            PlexClient(name="plex", base_url="https://plex.lan:32400", token="t", verify_tls=False)

        warnings.warn("simulated unverified request", urllib3.exceptions.InsecureRequestWarning, stacklevel=2)

    assert not caught  # client construction suppressed it before the warn() above
    assert "TLS certificate verification is disabled" in caplog.text
    assert "plex" in caplog.text


def test_verify_tls_true_leaves_insecure_request_warning_enabled():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        PlexClient(name="plex", base_url="https://plex.lan:32400", token="t", verify_tls=True)

        warnings.warn("simulated unverified request", urllib3.exceptions.InsecureRequestWarning, stacklevel=2)

    assert len(caught) == 1
