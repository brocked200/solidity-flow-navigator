"""Contract IDs must survive duplicate names without changing virtual dispatch."""

import importlib
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from crytic_compile import CryticCompile
from crytic_compile.utils.naming import Filename
from slither import Slither
from slither.core.declarations.contract import Contract
from slither.core.declarations.function_contract import FunctionContract
from slither.core.scope.scope import FileScope
from slither.core.solidity_types import UserDefinedType
from slither.solc_parsing.declarations.contract import ContractSolc
from slither.solc_parsing.declarations.custom_error import CustomErrorSolc
from slither.solc_parsing.declarations.event_top_level import EventTopLevelSolc
from slither.solc_parsing.declarations.function import FunctionSolc
from slither.solc_parsing.declarations.structure_top_level import StructureTopLevelSolc
from slither.solc_parsing.exceptions import VariableNotFound
from slither.solc_parsing.slither_compilation_unit_solc import (
    SlitherCompilationUnitSolc,
)
from slither.solc_parsing.variables.top_level_variable import TopLevelVariableSolc

from solidity_flow_navigator.analysis.slither_resolver import contract_id_resolution

resolver = importlib.import_module("slither.solc_parsing.expressions.find_variable")
expressions = importlib.import_module(
    "slither.solc_parsing.expressions.expression_parsing"
)
vyper_resolver = importlib.import_module(
    "slither.vyper_parsing.expressions.find_variable"
)
types = importlib.import_module("slither.solc_parsing.solidity_types.type_parsing")
variable_types = importlib.import_module(
    "slither.solc_parsing.variables.variable_declaration"
)


def _scope(name):
    return FileScope(Filename(name, name, name, name))


def _unit(scope):
    return SlitherCompilationUnitSolc(
        SimpleNamespace(scopes={scope.filename: scope}, is_solidity=True)
    )


def _contract(parser, scope, ref=30774, name="Math"):
    contract = Contract(parser.compilation_unit, scope)
    contract.name = name
    contract.id = ref
    parser.contracts_by_id[ref] = contract
    return contract


def _duplicate_math():
    scope = _scope("Main.sol")
    parser = _unit(scope)
    local = _contract(parser, _scope("LocalMath.sol"))
    vendor = _contract(parser, _scope("VendorMath.sol"), 103119)
    scope.contracts["Math"] = vendor
    return parser, scope, local, vendor


@pytest.mark.parametrize("entry", [resolver, expressions])
@pytest.mark.parametrize("name", ["Math", "LocalMath"])
def test_complete_unit_id_wins_over_duplicate_name_and_import_alias(entry, name):
    with contract_id_resolution(threading.RLock()):
        _parser, scope, local, _vendor = _duplicate_math()
        scope.renaming["LocalMath"] = "Math"
        assert entry.find_variable(name, scope, 30774) == (local, False)


@pytest.mark.parametrize("node_type", ["UserDefinedTypeName", "IdentifierPath"])
@pytest.mark.parametrize("entry", [types, variable_types, expressions])
def test_contract_type_id_wins_over_collapsed_names(monkeypatch, node_type, entry):
    with contract_id_resolution(threading.RLock()):
        parser, scope, local, vendor = _duplicate_math()
        parser._is_compact_ast = True
        current = _contract(parser, scope, 404, "Main")
        context = object.__new__(ContractSolc)
        context._slither_parser = parser
        context._contract = current
        references = []
        for candidate in (local, vendor):
            monkeypatch.setattr(
                candidate,
                "add_reference_from_raw_source",
                lambda src, unit: references.append((src, unit)),
            )
        node = {
            "nodeType": node_type,
            "name": "Math",
            "src": "0:4:0",
            "referencedDeclaration": local.id,
            "typeDescriptions": {"typeString": "contract Math"},
        }
        result = entry.parse_type(node, context)
        assert isinstance(result, UserDefinedType)
        assert result.type is local
        assert references == [(node["src"], parser.compilation_unit)]


