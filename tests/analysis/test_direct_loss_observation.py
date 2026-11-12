"""Observe real Slither loss paths while host logging hides their records."""

import importlib
import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from crytic_compile import CompilationUnit, CryticCompile
from crytic_compile.compiler.compiler import CompilerVersion
from crytic_compile.utils.naming import Filename
from slither.core.scope.scope import FileScope
from slither.solc_parsing.slither_compilation_unit_solc import (
    SlitherCompilationUnitSolc,
)

from solidity_flow_navigator.analysis import slither_facts

slither_module = importlib.import_module("slither.slither")


def _parser(path):
    scope = FileScope(Filename(str(path), str(path), str(path), str(path)))
    unit = SimpleNamespace(scopes={scope.filename: scope})
    return SlitherCompilationUnitSolc(unit), scope


def _extract_hidden(monkeypatch, tmp_path, action):
    """Suppression starts and ends inside construction, hiding both snapshots."""
    initial_disable = logging.root.manager.disable
    assert initial_disable < logging.ERROR

    def construct(_cc):
        logging.disable(logging.CRITICAL)
        try:
            action()
        finally:
            logging.disable(initial_disable)
        return SimpleNamespace(contracts=[], compilation_units=[], source_code={})

    monkeypatch.setattr(slither_facts, "Slither", construct)
    return slither_facts.extract_facts(object(), tmp_path).analysis


@pytest.mark.parametrize("ast", [None, {}])
def test_empty_ast_is_seen_with_suppression_entirely_inside_constructor(
    monkeypatch, tmp_path, ast
):
    parser, _scope = _parser(tmp_path / "Empty.sol")
    report = _extract_hidden(
        monkeypatch,
        tmp_path,
        lambda: parser.parse_top_level_items(ast, str(tmp_path / "Empty.sol")),
    )

    assert report.complete is False
    assert any("empty AST" in message for message in report.diagnostics)
    assert report.degraded_functions == ()
    assert not any("unverified" in message for message in report.diagnostics)


def test_failed_reference_is_seen_with_suppression_inside_constructor(
    monkeypatch, tmp_path
):
    parser, scope = _parser(tmp_path / "Broken.sol")
    scope.exported_symbols.add(17)
    report = _extract_hidden(
        monkeypatch, tmp_path, lambda: slither_module._update_file_scopes(parser)
    )

    assert report.complete is False
    assert report.diagnostics == (
        "Failed to resolved name for reference id 17 in Broken.sol.",
    )
    assert report.degraded_functions == ()


class _Declaration:
    def __init__(self, name):
        self.name = name


def test_all_reference_kinds_are_recognized_without_false_degradation(
    monkeypatch, tmp_path
):
    parser, scope = _parser(tmp_path / "All.sol")
    for ref_id, table in enumerate(
        (
            parser.contracts_by_id,
            parser.functions_by_id,
            parser.imports_by_id,
            parser.top_level_variables_by_id,
            parser.top_level_events_by_id,
            parser.top_level_structures_by_id,
            parser.top_level_type_aliases_by_id,
            parser.top_level_enums_by_id,
            parser.top_level_errors_by_id,
        ),
        start=1,
    ):
        declaration = _Declaration(f"Symbol{ref_id}")
        table[ref_id] = [declaration] if ref_id == 2 else declaration
        scope.exported_symbols.add(ref_id)

    report = _extract_hidden(
        monkeypatch, tmp_path, lambda: slither_module._update_file_scopes(parser)
    )

    assert report.complete is True
    assert report.diagnostics == ()
    assert report.degraded_functions == ()


def test_reference_ids_are_resolved_within_their_own_compilation_unit(
    monkeypatch, tmp_path
):
    resolved, resolved_scope = _parser(tmp_path / "Resolved.sol")
    broken, broken_scope = _parser(tmp_path / "Broken.sol")
    resolved_scope.exported_symbols.add(17)
    resolved.contracts_by_id[17] = _Declaration("Resolved")
    broken_scope.exported_symbols.add(17)

    def update_both():
        slither_module._update_file_scopes(resolved)
        slither_module._update_file_scopes(broken)

    report = _extract_hidden(monkeypatch, tmp_path, update_both)

    assert report.complete is False
    assert report.diagnostics == (
        "Failed to resolved name for reference id 17 in Broken.sol.",
    )
    assert report.degraded_functions == ()


def test_reference_scan_runs_after_accessible_scope_propagation(monkeypatch, tmp_path):
    parser, scope = _parser(tmp_path / "Importer.sol")
    _other_parser, imported = _parser(tmp_path / "Dependency.sol")
    imported.exported_symbols.add(99)
    scope.accessible_scopes.append(imported)
    # Only the importing scope is in this parser. The real upstream fixed-point
    # expansion brings the missing ref into it during _update_file_scopes.
    report = _extract_hidden(
        monkeypatch, tmp_path, lambda: slither_module._update_file_scopes(parser)
    )

    assert report.complete is False
    assert report.diagnostics == (
        "Failed to resolved name for reference id 99 in Importer.sol.",
    )


