"""Extracts raw facts from Slither's object model into the RepoFacts dataclass tree.

Layer 1's only interpretive job: mapping SlithIR operation classes to the fixed
``CallEdge.kind`` taxonomy. Everything else is verbatim transcription of what
Slither already computed (inheritance order, source mappings, source text,
binding outcomes).
"""

from pathlib import Path

from crytic_compile import CryticCompile
from slither import Slither
from slither.core.declarations import Contract as SlitherContract
from slither.core.declarations import Enum as SlitherEnum
from slither.core.declarations import Function as SlitherFunction
from slither.core.declarations import FunctionContract
from slither.core.declarations import Modifier as SlitherModifier
from slither.core.declarations import Structure as SlitherStructure
from slither.core.declarations.solidity_variables import SolidityVariable
from slither.core.expressions.identifier import Identifier
from slither.core.expressions.member_access import MemberAccess
from slither.core.expressions.super_call_expression import SuperCallExpression
from slither.core.solidity_types import (
    ArrayType,
    MappingType,
    TypeAlias,
    UserDefinedType,
)
from slither.core.variables.state_variable import StateVariable
from slither.slithir.operations import (
    HighLevelCall,
    InternalCall,
    InternalDynamicCall,
    LibraryCall,
    LowLevelCall,
    NewContract,
    Operation,
    Send,
    SolidityCall,
    Transfer,
)

from .types import (
    CallEdge,
    Contract,
    Function,
    Parameter,
    RepoFacts,
    SourceLocation,
    TypeDef,
)


def extract_facts(
    crytic_compile_obj: CryticCompile, repo_path: Path | str
) -> RepoFacts:
    """Run Slither over the compiled artifacts and return the raw facts tree.

    ``repo_path`` is resolved to an absolute path, recorded on the result, and
    used as the base for every ``SourceLocation.filename_relative`` so all
    relative paths in the output are repo-rooted (not CWD-relative).
    """

    repo_root = Path(repo_path).resolve()
    slither = Slither(crytic_compile_obj)
    # This cache belongs to one analysis, rather than a process-global path.
    # Preserve the exact newlines retained by the compilation adapter: source
    # mappings are byte offsets into that input, not normalized checkout text.
    slither._solflow_source_bytes = {
        path: text.encode("utf-8")
        for path, text in getattr(slither, "source_code", {}).items()
    }
    # Multi-unit builds (Hardhat hands Slither one compilation unit per source
    # file) repeat a contract in ``slither.contracts`` once per unit that
    # includes it, and every duplicate would produce an identical Flow per
    # entry point downstream. Keep the first occurrence per uid; keying by uid
    # (not bare name, §11.5) keeps same-name contracts in different files
    # distinct, mirroring the free-function and type-def de-dup below.
    unique_contracts: dict[tuple[str, str], SlitherContract] = {}
    for c in slither.contracts:
        unique_contracts.setdefault(_contract_uid(c, repo_root), c)
    contracts = tuple(_build_contract(c, repo_root) for c in unique_contracts.values())
    free_functions = _build_free_functions(slither, repo_root)
    type_defs = _build_type_defs(slither, repo_root)
    return RepoFacts(
        repo_path=str(repo_root),
        contracts=contracts,
        free_functions=free_functions,
        type_defs=type_defs,
    )


# ---------------------------------------------------------------------------
# Contract / function / modifier construction
# ---------------------------------------------------------------------------


def _contract_uid(c: SlitherContract, repo_root: Path) -> tuple[str, str]:
    """Stable identity for a Slither contract: ``(filename_relative, name)``.

    Mirrors ``Contract.uid`` (spec §11.5) so the uid computed here for a base
    contract matches that base's own record, letting Layer 2 resolve a
    linearization chain by identity rather than by bare (possibly colliding)
    name.
    """
    return (_source_location(c.source_mapping, repo_root).filename_relative, c.name)