def test_contract_type_path_id_disagreement_fails_closed():
    with contract_id_resolution(threading.RLock()):
        parser, _scope, local, vendor = _duplicate_math()
        node = {
            "nodeType": "UserDefinedTypeName",
            "referencedDeclaration": local.id,
            "pathNode": {"referencedDeclaration": vendor.id},
        }
        with pytest.raises(VariableNotFound, match="[Ii]nconsistent"):
            types.parse_type(node, parser)


def test_identical_ids_are_local_to_compilation_units():
    with contract_id_resolution(threading.RLock()):
        _first, first_scope, first, _vendor = _duplicate_math()
        _second, second_scope, second, _vendor = _duplicate_math()
        assert first_scope.filename == second_scope.filename
        assert resolver.find_variable("Math", first_scope, 30774)[0] is first
        assert resolver.find_variable("Math", second_scope, 30774)[0] is second


@pytest.mark.parametrize(
    "context_type",
    [
        ContractSolc,
        FunctionSolc,
        StructureTopLevelSolc,
        TopLevelVariableSolc,
        EventTopLevelSolc,
        CustomErrorSolc,
    ],
)
def test_all_solc_caller_contexts_use_their_own_parser(context_type):
    with contract_id_resolution(threading.RLock()):
        parser, scope, local, vendor = _duplicate_math()
        context = object.__new__(context_type)
        context._slither_parser = parser
        # A valid Contract ID needs no name-map or inherited-function lookup.
        # These real context classes deliberately expose only their owner.
        assert vendor is not local
        assert scope.contracts["Math"] is vendor
        assert resolver.find_variable("Math", context, local.id) == (local, False)


@pytest.mark.parametrize(
    "failure", ["missing", "wrong_id", "wrong_unit", "ambiguous_id", "other_kind"]
)
def test_inconsistent_contract_id_evidence_fails_closed(failure):
    with contract_id_resolution(threading.RLock()):
        parser, scope, local, vendor = _duplicate_math()
        if failure == "missing":
            del parser.contracts_by_id[local.id]
        elif failure == "wrong_id":
            parser.contracts_by_id[local.id] = vendor
        elif failure == "wrong_unit":
            foreign = _unit(_scope("Foreign.sol"))
            parser.contracts_by_id[local.id] = _contract(foreign, scope)
        elif failure == "ambiguous_id":
            duplicate = Contract(parser.compilation_unit, scope)
            duplicate.name, duplicate.id = "Math", local.id
            parser.contracts_by_id[12] = duplicate
        else:
            parser.functions_by_id[local.id] = []
        with pytest.raises(VariableNotFound, match="[Cc]ontract"):
            resolver.find_variable("Math", scope, local.id)


def test_scope_with_two_owners_fails_closed():
    with contract_id_resolution(threading.RLock()):
        _parser, scope, local, _vendor = _duplicate_math()
        _unit(scope)
        with pytest.raises(VariableNotFound, match="[Aa]mbiguous"):
            resolver.find_variable("Math", scope, local.id)


def test_unregistered_scope_cannot_trust_a_contract_name_lookup():
    parser, scope, local, _vendor = _duplicate_math()
    with contract_id_resolution(threading.RLock()):
        with pytest.raises(VariableNotFound, match="[Cc]ontract"):
            resolver.find_variable("Math", scope, local.id)
    assert parser.contracts_by_id[local.id] is local


@pytest.mark.parametrize("ref", [None, -1])
def test_legacy_or_builtin_reference_uses_original_name_resolution(ref):
    with contract_id_resolution(threading.RLock()):
        _parser, scope, _local, vendor = _duplicate_math()
        assert resolver.find_variable("Math", scope, ref) == (vendor, False)


