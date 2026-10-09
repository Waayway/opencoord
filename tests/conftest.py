import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("OPENCOORD_HARDWARE") == "1":
        return
    skip = pytest.mark.skip(reason="hardware test: set OPENCOORD_HARDWARE=1")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)
