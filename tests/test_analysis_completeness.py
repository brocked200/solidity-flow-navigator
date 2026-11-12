"""Regressions for the completeness and identity defects found 2026-11-12.

Each test here fails on the unpatched tree. The defect class they all guard is
the same: SolFlow reporting success while part of the input was silently lost
or merged. Evidence output that cannot distinguish a complete analysis from a
partial one is worse than no output, because a human then skips the code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from solidity_flow_navigator import cli
from solidity_flow_navigator.analysis.slither_facts import (
    _build_analysis_report,
    _capture_analysis_diagnostics,
)
from solidity_flow_navigator.analysis.types import AnalysisReport, RepoFacts
from solidity_flow_navigator.flow.builder import _child_identity
from solidity_flow_navigator.flow.scope import (
    DEFAULT_SCOPE,
    dependency_path,
    path_excluded,
)

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
    report = _build_analysis_report([REAL_IR_ERROR], Path("/repo"))
    assert report.complete is False
    assert report.degraded_functions == ("FxUSDBasePool.arbitrage",)
    assert report.diagnostics == (REAL_IR_ERROR,)


def test_report_is_complete_when_nothing_was_logged() -> None:
    report = _build_analysis_report([], Path("/repo"))
    assert report.complete is True
    assert report.degraded_functions == ()


def test_repeated_diagnostics_collapse_but_order_is_kept() -> None:
    other = "Impossible to generate IR for A.b(): boom"
    report = _build_analysis_report(
        [REAL_IR_ERROR, other, REAL_IR_ERROR], Path("/repo")
    )
    assert report.degraded_functions == ("FxUSDBasePool.arbitrage", "A.b()")
    assert len(report.diagnostics) == 2


def test_collector_takes_slither_loggers_and_ignores_everything_else() -> None:
    """Collecting from the root logger made any unrelated library ERROR -- a
    background HTTP failure, another thread -- mark a valid analysis
    incomplete. Only Slither's own emitters count."""
    with _capture_analysis_diagnostics() as (collector, status):
        logging.getLogger("ContractSolcParsing").error(REAL_IR_ERROR)
        logging.getLogger("requests.adapters").error("connection reset")
        logging.getLogger("ContractSolcParsing").warning("not an error")
    assert status.verified is True
    assert collector.records == [REAL_IR_ERROR]


def test_capture_leaves_global_logging_state_exactly_as_found() -> None:
    root = logging.getLogger()
    root_before = (root.level, list(root.handlers))
    target = logging.getLogger("ContractSolcParsing")
    target.setLevel(logging.CRITICAL)
    target.propagate = False
    before = (target.level, target.disabled, target.propagate, list(target.handlers))
    with _capture_analysis_diagnostics():
        pass
    after = (target.level, target.disabled, target.propagate, list(target.handlers))
    assert after == before
    assert (root.level, list(root.handlers)) == root_before, (
        "the root logger must not be touched: raising its level changes what "
        "the host application logs"
    )


def test_capture_sees_errors_a_raised_logger_level_would_have_hidden() -> None:
    target = logging.getLogger("ContractSolcParsing")
    original = target.level
    target.setLevel(logging.CRITICAL)
    try:
        with _capture_analysis_diagnostics() as (collector, _status):
            target.error(REAL_IR_ERROR)
        assert collector.records == [REAL_IR_ERROR]
    finally:
        target.setLevel(original)


def test_completeness_fails_closed_when_capture_is_impossible() -> None:
    """logging.disable() suppresses records before any handler sees them. A
    gate that certifies completeness it never observed is worse than no gate."""
    logging.disable(logging.CRITICAL)
    try:
        with _capture_analysis_diagnostics() as (_collector, status):
            pass
        assert status.verified is False
        report = _build_analysis_report(
            [], Path("/repo"), capture_unverified=tuple(status.reasons)
        )
        assert report.complete is False
        assert any("completeness unverified" in d for d in report.diagnostics)
    finally:
        logging.disable(logging.NOTSET)