def test_non_contract_function_id_keeps_virtual_dispatch():
    with contract_id_resolution(threading.RLock()):
        parser, scope, _local, _vendor = _duplicate_math()
        base = _contract(parser, scope, 20, "Base")
        derived = _contract(parser, scope, 30, "Derived")
        base_function = FunctionContract(parser.compilation_unit)
        base_function.name, base_function.id = "f", 21
        base_function.set_contract(base)
        base_function.set_contract_declarer(base)
        derived_function = FunctionContract(parser.compilation_unit)
        derived_function.name, derived_function.id = "f", 31
        derived_function.set_contract(derived)
        derived_function.set_contract_declarer(derived)
        base.set_functions({"f()": base_function})
        derived.set_functions({"f()": derived_function})
        contract_parser = object.__new__(ContractSolc)
        contract_parser._contract = derived
        contract_parser._slither_parser = parser
        contract_parser._functions_parser = []
        contract_parser._modifiers_parser = []
        function_parser = object.__new__(FunctionSolc)
        function_parser._slither_parser = parser
        function_parser._contract_parser = contract_parser
        function_parser._function = derived_function
        function_parser._variables_renamed = {}
        assert resolver.find_variable("f()", function_parser, 21) == (
            derived_function,
            False,
        )


def test_non_contract_and_legacy_arguments_are_delegated_exactly(monkeypatch):
    calls = []
    sentinel = object()

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel, True

    monkeypatch.setattr(resolver, "find_variable", original)
    monkeypatch.setattr(expressions, "find_variable", original)
    with contract_id_resolution(threading.RLock()):
        scope = _scope("Empty.sol")
        _unit(scope)
        assert expressions.find_variable("f()", scope, 11, True, True) == (
            sentinel,
            True,
        )
    assert calls == [(("f()", scope, 11, True, True), {})]