def _build_contract(c: SlitherContract, repo_root: Path) -> Contract:
    declared = tuple(
        _build_function(f, repo_root, is_modifier=False) for f in c.functions_declared
    )
    # Public state variables auto-generate getter functions in Solidity. Slither
    # resolves cross-contract calls to those getters but doesn't surface them in
    # ``functions_declared``; we synthesize Function records so they're indexable
    # by canonical_name like any other entry point.
    getters = tuple(
        _build_synthetic_getter(v, c, repo_root)
        for v in c.state_variables_declared
        if v.visibility == "public"
    )
    functions = declared + getters
    modifiers = tuple(
        _build_function(m, repo_root, is_modifier=True) for m in c.modifiers_declared
    )
    return Contract(
        name=c.name,
        kind=_contract_kind(c),
        is_interface=c.is_interface,
        is_library=c.is_library,
        is_abstract=bool(getattr(c, "is_abstract", False)),
        linearized_base_contract_names=tuple(b.name for b in c.inheritance),
        linearized_base_contract_uids=tuple(
            _contract_uid(b, repo_root) for b in c.inheritance
        ),
        immediate_base_contract_names=tuple(b.name for b in c.immediate_inheritance),
        source_location=_source_location(c.source_mapping, repo_root),
        functions=functions,
        modifiers=modifiers,
    )


def _contract_kind(c: SlitherContract) -> str:
    # Slither sets contract_kind to "contract" / "interface" / "library" (it can be
    # None on legacy AST output, but Solidity 0.8.x always sets it). We collapse
    # the None case to "contract" since is_interface / is_library are independent
    # signals and is_abstract carries the abstract flag.
    return c.contract_kind or "contract"


def _build_function(
    f: SlitherFunction, repo_root: Path, *, is_modifier: bool
) -> Function:
    # Modifiers are FunctionContract subclasses but their .visibility is a
    # placeholder ("internal"). Keep the raw value either way.
    declarer = (
        f.contract_declarer.name
        if isinstance(f, FunctionContract) and f.contract_declarer is not None
        else ""
    )
    parameters = tuple(_parameter(p) for p in f.parameters)
    returns = tuple(_parameter(p) for p in f.returns)
    modifier_names = tuple(m.name for m in f.modifiers)
    # Struct/enum types named in the signature (params then returns), resolved
    # from the Slither type objects (not the stringified type_str, which can't
    # be matched robustly through arrays/mappings/qualified names). Order-
    # preserving, de-duplicated. Resolved to TypeDefs by Layer 2 (spec §10.2).
    sig_types: list[str] = []
    for p in list(f.parameters) + list(f.returns):
        _user_types_in(getattr(p, "type", None), sig_types)
    signature_type_canonical_names = tuple(dict.fromkeys(sig_types))
    # Walk this function's OWN CFG nodes only. ``all_slithir_operations()``
    # would recursively flatten ops from every reachable callee into this
    # function's edge list, including ops inside library / assembly blocks
    # of those callees. That defeats the purpose of having one CallEdge per
    # actual outgoing call site in the function's body.
    calls = tuple(
        edge
        for node in f.nodes
        for edge in (_call_edge(op, repo_root) for op in node.irs)
        if edge is not None
    )
    return Function(
        canonical_name=f.canonical_name,
        name=f.name,
        full_name=f.full_name,
        contract_declarer_name=declarer,
        visibility=f.visibility,
        is_constructor=bool(getattr(f, "is_constructor", False)),
        is_fallback=bool(getattr(f, "is_fallback", False)),
        is_receive=bool(getattr(f, "is_receive", False)),
        is_modifier=is_modifier,
        is_implemented=bool(f.is_implemented),
        is_virtual=bool(getattr(f, "is_virtual", False)),
        is_entry_point=_is_entry_point(f, is_modifier=is_modifier),
        payable=bool(getattr(f, "payable", False)),
        view=bool(getattr(f, "view", False)),
        pure=bool(getattr(f, "pure", False)),
        parameters=parameters,
        returns=returns,
        modifier_names=modifier_names,
        source_location=_source_location(f.source_mapping, repo_root),
        source_code=_source_content(f.source_mapping),
        calls=calls,
        signature_type_canonical_names=signature_type_canonical_names,
    )


