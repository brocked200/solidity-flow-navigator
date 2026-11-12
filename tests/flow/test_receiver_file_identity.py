"""Dedup preserves distinct receiver UIDs while merging CFG branch duplicates."""

from dataclasses import fields, replace

import pytest

from solidity_flow_navigator.analysis.types import (
    CallEdge,
    Contract,
    Function,
    RepoFacts,
    SourceLocation,
)
from solidity_flow_navigator.flow.builder import _child_identity, build_flows
from solidity_flow_navigator.flow.scope import Scope
from solidity_flow_navigator.flow.types import FunctionNode, SinkNode


def _source(filename: str) -> SourceLocation:
    return SourceLocation("/abs/" + filename, filename, 0, 1, (1,), 1, 2)


def _node() -> FunctionNode:
    # Retain the pre-field positional construction to guard API compatibility.
    return FunctionNode(
        "Base.work()",
        "work",
        "work()",
        "Base",
        "D",
        False,
        False,
        "public",
        False,
        False,
        False,
        "function work() public {}",
        _source("Base.sol"),
        (),
        (),
    )


def test_receiver_file_is_optional_and_keyword_only() -> None:
    node = _node()
    node_fields = {item.name: item for item in fields(node)}
    assert "invoked_via_contract_file" in node_fields
    receiver_file_field = node_fields["invoked_via_contract_file"]
    assert receiver_file_field.kw_only
    assert node.invoked_via_contract_file is None
    assert (
        replace(node, invoked_via_contract_file="one/D.sol").invoked_via_contract_file
        == "one/D.sol"
    )


def test_function_identity_distinguishes_receiver_files() -> None:
    first = replace(_node(), invoked_via_contract_file="one/D.sol")
    second = replace(_node(), invoked_via_contract_file="two/D.sol")
    assert _child_identity(first) != _child_identity(second)


def test_function_identity_matches_identical_receiver_uids() -> None:
    first = replace(_node(), invoked_via_contract_file="one/D.sol")
    second = replace(_node(), invoked_via_contract_file="one/D.sol")
    assert first is not second
    assert _child_identity(first) == _child_identity(second)


def _function(contract: str, name: str, filename: str, **changes) -> Function:
    return replace(
        Function(
            canonical_name=f"{contract}.{name}()",
            name=name,
            full_name=f"{name}()",
            contract_declarer_name=contract,
            visibility="public",
            is_constructor=False,
            is_fallback=False,
            is_receive=False,
            is_modifier=False,
            is_implemented=True,
            is_virtual=False,
            is_entry_point=False,
            payable=False,
            view=False,
            pure=False,
            parameters=(),
            returns=(),
            modifier_names=(),
            source_location=_source(filename),
            source_code=f"function {name}() public {{}}",
            calls=(),
        ),
        **changes,
    )


def _contract(name: str, filename: str, functions, bases=()) -> Contract:
    return Contract(
        name=name,
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=tuple(base.name for base in bases),
        immediate_base_contract_names=tuple(base.name for base in bases),
        linearized_base_contract_uids=tuple(base.uid for base in bases),
        source_location=_source(filename),
        functions=functions,
        modifiers=(),
    )


def _internal(function: Function) -> CallEdge:
    return CallEdge(
        kind="internal",
        subkind=None,
        target_canonical_name=function.canonical_name,
        target_function_name=function.name,
        target_contract_name=function.contract_declarer_name,
        is_resolved=True,
        source_location=_source("Base.sol"),
        is_super_call=False,
        target_contract_uid=function.contract_declarer_uid,
    )


def _facts(receiver_files: tuple[str, ...]) -> RepoFacts:
    hook = _function("Base", "hook", "Base.sol", is_virtual=True)
    work = _function("Base", "work", "Base.sol")
    work = replace(work, calls=(_internal(hook), _internal(work)))
    base = _contract("Base", "Base.sol", (work, hook))
    receivers = tuple(
        _contract(
            "D",
            filename,
            (
                _function(
                    "D",
                    "hook",
                    filename,
                    calls=(
                        CallEdge(
                            kind="transfer",
                            subkind=None,
                            target_canonical_name=None,
                            target_function_name=None,
                            target_contract_name=None,
                            is_resolved=False,
                            source_location=_source(filename),
                            value_expression=filename,
                        ),
                    ),
                ),
            ),
            (base,),
        )
        for filename in ("one/D.sol", "two/D.sol")
    )
    # All edges share one source mapping, exactly the dedup collision trigger.
    calls = tuple(
        CallEdge(
            kind="high_level",
            subkind=None,
            target_canonical_name=work.canonical_name,
            target_function_name=work.name,
            target_contract_name="D",
            is_resolved=True,
            source_location=_source("Vault.sol"),
            target_contract_uid=work.contract_declarer_uid,
            static_receiver_uid=(filename, "D"),
            is_self_call=False,
        )
        for filename in receiver_files
    )
    vault = _contract(
        "Vault",
        "Vault.sol",
        (_function("Vault", "entry", "Vault.sol", is_entry_point=True, calls=calls),),
    )
    return RepoFacts("/abs", (vault, base, *receivers), ())


@pytest.mark.parametrize("reverse_receivers", [False, True])
def test_build_flows_preserves_same_named_receiver_subtrees(reverse_receivers) -> None:
    files = ("one/D.sol", "two/D.sol")
    if reverse_receivers:
        files = tuple(reversed(files))
    flow = build_flows(_facts(files), Scope())[0]
    assert len(flow.root.children) == 2
    assert flow.value_sink_count == 2
    assert flow.unresolved_count == 0
    for node, filename in zip(flow.root.children, files, strict=True):
        assert isinstance(node, FunctionNode)
        assert node.canonical_name == "Base.work()"
        assert node.source_location.filename_relative == "Base.sol"
        assert node.invoked_via_contract_name == "D"
        assert node.invoked_via_contract_file == filename
        override = node.children[0]
        assert isinstance(override, FunctionNode)
        assert override.canonical_name == "D.hook()"
        assert override.source_location.filename_relative == filename
        sink = override.children[0]
        assert isinstance(sink, SinkNode)
        assert sink.value_expression == filename


def test_build_flows_identical_receiver_duplicates_still_collapse() -> None:
    flow = build_flows(_facts(("one/D.sol", "one/D.sol")), Scope())[0]
    assert len(flow.root.children) == 1
    assert flow.value_sink_count == 1
    assert flow.unresolved_count == 0


@pytest.mark.parametrize("node_kind", ["root", "expanded", "override", "cycle"])
def test_build_flows_populates_receiver_files(node_kind) -> None:
    flow = build_flows(_facts(("one/D.sol",)), Scope())[0]
    expanded = flow.root.children[0]
    assert isinstance(expanded, FunctionNode)
    override, cycle = expanded.children
    assert isinstance(override, FunctionNode)
    assert isinstance(cycle, FunctionNode)
    assert cycle.canonical_name == "Base.work()"
    assert cycle.children == ()
    nodes = {
        "root": flow.root,
        "expanded": expanded,
        "override": override,
        "cycle": cycle,
    }
    expected = "Vault.sol" if node_kind == "root" else "one/D.sol"
    assert nodes[node_kind].invoked_via_contract_file == expected
