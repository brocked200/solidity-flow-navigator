"""Determinism and schema tests for the machine-readable export."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from solidity_flow_navigator.analysis.types import (
    Contract,
    Function,
    RepoFacts,
    SourceLocation,
)
from solidity_flow_navigator.flow.scope import Scope
from solidity_flow_navigator.flow.types import (
    Binding,
    Flow,
    FunctionNode,
    UnresolvedNode,
    UnresolvedReason,
)
from solidity_flow_navigator.machine_export import (
    build_machine_export,
    write_machine_export,
)


def _analysis(root: Path):
    source = root / "src" / "A.sol"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"contract A {\r\n function f() external {}\r\n}\r\n")
    loc = SourceLocation(
        filename_absolute=str(source),
        filename_relative="src/A.sol",
        start=0,
        length=source.stat().st_size,
        lines=(1, 2, 3),
        starting_column=1,
        ending_column=2,
    )
    fn = Function(
        canonical_name="A.f()",
        name="f",
        full_name="f()",
        contract_declarer_name="A",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=loc,
        source_code="function f() external {}",
        calls=(),
    )
    contract = Contract(
        name="A",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=loc,
        functions=(fn,),
        modifiers=(),
    )
    bound = FunctionNode(
        canonical_name="Impl.run()",
        name="run",
        full_name="run()",
        declarer_contract_name="Impl",
        invoked_via_contract_name="Impl",
        invoked_via_super=False,
        is_modifier=False,
        visibility="external",
        payable=False,
        view=False,
        pure=False,
        source_code="function run() external {}",
        source_location=loc,
        builtins_used=(),
        children=(),
        call_site_line=2,
        call_kind="external",
        bound_via=Binding("IRunner", "Impl"),
    )
    unresolved = UnresolvedNode(
        reason=UnresolvedReason.LOW_LEVEL_CALL,
        descriptor="call()",
        call_site=loc,
        raw_kind="low_level",
        raw_subkind="call",
    )
    root_node = FunctionNode(
        canonical_name="A.f()",
        name="f",
        full_name="f()",
        declarer_contract_name="A",
        invoked_via_contract_name="A",
        invoked_via_super=False,
        is_modifier=False,
        visibility="external",
        payable=False,
        view=False,
        pure=False,
        source_code=fn.source_code,
        source_location=loc,
        builtins_used=(),
        children=(bound, unresolved),
    )
    flow = Flow(
        entry_point_declarer_canonical_name="A.f()",
        entry_point_invoker_canonical_name="A.f()",
        entry_point_contract_name="A",
        entry_point_function_name="f",
        root=root_node,
        unresolved_count=1,
        max_depth=1,
    )
    facts = RepoFacts(
        repo_path=str(root),
        contracts=(contract,),
        free_functions=(),
    )
    version = SimpleNamespace(
        compiler="missing-test-compiler",
        version="1.2.3",
        optimized=True,
        optimize_runs=999,
        via_ir=False,
    )
    filename = SimpleNamespace(absolute=str(source), short="A.sol")
    unit = SimpleNamespace(compiler_version=version, filenames=(filename,))
    compilation = SimpleNamespace(compilation_units={"random-id": unit})
    scope = Scope(interface_bindings=(("IRunner", "Impl"),))
    return facts, (flow,), scope, compilation


def test_export_is_checkout_independent_and_complete(tmp_path: Path) -> None:
    args_a = _analysis(tmp_path / "a")
    args_b = _analysis(tmp_path / "b")
    one = build_machine_export(*args_a, tool_version="test")
    two = build_machine_export(*args_b, tool_version="test")

    assert one == two
    assert one["schema_version"] == 1
    assert one["sources"][0]["path"] == "src/A.sol"
    assert one["sources"][0]["newline"] == "crlf"
    assert one["summary"]["unresolved_nodes"] == 1
    assert one["binding_assumptions"] == [{"interface": "IRunner", "contract": "Impl"}]
    assert [edge["resolved"] for edge in one["edges"]] == [True, False]
    assert one["edges"][0]["binding_assumption"] == {
        "interface_name": "IRunner",
        "contract_name": "Impl",
    }
    assert "filename_absolute" not in json.dumps(one)


def test_export_writer_is_byte_deterministic(tmp_path: Path) -> None:
    payload = build_machine_export(*_analysis(tmp_path / "repo"), tool_version="test")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    digest_a = write_machine_export(payload, first)
    digest_b = write_machine_export(payload, second)

    assert digest_a == digest_b
    assert first.read_bytes() == second.read_bytes()
    parsed = json.loads(first.read_text())
    assert parsed["payload_sha256"] == payload["payload_sha256"]
