"""Layer 1 unit tests for the Slither fact extractor (analysis/slither_facts.py).

These run without the Solmate integration fixture: ``Slither`` is replaced by
a minimal stub, so the tests can exercise ``extract_facts`` mechanics that a
single-compilation-unit repo (Foundry, i.e. the whole integration test folder)
can never reach. Regression coverage for the v0.25.2 contract de-dup (spec
§11.5, "Contract de-duplication across compilation units"): Hardhat-style
builds hand Slither one compilation unit per source file, so
``slither.contracts`` yields the same contract once per unit that includes it,
and every duplicate passed through built an identical Flow per entry point.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from slither.core.declarations import Contract as SlitherContract
from slither.core.declarations import SolidityFunction, SolidityVariable
from slither.core.declarations.function_contract import FunctionContract
from slither.core.expressions.call_expression import CallExpression
from slither.core.expressions.identifier import Identifier
from slither.core.expressions.member_access import MemberAccess
from slither.core.expressions.super_call_expression import SuperCallExpression
from slither.core.solidity_types import FunctionType, UserDefinedType
from slither.core.variables.local_variable import LocalVariable
from slither.slithir.operations import (
    HighLevelCall,
    InternalCall,
    InternalDynamicCall,
    SolidityCall,
)
from slither.slithir.variables.constant import Constant
from slither.solc_parsing.yul.evm_functions import format_function_descriptor

from solidity_flow_navigator.analysis import slither_facts
from solidity_flow_navigator.analysis.types import RepoFacts


def test_value_expression_preserves_symbolic_text() -> None:
    assert (
        slither_facts._value_expression(SimpleNamespace(call_value="amount * 2"))
        == "amount * 2"
    )
    assert slither_facts._value_expression(SimpleNamespace(call_value=None)) is None


def _source_mapping(absolute: str) -> SimpleNamespace:
    """The attribute subset of a Slither source mapping ``_source_location`` reads."""

    return SimpleNamespace(
        filename=SimpleNamespace(absolute=absolute),
        start=0,
        length=0,
        lines=(1,),
        starting_column=1,
        ending_column=1,
    )


def _stub_contract(
    name: str, absolute_path: str, *, is_abstract: bool = False
) -> SimpleNamespace:
    """A minimal object satisfying every contract attribute ``extract_facts`` reads."""

    return SimpleNamespace(
        name=name,
        contract_kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=is_abstract,
        inheritance=[],
        immediate_inheritance=[],
        functions_declared=[],
        state_variables_declared=[],
        modifiers_declared=[],
        structures_declared=[],
        enums_declared=[],
        source_mapping=_source_mapping(absolute_path),
    )


def _extract(monkeypatch, repo_root: Path, contracts: list) -> RepoFacts:
    """Run ``extract_facts`` against a stub Slither exposing ``contracts``."""

    stub = SimpleNamespace(contracts=contracts, compilation_units=[])
    monkeypatch.setattr(slither_facts, "Slither", lambda _cc: stub)
    return slither_facts.extract_facts(object(), repo_root)


def test_same_contract_repeated_across_units_collapses_keep_first(
    monkeypatch, tmp_path
):
    repo = tmp_path.resolve()
    pool = str(repo / "contracts" / "Pool.sol")
    # Real cross-unit duplicates are byte-identical; the differing is_abstract
    # flag is a marker to observe WHICH copy survives, not a realistic shape.
    first = _stub_contract("Pool", pool, is_abstract=True)
    duplicates = [_stub_contract("Pool", pool) for _ in range(2)]

    facts = _extract(monkeypatch, repo, [first, *duplicates])

    assert len(facts.contracts) == 1
    kept = facts.contracts[0]
    assert kept.uid == ("contracts/Pool.sol", "Pool")
    assert kept.is_abstract is True


def test_distinct_same_name_contracts_in_different_files_both_survive(
    monkeypatch, tmp_path
):
    # The v0.25.0 name-collision case (two files each declaring IERC20) must
    # NOT be collapsed by the de-dup: identity is the uid, never the bare name.
    repo = tmp_path.resolve()
    a = _stub_contract("IERC20", str(repo / "a" / "IERC20.sol"))
    b = _stub_contract("IERC20", str(repo / "b" / "IERC20.sol"))

    facts = _extract(monkeypatch, repo, [a, b])

    assert [c.uid for c in facts.contracts] == [
        ("a/IERC20.sol", "IERC20"),
        ("b/IERC20.sol", "IERC20"),
    ]


def test_mapping_uses_adapter_normalized_snapshot(tmp_path) -> None:
    """An adapter-supplied normalized input can differ from checkout bytes.

    The unicode comment makes a byte offset differ from a Python character
    offset as well, so this one fixture protects both sources of drift.
    ``content`` and ``lines`` deliberately model the bad values Slither can
    derive when it applies those offsets to original CRLF disk bytes.
    """

    source = (
        "// π-prefixed source\r\n"
        "contract C {\r\n"
        "    function before() external {}\r\n"
        "    function deposit() external {\r\n"
        "        uint256 x = 1;\r\n"
        "    }\r\n"
        "}\r\n"
    ).encode()
    compiler_source = source.replace(b"\r\n", b"\n")
    fragment = b"    function deposit() external {\n        uint256 x = 1;\n    }"
    path = tmp_path / "C.sol"
    path.write_bytes(source)

    start = compiler_source.index(fragment)
    sm = SimpleNamespace(
        filename=SimpleNamespace(absolute=str(path)),
        start=start,
        length=len(fragment),
        lines=(999,),
        starting_column=5,
        ending_column=6,
        content="shifted normalized text",
        compilation_unit=SimpleNamespace(
            core=SimpleNamespace(source_code={str(path): compiler_source.decode()})
        ),
    )

    assert slither_facts._source_content(sm) == fragment.decode()
    location = slither_facts._source_location(sm, tmp_path)
    assert location.start == start
    assert location.length == len(fragment)
    assert location.lines == (4, 5, 6)


def test_source_mapping_falls_back_when_raw_file_is_unavailable(tmp_path) -> None:
    """Compiler adapters with virtual sources keep the old Slither fields."""

    sm = SimpleNamespace(
        filename=SimpleNamespace(absolute=str(tmp_path / "virtual.sol")),
        start=7,
        length=3,
        lines=(9,),
        starting_column=2,
        ending_column=4,
        content="abc",
    )

    assert slither_facts._source_content(sm) == "abc"
    assert slither_facts._source_location(sm, tmp_path).lines == (9,)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "mixed"])
@pytest.mark.parametrize(
    "declaration",
    [
        "function deposit() external {\n    uint256 x = 1;\n}",
        "uint256 public balance;",
        "struct Amount {\n    uint256 value;\n}",
        "enum State {\n    Open, Closed\n}",
        "type Amount is uint256;",
    ],
)
def test_mapping_uses_compilation_source_bytes(
    tmp_path, newline: str, declaration: str
) -> None:
    """Offsets address the compilation's bytes, with no newline guessing."""
    prefix = "// π and 🐍\ncontract C {\n"
    compiler_text = prefix + declaration + "\n}\n"
    if newline == "mixed":
        compiler_text = compiler_text.replace("\n", "\r\n", 1)
    else:
        compiler_text = compiler_text.replace("\n", newline)
    raw = compiler_text.encode()
    fragment = declaration.replace("\n", "\r\n" if newline == "\r\n" else "\n")
    path = tmp_path / "C.sol"
    # The checkout can change after the adapter retained its compiler input.
    path.write_text("// a different checkout\n")
    start = raw.index(fragment.encode())
    sm = SimpleNamespace(
        filename=SimpleNamespace(absolute=str(path)),
        start=start,
        length=len(fragment.encode()),
        lines=(999,),
        starting_column=1,
        ending_column=1,
        content="incorrect fallback",
        compilation_unit=SimpleNamespace(
            core=SimpleNamespace(source_code={str(path): compiler_text})
        ),
    )

    assert slither_facts._source_content(sm) == fragment
    location = slither_facts._source_location(sm, tmp_path)
    assert location.lines == tuple(range(3, 3 + fragment.count("\n") + 1))
    assert location.starting_column == start - raw.rfind(b"\n", 0, start)
    end = start + len(fragment.encode())
    assert location.ending_column == end - raw.rfind(b"\n", 0, end)


