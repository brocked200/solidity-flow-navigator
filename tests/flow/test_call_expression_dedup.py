"""Distinct call expressions survive, while real CFG fork copies collapse."""

import pytest

from solidity_flow_navigator.flow.builder import build_flows
from solidity_flow_navigator.flow.scope import Scope
from solidity_flow_navigator.flow.types import (
    ExternalNode,
    FunctionNode,
    SinkNode,
    UnresolvedNode,
)
from tests.analysis.test_call_expression_locations import (
    call_expression_model as call_expression_model,
)


@pytest.mark.parametrize(
    ("function_name", "node_type", "scope"),
    [
        ("resolved", FunctionNode, Scope()),
        ("nested", FunctionNode, Scope()),
        ("resolved", ExternalNode, Scope(stub_paths=("Calls.sol",))),
        ("unresolved", UnresolvedNode, Scope()),
        ("sinks", SinkNode, Scope()),
    ],
)
def test_distinct_identical_method_expressions_remain_two_children(
    call_expression_model, function_name, node_type, scope
):
    facts, _ = call_expression_model
    flow = next(
        flow
        for flow in build_flows(facts, scope)
        if flow.root.name == function_name
        and flow.root.declarer_contract_name == "Calls"
    )
    assert len(flow.root.children) == 2
    assert all(isinstance(child, node_type) for child in flow.root.children)
    if node_type is UnresolvedNode:
        assert flow.unresolved_count == 2


@pytest.mark.parametrize(
    ("library_name", "function_name", "target"),
    [
        ("Strings", "toStringSigned", "SignedMath.abs(int256)"),
        ("Strings", "toStringSigned", "Strings.toString(uint256)"),
        ("StringsUpgradeable", "toString", "SignedMathUpgradeable.abs(int256)"),
        ("StringsUpgradeable", "toString", "StringsUpgradeable.toString(uint256)"),
    ],
)
def test_real_ternary_cfg_copies_still_collapse_to_one_child(
    call_expression_model, library_name, function_name, target
):
    facts, _ = call_expression_model
    library = next(c for c in facts.contracts if c.name == library_name)
    function = next(
        f
        for f in library.functions
        if f.name == function_name and f.parameters[0].type_str == "int256"
    )
    copies = [call for call in function.calls if call.target_canonical_name == target]
    assert len(copies) == 2, "Fixture must contain actual Slither CFG duplicates"
    assert copies[0].source_location == copies[1].source_location
    flow = next(
        flow
        for flow in build_flows(facts, Scope())
        if flow.root.name
        == ("strings" if library_name == "Strings" else "stringsUpgradeable")
    )
    children = [
        child
        for child in flow.root.children[0].children
        if isinstance(child, FunctionNode) and child.canonical_name == target
    ]
    assert len(children) == 1
