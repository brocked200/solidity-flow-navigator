"""Call sites use their originating expression, retaining compiler byte offsets."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from crytic_compile import CryticCompile
from crytic_compile.platform.solc_standard_json import SolcStandardJson
from slither.core.expressions.call_expression import CallExpression
from slither.core.expressions.identifier import Identifier
from slither.core.variables.local_variable import LocalVariable
from slither.slithir.operations import InternalCall

from solidity_flow_navigator.analysis import slither_facts

CALL_SOURCE = """// π and 🐍 preserve compiler byte positions.
pragma solidity ^0.8.26;
interface IERC { function balanceOf(address) external view returns (uint); }
library SignedMath {
    function abs(int value) internal pure returns (uint) { return uint(value); }
}
library SignedMathUpgradeable {
    function abs(int value) internal pure returns (uint) { return uint(value); }
}
library Strings {
    function toString(uint) internal pure returns (string memory) { return ""; }
    function toStringSigned(int value) internal pure returns (string memory) {
        return string(abi.encodePacked(
            value < 0 ? "-" : "", toString(SignedMath.abs(value))
        ));
    }
}
library StringsUpgradeable {
    function toString(uint) internal pure returns (string memory) { return ""; }
    function toString(int value) internal pure returns (string memory) {
        return string(abi.encodePacked(
            value < 0 ? "-" : "", toString(SignedMathUpgradeable.abs(value))
        ));
    }
}
contract Calls {
    function same(uint value) internal pure returns (uint) { return value; }
    function resolved(uint value) external pure returns (uint) {
        return same(value) + same(value);
    }
    function nested(uint value) external pure returns (uint) {
        return same(same(value));
    }
    function unresolved(IERC token, address who) external view returns (uint) {
        return token.balanceOf(who) + token.balanceOf(who);
    }
    function sinks(address payable to) external returns (bool) {
        return to.send(1) == to.send(1);
    }
    function strings(int value) external pure returns (string memory) {
        return Strings.toStringSigned(value);
    }
    function stringsUpgradeable(int value) external pure returns (string memory) {
        return StringsUpgradeable.toString(value);
    }
}
"""


@pytest.fixture(scope="module", params=["\n", "\r\n", "mixed"])
def call_expression_model(request, tmp_path_factory):
    """Compile real functions once for each compiler-input newline convention."""
    compiler = os.environ.get("SOLFLOW_TEST_SOLC") or shutil.which("solc")
    assert compiler, "A preinstalled solc is required for the integration gate"
    repo = tmp_path_factory.mktemp("call-expression")
    newline = request.param
    text = (
        CALL_SOURCE.replace("\n", "\r\n", 1)
        if newline == "mixed"
        else CALL_SOURCE.replace("\n", newline)
    )
    (repo / "Calls.sol").write_bytes(text.encode())
    compilation = CryticCompile(
        SolcStandardJson(
            {"language": "Solidity", "sources": {"Calls.sol": {"content": text}}}
        ),
        solc=compiler,
        solc_working_dir=str(repo),
        cwd=str(repo),
    )
    # Capture the actual compiler input independently of Slither's potentially
    # newline-normalized source snapshot, as compile_repo's adapter does.
    for unit in compilation.compilation_units.values():
        unit._solflow_source_bytes = {str(repo / "Calls.sol"): text.encode()}
    facts = slither_facts.extract_facts(compilation, repo)
    assert facts.analysis.complete, facts.analysis
    return facts, text.encode()


@pytest.mark.parametrize(
    ("function_name", "snippet"),
    [
        ("resolved", b"same(value)"),
        ("unresolved", b"token.balanceOf(who)"),
        ("sinks", b"to.send(1)"),
    ],
)
def test_compiled_identical_calls_have_distinct_exact_expression_locations(
    call_expression_model, function_name, snippet
):
    facts, source = call_expression_model
    contract = next(c for c in facts.contracts if c.name == "Calls")
    function = next(f for f in contract.functions if f.name == function_name)
    assert len(function.calls) == 2
    expected_starts = []
    cursor = function.source_location.start
    for _ in range(2):
        cursor = source.index(snippet, cursor)
        expected_starts.append(cursor)
        cursor += len(snippet)
    locations = [edge.source_location for edge in function.calls]
    assert [location.start for location in locations] == expected_starts
    assert [location.length for location in locations] == [len(snippet)] * 2
    assert all(location.filename_relative == "Calls.sol" for location in locations)
    for location in locations:
        assert location.lines == (source.count(b"\n", 0, location.start) + 1,)
        assert location.starting_column == location.start - source.rfind(
            b"\n", 0, location.start
        )
        assert location.ending_column == location.starting_column + len(snippet)
        assert source[location.start : location.start + location.length] == snippet


def test_nested_compiled_calls_keep_their_own_expression_spans(call_expression_model):
    facts, source = call_expression_model
    contract = next(c for c in facts.contracts if c.name == "Calls")
    function = next(f for f in contract.functions if f.name == "nested")
    assert len(function.calls) == 2
    assert [
        source[edge.source_location.start :][: edge.source_location.length]
        for edge in function.calls
    ] == [b"same(value)", b"same(same(value))"]


def _mapping(path: Path, start=2, length=20, *, source=None):
    sm = SimpleNamespace(
        filename=SimpleNamespace(absolute=str(path)),
        start=start,
        length=length,
        lines=(7,),
        starting_column=3,
        ending_column=23,
    )
    if source is not None:
        sm.compilation_unit = SimpleNamespace(
            core=SimpleNamespace(_solflow_source_bytes={str(path): source})
        )
    return sm


def _operation(node_mapping, expression_mapping):
    op = InternalCall(("same", "Calls"), 0, None, "")
    op.set_node(SimpleNamespace(source_mapping=node_mapping))
    expression = CallExpression(Identifier(LocalVariable()), [], "")
    expression.source_mapping = expression_mapping
    op.set_expression(expression)
    return op


def test_valid_call_mapping_without_snapshot_retains_slither_coordinates(tmp_path):
    node = _mapping(tmp_path / "Virtual.sol")
    expression = _mapping(tmp_path / "Virtual.sol", 5, 6)
    op = _operation(node, expression)
    assert slither_facts._op_location(op, tmp_path) == slither_facts._source_location(
        expression, tmp_path
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "empty",
        "negative",
        "outside-node",
        "other-file",
        "missing-filename",
        "empty-filename",
        "missing-start",
        "missing-length",
        "noninteger",
        "float",
        "boolean",
        "numeric-string",
        "missing-lines",
        "outside-snapshot",
        "split-utf8",
        "inconsistent-snapshot",
        "missing-expression-snapshot",
    ],
)
def test_invalid_expression_mapping_preserves_statement_fallback(tmp_path, invalid):
    path = tmp_path / "Calls.sol"
    node = _mapping(path)
    expression = _mapping(path, 5, 6)
    if invalid == "missing":
        expression = None
    elif invalid == "empty":
        expression.length = 0
    elif invalid == "negative":
        expression.start = -1
    elif invalid == "outside-node":
        expression.start = node.start + node.length
    elif invalid == "other-file":
        expression.filename.absolute = str(tmp_path / "Other.sol")
    elif invalid == "missing-filename":
        expression.filename = None
    elif invalid == "empty-filename":
        expression.filename.absolute = ""
    elif invalid == "missing-start":
        del expression.start
    elif invalid == "missing-length":
        del expression.length
    elif invalid == "noninteger":
        expression.start = "bad"
    elif invalid == "float":
        expression.start = 5.0
    elif invalid == "boolean":
        expression.length = True
    elif invalid == "numeric-string":
        expression.start = "5"
    elif invalid == "missing-lines":
        del expression.lines
    elif invalid == "outside-snapshot":
        expression = _mapping(path, 5, 6, source=b"short")
    elif invalid == "split-utf8":
        expression = _mapping(path, 5, 1, source="     π source".encode())
    elif invalid == "inconsistent-snapshot":
        node = _mapping(path, source=b" " * 30)
        expression = _mapping(path, 5, 6, source=b"x" * 30)
    elif invalid == "missing-expression-snapshot":
        node = _mapping(path, source=b" " * 30)
    op = _operation(node, expression)
    assert slither_facts._op_location(op, tmp_path) == slither_facts._source_location(
        node, tmp_path
    )


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_expression_coordinates_use_snapshot_when_checkout_changes(tmp_path, newline):
    path = tmp_path / "Calls.sol"
    source = "// π 🐍".encode() + newline + b"  same(value) + same(value);"
    path.write_text("a changed checkout")
    node_start = source.index(b"same(value)")
    node = _mapping(path, node_start, len(source) - node_start, source=source)
    expression = _mapping(path, node_start, len(b"same(value)"), source=source)
    location = slither_facts._op_location(_operation(node, expression), tmp_path)
    assert location.start == node_start
    assert location.length == len(b"same(value)")
    assert location.lines == (2,)
    assert location.starting_column == 3
    assert location.ending_column == 14


@pytest.mark.parametrize("expression", [None, Identifier(LocalVariable())])
def test_missing_or_noncall_expression_preserves_statement_fallback(
    tmp_path, expression
):
    node = _mapping(tmp_path / "Calls.sol")
    op = _operation(node, None)
    op._expression = expression
    assert slither_facts._op_location(op, tmp_path) == slither_facts._source_location(
        node, tmp_path
    )
