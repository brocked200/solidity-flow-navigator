"""Nominal declaration IDs must survive duplicate names in actual Slither models."""

import importlib
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from crytic_compile import CryticCompile
from crytic_compile.utils.naming import Filename
from slither import Slither
from slither.core.declarations.contract import Contract
from slither.core.declarations.enum_contract import EnumContract
from slither.core.declarations.enum_top_level import EnumTopLevel
from slither.core.declarations.structure_contract import StructureContract
from slither.core.declarations.structure_top_level import StructureTopLevel
from slither.core.scope.scope import FileScope
from slither.core.solidity_types import (
    ElementaryType,
    TypeAliasContract,
    TypeAliasTopLevel,
)
from slither.core.source_mapping.source_mapping import Source
from slither.core.variables.structure_variable import StructureVariable
from slither.solc_parsing.declarations.contract import ContractSolc
from slither.solc_parsing.exceptions import VariableNotFound
from slither.solc_parsing.slither_compilation_unit_solc import (
    SlitherCompilationUnitSolc,
)

from solidity_flow_navigator.analysis.slither_resolver import contract_id_resolution
from solidity_flow_navigator.analysis.slither_type_ids import type_id_resolution

MODULES = [
    importlib.import_module(f"slither.solc_parsing.{name}")
    for name in (
        "solidity_types.type_parsing",
        "expressions.expression_parsing",
        "declarations.contract",
        "declarations.using_for_top_level",
        "variables.variable_declaration",
    )
]
types = MODULES[0]


@contextmanager
def _capture():
    with contract_id_resolution(threading.RLock()) as owners:
        with type_id_resolution(owners):
            yield


def _source(model, parser, scope, start, length):
    source = Source(parser.compilation_unit)
    source.filename = scope.filename
    source.start = start
    source.length = length
    source.end = start + length
    model.source_mapping = source


def _fixture(kind="enum"):
    scope = FileScope(Filename("Main.sol", "Main.sol", "Main.sol", "Main.sol"))
    unit = SimpleNamespace(
        scopes={scope.filename: scope},
        is_solidity=True,
        contracts=[],
        structures_top_level=[],
        enums_top_level=[],
        type_aliases={},
        source_units={
            index: name
            for index, name in enumerate(("Local.sol", "Vendor.sol", "Main.sol"))
        },
        core=SimpleNamespace(
            crytic_compile=SimpleNamespace(
                filename_lookup=lambda name: Filename(name, name, name, name)
            )
        ),
    )
    parser = SlitherCompilationUnitSolc(unit)
    parser._is_compact_ast = True
    declarations = []
    for index, name in enumerate(("Local.sol", "Vendor.sol", "Main.sol")):
        own_scope = FileScope(Filename(name, name, name, name))
        contract = Contract(unit, own_scope)
        contract.name = "Main" if index == 2 else "Math"
        contract.id = 100 + index
        _source(contract, parser, own_scope, 0, 1000)
        context = object.__new__(ContractSolc)
        context._slither_parser = parser
        context._contract = contract
        context._data = {
            "nodeType": "ContractDefinition",
            "id": contract.id,
            "src": f"0:1000:{index}",
            "nodes": [],
        }
        parser._underlying_contract_to_parser[contract] = context
        parser.contracts_by_id[contract.id] = contract
        unit.contracts.append(contract)
        if index == 2:
            current = context
            continue
        if kind == "enum":
            declaration = EnumContract(
                "Rounding",
                "Math.Rounding",
                ["Up", "Down"] if index == 0 else ["Floor", "Ceil", "Trunc"],
            )
            declaration.set_contract(contract)
            contract.enums_as_dict[declaration.canonical_name] = declaration
            node_kind = "EnumDefinition"
        elif kind == "struct":
            declaration = StructureContract(unit)
            declaration.set_contract(contract)
            declaration.name = "Rounding"
            declaration.canonical_name = "Math.Rounding"
            member = StructureVariable()
            member.name = "amount"
            member.set_type(ElementaryType("uint32" if index == 0 else "uint256"))
            declaration.elems[member.name] = member
            declaration._elems_ordered = [member.name]
            contract.structures_as_dict[declaration.name] = declaration
            node_kind = "StructDefinition"
        else:
            declaration = TypeAliasContract(
                ElementaryType("uint32" if index == 0 else "uint256"),
                "Rounding",
                contract,
            )
            contract.type_aliases_as_dict[declaration.name] = declaration
            scope.type_aliases["Math.Rounding"] = declaration
            node_kind = "UserDefinedValueTypeDefinition"
        _source(declaration, parser, own_scope, 10, 20)
        context._data["nodes"].append(
            {"nodeType": node_kind, "id": 30451 + index, "src": f"10:20:{index}"}
        )
        declarations.append(declaration)
    # Reproduce the transitive-import overwrite. Local.Math's exact-ID enum
    # is absent from the file's name table while both declarations still exist.
    current._contract.file_scope = scope
    scope.contracts["Math"] = unit.contracts[1]
    scope.contracts["Main"] = current.underlying_contract
    node = {
        "nodeType": "UserDefinedTypeName",
        "src": "100:13:2",
        "referencedDeclaration": 30451,
        "typeDescriptions": {
            "typeString": {
                "enum": "enum Math.Rounding",
                "struct": "struct Math.Rounding",
                "alias": "Math.Rounding",
            }[kind]
        },
        "pathNode": {"referencedDeclaration": 30451},
    }
    return parser, current, node, declarations


