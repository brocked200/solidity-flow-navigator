"""Scoped compatibility fixes for Slither's Solidity declaration resolver."""

import importlib
import threading
from contextlib import contextmanager

from slither.core.declarations.contract import Contract
from slither.core.expressions.identifier import Identifier
from slither.core.scope.scope import FileScope
from slither.core.solidity_types import UserDefinedType
from slither.solc_parsing.exceptions import VariableNotFound
from slither.solc_parsing.slither_compilation_unit_solc import (
    SlitherCompilationUnitSolc,
)


class _UnitOwners:
    """Per-capture scope ownership; compiler IDs are never pooled across units."""

    def __init__(self):
        self.parsers = []
        self._scopes = {}

    def for_scope(self, scope):
        # Check one dictionary entry per unit, not every source scope. A unit
        # can replace a scope without changing its dictionary size; checking
        # live owners also detects a second owner introduced that way.
        live = [
            parser
            for parser in self.parsers
            if parser.compilation_unit.scopes.get(scope.filename) is scope
        ]
        entry = self._scopes.get(id(scope))
        if entry is None or entry[0] is not scope or entry[1] != live:
            # Keeping the scope prevents id() reuse from changing owners.
            entry = (scope, live)
            self._scopes[id(scope)] = entry
        return entry[1]

    def parser_for(self, context):
        """Return a single owner, without inferring ownership from names."""
        parsers = (
            self.for_scope(context)
            if isinstance(context, FileScope)
            else [context.slither_parser]
        )
        if len(parsers) > 1:
            raise VariableNotFound("Ambiguous Contract scope ownership")
        return parsers[0] if parsers else None


def _contract_for_id(parser, reference):
    """Use the complete unit table, with conservative consistency checks."""
    table = parser.contracts_by_id
    if reference not in table:
        return None
    candidate = table[reference]
    if (
        not isinstance(candidate, Contract)
        or candidate.id != reference
        or candidate.compilation_unit is not parser.compilation_unit
        or any(
            reference in getattr(parser, table_name)
            for table_name in (
                "functions_by_id",
                "imports_by_id",
                "top_level_events_by_id",
                "top_level_errors_by_id",
                "top_level_structures_by_id",
                "top_level_variables_by_id",
                "top_level_type_aliases_by_id",
                "top_level_enums_by_id",
            )
        )
        or any(
            other is not candidate and getattr(other, "id", None) == reference
            for other in table.values()
        )
    ):
        raise VariableNotFound(f"Inconsistent Contract declaration ID {reference}")
    return candidate


