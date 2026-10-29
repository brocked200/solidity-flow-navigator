"""Inherited external receivers must keep unambiguous dispatch context."""

from dataclasses import replace

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
from solidity_flow_navigator.flow.types import (
    FunctionNode,
    UnresolvedNode,
    UnresolvedReason,
)


def _source(filename: str) -> SourceLocation:
    return SourceLocation("/abs/" + filename, filename, 0, 1, (1,), 1, 2)


def _function(contract: str, name: str, filename: str, **changes) -> Function:
    result = Function(
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
    )
    return replace(result, **changes)


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


@pytest.mark.parametrize("reverse_receivers", [False, True])
@pytest.mark.parametrize("receiver_identity", ["known", "missing", "unknown"])
def test_duplicate_inherited_receiver_context(
    reverse_receivers, receiver_identity
) -> None:
    hook = _function("Base", "hook", "Base.sol", is_virtual=True)
    internal = CallEdge(
        kind="internal",
        subkind=None,
        target_canonical_name="Base.hook()",
        target_function_name="hook",
        target_contract_name="Base",
        is_resolved=True,
        source_location=_source("Base.sol"),
        is_super_call=False,
        target_contract_uid=hook.contract_declarer_uid,
    )
    work = _function("Base", "work", "Base.sol", calls=(internal,))
    base = _contract("Base", "Base.sol", (work, hook))
    first = _contract(
        "Derived",
        "one/Derived.sol",
        (_function("Derived", "hook", "one/Derived.sol"),),
        (base,),
    )
    second = _contract(
        "Derived",
        "two/Derived.sol",
        (_function("Derived", "hook", "two/Derived.sol"),),
        (base,),
    )
    metadata = {}
    if receiver_identity == "known":
        metadata["static_receiver_uid"] = first.uid
    elif receiver_identity == "unknown":
        metadata["static_receiver_uid"] = ("absent/Derived.sol", "Derived")
    external = CallEdge(
        kind="high_level",
        subkind=None,
        target_canonical_name="Base.work()",
        target_function_name="work",
        target_contract_name="Derived",
        is_resolved=True,
        source_location=_source("Vault.sol"),
        value_expression="amount",
        is_self_call=False,
        target_contract_uid=work.contract_declarer_uid,
        **metadata,
    )
    vault = _contract(
        "Vault",
        "Vault.sol",
        (
            _function(
                "Vault", "entry", "Vault.sol", is_entry_point=True, calls=(external,)
            ),
        ),
    )
    receivers = (second, first) if reverse_receivers else (first, second)
    flow = build_flows(RepoFacts("/abs", (vault, base, *receivers), ()), Scope())[0]
    node = flow.root.children[0]
    assert flow.value_sink_count == 1
    if receiver_identity == "known":
        assert isinstance(node, FunctionNode)
        assert node.invoked_via_contract_name == "Derived"
        assert node.call_value == "amount"
        assert isinstance(node.children[0], FunctionNode)
        assert node.children[0].source_location.filename_relative == "one/Derived.sol"
        assert flow.unresolved_count == 0
    else:
        assert isinstance(node, UnresolvedNode)
        assert node.reason == UnresolvedReason.AMBIGUOUS_TARGET
        assert node.value_expression == "amount"
        assert flow.unresolved_count == 1