def _parameter(p) -> Parameter:
    return Parameter(
        name=p.name or "", type_str=str(p.type) if p.type is not None else ""
    )


def _is_entry_point(f: SlitherFunction, *, is_modifier: bool) -> bool:
    if is_modifier:
        return False
    if getattr(f, "is_constructor", False):
        return False
    if not isinstance(f, FunctionContract):
        # Free functions / top-level functions are never entry points in the
        # per-contract Flow model.
        return False
    declarer = f.contract_declarer
    if declarer is None or declarer.is_interface or declarer.is_library:
        return False
    if getattr(f, "is_receive", False) or getattr(f, "is_fallback", False):
        return True
    return f.visibility in ("public", "external")


# ---------------------------------------------------------------------------
# Call-edge construction
# ---------------------------------------------------------------------------


def _call_edge(op: Operation, repo_root: Path) -> CallEdge | None:
    """Translate a SlithIR operation into a CallEdge, or None for non-call ops."""

    # LibraryCall is a subclass of HighLevelCall in Slither - check it first.
    if isinstance(op, LibraryCall):
        return _high_or_library_edge(op, repo_root, kind="library")
    if isinstance(op, HighLevelCall):
        return _high_or_library_edge(op, repo_root, kind="high_level")
    if isinstance(op, InternalCall):
        return _internal_edge(op, repo_root)
    if isinstance(op, InternalDynamicCall):
        return CallEdge(
            kind="internal_dynamic",
            subkind=str(op.function_type),
            target_canonical_name=None,
            target_function_name=op.function.name,
            target_contract_name=None,
            is_resolved=False,
            source_location=_op_location(op, repo_root),
            value_expression=_value_expression(op),
            is_super_call=False,
            is_self_call=False,
            is_static_call=False,
        )
    if isinstance(op, LowLevelCall):
        return CallEdge(
            kind="low_level",
            subkind=str(op.function_name) if op.function_name is not None else None,
            target_canonical_name=None,
            target_function_name=None,
            target_contract_name=None,
            is_resolved=False,
            source_location=_op_location(op, repo_root),
            value_expression=_value_expression(op),
            is_super_call=False,
            is_self_call=False,
            is_static_call=False,
        )
    if isinstance(op, SolidityCall):
        sf_name = op.function.name if op.function is not None else None
        return CallEdge(
            kind="solidity",
            subkind=sf_name,
            target_canonical_name=None,
            target_function_name=sf_name,
            target_contract_name=None,
            is_resolved=False,
            source_location=_op_location(op, repo_root),
            value_expression=_value_expression(op),
            is_super_call=False,
            is_self_call=False,
            is_static_call=False,
        )
    if isinstance(op, Send):
        return CallEdge(
            kind="send",
            subkind=None,
            target_canonical_name=None,
            target_function_name=None,
            target_contract_name=None,
            is_resolved=False,
            source_location=_op_location(op, repo_root),
            value_expression=_value_expression(op),
            is_super_call=False,
            is_self_call=False,
            is_static_call=False,
        )
    if isinstance(op, Transfer):
        return CallEdge(
            kind="transfer",
            subkind=None,
            target_canonical_name=None,
            target_function_name=None,
            target_contract_name=None,
            is_resolved=False,
            source_location=_op_location(op, repo_root),
            value_expression=_value_expression(op),
            is_super_call=False,
            is_self_call=False,
            is_static_call=False,
        )
    if isinstance(op, NewContract):
        # NewContract.contract_name is a Slither UserDefinedType, not a Python
        # str. str() on it returns the contract type's name (e.g. "MockAuth").
        raw = getattr(op, "contract_name", None)
        return CallEdge(
            kind="new_contract",
            subkind=None,
            target_canonical_name=None,
            target_function_name=None,
            target_contract_name=str(raw) if raw is not None else None,
            is_resolved=False,
            source_location=_op_location(op, repo_root),
            value_expression=_value_expression(op),
            is_super_call=False,
            is_self_call=False,
            is_static_call=False,
        )
    return None