def test_mapping_cache_does_not_outlive_compilation(tmp_path) -> None:
    path = tmp_path / "C.sol"
    for name in ("before", "after_"):
        text = f"contract C {{ function {name}() external {{}} }}"
        path.write_text(text)
        fragment = f"function {name}() external {{}}"
        sm = SimpleNamespace(
            filename=SimpleNamespace(absolute=str(path)),
            start=text.index(fragment),
            length=len(fragment),
            content=fragment,
            compilation_unit=SimpleNamespace(
                core=SimpleNamespace(source_code={str(path): text})
            ),
        )
        assert slither_facts._source_content(sm) == fragment


def test_mapping_source_is_specific_to_its_compilation_unit(tmp_path) -> None:
    path = tmp_path / "C.sol"
    sources = ["function first_() {}", "function second() {}"]
    path.write_text(sources[0])
    for text in sources:
        sm = SimpleNamespace(
            filename=SimpleNamespace(absolute=str(path)),
            start=0,
            length=len(text),
            content=text,
            compilation_unit=SimpleNamespace(
                # A shared Slither core can only retain one version per path;
                # the per-unit captured input must take precedence over it.
                core=SimpleNamespace(source_code={str(path): sources[0]}),
                crytic_compile_compilation_unit=SimpleNamespace(
                    _solflow_source_bytes={str(path): text.encode()}
                ),
            ),
        )
        assert slither_facts._source_content(sm) == text


