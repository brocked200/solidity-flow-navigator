"""Opaque self-call receiver facts survive every downstream representation."""

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from solidity_flow_navigator.analysis.types import (
    CallEdge,
    Contract,
    Function,
    RepoFacts,
    SourceLocation,
)
from solidity_flow_navigator.flow.builder import build_flows
from solidity_flow_navigator.flow.scope import Scope
from solidity_flow_navigator.flow.types import UnresolvedNode, UnresolvedReason
from solidity_flow_navigator.machine_export import build_machine_export
from solidity_flow_navigator.serve.serializer import serialize_flow


def _analysis(root: Path, self_call: bool | None):
    root.mkdir()
    source = root / "Facet.sol"
    body = "contract Facet { function run() external payable {} }\n"
    source.write_text(body)
    location = SourceLocation(
        filename_absolute=str(source),
        filename_relative="Facet.sol",
        start=0,
        length=len(body),
        lines=(1,),
        starting_column=1,
        ending_column=len(body),
    )
    receiver_fact = {} if self_call is None else {"is_self_call": self_call}
    edge = CallEdge(
        kind="low_level",
        subkind="call",
        target_canonical_name=None,
        target_function_name=None,
        target_contract_name=None,
        is_resolved=False,
        source_location=location,
        value_expression="amount",
        **receiver_fact,
    )
    function = Function(
        canonical_name="Facet.run()",
        name="run",
        full_name="run()",
        contract_declarer_name="Facet",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=True,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=location,
        source_code=body,
        calls=(edge,),
    )
    contract = Contract(
        name="Facet",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=location,
        functions=(function,),
        modifiers=(),
    )
    facts = RepoFacts(repo_path=str(root), contracts=(contract,), free_functions=())
    scope = Scope()
    flows = build_flows(facts, scope)
    version = SimpleNamespace(
        compiler="missing-test-compiler",
        version="0.8.26",
        optimized=False,
        optimize_runs=None,
        via_ir=False,
    )
    unit = SimpleNamespace(
        compiler_version=version,
        filenames=(SimpleNamespace(absolute=str(source), short="Facet.sol"),),
    )
    compilation = SimpleNamespace(compilation_units={"unit": unit})
    return facts, flows, scope, compilation


@pytest.mark.parametrize("self_call", [True, False, None])
def test_low_level_self_fact_round_trips_without_resolving_callee(
    tmp_path: Path, self_call: bool | None
) -> None:
    args = _analysis(tmp_path / "repo", self_call)
    facts, flows, _, _ = args
    assert len(flows) == 1
    flow = flows[0]
    assert len(flow.root.children) == 1
    node = flow.root.children[0]
    assert isinstance(node, UnresolvedNode)
    assert node.reason == UnresolvedReason.LOW_LEVEL_CALL
    assert node.is_self_call is self_call
    assert node.raw_kind == "low_level"
    assert node.raw_subkind == "call"
    assert node.value_expression == "amount"
    assert flow.unresolved_count == 1
    assert flow.dynamic_dispatch_count == 1
    assert flow.value_sink_count == 1
    raw_edge = asdict(facts)["contracts"][0]["functions"][0]["calls"][0]
    assert raw_edge["is_self_call"] is self_call
    assert raw_edge["target_canonical_name"] is None
    assert raw_edge["target_contract_name"] is None

    payload = build_machine_export(*args, tool_version="test")
    tree_node = payload["flows"][0]["root"]["children"][0]
    flat_node = payload["edges"][0]
    ui_node = serialize_flow(flow)["root"]["children"][0]
    for exported in (tree_node, flat_node, ui_node):
        assert exported["is_self_call"] is self_call
        assert exported["value_expression"] == "amount"
        assert exported["raw_kind"] == "low_level"
        assert exported["raw_subkind"] == "call"
        assert exported["reason"] == "low_level_call"
        assert "target" not in exported
        assert json.loads(json.dumps(exported))["is_self_call"] is self_call
    assert flat_node["resolved"] is False
    assert payload["summary"]["unresolved_nodes"] == 1
    assert payload["summary"]["dynamic_dispatch_nodes"] == 1


@pytest.mark.parametrize("self_call", [True, False, None])
def test_self_call_export_is_portable_and_deterministic(
    tmp_path: Path, self_call: bool | None
) -> None:
    first = build_machine_export(
        *_analysis(tmp_path / "first", self_call), tool_version="test"
    )
    second = build_machine_export(
        *_analysis(tmp_path / "second", self_call), tool_version="test"
    )
    assert first == second
    encoded = json.dumps(first)
    assert str(tmp_path) not in encoded
    assert "filename_absolute" not in encoded
    assert first["flows"][0]["root"]["children"][0]["call_site"] == {
        "filename_relative": "Facet.sol",
        "start": 0,
        "length": len("contract Facet { function run() external payable {} }\n"),
        "lines": [1],
        "starting_column": 1,
        "ending_column": len("contract Facet { function run() external payable {} }\n"),
    }
    assert not {"timestamp", "generated_at", "created_at"} & first.keys()


def test_unresolved_node_legacy_positional_construction_keeps_value(tmp_path) -> None:
    facts, _, _, _ = _analysis(tmp_path / "repo", None)
    location = facts.contracts[0].source_location
    node = UnresolvedNode(
        UnresolvedReason.LOW_LEVEL_CALL,
        "<address>.call(...)",
        location,
        "low_level",
        "call",
        "amount",
    )
    assert node.value_expression == "amount"
    assert node.is_self_call is None
    with pytest.raises(TypeError):
        UnresolvedNode(
            UnresolvedReason.LOW_LEVEL_CALL,
            "<address>.call(...)",
            location,
            "low_level",
            "call",
            "amount",
            True,
        )