def test_top_level_function_is_not_reported_as_a_function_named_top() -> None:
    msg = "Impossible to generate IR for top level function doThing(uint256): boom"
    report = _build_analysis_report([msg], Path("/repo"))
    assert report.degraded_functions == ("doThing(uint256)",)


def test_diagnostics_are_portable_across_checkouts() -> None:
    """The report is inside the hashed export core, so a host-dependent
    absolute path would make two clones of one repo hash differently."""
    a = _build_analysis_report(
        ["cannot resolve /home/alice/work/repo/contracts/A.sol"],
        Path("/home/alice/work/repo"),
    )
    b = _build_analysis_report(
        ["cannot resolve /srv/ci/repo/contracts/A.sol"], Path("/srv/ci/repo")
    )
    assert a.diagnostics == b.diagnostics == ("cannot resolve contracts/A.sol",)


def test_structurally_incomplete_contracts_fail_closed_without_logs() -> None:
    report = _build_analysis_report(
        [], Path("/repo"), structurally_incomplete=("Broken <incomplete>",)
    )
    assert report.complete is False
    assert "Broken <incomplete>" in report.degraded_functions


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
        AnalysisReport(
            complete=False, degraded_functions=("A.b()",), diagnostics=("boom",)
        ),
    )
    exported: dict = {}
    monkeypatch.setattr(
        cli,
        "build_machine_export",
        lambda *_a, **_k: exported.setdefault("payload", {"ok": True}),
    )
    rc = cli.main(["repo", "--export-json", "out.json", "--allow-partial-analysis"])
    assert rc == 0, "the opt-in must still produce an export"
    assert (
        "analysis incomplete" in capsys.readouterr().err
    ), "opting in must not silence the warning"


# --------------------------------------------------------------------------
# P2 - dedup identity must include the declaring file
# --------------------------------------------------------------------------


def test_same_name_in_two_vendored_trees_are_distinct_identities():
    """Two OpenZeppelin majors declare Math.max; they are not the same callee.

    Without the file in the key, both collapse to one identity and the dedup in
    _process_calls discards an entire distinct callee subtree with no warning.
    """
    a = _real_function_node(
        "Math.max(uint256,uint256)",
        "node_modules/@openzeppelin/contracts/utils/math/Math.sol",
    )
    b = _real_function_node(
        "Math.max(uint256,uint256)",
        "node_modules/@openzeppelin/contracts-v4/utils/math/Math.sol",
    )
    assert _child_identity(a) != _child_identity(b)


# --------------------------------------------------------------------------
# P3 / P4 - vendored trees must not abort the run, and the error must say why
# --------------------------------------------------------------------------