def _internal_edge(op: InternalCall, repo_root: Path) -> CallEdge:
    target_fn = op.function
    target_canonical = target_fn.canonical_name if target_fn is not None else None
    target_name = (
        target_fn.name
        if target_fn is not None
        else (str(op.function_name) if op.function_name is not None else None)
    )
    target_contract = (
        target_fn.contract.name
        if isinstance(target_fn, FunctionContract) and target_fn.contract is not None
        else None
    )
    called = getattr(op.expression, "called", None)
    is_static = (
        isinstance(called, MemberAccess)
        and isinstance(called.expression, Identifier)
        and isinstance(called.expression.value, SlitherContract)
    )
    return CallEdge(
        kind="internal",
        subkind=None,
        target_canonical_name=target_canonical,
        target_function_name=target_name,
        target_contract_name=target_contract,
        is_resolved=target_fn is not None,
        source_location=_op_location(op, repo_root),
        is_super_call=isinstance(op.expression, SuperCallExpression),
        is_self_call=False,
        is_static_call=is_static,
        target_contract_uid=_target_contract_uid(target_fn, repo_root),
    )


def _high_or_library_edge(op: HighLevelCall, repo_root: Path, *, kind: str) -> CallEdge:
    target_fn = op.function
    if isinstance(target_fn, StateVariable):
        # Slither models a high_level call to a public state variable as a call
        # to the variable itself; ``StateVariable.canonical_name`` is
        # ``"Contract.varname"`` (no parentheses). Layer 2 looks targets up by
        # the with-signature canonical_name of the synthetic getter we emit on
        # the same contract; build that form here so the two sides match.
        target_canonical = f"{target_fn.contract.name}.{target_fn.full_name}"
        target_name = target_fn.name
        target_contract = target_fn.contract.name
    else:
        target_canonical = target_fn.canonical_name if target_fn is not None else None
        target_name = (
            target_fn.name
            if target_fn is not None
            else (str(op.function_name) if op.function_name is not None else None)
        )
        target_contract = (
            target_fn.contract.name
            if isinstance(target_fn, FunctionContract)
            and target_fn.contract is not None
            else None
        )
    return CallEdge(
        kind=kind,
        subkind=None,
        target_canonical_name=target_canonical,
        target_function_name=target_name,
        target_contract_name=target_contract,
        is_resolved=target_fn is not None,
        source_location=_op_location(op, repo_root),
        value_expression=_value_expression(op),
        is_super_call=False,
        is_static_call=False,
        is_self_call=(
            kind == "high_level"
            and isinstance(op.destination, SolidityVariable)
            and op.destination.name == "this"
        ),
        target_contract_uid=_target_contract_uid(target_fn, repo_root),
        static_receiver_uid=_static_receiver_uid(op, repo_root),
    )


def _target_contract_uid(target, repo_root: Path) -> tuple[str, str] | None:
    if isinstance(target, FunctionContract):
        return _contract_uid(target.contract_declarer, repo_root)
    if isinstance(target, StateVariable):
        return _contract_uid(target.contract, repo_root)
    return None


def _static_receiver_uid(op, repo_root: Path) -> tuple[str, str] | None:
    receiver_type = getattr(getattr(op, "destination", None), "type", None)
    if isinstance(receiver_type, UserDefinedType) and isinstance(
        receiver_type.type, SlitherContract
    ):
        return _contract_uid(receiver_type.type, repo_root)
    return None


