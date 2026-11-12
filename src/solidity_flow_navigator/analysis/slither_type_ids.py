"""Scoped compatibility fixes for nominal Solidity type declaration IDs."""

import importlib
import threading
from contextlib import contextmanager

from slither.core.declarations.enum import Enum
from slither.core.declarations.enum_top_level import EnumTopLevel
from slither.core.declarations.structure import Structure
from slither.core.declarations.structure_top_level import StructureTopLevel
from slither.core.solidity_types import TypeAlias, TypeAliasTopLevel, UserDefinedType
from slither.solc_parsing.exceptions import VariableNotFound

_KINDS = {
    "EnumDefinition": (Enum, "enums"),
    "StructDefinition": (Structure, "structures"),
    "UserDefinedValueTypeDefinition": (TypeAlias, "type_aliases"),
}
_TOP_LEVEL = (
    ("top_level_enums_by_id", EnumTopLevel),
    ("top_level_structures_by_id", StructureTopLevel),
    ("top_level_type_aliases_by_id", TypeAliasTopLevel),
)


def _span(raw):
    try:
        parts = tuple(int(part) for part in raw.split(":"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise VariableNotFound("Invalid nominal type source identity") from exc
    if len(parts) != 3 or any(part < 0 for part in parts):
        raise VariableNotFound("Invalid nominal type source identity")
    return parts


class _TypeDeclarations:
    """Index raw declaration IDs, retaining their owning parser and source."""

    def __init__(self):
        self._indices = {}

    def for_id(self, parser, reference):
        candidates = []
        for name, kind in _TOP_LEVEL:
            table = getattr(parser, name)
            if reference in table:
                candidate = table[reference]
                if (
                    not isinstance(candidate, kind)
                    or candidate.source_mapping.compilation_unit
                    is not parser.compilation_unit
                    or candidate.source_mapping.filename
                    != candidate.file_scope.filename
                    or any(
                        key != reference and value is candidate
                        for key, value in table.items()
                    )
                ):
                    raise VariableNotFound(
                        f"Inconsistent nominal type declaration ID {reference}"
                    )
                candidates.append(candidate)

        contracts = parser._underlying_contract_to_parser
        signature = tuple(
            (
                id(contract),
                id(context),
                id(context._data),
                id(context._data.get("nodes")),
                len(context._data.get("nodes", [])),
            )
            for contract, context in contracts.items()
        )
        cached = self._indices.get(parser)
        if cached is None or cached[0] != signature:
            index = {}
            for contract, context in contracts.items():
                for node in context._data.get("nodes", []):
                    if node.get("nodeType") in _KINDS:
                        index.setdefault(node.get("id"), []).append(
                            (contract, context, node)
                        )
            cached = (signature, index)
            self._indices[parser] = cached
        declarations = cached[1].get(reference, [])
        if len(declarations) > 1:
            raise VariableNotFound(f"Ambiguous nominal type declaration ID {reference}")
        for contract, context, node in declarations:
            if (
                contracts.get(contract) is not context
                or not any(item is node for item in context._data.get("nodes", []))
                or node.get("nodeType") not in _KINDS
            ):
                raise VariableNotFound(
                    f"Inconsistent nominal type declaration ID {reference}"
                )
            kind, collection = _KINDS[node["nodeType"]]
            start, length, file_id = _span(node.get("src"))
            contract_file_id = _span(context._data.get("src"))[2]
            unit = parser.compilation_unit
            source_name = unit.source_units.get(file_id)
            filename = (
                unit.core.crytic_compile.filename_lookup(source_name)
                if source_name is not None
                else None
            )
            if (
                node.get("id") != reference
                or context.underlying_contract is not contract
                or contract.compilation_unit is not parser.compilation_unit
                or file_id != contract_file_id
                or filename != contract.source_mapping.filename
                or parser.contracts_by_id.get(context._data.get("id")) is not contract
            ):
                raise VariableNotFound(
                    f"Inconsistent nominal type declaration ID {reference}"
                )
            matches = [
                candidate
                for candidate in getattr(contract, collection)
                if isinstance(candidate, kind)
                and getattr(candidate, "contract", None) is contract
                and candidate.source_mapping.compilation_unit is parser.compilation_unit
                and candidate.source_mapping.filename
                == contract.source_mapping.filename
                and candidate.source_mapping.start == start
                and candidate.source_mapping.length == length
            ]
            if len(matches) != 1:
                raise VariableNotFound(
                    f"Unverified nominal type declaration ID {reference}"
                )
            candidates.extend(matches)
        if len(candidates) > 1:
            raise VariableNotFound(f"Ambiguous nominal type declaration ID {reference}")
        if candidates and any(
            reference in getattr(parser, name)
            for name in (
                "contracts_by_id",
                "functions_by_id",
                "imports_by_id",
                "top_level_events_by_id",
                "top_level_errors_by_id",
                "top_level_variables_by_id",
            )
        ):
            raise VariableNotFound(f"Ambiguous nominal type declaration ID {reference}")
        return candidates[0] if candidates else None


@contextmanager
def type_id_resolution(owners):
    """Prefer unit-local enum, struct and value-type IDs during one capture.

    Enter inside ``contract_id_resolution(lock)``: its reentrant capture lock
    serializes these five process aliases and its registry owns the units.
    Nested contract declarations do not retain compiler IDs in Slither 0.11.x;
    associate their raw AST IDs with models by kind, owner and exact source
    span. Names never establish declaration identity.
    """
    modules = [
        importlib.import_module(f"slither.solc_parsing.{name}")
        for name in (
            "solidity_types.type_parsing",
            "expressions.expression_parsing",
            "declarations.contract",
            "declarations.using_for_top_level",
            "variables.variable_declaration",
        )
    ]
    original = modules[0].parse_type
    thread = threading.get_ident()
    if (
        getattr(original, "_solflow_type_id_thread", None) == thread
        and getattr(original, "_solflow_type_id_owners", None) is owners
    ):
        yield
        return
    saved = [(module, module.parse_type) for module in modules]
    declarations = _TypeDeclarations()

    def resolved(node, context):
        if (
            threading.get_ident() != thread
            or not isinstance(node, dict)
            or node.get("nodeType") not in ("UserDefinedTypeName", "IdentifierPath")
        ):
            return original(node, context)
        reference = node.get("referencedDeclaration")
        path_reference = node.get("pathNode", {}).get("referencedDeclaration")
        if (
            type(reference) is int
            and type(path_reference) is int
            and reference != path_reference
        ):
            raise VariableNotFound("Inconsistent nominal type path declaration ID")
        if reference is None:
            reference = path_reference
        if type(reference) is not int or reference < 0:
            return original(node, context)
        parser = owners.parser_for(context)
        if parser is not None:
            candidate = declarations.for_id(parser, reference)
            if candidate is not None:
                result = (
                    candidate
                    if isinstance(candidate, TypeAlias)
                    else UserDefinedType(candidate)
                )
                # Slither's IdentifierPath alias branch returns directly;
                # UserDefinedTypeName aliases record both underlying and alias.
                if not (
                    isinstance(candidate, TypeAlias)
                    and node["nodeType"] == "IdentifierPath"
                ):
                    modules[0]._add_type_references(
                        result, node["src"], parser.compilation_unit
                    )
                return result
        result = original(node, context)
        if isinstance(result, TypeAlias) or (
            isinstance(result, UserDefinedType)
            and isinstance(result.type, (Enum, Structure))
        ):
            raise VariableNotFound(
                f"Unverified nominal type declaration ID {reference}"
            )
        return result

    resolved._solflow_type_id_thread = thread
    resolved._solflow_type_id_owners = owners
    for module, _saved in saved:
        module.parse_type = resolved
    try:
        yield
    finally:
        for module, previous in saved:
            module.parse_type = previous