@contextmanager
def contract_id_resolution(lock):
    """Prefer exact Contract IDs during one serialized Slither construction.

    Slither 0.11.x may overwrite a file's contract-name dictionary while
    propagating transitive imports. Its late ID fallback then sees the same
    incomplete dictionary. Only Contract IDs are repaired here: function IDs
    still need Slither's contextual/virtual dispatch rules.

    ``lock`` must be the extractor's shared reentrant capture lock. All process
    patches, including the imported solc expression alias, are restored on
    every exit. Foreign-thread callers continue through their original APIs.
    """
    resolver = importlib.import_module("slither.solc_parsing.expressions.find_variable")
    expressions = importlib.import_module(
        "slither.solc_parsing.expressions.expression_parsing"
    )
    type_modules = [
        importlib.import_module(f"slither.solc_parsing.{name}")
        for name in (
            "solidity_types.type_parsing",
            "expressions.expression_parsing",
            "declarations.contract",
            "declarations.using_for_top_level",
            "variables.variable_declaration",
        )
    ]
    types = type_modules[0]
    expression_modules = [
        importlib.import_module(f"slither.solc_parsing.{name}")
        for name in (
            "expressions.expression_parsing",
            "cfg.node",
            "declarations.function",
            "declarations.contract",
            "variables.variable_declaration",
        )
    ]
    with lock:
        owner_thread = threading.get_ident()
        original_find = resolver.find_variable
        # A nested extraction can use parsers created before its own entry.
        # Reuse the outer owner's registry rather than creating an empty one.
        if getattr(original_find, "_solflow_contract_id_owner", None) == owner_thread:
            yield original_find._solflow_contract_id_owners
            return
        original_alias = expressions.find_variable
        original_init = SlitherCompilationUnitSolc.__init__
        original_types = [(module, module.parse_type) for module in type_modules]
        original_type = types.parse_type
        original_expressions = [
            (module, module.parse_expression) for module in expression_modules
        ]
        original_expression = expressions.parse_expression
        owners = _UnitOwners()

        def observed_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            if threading.get_ident() == owner_thread:
                owners.parsers.append(self)

        def resolved(
            var_name,
            caller_context,
            referenced_declaration=None,
            is_super=False,
            is_identifier_path=False,
        ):
            if (
                threading.get_ident() != owner_thread
                or type(referenced_declaration) is not int
                or referenced_declaration < 0
            ):
                return original_find(
                    var_name,
                    caller_context,
                    referenced_declaration,
                    is_super,
                    is_identifier_path,
                )
            # All supported solc caller contexts implement slither_parser,
            # including top-level declarations without a contract parser.
            try:
                parser = owners.parser_for(caller_context)
            except VariableNotFound:
                # Scope ambiguity must not change unrelated function/variable
                # behavior. A Contract result still requires verified ownership.
                result = original_find(
                    var_name,
                    caller_context,
                    referenced_declaration,
                    is_super,
                    is_identifier_path,
                )
                if isinstance(result[0], Contract):
                    raise
                return result
            if parser is not None:
                candidate = _contract_for_id(parser, referenced_declaration)
                if candidate is not None:
                    return candidate, False

            result = original_find(
                var_name,
                caller_context,
                referenced_declaration,
                is_super,
                is_identifier_path,
            )
            if isinstance(result[0], Contract):
                # A supplied ID may never be replaced by an unverified name
                # match when the owning unit/map is missing or inconsistent.
                raise VariableNotFound(
                    f"Unverified Contract declaration ID {referenced_declaration}"
                )
            return result

        def resolved_type(node, caller_context):
            # Legacy and synthesized UnknownType inputs carry no authoritative
            # declaration ID. Let Slither handle them and every non-Contract
            # declaration exactly as before.
            if (
                threading.get_ident() != owner_thread
                or not isinstance(node, dict)
                or node.get("nodeType") not in ("UserDefinedTypeName", "IdentifierPath")
            ):
                return original_type(node, caller_context)
            reference = node.get("referencedDeclaration")
            path_reference = node.get("pathNode", {}).get("referencedDeclaration")
            if (
                type(reference) is int
                and type(path_reference) is int
                and reference != path_reference
            ):
                raise VariableNotFound("Inconsistent Contract type path declaration ID")
            if reference is None:
                reference = path_reference
            if type(reference) is not int or reference < 0:
                return original_type(node, caller_context)
            parser = owners.parser_for(caller_context)
            if parser is not None:
                candidate = _contract_for_id(parser, reference)
                if candidate is not None:
                    result = UserDefinedType(candidate)
                    types._add_type_references(
                        result, node["src"], parser.compilation_unit
                    )
                    return result
            result = original_type(node, caller_context)
            if isinstance(result, UserDefinedType) and isinstance(
                result.type, Contract
            ):
                raise VariableNotFound(
                    f"Unverified Contract type declaration ID {reference}"
                )
            return result

        def resolved_expression(node, caller_context):
            if (
                threading.get_ident() != owner_thread
                or not isinstance(node, dict)
                or node.get("nodeType") != "MemberAccess"
                or type(node.get("referencedDeclaration")) is not int
                or node["referencedDeclaration"] < 0
            ):
                return original_expression(node, caller_context)
            # Namespace.Member carries its own exact Contract declaration ID.
            # Retaining a name-only MemberAccess lets SlithIR later select the
            # wrong imported Contract, even when ordinary identifiers are fixed.
            reference = node["referencedDeclaration"]
            parser = owners.parser_for(caller_context)
            if parser is not None:
                candidate = _contract_for_id(parser, reference)
                if candidate is not None:
                    identifier = Identifier(candidate)
                    identifier.set_offset(node["src"], parser.compilation_unit)
                    candidate.references.append(identifier.source_mapping)
                    return identifier
            member_type = node.get("typeDescriptions", {}).get("typeString", "")
            if member_type.startswith(("type(library ", "type(contract ")):
                raise VariableNotFound(
                    f"Unverified Contract member declaration ID {reference}"
                )
            return original_expression(node, caller_context)

        resolved._solflow_contract_id_owner = owner_thread
        resolved._solflow_contract_id_owners = owners
        resolver.find_variable = resolved
        expressions.find_variable = resolved
        SlitherCompilationUnitSolc.__init__ = observed_init
        for module, _original in original_types:
            module.parse_type = resolved_type
        for module, _original in original_expressions:
            module.parse_expression = resolved_expression
        try:
            yield owners
        finally:
            for module, original in original_expressions:
                module.parse_expression = original
            for module, original in original_types:
                module.parse_type = original
            SlitherCompilationUnitSolc.__init__ = original_init
            expressions.find_variable = original_alias
            resolver.find_variable = original_find