def test_ambiguous_scope_does_not_change_non_contract_delegation(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(resolver, "find_variable", lambda *_args: (sentinel, False))
    with contract_id_resolution(threading.RLock()):
        scope = _scope("Ambiguous.sol")
        _unit(scope)
        _unit(scope)
        assert resolver.find_variable("f()", scope, 123) == (sentinel, False)


def test_aliases_and_constructor_are_restored_after_failure():
    before = (
        resolver.find_variable,
        expressions.find_variable,
        SlitherCompilationUnitSolc.__init__,
        vyper_resolver.find_variable,
    )
    type_modules = [
        types,
        variable_types,
        expressions,
        importlib.import_module("slither.solc_parsing.declarations.contract"),
        importlib.import_module(
            "slither.solc_parsing.declarations.using_for_top_level"
        ),
    ]
    type_before = [module.parse_type for module in type_modules]
    expression_modules = [
        expressions,
        importlib.import_module("slither.solc_parsing.cfg.node"),
        importlib.import_module("slither.solc_parsing.declarations.function"),
        importlib.import_module("slither.solc_parsing.declarations.contract"),
        variable_types,
    ]
    expression_before = [module.parse_expression for module in expression_modules]
    with pytest.raises(RuntimeError, match="construction failed"):
        with contract_id_resolution(threading.RLock()):
            assert resolver.find_variable is not before[0]
            assert expressions.find_variable is resolver.find_variable
            assert SlitherCompilationUnitSolc.__init__ is not before[2]
            assert vyper_resolver.find_variable is before[3]
            assert all(
                module.parse_type is not saved
                for module, saved in zip(type_modules, type_before, strict=True)
            )
            assert all(
                module.parse_expression is not saved
                for module, saved in zip(
                    expression_modules, expression_before, strict=True
                )
            )
            raise RuntimeError("construction failed")
    assert (
        resolver.find_variable,
        expressions.find_variable,
        SlitherCompilationUnitSolc.__init__,
        vyper_resolver.find_variable,
    ) == before
    assert [module.parse_type for module in type_modules] == type_before
    assert [
        module.parse_expression for module in expression_modules
    ] == expression_before


def test_non_contract_member_and_foreign_thread_delegate_original_expression(
    monkeypatch,
):
    sentinel = object()
    seen = []

    def original(node, context):
        seen.append((node, context))
        return sentinel

    monkeypatch.setattr(expressions, "parse_expression", original)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with contract_id_resolution(threading.RLock()):
            parser, _scope, local, _vendor = _duplicate_math()
            node = {"nodeType": "MemberAccess", "referencedDeclaration": 999}
            assert expressions.parse_expression(node, parser) is sentinel
            contract_node = {
                "nodeType": "MemberAccess",
                "referencedDeclaration": local.id,
            }
            assert (
                pool.submit(
                    expressions.parse_expression, contract_node, parser
                ).result()
                is sentinel
            )
    assert seen == [(node, parser), (contract_node, parser)]


def test_nested_context_can_resolve_outer_scope_and_restores_outer_hooks():
    lock = threading.RLock()
    original = resolver.find_variable
    with contract_id_resolution(lock):
        _parser, scope, local, _vendor = _duplicate_math()
        outer = resolver.find_variable
        with contract_id_resolution(lock):
            assert expressions.find_variable("Math", scope, local.id)[0] is local
        assert resolver.find_variable is outer
        assert resolver.find_variable("Math", scope, local.id)[0] is local
    assert resolver.find_variable is original


def test_scope_cache_tracks_scope_growth_and_new_owners():
    with contract_id_resolution(threading.RLock()):
        parser, scope, local, vendor = _duplicate_math()
        assert resolver.find_variable("Math", scope, local.id)[0] is local
        late_scope = _scope("Late.sol")
        late_scope.contracts["Math"] = vendor
        parser.compilation_unit.scopes[late_scope.filename] = late_scope
        assert resolver.find_variable("Math", late_scope, local.id)[0] is local
        _unit(scope)
        with pytest.raises(VariableNotFound, match="[Aa]mbiguous"):
            resolver.find_variable("Math", scope, local.id)


def test_scope_owner_cache_detects_same_count_replacement():
    with contract_id_resolution(threading.RLock()):
        _parser, scope, local, _vendor = _duplicate_math()
        other = _unit(_scope("Other.sol"))
        assert resolver.find_variable("Math", scope, local.id)[0] is local
        other.compilation_unit.scopes = {scope.filename: scope}
        with pytest.raises(VariableNotFound, match="[Aa]mbiguous"):
            resolver.find_variable("Math", scope, local.id)


def test_scope_owner_cache_accepts_new_scope_after_same_count_replacement():
    with contract_id_resolution(threading.RLock()):
        parser, scope, local, vendor = _duplicate_math()
        assert resolver.find_variable("Math", scope, local.id)[0] is local
        replacement = _scope("Replacement.sol")
        replacement.contracts["Math"] = vendor
        parser.compilation_unit.scopes = {replacement.filename: replacement}
        assert resolver.find_variable("Math", replacement, local.id)[0] is local


def test_foreign_thread_delegates_without_registering_its_parser():
    with ThreadPoolExecutor(max_workers=1) as pool:
        with contract_id_resolution(threading.RLock()):
            _parser, scope, local, vendor = _duplicate_math()
            assert pool.submit(
                resolver.find_variable, "Math", scope, local.id
            ).result() == (vendor, False)
            _foreign_parser, foreign_scope, foreign, _vendor = pool.submit(
                _duplicate_math
            ).result()
            with pytest.raises(VariableNotFound, match="[Cc]ontract"):
                resolver.find_variable("Math", foreign_scope, foreign.id)
            assert resolver.find_variable("Math", scope, local.id)[0] is local


def test_concurrent_adapter_contexts_are_serialized_by_parent_lock():
    lock = threading.RLock()
    entered = threading.Event()
    attempting = threading.Event()

    def analyze():
        attempting.set()
        with contract_id_resolution(lock):
            entered.set()
            _parser, scope, local, _vendor = _duplicate_math()
            return resolver.find_variable("Math", scope, local.id)[0] is local

    with ThreadPoolExecutor(max_workers=1) as pool:
        with contract_id_resolution(lock):
            pending = pool.submit(analyze)
            assert attempting.wait(5)
            assert not entered.is_set()
        assert pending.result(timeout=5)
    assert entered.is_set()


def test_real_compiler_import_aliases_contract_types_and_virtual_dispatch(tmp_path):
    if shutil.which("solc") is None:
        pytest.skip("solc is required for this compiler integration fixture")
    (tmp_path / "Local.sol").write_text(
        "pragma solidity ^0.8.0;\n"
        "library Math { function mulDivUp(uint x) internal pure returns(uint) "
        "{ return x + 1; } }\n"
        "contract Counter { function ping() public pure returns(uint) { return 1; } }\n"
    )
    (tmp_path / "Vendor.sol").write_text(
        "pragma solidity ^0.8.0;\n"
        "library Math { function mulDivUp(uint x) internal pure returns(uint) "
        "{ return x + 2; } }\n"
        "contract Counter { function ping() public pure returns(uint) { return 2; } }\n"
    )
    (tmp_path / "Bridge.sol").write_text(
        'pragma solidity ^0.8.0; import "./Vendor.sol";\n'
    )
    main = tmp_path / "Main.sol"
    main.write_text(
        "pragma solidity ^0.8.0; import {Math as LocalMath, Counter as LocalCounter} "
        'from "./Local.sol"; import "./Bridge.sol";\n'
        "contract Base { function f() internal pure virtual returns(uint) "
        "{ return 1; } "
        "function g() public pure returns(uint) { return f(); } }\n"
        "contract Derived is Base { LocalCounter internal target; "
        "function f() internal pure override returns(uint) { return 2; } "
        "function probe(uint x) public pure returns(uint) "
        "{ return LocalMath.mulDivUp(x); } }\n"
    )
    cc = CryticCompile(str(main))
    with contract_id_resolution(threading.RLock()):
        slither = Slither(cc)
    derived = slither.get_contract_from_name("Derived")[0]
    target = derived.get_state_variable_from_name("target")
    assert target.type.type.source_mapping.filename.absolute == str(
        tmp_path / "Local.sol"
    )
    probe = derived.get_function_from_signature("probe(uint256)")
    library_calls = [
        ir
        for node in probe.nodes
        for ir in node.irs
        if type(ir).__name__ == "LibraryCall"
    ]
    assert len(library_calls) == 1
    assert library_calls[0].function.contract.source_mapping.filename.absolute == str(
        tmp_path / "Local.sol"
    )
    virtual = derived.get_function_from_signature("g()")
    calls = [
        ir
        for node in virtual.nodes
        for ir in node.irs
        if type(ir).__name__ == "InternalCall"
    ]
    assert len(calls) == 1
    assert calls[0].function.contract_declarer is derived
    assert not derived.is_incorrectly_constructed


def test_real_compiler_namespace_member_contract_id(tmp_path):
    if shutil.which("solc") is None:
        pytest.skip("solc is required for this compiler integration fixture")
    fixtures = {
        "Local.sol": 'pragma solidity ^0.8.0; import {Bridge} from "./Bridge.sol"; '
        "library Math { function probe() internal pure returns(uint) { return 1; } }",
        "Vendor.sol": "pragma solidity ^0.8.0; library Math { "
        "function probe() internal pure returns(uint) { return 2; } }",
        "Bridge.sol": 'pragma solidity ^0.8.0; import {Math} from "./Vendor.sol"; '
        "contract Bridge {}",
        "Main.sol": 'pragma solidity ^0.8.0; import * as LocalNS from "./Local.sol"; '
        "contract Main { function run() public pure returns(uint) "
        "{ return LocalNS.Math.probe(); } }",
    }
    for name, source in fixtures.items():
        (tmp_path / name).write_text(source + "\n")
    cc = CryticCompile(str(tmp_path / "Main.sol"))
    with contract_id_resolution(threading.RLock()):
        slither = Slither(cc)
    main = slither.get_contract_from_name("Main")[0]
    function = main.get_function_from_signature("run()")
    calls = [
        ir
        for node in function.nodes
        for ir in node.irs
        if type(ir).__name__ == "LibraryCall"
    ]
    assert len(calls) == 1
    assert calls[0].destination.source_mapping.filename.absolute == str(
        tmp_path / "Local.sol"
    )
    assert calls[0].function.contract is calls[0].destination
    source = fixtures["Main.sol"]
    expected_start = source.index("LocalNS.Math.probe")
    assert any(
        reference.filename.absolute == str(tmp_path / "Main.sol")
        and reference.start == expected_start
        and reference.length == len("LocalNS.Math")
        for reference in calls[0].destination.references
    )
    assert not main.is_incorrectly_constructed