def test_vendored_deps_suppress_flow_roots_without_blocking_bindings() -> None:
    """``exclude_paths`` also gates ``--bind`` eligibility, so putting the
    dependency default there made a unique implementation under node_modules
    unbindable and aborted the CLI. The two concepts are separate fields."""
    vendored = "node_modules/@openzeppelin/contracts/utils/math/Math.sol"
    assert dependency_path(DEFAULT_SCOPE, vendored) is True
    assert (
        path_excluded(DEFAULT_SCOPE, vendored) is False
    ), "a vendored contract must stay eligible as an explicit --bind target"
    assert "**/node_modules/**" not in DEFAULT_SCOPE.exclude_paths
    # lib/** is deliberately not a dependency default: inline_libraries
    # addresses lib/{name}/** and excluding it loses in-tree library Flows.
    assert dependency_path(DEFAULT_SCOPE, "lib/forge-std/src/Test.sol") is False


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
        cli,
        "build_machine_export",
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
    """Build the production node type with declaration and receiver metadata."""
    from solidity_flow_navigator.analysis.types import SourceLocation
    from solidity_flow_navigator.flow.types import FunctionNode

    return FunctionNode(
        canonical_name=canonical,
        name="max",
        full_name="max(uint256,uint256)",
        declarer_contract_name="Math",
        invoked_via_contract_name="Math",
        invoked_via_contract_file=filename,
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
    assert _child_identity(node) == (
        "fn",
        "a/Math.sol",
        "Math.max(uint256,uint256)",
        "a/Math.sol",
        "Math",
    )


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


def test_external_nodes_from_different_dependency_files_stay_distinct() -> None:
    """The motivating case: MathV4.max(a,b) + MathV5.max(a,b) on one statement.
    node_modules targets are stubbed as ExternalNode, so fixing only
    FunctionNode left the original defect in place."""
    from solidity_flow_navigator.analysis.types import SourceLocation
    from solidity_flow_navigator.flow.types import ExternalNode

    def _ext(path: str):
        return ExternalNode(
            target_canonical_name="Math.max(uint256,uint256)",
            target_function_name="max",
            target_contract_name="Math",
            source_path=path,
            call_site=SourceLocation(
                filename_absolute="/a.sol",
                filename_relative="a.sol",
                start=0,
                length=1,
                lines=(1,),
                starting_column=1,
                ending_column=2,
            ),
        )

    a = _ext("node_modules/@openzeppelin/contracts/utils/math/Math.sol")
    b = _ext("node_modules/@openzeppelin/contracts-v4/utils/math/Math.sol")
    assert _child_identity(a) != _child_identity(b)
    assert _child_identity(a) == _child_identity(
        _ext("node_modules/@openzeppelin/contracts/utils/math/Math.sol")
    ), "identical dependency targets must still collapse"


def test_analysis_report_reaches_the_hashed_export_core(tmp_path) -> None:
    """A partial analysis must not hash the same as a complete one over the
    same sources, or a consumer pinning the digest cannot tell them apart."""
    import dataclasses

    from solidity_flow_navigator.machine_export import build_machine_export
    from tests.test_machine_export import _analysis as _export_fixture

    facts, flows, scope, compilation = _export_fixture(tmp_path / "repo")

    clean = build_machine_export(facts, flows, scope, compilation, tool_version="test")
    degraded_facts = dataclasses.replace(
        facts,
        analysis=AnalysisReport(
            complete=False,
            degraded_functions=("A.f()",),
            diagnostics=("Impossible to generate IR for A.f(): boom",),
        ),
    )
    degraded = build_machine_export(
        degraded_facts, flows, scope, compilation, tool_version="test"
    )

    assert clean["analysis"]["complete"] is True
    assert degraded["analysis"]["complete"] is False
    assert degraded["analysis"]["degraded_functions"] == ["A.f()"]
    assert (
        clean["payload_sha256"] != degraded["payload_sha256"]
    ), "the completeness of the analysis must be inside the hashed core"


def test_a_host_filter_makes_capture_unverified() -> None:
    """Logger filters run BEFORE handlers, so a host filter can drop a record
    this collector would otherwise see. We cannot know what it dropped."""
    target = logging.getLogger("SlitherSolcParsing")
    drop_everything = logging.Filter()
    drop_everything.filter = lambda record: False  # type: ignore[assignment]
    target.addFilter(drop_everything)
    try:
        with _capture_analysis_diagnostics() as (_collector, status):
            pass
        assert status.verified is False
        assert any("log filter" in r for r in status.reasons)
        report = _build_analysis_report(
            [], Path("/repo"), capture_unverified=tuple(status.reasons)
        )
        assert report.complete is False
    finally:
        target.removeFilter(drop_everything)


def test_suppression_starting_during_analysis_is_detected() -> None:
    """The opening snapshot cannot see suppression that begins after it. The
    exit re-check can, and the status object is mutable so the caller sees it."""
    with _capture_analysis_diagnostics() as (_collector, status):
        assert status.verified is True
        logging.disable(logging.CRITICAL)
    try:
        assert status.verified is False
        assert any("during analysis" in r for r in status.reasons)
    finally:
        logging.disable(logging.NOTSET)


def test_same_declaration_via_different_receivers_stays_distinct() -> None:
    """x.f() + y.f() with x:D1 and y:D2, both inheriting Base.f. One
    declaration, one file, one statement -- but the subtrees below them differ
    because dispatch resolves against different receivers."""
    a = _real_function_node("Base.f()", "src/Base.sol")
    b = _real_function_node("Base.f()", "src/Base.sol")
    import dataclasses

    a = dataclasses.replace(
        a, invoked_via_contract_name="D1", invoked_via_contract_file="src/D1.sol"
    )
    b = dataclasses.replace(
        b, invoked_via_contract_name="D2", invoked_via_contract_file="src/D2.sol"
    )
    assert _child_identity(a) != _child_identity(b)
    assert _child_identity(a) == _child_identity(
        dataclasses.replace(a, invoked_via_contract_name="D1")
    ), "the same receiver must still collapse"


def test_dependency_paths_can_be_cleared_and_extended_from_config() -> None:
    """A built-in default nobody can turn off is a defect in itself."""
    from solidity_flow_navigator.flow.config import PartialScope, apply_partial

    cleared = apply_partial(DEFAULT_SCOPE, PartialScope(dependency_paths=()))
    assert cleared.dependency_paths == ()
    assert dependency_path(cleared, "node_modules/x/A.sol") is False

    replaced = apply_partial(
        DEFAULT_SCOPE, PartialScope(dependency_paths=("vendor/**",))
    )
    assert replaced.dependency_paths == ("vendor/**",)
    assert dependency_path(replaced, "vendor/A.sol") is True

    untouched = apply_partial(DEFAULT_SCOPE, PartialScope())
    assert untouched.dependency_paths == DEFAULT_SCOPE.dependency_paths


def test_ir_failures_are_seen_even_with_logging_fully_disabled() -> None:
    """The gap the previous two rounds could not close: a host that suppresses
    logging for the whole of Slither(cc). Direct observation of the abort does
    not care about logging policy at all."""
    from slither.core.declarations.function import Function as SlitherFunction

    from solidity_flow_navigator.analysis.slither_facts import _capture_ir_failures

    class _Node:
        def slithir_generation(self):
            # the real upstream failure, verbatim
            raise AttributeError("'NoneType' object has no attribute 'parameters'")

    # Duck-typed rather than subclassed: Function is abstract, and the real
    # method only touches self.nodes before failing, so this exercises the
    # genuine path through the wrapped method.
    victim = SimpleNamespace(
        canonical_name="Top.scale(uint256,uint256)", nodes=[_Node()]
    )

    logging.disable(logging.CRITICAL)
    try:
        with _capture_ir_failures() as failures:
            try:
                SlitherFunction.generate_slithir_and_analyze(victim)
            except AttributeError:
                pass  # Slither's own handler swallows it the same way
    finally:
        logging.disable(logging.NOTSET)

    assert failures, "an IR abort must be observed with logging fully disabled"
    assert failures[0].startswith("Top.scale(uint256,uint256):")

    report = _build_analysis_report([], Path("/repo"), ir_failures=tuple(failures))
    assert report.complete is False
    assert "Top.scale(uint256,uint256)" in report.degraded_functions


def test_the_wrap_is_removed_afterwards() -> None:
    from slither.core.declarations.function import Function as SlitherFunction

    from solidity_flow_navigator.analysis.slither_facts import _capture_ir_failures

    before = SlitherFunction.generate_slithir_and_analyze
    with _capture_ir_failures():
        assert SlitherFunction.generate_slithir_and_analyze is not before
    assert SlitherFunction.generate_slithir_and_analyze is before


def test_convert_to_ir_logger_is_audited() -> None:
    """convert.py logs "Function not found" and continues with
    ir.function = None, losing a call edge's target in normal operation."""
    from solidity_flow_navigator.analysis.slither_facts import _SLITHER_LOGGERS

    assert "ConvertToIR" in _SLITHER_LOGGERS
    with _capture_analysis_diagnostics() as (collector, _status):
        logging.getLogger("ConvertToIR").error("Function not found Foo.bar")
    assert collector.records == ["Function not found Foo.bar"]


def test_export_scope_carries_dependency_paths(tmp_path) -> None:
    """dependency_paths deletes whole trees of Flow roots. Omitting it from the
    exported scope let two artifacts disagreeing about most of the attack
    surface declare an identical scope and scope_sha256."""
    import dataclasses

    from solidity_flow_navigator.machine_export import build_machine_export
    from tests.test_machine_export import _analysis as _export_fixture

    facts, flows, scope, compilation = _export_fixture(tmp_path / "repo")
    wide = build_machine_export(facts, flows, scope, compilation, tool_version="t")
    narrow_scope = dataclasses.replace(
        scope, dependency_paths=scope.dependency_paths + ("contracts/core/**",)
    )
    narrow = build_machine_export(
        facts, flows, narrow_scope, compilation, tool_version="t"
    )

    assert "dependency_paths" in wide["scope"]
    assert wide["scope"] != narrow["scope"]
    assert (
        wide["scope_sha256"] != narrow["scope_sha256"]
    ), "a scope rule that changes which roots exist must change the scope hash"


# --------------------------------------------------------------------------
# serve/ must not disagree with the export
# --------------------------------------------------------------------------


def test_served_pages_warn_about_a_partial_analysis() -> None:
    """The CLI refuses to serve a partial analysis, so reaching the UI means
    --allow-partial-analysis was passed. The degraded Flows then render as
    ordinary complete-looking rows and the one-time stderr warning has
    scrolled away, so the warning has to be in the page."""
    base = Path("src/solidity_flow_navigator/serve/templates/base.html").read_text()
    assert "partial-analysis-banner" in base
    assert "analysis.degraded_functions" in base

    app_src = Path("src/solidity_flow_navigator/serve/app.py").read_text()
    start = app_src.index("def _inject_globals()")
    ctx = app_src[start : app_src.index("@app.after_request", start)]
    assert (
        '"analysis": getattr(facts, "analysis", None)' in ctx
    ), "the completeness report must reach the template context"


def test_serve_rejects_a_binding_the_cli_treats_as_fatal() -> None:
    """Serve was fail-open where the CLI is fail-closed: /bind/ accepted any
    contract name, and /bindings/save would then persist a solflow.toml the
    CLI refuses with INTERFACE_BINDING_FAILED."""
    app_src = Path("src/solidity_flow_navigator/serve/app.py").read_text()
    start = app_src.index('contract = request.args.get("contract", "")')
    handler = app_src[start : app_src.index("def save_bindings", start)]
    assert "candidates_by_iface.get(iface, ())" in handler, (
        "an unknown contract must be rejected before _rebuild commits it"
    )


def test_one_failure_is_named_once_not_twice() -> None:
    """Log parsing yields "C.f" and direct observation yields "C.f(uint256)"
    for the SAME failure. Listing both makes one problem look like two, and an
    operator counting degraded functions gets a wrong number. Found by running
    the installed tool, not by a test."""
    report = _build_analysis_report(
        [REAL_IR_ERROR],
        Path("/repo"),
        ir_failures=(
            "FxUSDBasePool.arbitrage(address,uint256,address,bytes): "
            "AttributeError: boom",
        ),
        structurally_incomplete=("FxUSDBasePool <incomplete>",),
    )
    names = report.degraded_functions
    assert "FxUSDBasePool.arbitrage" not in names, "bare duplicate must go"
    assert "FxUSDBasePool.arbitrage(address,uint256,address,bytes)" in names
    assert "FxUSDBasePool <incomplete>" in names
    assert len(names) == 2
