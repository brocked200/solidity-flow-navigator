"""Regressions for the completeness and identity defects found 2026-11-12.

Each test here fails on the unpatched tree. The defect class they all guard is
the same: SolFlow reporting success while part of the input was silently lost
or merged. Evidence output that cannot distinguish a complete analysis from a
partial one is worse than no output, because a human then skips the code.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from solidity_flow_navigator import cli
from solidity_flow_navigator.analysis.slither_facts import (
    _build_analysis_report,
    _capture_analysis_diagnostics,
)
from solidity_flow_navigator.analysis.types import AnalysisReport, RepoFacts
from solidity_flow_navigator.flow.builder import _child_identity
from solidity_flow_navigator.flow.scope import DEFAULT_SCOPE

# The exact message Slither 0.11.6 emits, measured against AladdinDAO
# fx-protocol-contracts. Slither logs this and CONTINUES: the function survives
# with partial IR rather than being removed, which is why its absence from the
# export cannot be used to detect the problem.
REAL_IR_ERROR = (
    "Impossible to generate IR for FxUSDBasePool.arbitrage "
    "(contracts/core/FxUSDBasePool.sol#461-513): "
    "'NoneType' object has no attribute 'parameters'"
)


# --------------------------------------------------------------------------
# P1 - partial analysis must be visible
# --------------------------------------------------------------------------


def test_ir_failure_is_parsed_into_a_degraded_function() -> None:
    report = _build_analysis_report([REAL_IR_ERROR])
    assert report.complete is False
    assert report.degraded_functions == ("FxUSDBasePool.arbitrage",)
    assert report.diagnostics == (REAL_IR_ERROR,)


def test_report_is_complete_when_nothing_was_logged() -> None:
    report = _build_analysis_report([])
    assert report.complete is True
    assert report.degraded_functions == ()


def test_repeated_diagnostics_collapse_but_order_is_kept() -> None:
    other = "Impossible to generate IR for A.b(): boom"
    report = _build_analysis_report([REAL_IR_ERROR, other, REAL_IR_ERROR])
    assert report.degraded_functions == ("FxUSDBasePool.arbitrage", "A.b()")
    assert len(report.diagnostics) == 2


def test_collector_captures_errors_from_any_logger_and_detaches() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    with _capture_analysis_diagnostics() as collector:
        logging.getLogger("ContractSolcParsing").error(REAL_IR_ERROR)
        logging.getLogger("something.else").warning("not an error")
    assert collector.records == [REAL_IR_ERROR]
    assert list(root.handlers) == before, "handler must not leak past the block"


def test_repo_facts_defaults_to_complete_for_synthetic_facts() -> None:
    facts = RepoFacts(repo_path="/x", contracts=(), free_functions=())
    assert facts.analysis == AnalysisReport()
    assert facts.analysis.complete is True


def _cli_with_facts(monkeypatch, tmp_path, analysis: AnalysisReport):
    monkeypatch.chdir(tmp_path)
    facts = RepoFacts(
        repo_path=str(tmp_path), contracts=(), free_functions=(), analysis=analysis
    )
    monkeypatch.setattr(cli, "compile_repo", lambda _: object())
    monkeypatch.setattr(cli, "extract_facts", lambda *_: facts)
    monkeypatch.setattr(cli, "build_flows", lambda *_: ())
    return facts


def test_cli_refuses_to_export_a_partial_analysis(monkeypatch, tmp_path, capsys):
    _cli_with_facts(
        monkeypatch,
        tmp_path,
        AnalysisReport(
            complete=False,
            degraded_functions=("FxUSDBasePool.arbitrage",),
            diagnostics=(REAL_IR_ERROR,),
        ),
    )
    monkeypatch.setattr(
        cli,
        "build_machine_export",
        lambda *_a, **_k: pytest.fail("a partial analysis must not be exported"),
    )
    assert cli.main(["repo", "--export-json", "out.json"]) == 1
    err = capsys.readouterr().err
    assert "analysis incomplete" in err
    assert "FxUSDBasePool.arbitrage" in err, "the degraded function must be named"
    assert not (tmp_path / "out.json").exists()


def test_allow_partial_analysis_opts_back_in(monkeypatch, tmp_path, capsys):
    _cli_with_facts(
        monkeypatch,
        tmp_path,
        AnalysisReport(complete=False, degraded_functions=("A.b()",),
                       diagnostics=("boom",)),
    )
    exported: dict = {}
    monkeypatch.setattr(
        cli, "build_machine_export",
        lambda *_a, **_k: exported.setdefault("payload", {"ok": True}),
    )
    rc = cli.main(
        ["repo", "--export-json", "out.json", "--allow-partial-analysis"]
    )
    assert rc == 0, "the opt-in must still produce an export"
    assert "analysis incomplete" in capsys.readouterr().err, (
        "opting in must not silence the warning"
    )


# --------------------------------------------------------------------------
# P2 - dedup identity must include the declaring file
# --------------------------------------------------------------------------


def _function_node(canonical: str, filename: str):
    return SimpleNamespace(
        canonical_name=canonical,
        source_location=SimpleNamespace(filename_relative=filename),
    )


def test_same_name_in_two_vendored_trees_are_distinct_identities(monkeypatch):
    """Two OpenZeppelin majors declare Math.max; they are not the same callee.

    Without the file in the key, both collapse to one identity and the dedup in
    _process_calls discards an entire distinct callee subtree with no warning.
    """
    from solidity_flow_navigator.flow import builder as builder_mod

    a = _function_node(
        "Math.max(uint256,uint256)", "node_modules/@openzeppelin/contracts/utils/math/Math.sol"
    )
    b = _function_node(
        "Math.max(uint256,uint256)",
        "node_modules/@openzeppelin/contracts-v4/utils/math/Math.sol",
    )
    monkeypatch.setattr(builder_mod, "FunctionNode", SimpleNamespace, raising=True)
    assert _child_identity(a) != _child_identity(b)


# --------------------------------------------------------------------------
# P3 / P4 - vendored trees must not abort the run, and the error must say why
# --------------------------------------------------------------------------


def test_node_modules_is_excluded_from_flow_generation_by_default() -> None:
    assert "**/node_modules/**" in DEFAULT_SCOPE.exclude_paths
    # lib/** is deliberately NOT defaulted: inline_libraries addresses
    # lib/{name}/** and excluding it loses in-tree library Flows.
    assert "**/lib/**" not in DEFAULT_SCOPE.exclude_paths


def test_duplicate_identity_error_names_the_offenders(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "compile_repo", lambda _: object())
    monkeypatch.setattr(cli, "extract_facts", lambda *_: object())

    def _flow(path: str):
        return SimpleNamespace(
            entry_point_invoker_canonical_name="Math.max(uint256,uint256)",
            root=SimpleNamespace(
                children=(), source_location=SimpleNamespace(filename_relative=path)
            ),
        )

    monkeypatch.setattr(
        cli,
        "build_flows",
        lambda *_: (_flow("node_modules/a/Math.sol"), _flow("node_modules/b/Math.sol")),
    )
    monkeypatch.setattr(
        cli, "build_machine_export",
        lambda *_a, **_k: pytest.fail("ambiguous identities must abort"),
    )
    assert cli.main(["repo", "--export-json", "out.json"]) == 1
    err = capsys.readouterr().err
    assert "duplicate" in err
    assert "Math.max(uint256,uint256)" in err, "name the colliding identity"
    assert "node_modules/a/Math.sol" in err, "name the file it came from"
    assert "node_modules/b/Math.sol" in err
    assert "--exclude-path" in err, "tell the user which flag fixes it"

def _real_function_node(canonical: str, filename: str):
    """A genuine FunctionNode, not a stand-in.

    The monkeypatched test above proves the KEY changed; this one proves the
    production type actually carries the attribute the key now reads, and that
    the dedup the key exists to perform still works.
    """
    from solidity_flow_navigator.flow.types import FunctionNode
    from solidity_flow_navigator.analysis.types import SourceLocation

    return FunctionNode(
        canonical_name=canonical,
        name="max",
        full_name="max(uint256,uint256)",
        declarer_contract_name="Math",
        invoked_via_contract_name="Math",
        invoked_via_super=False,
        is_modifier=False,
        visibility="internal",
        payable=False,
        view=True,
        pure=True,
        source_code="function max(uint256 a, uint256 b) {}",
        source_location=SourceLocation(
            filename_absolute="/" + filename,
            filename_relative=filename,
            start=0,
            length=1,
            lines=(1,),
            starting_column=1,
            ending_column=2,
        ),
        builtins_used=(),
        children=(),
    )


def test_real_function_node_carries_the_file_the_identity_reads() -> None:
    node = _real_function_node("Math.max(uint256,uint256)", "a/Math.sol")
    assert node.source_location.filename_relative == "a/Math.sol"
    assert _child_identity(node) == ("fn", "a/Math.sol", "Math.max(uint256,uint256)")


def test_real_nodes_same_name_different_file_stay_distinct() -> None:
    a = _real_function_node("Math.max(uint256,uint256)", "oz/contracts/Math.sol")
    b = _real_function_node("Math.max(uint256,uint256)", "oz/contracts-v4/Math.sol")
    assert _child_identity(a) != _child_identity(b)


def test_identical_nodes_still_collapse() -> None:
    """The key's purpose is collapsing CFG-branch duplicates Slither forks on
    ternaries and short-circuit booleans. Both forks resolve to the same
    function in the same file, so adding the file must NOT stop them merging."""
    a = _real_function_node("Math.max(uint256,uint256)", "oz/contracts/Math.sol")
    b = _real_function_node("Math.max(uint256,uint256)", "oz/contracts/Math.sol")
    assert _child_identity(a) == _child_identity(b)
