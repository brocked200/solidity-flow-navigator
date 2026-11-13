"""Low-level self-call facts come from the receiver, without callee binding."""

import os
import shutil
from types import SimpleNamespace

import pytest
from crytic_compile import CryticCompile
from crytic_compile.platform.solc_standard_json import SolcStandardJson
from slither.core.declarations import SolidityVariable
from slither.core.expressions.call_expression import CallExpression
from slither.core.expressions.identifier import Identifier
from slither.core.expressions.member_access import MemberAccess
from slither.core.expressions.type_conversion import TypeConversion
from slither.core.solidity_types import ElementaryType
from slither.core.variables.local_variable import LocalVariable
from slither.slithir.operations import LowLevelCall
from slither.slithir.variables.constant import Constant

from solidity_flow_navigator.analysis import slither_facts


def _receiver(name="this"):
    value = SolidityVariable("this") if name == "this" else LocalVariable()
    if name != "this":
        value.name = name
    return Identifier(value)


def _cast(receiver, type_name="address"):
    return TypeConversion(receiver, ElementaryType(type_name))


@pytest.mark.parametrize("opcode", ["call", "delegatecall", "staticcall"])
@pytest.mark.parametrize(
    ("receiver", "expected"),
    [
        (_receiver(), True),
        (_cast(_receiver()), True),
        (_cast(_cast(_receiver())), True),
        (_receiver("other"), False),
        (_cast(_receiver("other")), False),
        (_receiver("aliasOfThis"), False),
        (_cast(_receiver(), "uint256"), False),
    ],
)
def test_low_level_call_records_actual_receiver(tmp_path, opcode, receiver, expected):
    # These are Slither expressions and IR operations, not source-text matches.
    op = LowLevelCall(LocalVariable(), Constant(opcode), 1, None, "")
    op.set_expression(
        CallExpression(MemberAccess(opcode, "", receiver), [_receiver()], "")
    )
    op.set_node(
        SimpleNamespace(
            source_mapping=SimpleNamespace(
                filename=SimpleNamespace(absolute=str(tmp_path / "Calls.sol")),
                start=0,
                length=0,
                lines=(1,),
                starting_column=1,
                ending_column=1,
            )
        )
    )
    op.call_value = "amount"

    edge = slither_facts._call_edge(op, tmp_path)

    assert edge.is_self_call is expected
    assert edge.kind == "low_level"
    assert edge.subkind == opcode
    assert edge.is_resolved is False
    assert edge.target_canonical_name is None
    assert edge.target_contract_name is None
    assert edge.value_expression == "amount"


_SOURCE = """// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.26;
contract Calls {
    receive() external payable {}
    function ownAddress(bytes memory payload) external returns (bool, bytes memory) {
        return address(this).call(payload);
    }
    function ownPayable(bytes memory payload, uint256 amount)
        external payable returns (bool, bytes memory)
    {
        return payable(this).call{value: amount}(payload);
    }
    function ownDelegate(bytes memory payload) external returns (bool, bytes memory) {
        return address(this).delegatecall(payload);
    }
    function ownStatic(bytes memory payload)
        external view returns (bool, bytes memory)
    {
        return address(this).staticcall(payload);
    }
    function foreign(address other, bytes memory payload)
        external returns (bool, bytes memory)
    {
        return other.call(abi.encode(address(this), payload));
    }
    function foreignCast(Calls other, bytes memory payload)
        external returns (bool, bytes memory)
    {
        return address(other).call(payload);
    }
    function aliasReceiver(bytes memory payload) external returns (bool, bytes memory) {
        address aliasOfThis = address(this);
        return aliasOfThis.call(payload);
    }
    function echo() external {}
    function highSelf() external { this.echo(); }
    function highOther(Calls other) external { other.echo(); }
}
"""


def test_compiled_low_level_casts_and_options_preserve_receiver_facts(tmp_path):
    compiler = os.environ.get("SOLFLOW_TEST_SOLC") or shutil.which("solc")
    assert compiler, "A preinstalled solc is required for the integration gate"
    (tmp_path / "Calls.sol").write_text(_SOURCE)
    compilation = CryticCompile(
        SolcStandardJson(
            {"language": "Solidity", "sources": {"Calls.sol": {"content": _SOURCE}}}
        ),
        solc=compiler,
        solc_working_dir=str(tmp_path),
        cwd=str(tmp_path),
    )

    facts = slither_facts.extract_facts(compilation, tmp_path)

    assert facts.analysis.complete, facts.analysis
    contract = next(c for c in facts.contracts if c.name == "Calls")
    expected = {
        "ownAddress": (True, "call", None),
        "ownPayable": (True, "call", "amount"),
        "ownDelegate": (True, "delegatecall", None),
        "ownStatic": (True, "staticcall", None),
        "foreign": (False, "call", None),
        "foreignCast": (False, "call", None),
        "aliasReceiver": (False, "call", None),
    }
    for name, (is_self, opcode, value) in expected.items():
        fn = next(f for f in contract.functions if f.name == name)
        edges = [edge for edge in fn.calls if edge.kind == "low_level"]
        assert len(edges) == 1, (name, fn.calls)
        edge = edges[0]
        assert edge.is_self_call is is_self, name
        assert edge.subkind == opcode
        assert edge.value_expression == value
        assert edge.is_resolved is False
        assert edge.target_canonical_name is None
        assert edge.target_contract_uid is None
    for name, is_self in (("highSelf", True), ("highOther", False)):
        fn = next(f for f in contract.functions if f.name == name)
        edge = next(edge for edge in fn.calls if edge.kind == "high_level")
        assert edge.is_self_call is is_self
        assert edge.is_resolved is True
        assert edge.target_canonical_name == "Calls.echo()"