@pytest.mark.parametrize("is_super", [True, False])
def test_internal_edge_records_actual_super_expression_and_declarer_uid(
    tmp_path, is_super: bool
) -> None:
    declarer = SimpleNamespace(
        name="Base", source_mapping=_source_mapping(str(tmp_path / "Base.sol"))
    )
    context = SimpleNamespace(name="Derived")
    target = FunctionContract(SimpleNamespace(is_solidity=True))
    target._name = "run"
    target.set_contract_declarer(declarer)
    target.set_contract(context)
    op = InternalCall(target, 0, None, "")
    op.set_node(
        SimpleNamespace(source_mapping=_source_mapping(str(tmp_path / "C.sol")))
    )
    expression_class = SuperCallExpression if is_super else CallExpression
    op.set_expression(expression_class(Identifier(target), [], ""))

    edge = slither_facts._internal_edge(op, tmp_path)

    assert edge.is_super_call is is_super
    assert edge.is_self_call is False
    assert edge.target_contract_uid == ("Base.sol", "Base")


@pytest.mark.parametrize("self_call", [True, False])
def test_high_level_edge_records_actual_receiver_and_declarer_uid(
    tmp_path, self_call: bool
) -> None:
    declarer = SimpleNamespace(
        name="Base", source_mapping=_source_mapping(str(tmp_path / "Base.sol"))
    )
    target = FunctionContract(SimpleNamespace(is_solidity=True))
    target._name = "run"
    target.set_contract_declarer(declarer)
    target.set_contract(SimpleNamespace(name="Derived"))
    receiver = SolidityVariable("this") if self_call else LocalVariable()
    op = HighLevelCall(receiver, Constant("run"), 0, None, "")
    op.function = target
    op.set_node(
        SimpleNamespace(source_mapping=_source_mapping(str(tmp_path / "C.sol")))
    )

    edge = slither_facts._high_or_library_edge(op, tmp_path, kind="high_level")

    assert edge.is_self_call is self_call
    assert edge.is_super_call is False
    assert edge.target_contract_uid == ("Base.sol", "Base")


@pytest.mark.parametrize("qualified", [True, False])
def test_internal_edge_records_explicit_base_qualification(
    tmp_path, qualified: bool
) -> None:
    unit = SimpleNamespace(is_solidity=True)
    declarer = SlitherContract(unit, None)
    declarer._name = "Base"
    declarer.source_mapping = _source_mapping(str(tmp_path / "Base.sol"))
    target = FunctionContract(unit)
    target._name = "run"
    target.set_contract_declarer(declarer)
    target.set_contract(declarer)
    called = (
        MemberAccess("run", "function ()", Identifier(declarer))
        if qualified
        else Identifier(target)
    )
    op = InternalCall(target, 0, None, "")
    op.set_node(
        SimpleNamespace(source_mapping=_source_mapping(str(tmp_path / "C.sol")))
    )
    op.set_expression(CallExpression(called, [], ""))

    edge = slither_facts._internal_edge(op, tmp_path)

    assert edge.is_static_call is qualified


