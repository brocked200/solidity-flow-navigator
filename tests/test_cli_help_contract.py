"""The partial-analysis escape hatch must explain the default from --help."""

import pytest

from solidity_flow_navigator.cli import main


def test_help_explains_refusal_default_and_partial_opt_in(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    start = help_text.index("--allow-partial-analysis", help_text.index("Scope:"))
    section = help_text[start : help_text.index("--exclude-path", start)]
    assert "By default" in section
    assert "exit 1" in section
    assert "before export or serving" in section
    assert "analysis.complete=false" in section
    assert "degraded_functions" in section
    assert "UNREAD" in section
