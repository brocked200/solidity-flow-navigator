"""Failure contracts for noninteractive audit/export invocations."""

from types import SimpleNamespace

import pytest

from solidity_flow_navigator import cli
from solidity_flow_navigator.analysis.compile import CompilationFailure
from solidity_flow_navigator.machine_export import ExportError


@pytest.mark.parametrize(
    "message", ["solc compilation failed", "missing dependency: Dependency.sol"]
)
def test_compile_errors_exit_nonzero(monkeypatch, tmp_path, capsys, message):
    monkeypatch.chdir(tmp_path)

    def fail(_):
        raise CompilationFailure(message)

    monkeypatch.setattr(cli, "compile_repo", fail)
    assert cli.main(["repo", "--export-json", "out.json"]) == 1
    assert message in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()


def test_malformed_config_fails_before_compile(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "solflow.toml").write_text("[scope\n")
    monkeypatch.setattr(cli, "compile_repo", lambda _: pytest.fail("must not compile"))
    assert cli.main(["repo"]) == 1
    assert "solflow:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error", [ExportError("source changed"), OSError("write denied")]
)
def test_export_errors_exit_cleanly(monkeypatch, tmp_path, capsys, error):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "compile_repo", lambda _: object())
    monkeypatch.setattr(cli, "extract_facts", lambda *_: object())
    monkeypatch.setattr(cli, "build_flows", lambda *_: ())

    def fail(*_, **__):
        raise error

    monkeypatch.setattr(cli, "build_machine_export", fail)
    assert cli.main(["repo", "--export-json", "out.json"]) == 1
    assert "solflow:" in capsys.readouterr().err


def test_failed_binding_aborts_export(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "compile_repo", lambda _: object())
    monkeypatch.setattr(cli, "extract_facts", lambda *_: object())
    from solidity_flow_navigator.flow.types import UnresolvedReason

    node = SimpleNamespace(
        reason=UnresolvedReason.INTERFACE_BINDING_FAILED,
        descriptor="I -> Missing: not in scope",
        children=(),
    )
    monkeypatch.setattr(
        cli,
        "build_flows",
        lambda *_: (
            SimpleNamespace(entry_point_invoker_canonical_name="C.f()", root=node),
        ),
    )
    monkeypatch.setattr(
        cli,
        "build_machine_export",
        lambda *_a, **_k: pytest.fail("invalid binding must abort"),
    )
    assert cli.main(["repo", "--bind", "I=Missing", "--export-json", "out.json"]) == 1
    assert "binding" in capsys.readouterr().err


def test_export_warns_private_source(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "compile_repo", lambda _: object())
    monkeypatch.setattr(cli, "extract_facts", lambda *_: object())
    monkeypatch.setattr(cli, "build_flows", lambda *_: ())
    monkeypatch.setattr(cli, "build_machine_export", lambda *_a, **_k: {})
    monkeypatch.setattr(cli, "write_machine_export", lambda *_: "abc")
    assert cli.main(["repo", "--export-json", "out.json"]) == 0
    assert "private source" in capsys.readouterr().err


def test_duplicate_entrypoint_identity_is_rejected(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "compile_repo", lambda _: object())
    monkeypatch.setattr(cli, "extract_facts", lambda *_: object())
    flow = SimpleNamespace(
        entry_point_invoker_canonical_name="Twin.run()",
        root=SimpleNamespace(children=()),
    )
    monkeypatch.setattr(cli, "build_flows", lambda *_: (flow, flow))
    monkeypatch.setattr(
        cli,
        "build_machine_export",
        lambda *_a, **_k: pytest.fail("ambiguous identities must abort"),
    )
    assert cli.main(["repo", "--export-json", "out.json"]) == 1
    assert "duplicate" in capsys.readouterr().err