@pytest.mark.parametrize(
    ("opcode", "argument_count", "expected"),
    [
        ("call", 7, "amount * 2"),
        ("callcode", 7, "amount * 2"),
        ("delegatecall", 6, None),
        ("staticcall", 6, None),
    ],
)
def test_yul_native_value_is_third_argument(
    tmp_path, opcode: str, argument_count: int, expected: str | None
) -> None:
    op = SolidityCall(
        SolidityFunction(format_function_descriptor(opcode)), argument_count, None, ""
    )
    op.arguments = ["gas", "destination", "amount * 2", *(["0"] * (argument_count - 3))]
    op.set_node(
        SimpleNamespace(source_mapping=_source_mapping(str(tmp_path / "C.sol")))
    )

    edge = slither_facts._call_edge(op, tmp_path)

    assert edge is not None
    assert edge.kind == "solidity"
    assert edge.value_expression == expected


def test_free_functions_with_same_signature_in_different_files_survive(
    monkeypatch, tmp_path
) -> None:
    first = SimpleNamespace(
        canonical_name="helper()",
        source_mapping=_source_mapping(str(tmp_path / "A.sol")),
    )
    second = SimpleNamespace(
        canonical_name="helper()",
        source_mapping=_source_mapping(str(tmp_path / "B.sol")),
    )
    slither = SimpleNamespace(
        compilation_units=[
            SimpleNamespace(functions_top_level=[first, second]),
            SimpleNamespace(functions_top_level=[first]),
        ]
    )
    monkeypatch.setattr(
        slither_facts, "_build_function", lambda function, *_a, **_k: function
    )
    assert slither_facts._build_free_functions(slither, tmp_path) == (first, second)


def test_function_pointer_call_is_retained_as_explicit_dynamic_edge(tmp_path) -> None:
    pointer = LocalVariable()
    pointer.name = "callback"
    op = InternalDynamicCall(None, pointer, FunctionType([], []))
    op.call_value = "amount"
    op.set_node(
        SimpleNamespace(source_mapping=_source_mapping(str(tmp_path / "C.sol")))
    )
    edge = slither_facts._call_edge(op, tmp_path)
    assert edge is not None
    assert edge.kind == "internal_dynamic"
    assert edge.is_resolved is False
    assert edge.target_function_name == "callback"
    assert edge.value_expression == "amount"


def test_function_pointer_value_survives_missing_ir_value(tmp_path) -> None:
    pointer = LocalVariable()
    pointer.name = "callback"
    amount = LocalVariable()
    amount.name = "amount"
    op = InternalDynamicCall(None, pointer, FunctionType([], []))
    expression = CallExpression(Identifier(pointer), [], "")
    expression.call_value = Identifier(amount)
    op.set_expression(expression)
    op.set_node(
        SimpleNamespace(source_mapping=_source_mapping(str(tmp_path / "C.sol")))
    )

    edge = slither_facts._call_edge(op, tmp_path)

    assert edge is not None
    assert edge.value_expression == "amount"


def test_high_level_edge_retains_distinct_static_receiver_identity(tmp_path) -> None:
    unit = SimpleNamespace(is_solidity=True)
    declarer = SlitherContract(unit, None)
    declarer._name = "Base"
    declarer.source_mapping = _source_mapping(str(tmp_path / "Base.sol"))
    receiver_contract = SlitherContract(unit, None)
    receiver_contract._name = "Derived"
    receiver_contract.source_mapping = _source_mapping(
        str(tmp_path / "second" / "Derived.sol")
    )
    target = FunctionContract(unit)
    target._name = "work"
    target.set_contract_declarer(declarer)
    target.set_contract(receiver_contract)
    receiver = LocalVariable()
    receiver.set_type(UserDefinedType(receiver_contract))
    op = HighLevelCall(receiver, Constant("work"), 0, None, "")
    op.function = target
    op.set_node(
        SimpleNamespace(source_mapping=_source_mapping(str(tmp_path / "C.sol")))
    )
    edge = slither_facts._high_or_library_edge(op, tmp_path, kind="high_level")
    assert edge.target_contract_uid == ("Base.sol", "Base")
    assert edge.static_receiver_uid == ("second/Derived.sol", "Derived")