@pytest.mark.parametrize("kind", ["enum", "struct", "alias"])
@pytest.mark.parametrize("entry", MODULES)
def test_exact_id_selects_real_nominal_model_and_content(monkeypatch, kind, entry):
    with _capture():
        parser, context, node, (local, vendor) = _fixture(kind)
        references = []
        for target in (
            local,
            vendor,
            getattr(local, "underlying_type", local),
            getattr(vendor, "underlying_type", vendor),
        ):
            monkeypatch.setattr(
                target,
                "add_reference_from_raw_source",
                lambda src, unit: references.append((src, unit)),
            )
        result = entry.parse_type(node, context)
        actual = result if kind == "alias" else result.type
        assert actual is local
        assert actual is not vendor
        if kind == "enum":
            assert actual.values == ["Up", "Down"]
        elif kind == "struct":
            assert str(actual.elems["amount"].type) == "uint32"
        else:
            assert str(actual.underlying_type) == "uint32"
        assert references == [(node["src"], parser.compilation_unit)] * (
            2 if kind == "alias" else 1
        )


def _ignore_references(monkeypatch, declarations):
    for target in declarations:
        monkeypatch.setattr(
            target, "add_reference_from_raw_source", lambda *_args: None
        )
        if isinstance(target, (TypeAliasContract, TypeAliasTopLevel)):
            monkeypatch.setattr(
                target.underlying_type,
                "add_reference_from_raw_source",
                lambda *_args: None,
            )


@pytest.mark.parametrize("kind", ["enum", "struct", "alias"])
def test_top_level_ids_use_actual_models(monkeypatch, kind):
    with _capture():
        parser, context, node, (local, vendor) = _fixture(kind)
        owner = parser._underlying_contract_to_parser[local.contract]
        owner._data["nodes"].clear()
        scope = local.contract.file_scope
        if kind == "enum":
            exact = EnumTopLevel("Rounding", "Rounding", ["Up", "Down"], scope)
            table = parser.top_level_enums_by_id
        elif kind == "struct":
            exact = StructureTopLevel(parser.compilation_unit, scope)
            exact.name = "Rounding"
            table = parser.top_level_structures_by_id
        else:
            exact = TypeAliasTopLevel(ElementaryType("uint32"), "Rounding", scope)
            table = parser.top_level_type_aliases_by_id
        _source(exact, parser, scope, 10, 20)
        table[30451] = exact
        _ignore_references(monkeypatch, (exact, vendor))
        result = types.parse_type(node, context)
        assert (result if kind == "alias" else result.type) is exact


@pytest.mark.parametrize("node_kind", ["UserDefinedTypeName", "IdentifierPath"])
@pytest.mark.parametrize("path_only", [False, True])
def test_path_ids_and_identifier_paths(monkeypatch, node_kind, path_only):
    with _capture():
        _parser, context, node, (local, vendor) = _fixture()
        node["nodeType"] = node_kind
        node["name"] = "Math.Rounding"
        if path_only:
            del node["referencedDeclaration"]
        _ignore_references(monkeypatch, (local, vendor))
        assert types.parse_type(node, context).type is local


def test_identifier_path_alias_preserves_no_reference_branch(monkeypatch):
    with _capture():
        _parser, context, node, (local, vendor) = _fixture("alias")
        node["nodeType"] = "IdentifierPath"
        node["name"] = "Math.Rounding"
        calls = []
        monkeypatch.setattr(
            local, "add_reference_from_raw_source", lambda *_args: calls.append("alias")
        )
        monkeypatch.setattr(
            local.type,
            "add_reference_from_raw_source",
            lambda *_args: calls.append("underlying"),
        )
        assert types.parse_type(node, context) is local
        assert calls == []