def _value_expression(op: Operation) -> str | None:
    """Return Slither's native-value expression for a call, if present.

    High/low-level calls, send/transfer and contract creation all expose a
    ``call_value`` property in SlithIR.  We deliberately retain only its
    stable textual form: this layer records facts and must not guess whether
    a symbolic expression is zero or how much value reaches the runtime
    target.
    """

    value = getattr(op, "call_value", None)
    if value is not None:
        return str(value)
    if isinstance(op, InternalDynamicCall):
        # Slither may omit call options on the lowered function-pointer IR,
        # although the originating parsed call expression still retains them.
        value = getattr(op.expression, "call_value", None)
        if value is not None:
            return str(value)
    # Yul call/callcode carry native value as their third positional argument;
    # delegatecall/staticcall have no native-value argument. Preserve the IR
    # expression exactly, including a literal zero or symbolic temporary.
    if isinstance(op, SolidityCall):
        name = op.function.name if op.function is not None else ""
        if name.startswith(("call(", "callcode(")) and len(op.arguments) >= 3:
            return str(op.arguments[2])
    return None


def _build_synthetic_getter(
    var: StateVariable, contract: SlitherContract, repo_root: Path
) -> Function:
    """Build a Function record for a public state variable's auto-getter.

    Mirrors what the Solidity compiler synthesizes: a public, view function
    whose ``full_name`` encodes the getter signature derived from the
    variable's type (e.g. ``balanceOf(address)`` for
    ``mapping(address => uint256) public balanceOf``). Slither populates
    ``StateVariable.full_name`` with this signature already.

    Parameters and returns are left empty in v0 — Layer 2 only uses the
    canonical_name for lookup and source_code/source_location for display.
    Reconstructing structured Parameter records from the variable's type
    isn't needed for the v0 use case.
    """

    declarer_name = contract.name
    is_ep = (not contract.is_interface) and (not contract.is_library)
    return Function(
        canonical_name=f"{declarer_name}.{var.full_name}",
        name=var.name,
        full_name=var.full_name,
        contract_declarer_name=declarer_name,
        visibility="public",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=is_ep,
        payable=False,
        view=True,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_source_location(var.source_mapping, repo_root),
        source_code=_source_content(var.source_mapping),
        calls=(),
    )