def test_parser_hooks_are_restored_after_constructor_raises(monkeypatch, tmp_path):
    parse_before = SlitherCompilationUnitSolc.parse_top_level_items
    update_before = slither_module._update_file_scopes

    def construct(_cc):
        assert SlitherCompilationUnitSolc.parse_top_level_items is not parse_before
        assert slither_module._update_file_scopes is not update_before
        raise RuntimeError("construction failed")

    monkeypatch.setattr(slither_facts, "Slither", construct)
    with pytest.raises(RuntimeError, match="construction failed"):
        slither_facts.extract_facts(object(), tmp_path)

    assert SlitherCompilationUnitSolc.parse_top_level_items is parse_before
    assert slither_module._update_file_scopes is update_before


def test_unsuppressed_reference_diagnostic_is_not_duplicated(monkeypatch, tmp_path):
    parser, scope = _parser(tmp_path / "Broken.sol")
    scope.exported_symbols.add(17)

    def construct(_cc):
        slither_module._update_file_scopes(parser)
        return SimpleNamespace(contracts=[], compilation_units=[], source_code={})

    monkeypatch.setattr(slither_facts, "Slither", construct)
    report = slither_facts.extract_facts(object(), tmp_path).analysis

    assert report.complete is False
    assert report.diagnostics == (
        "Failed to resolved name for reference id 17 in Broken.sol.",
    )
    assert report.degraded_functions == ()


def test_unrelated_thread_does_not_contaminate_direct_observations(
    monkeypatch, tmp_path
):
    parser, scope = _parser(tmp_path / "Unrelated.sol")
    scope.exported_symbols.add(17)

    def unrelated_analysis():
        parser.parse_top_level_items(None, str(tmp_path / "Unrelated.sol"))
        slither_module._update_file_scopes(parser)

    def construct_action():
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(unrelated_analysis).result(timeout=5)

    report = _extract_hidden(monkeypatch, tmp_path, construct_action)

    assert report.complete is True
    assert report.diagnostics == ()


def test_parser_hooks_are_restored_after_success(monkeypatch, tmp_path):
    parse_before = SlitherCompilationUnitSolc.parse_top_level_items
    update_before = slither_module._update_file_scopes
    parser, _scope = _parser(tmp_path / "Empty.sol")

    report = _extract_hidden(
        monkeypatch,
        tmp_path,
        lambda: parser.parse_top_level_items({}, str(tmp_path / "Empty.sol")),
    )

    assert report.complete is False
    assert SlitherCompilationUnitSolc.parse_top_level_items is parse_before
    assert slither_module._update_file_scopes is update_before


def test_missing_upstream_reference_map_raises_instead_of_reporting_success(
    monkeypatch, tmp_path
):
    parser, _scope = _parser(tmp_path / "ChangedAPI.sol")
    del parser.top_level_errors_by_id

    with pytest.raises(AttributeError, match="top_level_errors_by_id"):
        _extract_hidden(
            monkeypatch, tmp_path, lambda: slither_module._update_file_scopes(parser)
        )


@pytest.mark.parametrize("loss", ["empty_ast", "missing_reference"])
def test_actual_slither_constructor_with_internal_suppression(
    monkeypatch, tmp_path, loss
):
    # Hand CryticCompile an in-memory adapter result: no solc, forge, subprocess,
    # or network is needed. The genuine Slither constructor and parsers run.
    cc = CryticCompile(
        SimpleNamespace(target=str(tmp_path)), crytic_defer_compilation="true"
    )
    unit = CompilationUnit(cc, "unit")
    unit.compiler_version = CompilerVersion("solc", "0.8.20", False)
    path = tmp_path / "Input.sol"
    filename = Filename(str(path), str(path), "Input.sol", str(path))
    source = unit.create_source_unit(filename)
    cc.src_content = {str(path): "// compiler input\n"}
    source.ast = (
        {}
        if loss == "empty_ast"
        else {
            "nodeType": "SourceUnit",
            "nodes": [],
            "src": "0:18:0",
            "exportedSymbols": {"Ghost": [17]},
        }
    )
    initial_disable = logging.root.manager.disable
    assert initial_disable < logging.ERROR

    class SuppressedSlither(slither_module.Slither):
        def __init__(self, target):
            logging.disable(logging.CRITICAL)
            try:
                super().__init__(target)
            finally:
                logging.disable(initial_disable)

    monkeypatch.setattr(slither_facts, "Slither", SuppressedSlither)
    report = slither_facts.extract_facts(cc, tmp_path).analysis

    assert report.complete is False
    expected = "empty AST" if loss == "empty_ast" else "reference id 17"
    assert any(expected in message for message in report.diagnostics)
    assert not any("unverified" in message for message in report.diagnostics)
    assert report.degraded_functions == ()
