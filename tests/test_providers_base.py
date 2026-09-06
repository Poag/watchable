from __future__ import annotations

import warnings

import urllib3

from watchable.providers.plex import PlexClient


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