@pytest.mark.parametrize("outer_kind", ["array", "mapping"])
def test_recursive_array_and_mapping_types(monkeypatch, outer_kind):
    with _capture():
        _parser, context, node, (local, vendor) = _fixture()
        _ignore_references(monkeypatch, (local, vendor))
        if outer_kind == "array":
            outer = {"nodeType": "ArrayTypeName", "baseType": node}
        else:
            outer = {
                "nodeType": "Mapping",
                "keyType": {"nodeType": "ElementaryTypeName", "name": "uint256"},
                "valueType": node,
            }
        result = types.parse_type(outer, context)
        inner = result.type if outer_kind == "array" else result.type_to
        assert inner.type is local


def test_same_ids_are_scoped_to_the_owning_compilation_unit(monkeypatch):
    with _capture():
        _first, first_context, first_node, first = _fixture()
        _second, second_context, second_node, second = _fixture()
        _ignore_references(monkeypatch, (*first, *second))
        assert types.parse_type(first_node, first_context).type is first[0]
        assert types.parse_type(second_node, second_context).type is second[0]
        assert first[0] is not second[0]


def test_index_rebuilds_for_same_count_parser_replacement(monkeypatch):
    with _capture():
        parser, context, node, (local, vendor) = _fixture()
        _ignore_references(monkeypatch, (local, vendor))
        assert types.parse_type(node, context).type is local
        contract = local.contract
        previous = parser._underlying_contract_to_parser[contract]
        replacement = object.__new__(ContractSolc)
        replacement._slither_parser = parser
        replacement._contract = contract
        replacement._data = dict(previous._data, nodes=[])
        parser._underlying_contract_to_parser[contract] = replacement
        with pytest.raises(VariableNotFound, match="[Uu]nverified"):
            types.parse_type(node, context)
        replacement._data["nodes"].append(dict(previous._data["nodes"][0]))
        assert types.parse_type(node, context).type is local


def test_index_rebuilds_for_declaration_growth_and_rejects_stale_node(monkeypatch):
    with _capture():
        parser, context, node, (local, vendor) = _fixture()
        _ignore_references(monkeypatch, (local, vendor))
        assert types.parse_type(node, context).type is local
        owner = parser._underlying_contract_to_parser[local.contract]
        original = owner._data["nodes"][0]
        extra = EnumContract("Extra", "Math.Extra", ["Only"])
        extra.set_contract(local.contract)
        _source(extra, parser, local.contract.file_scope, 40, 20)
        local.contract.enums_as_dict["Extra"] = extra
        _ignore_references(monkeypatch, (extra,))
        owner._data["nodes"].append(
            {"nodeType": "EnumDefinition", "id": 50000, "src": "40:20:0"}
        )
        later = dict(
            node, referencedDeclaration=50000, pathNode={"referencedDeclaration": 50000}
        )
        assert types.parse_type(later, context).type is extra
        # A same-length AST-list mutation must never reuse its stale model.
        owner._data["nodes"][0] = dict(original, id=60000)
        with pytest.raises(VariableNotFound, match="[Ii]nconsistent"):
            types.parse_type(node, context)


@pytest.mark.parametrize("problem", ["unit", "filename", "reused_model"])
def test_top_level_table_contradictions_fail_closed(monkeypatch, problem):
    with _capture():
        parser, context, node, (local, vendor) = _fixture()
        parser._underlying_contract_to_parser[local.contract]._data["nodes"].clear()
        exact = EnumTopLevel(
            "Rounding", "Rounding", ["Up", "Down"], local.contract.file_scope
        )
        _source(exact, parser, local.contract.file_scope, 10, 20)
        parser.top_level_enums_by_id[30451] = exact
        _ignore_references(monkeypatch, (exact, vendor))
        if problem == "unit":
            exact.source_mapping.compilation_unit = object()
        elif problem == "filename":
            exact.source_mapping.filename = vendor.source_mapping.filename
        else:
            parser.top_level_enums_by_id[50000] = exact
        with pytest.raises(VariableNotFound, match="[Ii]nconsistent"):
            types.parse_type(node, context)


@pytest.mark.parametrize(
    "problem",
    [
        "missing_id",
        "path_conflict",
        "duplicate_model",
        "wrong_file",
        "duplicate_ast",
        "cross_kind",
    ],
)
def test_contradictory_or_missing_identity_fails_closed(monkeypatch, problem):
    with _capture():
        parser, context, node, (local, vendor) = _fixture()
        _ignore_references(monkeypatch, (local, vendor))
        if problem == "missing_id":
            node["referencedDeclaration"] = 999999
            node["pathNode"]["referencedDeclaration"] = 999999
        elif problem == "path_conflict":
            node["pathNode"]["referencedDeclaration"] = 30452
        elif problem == "duplicate_model":
            extra = EnumContract("Other", "Math.Other", ["Other"])
            extra.set_contract(local.contract)
            extra.source_mapping = local.source_mapping
            local.contract.enums_as_dict["Other"] = extra
        elif problem == "wrong_file":
            local.source_mapping.filename = vendor.source_mapping.filename
        elif problem == "duplicate_ast":
            owner = parser._underlying_contract_to_parser[local.contract]
            owner._data["nodes"].append(dict(owner._data["nodes"][0]))
        else:
            parser.top_level_enums_by_id[30451] = vendor
        with pytest.raises(
            VariableNotFound, match="[Ii]nconsistent|[Aa]mbiguous|[Uu]nverified"
        ):
            types.parse_type(node, context)


