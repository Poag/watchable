from __future__ import annotations

import logging

from watchable import __version__
from watchable.app import configure_logging


def test_configure_logging_logs_running_version(caplog):
    # Confirmed useful in production: a traceback's line numbers alone
    # don't say whether a restarted container actually picked up a new
    # image, but the version logged at startup does.
    with caplog.at_level(logging.INFO, logger="watchable"):
        configure_logging("INFO")

    assert any(
        record.name == "watchable" and __version__ in record.getMessage()
        for record in caplog.records
    )
