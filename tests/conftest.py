from __future__ import annotations

import pytest

from watchable.db import Database


@pytest.fixture()
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()
