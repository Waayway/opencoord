from pathlib import Path

from opencoord.ui import app


def test_accepts_session_file_argument() -> None:
    # The Windows .opencoord file association and the macOS document type launch
    # `OpenCoord <file>`, so a positional session path must parse.
    args = app.build_parser().parse_args(["show.opencoord"])
    assert args.session == Path("show.opencoord")


def test_session_argument_is_optional() -> None:
    assert app.build_parser().parse_args([]).session is None