def _build_free_functions(slither: Slither, repo_root: Path) -> tuple[Function, ...]:
    """Extract Solidity top-level (file-scope) free functions.

    Free functions live outside any contract; Slither places them under
    ``CompilationUnit.functions_top_level``. They share the regular
    ``Function`` shape — ``contract_declarer_name`` is the empty string,
    ``is_entry_point`` is False, modifiers/visibility quirks are absent.

    Dedupes across compilation units by source file and canonical name;
    Slither typically reports each free function once, but multiple
    compilation units can report overlapping declarations after some
    crytic-compile configurations.
    """

    seen: set[tuple[str, str]] = set()
    out: list[Function] = []
    for cu in slither.compilation_units:
        for f in cu.functions_top_level:
            key = (
                _source_location(f.source_mapping, repo_root).filename_relative,
                f.canonical_name,
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(_build_function(f, repo_root, is_modifier=False))
    return tuple(out)


# ---------------------------------------------------------------------------
# Struct / enum extraction (spec §10.2 signature-type panel)
# ---------------------------------------------------------------------------


def _user_type_key(decl) -> str:
    """Stable registry key for a user-defined type declaration or type object.

    Prefers ``canonical_name`` when set (structs/enums always have one; a
    file-level value type's equals its bare name); falls back to ``name``. The
    fallback matters for value types: at a *use* site Slither's ``TypeAlias``
    reports ``canonical_name`` as ``None`` while the *declaration* reports the
    name, so keying both sides through this helper makes them match.
    """

    return getattr(decl, "canonical_name", None) or getattr(decl, "name", "")


def _user_types_in(t, out: list[str]) -> None:
    """Append the registry keys of every user-defined type reachable in type ``t``.

    Unwraps the type wrappers so a type is detected however it is used:
    ``MyStruct`` directly, ``MyStruct[]`` / ``MyStruct[3][]`` (ArrayType),
    ``mapping(K => V)`` (MappingType — both key and value, since an enum/value
    type is a legal mapping key). Collected kinds: ``Structure`` and ``Enum``
    (via ``UserDefinedType``) and ``TypeAlias`` (a user-defined value type such
    as ``type Currency is address``, which Slither models as the type itself,
    not wrapped). Everything else (elementary/function types, contract types)
    contributes nothing. Mutates ``out`` in place so callers accumulate across a
    whole parameter/return list and de-duplicate once.
    """

    if t is None:
        return
    if isinstance(t, ArrayType):
        _user_types_in(t.type, out)
        return
    if isinstance(t, MappingType):
        _user_types_in(t.type_from, out)
        _user_types_in(t.type_to, out)
        return
    if isinstance(t, TypeAlias):
        out.append(_user_type_key(t))
        return
    if isinstance(t, UserDefinedType):
        underlying = t.type
        if isinstance(underlying, (SlitherStructure, SlitherEnum)):
            out.append(_user_type_key(underlying))


def _build_type_defs(slither: Slither, repo_root: Path) -> tuple[TypeDef, ...]:
    """Build the repo-wide user-defined-type registry (spec §10.2).

    Enumerates structs, enums, and user-defined value types declared on every
    contract / interface / library plus the file-level (top-level) declarations
    Slither hangs off each compilation unit. Dedupes by registry key (Slither's
    globally-unique canonical name), so a type seen via multiple contracts or
    compilation units lands once.
    """

    seen: dict[str, TypeDef] = {}
    for c in slither.contracts:
        for s in c.structures_declared:
            _record_type_def(_struct_def(s, repo_root), seen)
        for e in c.enums_declared:
            _record_type_def(_enum_def(e, repo_root), seen)
        for a in _iter_decls(getattr(c, "type_aliases_declared", ())):
            _record_type_def(_udvt_def(a, repo_root), seen)
    for cu in slither.compilation_units:
        for s in cu.structures_top_level:
            _record_type_def(_struct_def(s, repo_root), seen)
        for e in cu.enums_top_level:
            _record_type_def(_enum_def(e, repo_root), seen)
        for a in _iter_decls(getattr(cu, "type_aliases", ())):
            _record_type_def(_udvt_def(a, repo_root), seen)
    return tuple(seen.values())


def _iter_decls(decls):
    """Yield declarations from a Slither collection that may be a dict or list."""
    return list(decls.values()) if isinstance(decls, dict) else list(decls or ())


def _record_type_def(td: TypeDef, seen: dict[str, TypeDef]) -> None:
    seen.setdefault(td.canonical_name, td)


def _struct_def(s: SlitherStructure, repo_root: Path) -> TypeDef:
    members: list[str] = []
    for var in s.elems_ordered:
        _user_types_in(getattr(var, "type", None), members)
    return TypeDef(
        kind="struct",
        canonical_name=_user_type_key(s),
        name=s.name,
        source_location=_source_location(s.source_mapping, repo_root),
        source_code=_source_content(s.source_mapping),
        member_type_canonical_names=tuple(dict.fromkeys(members)),
    )


def _enum_def(e: SlitherEnum, repo_root: Path) -> TypeDef:
    return TypeDef(
        kind="enum",
        canonical_name=_user_type_key(e),
        name=e.name,
        source_location=_source_location(e.source_mapping, repo_root),
        source_code=_source_content(e.source_mapping),
    )


def _udvt_def(a: TypeAlias, repo_root: Path) -> TypeDef:
    return TypeDef(
        kind="udvt",
        canonical_name=_user_type_key(a),
        name=a.name,
        source_location=_source_location(a.source_mapping, repo_root),
        source_code=_source_content(a.source_mapping),
    )


# ---------------------------------------------------------------------------
# Source mapping helpers
# ---------------------------------------------------------------------------


def _compiler_source_bytes(sm) -> bytes | None:
    """Return this mapping's compilation input, never a fresh checkout read.

    Build-info adapters can retain different inputs for the same filename in
    distinct compilation units. Prefer that unit's captured compiler input;
    otherwise use Slither's source snapshot, preserving its exact bytes. The
    cache is scoped to the Slither instance and cannot survive a later build.
    """
    unit = getattr(sm, "compilation_unit", None)
    filename = str(sm.filename.absolute)
    crytic_unit = getattr(unit, "crytic_compile_compilation_unit", None)
    captured = getattr(crytic_unit, "_solflow_source_bytes", {})
    if filename in captured:
        return captured[filename]
    core = getattr(unit, "core", None)
    cached = getattr(core, "_solflow_source_bytes", {})
    if filename in cached:
        return cached[filename]
    text = getattr(core, "source_code", {}).get(filename)
    return text.encode("utf-8") if isinstance(text, str) else None


def _compiler_source_slice(sm) -> bytes | None:
    """Return the compiler-input byte slice addressed by ``sm``.

    Sources unavailable from the compilation snapshot keep using Slither's
    ``content`` field; no disk read or newline-conversion guess is made.
    """

    try:
        source = _compiler_source_bytes(sm)
        start = int(sm.start)
        length = int(sm.length)
    except (TypeError, ValueError):
        return None

    if source is None:
        return None
    end = start + length
    if start < 0 or length < 0 or end > len(source):
        return None
    return source[start:end]


def _source_content(sm) -> str:
    """Decode a mapping from the exact compilation input's UTF-8 bytes."""

    fragment = _compiler_source_slice(sm)
    if fragment is not None:
        try:
            return fragment.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return sm.content or ""


def _compiler_source_lines(sm) -> tuple[int, ...] | None:
    """Compute the 1-indexed lines covered by exact compiler bytes.

    Handle mixed LF/CRLF input without changing the mapping's byte offsets.
    The last byte of a non-empty mapping is used because ``length`` is
    end-exclusive; a trailing newline therefore remains attributed to the
    declaration line rather than inventing an empty following line.
    """

    try:
        source = _compiler_source_bytes(sm)
        start = int(sm.start)
        length = int(sm.length)
    except (TypeError, ValueError):
        return None

    if source is None:
        return None
    end = start + length
    if start < 0 or length < 0 or end > len(source):
        return None

    first_line = source.count(b"\n", 0, start) + 1
    if length == 0:
        return (first_line,)
    last_line = source.count(b"\n", 0, end - 1) + 1
    return tuple(range(first_line, last_line + 1))


def _source_location(sm, repo_root: Path) -> SourceLocation:
    abs_path = Path(str(sm.filename.absolute))
    try:
        rel = str(abs_path.relative_to(repo_root))
    except ValueError:
        # File is outside the analyzed repo root (e.g. a system-wide installed
        # dependency referenced by absolute path). Fall back to the absolute
        # path so the field remains a usable filesystem reference.
        rel = str(abs_path)
    columns = _compiler_source_columns(sm)
    return SourceLocation(
        filename_absolute=str(abs_path),
        filename_relative=rel,
        start=sm.start,
        length=sm.length,
        lines=_compiler_source_lines(sm) or tuple(sm.lines),
        starting_column=columns[0] if columns is not None else sm.starting_column,
        ending_column=columns[1] if columns is not None else sm.ending_column,
    )


def _compiler_source_columns(sm) -> tuple[int, int] | None:
    """Return 1-indexed byte columns from the same snapshot as the source."""
    try:
        source = _compiler_source_bytes(sm)
        start = int(sm.start)
        length = int(sm.length)
    except (TypeError, ValueError):
        return None
    if source is None or start < 0 or length < 0 or start + length > len(source):
        return None
    end = start + length
    return (
        start - source.rfind(b"\n", 0, start),
        end - source.rfind(b"\n", 0, end),
    )


def _op_location(op: Operation, repo_root: Path) -> SourceLocation:
    # SlithIR operations carry their location via the parent CFG node. For
    # multi-call statements the same node-level location is reused for each
    # contained call; that's an acceptable v0 fidelity loss.
    return _source_location(op.node.source_mapping, repo_root)


# Make linter happy about unused symbols imported only for isinstance checks.
_ = SlitherModifier
