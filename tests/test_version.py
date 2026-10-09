import re

import pytest

import opencoord
from opencoord import cli


def test_version_string() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", opencoord.__version__)


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert opencoord.__version__ in capsys.readouterr().out


@pytest.mark.parametrize("command", ["info", "sweep", "scan"])
def test_cli_placeholders_exit_2(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([command]) == 2
    assert "not implemented yet" in capsys.readouterr().err
