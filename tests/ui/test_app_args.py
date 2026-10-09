from pathlib import Path

import pytest

from opencoord.ui import app


def test_accepts_session_file_argument() -> None:
    # The Windows .opencoord file association and the macOS document type launch
    # `OpenCoord <file>`, so a positional session path must parse.
    args = app.build_parser().parse_args(["show.opencoord"])
    assert args.session == Path("show.opencoord")


def test_session_argument_is_optional() -> None:
    assert app.build_parser().parse_args([]).session is None


def test_screenshot_and_tab_arguments() -> None:
    args = app.build_parser().parse_args(
        ["--simulator", "--screenshot", "x.png", "--tab", "analysis"]
    )
    assert args.screenshot == Path("x.png")
    assert args.tab == "analysis"
    assert app.build_parser().parse_args([]).screenshot is None


def test_tab_names_match_the_built_tabs() -> None:
    # --tab values map to the tags used when the tab bar is built.
    assert set(app.TABS) == {
        "device",
        "scan",
        "markers",
        "analysis",
        "record",
        "coordination",
        "profiles",
    }
    assert all(tag == f"tab.{name}" for name, tag in app.TABS.items())


def test_a_failing_callback_is_reported_and_the_rest_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ran: list[object] = []
    reports: list[str] = []

    def boom(sender: object, app_data: object) -> None:
        raise TypeError("bad thing")

    jobs = [
        [boom, "s1", "a1", None],
        [None, "s0", None, None],
        [lambda sender, app_data, user_data: ran.append((sender, app_data, user_data)), "s2", 2, 3],
        [lambda: ran.append("no args"), "s3", None, None],
    ]
    with caplog.at_level("ERROR"):
        app.run_callback_jobs(jobs, reports.append)
    assert ran == [("s2", 2, 3), "no args"]
    assert reports == ["Internal error: bad thing (see the log)"]
    assert "bad thing" in caplog.text and "Traceback" in caplog.text