def test_no_id_and_unknown_type_keep_native_name_semantics(monkeypatch):
    with _capture():
        _parser, context, node, (local, vendor) = _fixture()
        _ignore_references(monkeypatch, (local, vendor))
        del node["referencedDeclaration"]
        del node["pathNode"]
        assert types.parse_type(node, context).type is vendor
        assert (
            types.parse_type(types.UnknownType("enum Math.Rounding"), context).type
            is vendor
        )


def test_foreign_thread_delegates_to_the_native_resolver(monkeypatch):
    with _capture():
        _parser, context, node, (local, vendor) = _fixture()
        _ignore_references(monkeypatch, (local, vendor))
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(types.parse_type, node, context).result().type is vendor
        assert types.parse_type(node, context).type is local


def test_nested_context_and_exception_restore_all_five_aliases(monkeypatch):
    originals = [module.parse_type for module in MODULES]
    lock = threading.RLock()
    with pytest.raises(RuntimeError, match="fixture failure"):
        with contract_id_resolution(lock) as owners:
            outer_contract = [module.parse_type for module in MODULES]
            with type_id_resolution(owners):
                outer_type = types.parse_type
                _parser, context, node, declarations = _fixture()
                _ignore_references(monkeypatch, declarations)
                with contract_id_resolution(lock) as nested_owners:
                    with type_id_resolution(nested_owners):
                        assert types.parse_type(node, context).type is declarations[0]
                assert all(module.parse_type is outer_type for module in MODULES)
            assert [module.parse_type for module in MODULES] == outer_contract
            raise RuntimeError("fixture failure")
    assert [module.parse_type for module in MODULES] == originals


def test_real_compiler_collisions_and_recursive_nominal_types(tmp_path):
    if shutil.which("solc") is None:
        pytest.skip("solc is required for this compiler integration fixture")
    for name, values, width in (
        ("Local.sol", "Up, Down", "uint32"),
        ("Vendor.sol", "Floor, Ceil, Trunc", "uint256"),
    ):
        (tmp_path / name).write_text(
            f"pragma solidity ^0.8.8; library Math {{ enum Rounding {{ {values} }} "
            f"struct Box {{ {width} amount; }} type Amount is {width}; }}\n"
        )
    (tmp_path / "Bridge.sol").write_text(
        'pragma solidity ^0.8.8; import "./Vendor.sol";\n'
    )
    main = tmp_path / "Main.sol"
    main.write_text(
        'pragma solidity ^0.8.8; import {Math as LocalMath} from "./Local.sol"; '
        'import "./Bridge.sol";\n'
        "contract Main { LocalMath.Box[] internal boxes; "
        "mapping(uint => LocalMath.Rounding) internal modes; "
        "function(LocalMath.Rounding) internal pure "
        "returns(LocalMath.Amount) internal callback; "
        "function probe(LocalMath.Rounding mode, LocalMath.Box memory box, "
        "LocalMath.Amount amount) public pure returns(uint32) "
        "{ return box.amount + LocalMath.Amount.unwrap(amount) + uint32(mode); } }\n"
    )
    cc = CryticCompile(str(main))
    with _capture():
        slither = Slither(cc)
    contract = slither.get_contract_from_name("Main")[0]
    probe = next(
        function for function in contract.functions if function.name == "probe"
    )
    enum, struct, alias = [parameter.type for parameter in probe.parameters]
    assert enum.type.values == ["Up", "Down"]
    assert str(struct.type.elems["amount"].type) == "uint32"
    assert str(alias.underlying_type) == "uint32"
    for declaration in (enum.type, struct.type, alias):
        assert declaration.source_mapping.filename.absolute == str(
            tmp_path / "Local.sol"
        )
    assert contract.get_state_variable_from_name("boxes").type.type.type is struct.type
    assert contract.get_state_variable_from_name("modes").type.type_to.type is enum.type
    callback = contract.get_state_variable_from_name("callback").type
    assert callback.params[0].type.type is enum.type
    assert callback.return_values[0].type is alias
    assert not contract.is_incorrectly_constructed
