import os

import pytest

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(
        not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
        reason="no display available",
    ),
]


def test_smoke_frames() -> None:
    from opencoord.ui import app

    assert app.main(["--smoke-frames", "5"]) == 0
