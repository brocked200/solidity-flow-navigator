"""Deterministic, versioned machine export for SolFlow analysis results.

The browser serializer intentionally carries presentation-only fields and
absolute paths.  Neither is suitable for durable audit artifacts.  This
module emits a canonical JSON document whose identity depends on source
bytes, compiler metadata, scope/binding assumptions, and the Layer-2 flows --
never on checkout location, wall-clock time, or Flask state.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path, PureWindowsPath
from typing import Any

from crytic_compile import CryticCompile

from .analysis.types import RepoFacts, SourceLocation
from .flow.scope import Scope
from .flow.types import ExternalNode, Flow, FunctionNode, SinkNode, UnresolvedNode

SCHEMA_VERSION = 1


class ExportError(Exception):
    """Raised when an analysis artifact cannot be exported fail-closed."""


def build_machine_export(
    facts: RepoFacts,
    flows: Iterable[Flow],
    scope: Scope,
    compilation: CryticCompile,
    *,
    tool_version: str,
) -> dict[str, Any]:
    """Return a checkout-location-independent JSON-compatible payload."""

    ordered_flows = tuple(
        sorted(flows, key=lambda f: f.entry_point_invoker_canonical_name)
    )
    identities = [flow.entry_point_invoker_canonical_name for flow in ordered_flows]
    if len(identities) != len(set(identities)):
        raise ExportError("ambiguous Flow identity: duplicate entry-point name")
    aliases, sources = _source_manifest(facts, compilation)
    flow_dicts = [_sanitize(asdict(flow), aliases) for flow in ordered_flows]
    scope_dict = {
        # Gitignore negation is order-sensitive. Canonical JSON orders keys,
        # not configuration sequences: sorting these changes their meaning.
        "exclude_paths": list(scope.exclude_paths),
        "exclude_contracts": list(scope.exclude_contracts),
        "inline_libraries": list(scope.inline_libraries),
        "stub_paths": list(scope.stub_paths),
        "interface_bindings": [
            {"interface": iface, "contract": contract}
            for iface, contract in sorted(dict(scope.interface_bindings).items())
        ],
    }
    for key in ("exclude_paths", "exclude_contracts", "inline_libraries", "stub_paths"):
        if any(
            Path(p).is_absolute() or PureWindowsPath(p).is_absolute()
            for p in scope_dict[key]
        ):
            raise ExportError("scope paths must be relative for a portable export")
    core: dict[str, Any] = {
        "tool": {"name": "solflow", "version": tool_version},
        "sources": sources,
        "source_tree_sha256": _digest_json(sources),
        "compilers": _compiler_manifest(
            compilation, aliases, Path(facts.repo_path).resolve()
        ),
        "scope": scope_dict,
        "scope_sha256": _digest_json(scope_dict),
        "summary": {
            "contracts": len(facts.contracts),
            "free_functions": len(facts.free_functions),
            "type_definitions": len(facts.type_defs),
            "entry_points": len(ordered_flows),
            "unresolved_nodes": sum(f.unresolved_count for f in ordered_flows),
            "value_sink_nodes": sum(f.value_sink_count for f in ordered_flows),
            "dynamic_dispatch_nodes": sum(
                f.dynamic_dispatch_count for f in ordered_flows
            ),
        },
        "binding_assumptions": scope_dict["interface_bindings"],
        "flows": flow_dicts,
        "edges": _flat_edges(ordered_flows, aliases),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "payload_sha256": _digest_json(core),
        **core,
    }


def write_machine_export(payload: dict[str, Any], destination: str | Path) -> str:
    """Write canonical JSON atomically, or to stdout when destination is ``-``.

    Returns the SHA-256 of the exact emitted bytes.
    """

    data = _canonical_bytes(payload) + b"\n"
    digest = hashlib.sha256(data).hexdigest()
    if str(destination) == "-":
        sys.stdout.buffer.write(data)
        return digest

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return digest


def _source_manifest(
    facts: RepoFacts, compilation: CryticCompile
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    repo_root = Path(facts.repo_path).resolve()
    raw_paths: set[Path] = set()
    for loc in _fact_locations(facts):
        raw_paths.add(Path(loc.filename_absolute).resolve())
    for unit in compilation.compilation_units.values():
        for filename in unit.filenames:
            raw_paths.add(Path(filename.absolute).resolve())

    aliases: dict[str, str] = {}
    entries: dict[str, dict[str, Any]] = {}
    cached_sources = {
        str(Path(name).resolve()): text
        for name, text in getattr(compilation, "src_content", {}).items()
    }
    compiler_inputs: dict[str, list[bytes]] = {}
    for unit in compilation.compilation_units.values():
        for name, data in getattr(unit, "_solflow_source_bytes", {}).items():
            compiler_inputs.setdefault(str(Path(name).resolve()), []).append(data)
    for path in sorted(raw_paths, key=lambda p: str(p)):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ExportError(f"cannot read compiled source {path}: {exc}") from exc
        sha = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        cached = cached_sources.get(str(path))
        if cached is not None and cached not in (text, normalized):
            raise ExportError("compiled source changed since analysis")
        for compiler_input in compiler_inputs.get(str(path), ()):
            expected = (
                compiler_input.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            )
            if expected != normalized:
                raise ExportError("compiler input differs from source on disk")
        try:
            alias = path.relative_to(repo_root).as_posix()
        except ValueError:
            alias = f"external/{sha}/{path.name}"
        if alias in entries and entries[alias]["sha256"] != sha:
            raise ExportError(f"ambiguous compiled source alias: {alias}")
        aliases[str(path)] = alias
        entries[alias] = {
            "path": alias,
            "sha256": sha,
            "bytes": len(raw),
            "newline": _newline_style(raw),
        }
    # Stub paths originate from SourceLocation.filename_relative, which can
    # traverse outside the checkout. Resolve those labels using the same
    # compiled-source identity as locations, without retaining host paths.
    for loc in _fact_locations(facts):
        alias = aliases[str(Path(loc.filename_absolute).resolve())]
        previous = aliases.get(loc.filename_relative)
        if previous is not None and previous != alias:
            raise ExportError("ambiguous relative compiled source path")
        aliases[loc.filename_relative] = alias
    for alias in entries:
        aliases.setdefault(alias, alias)
    return aliases, sorted(entries.values(), key=lambda e: e["path"])


def _fact_locations(facts: RepoFacts) -> Iterable[SourceLocation]:
    for contract in facts.contracts:
        yield contract.source_location
        for fn in (*contract.functions, *contract.modifiers):
            yield fn.source_location
            for edge in fn.calls:
                yield edge.source_location
    for fn in facts.free_functions:
        yield fn.source_location
        for edge in fn.calls:
            yield edge.source_location
    for type_def in facts.type_defs:
        yield type_def.source_location


def _newline_style(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    bare_lf = raw.count(b"\n") - crlf
    bare_cr = raw.count(b"\r") - crlf
    if sum(bool(count) for count in (crlf, bare_lf, bare_cr)) > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if bare_lf:
        return "lf"
    if bare_cr:
        return "cr"
    return "none"


def _compiler_manifest(
    compilation: CryticCompile, aliases: dict[str, str], repo_root: Path
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for unit in compilation.compilation_units.values():
        version = unit.compiler_version
        files = sorted(
            {
                _source_alias(str(Path(f.absolute).resolve()), aliases)
                for f in unit.filenames
            }
        )
        settings = getattr(unit, "_solflow_compiler_settings", None)
        entries.append(
            {
                "compiler": version.compiler,
                "version": version.version,
                "long_version": getattr(unit, "_solflow_compiler_long_version", None),
                "optimized": version.optimized,
                "optimize_runs": version.optimize_runs,
                "via_ir": version.via_ir,
                "source_paths": files,
                "settings": _portable_settings(settings, aliases, repo_root),
                "settings_provenance": (
                    "compiler_input" if settings is not None else "unavailable"
                ),
                "source_inputs": sorted(
                    [
                        {
                            "path": _source_alias(str(Path(name).resolve()), aliases),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "bytes": len(data),
                        }
                        for name, data in getattr(
                            unit, "_solflow_source_bytes", {}
                        ).items()
                    ],
                    key=_canonical_bytes,
                ),
                "binary_sha256": getattr(unit, "_solflow_compiler_binary_sha256", None),
                "binary_provenance": getattr(
                    unit, "_solflow_compiler_binary_provenance", "unavailable"
                ),
            }
        )
    entries.sort(key=_canonical_bytes)
    return entries


def _portable_settings(value: Any, aliases: dict[str, str], repo_root: Path) -> Any:
    """Preserve compiler settings while replacing filesystem resource names."""
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            portable_key = _portable_settings(key, aliases, repo_root)
            if portable_key in out:
                raise ExportError("ambiguous portable compiler setting key")
            out[portable_key] = _portable_settings(child, aliases, repo_root)
        return out
    if isinstance(value, (list, tuple)):
        return [_portable_settings(child, aliases, repo_root) for child in value]
    if not isinstance(value, str):
        return value
    prefix, sep, target = value.partition("=")
    if sep and (Path(target).is_absolute() or PureWindowsPath(target).is_absolute()):
        return prefix + sep + _portable_settings(target, aliases, repo_root)
    if not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        return value
    path = Path(value).resolve()
    if str(path) in aliases:
        return aliases[str(path)]
    trailing = "/" if value.endswith(("/", "\\")) else ""
    try:
        return path.relative_to(repo_root).as_posix() + trailing
    except ValueError:
        members = sorted(
            {
                alias
                for name, alias in aliases.items()
                if Path(name).is_absolute() and Path(name).is_relative_to(path)
            }
        )
        if members:
            return f"external/{_digest_json(members)}/".rstrip("/") + trailing
        raise ExportError(
            "compiler setting references an unmapped absolute path"
        ) from None


def _sanitize(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, dict):
        out = {k: _sanitize(v, aliases) for k, v in value.items()}
        if "filename_absolute" in out and "filename_relative" in out:
            absolute = str(Path(out.pop("filename_absolute")).resolve())
            out["filename_relative"] = _source_alias(absolute, aliases)
        if isinstance(out.get("source_path"), str):
            source_path = Path(out["source_path"])
            label = (
                str(source_path.resolve())
                if source_path.is_absolute()
                else str(source_path)
            )
            out["source_path"] = _source_alias(label, aliases)
        return out
    if isinstance(value, tuple | list):
        return [_sanitize(item, aliases) for item in value]
    return value


def _source_alias(absolute: str, aliases: dict[str, str]) -> str:
    try:
        return aliases[absolute]
    except KeyError as exc:
        raise ExportError("source path is absent from the compiled manifest") from exc


def _flat_edges(flows: Iterable[Flow], aliases: dict[str, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for flow in flows:
        _walk_edges(
            flow.entry_point_invoker_canonical_name,
            flow.root,
            (),
            result,
            aliases,
        )
    result.sort(key=lambda e: (e["flow"], e["path"]))
    return result


def _walk_edges(
    flow_name: str,
    parent: FunctionNode,
    path: tuple[int, ...],
    out: list[dict[str, Any]],
    aliases: dict[str, str],
) -> None:
    for index, child in enumerate(parent.children):
        child_path = (*path, index)
        common: dict[str, Any] = {
            "flow": flow_name,
            "path": ".".join(str(i) for i in child_path),
            "from": parent.canonical_name,
            "node_type": child.node_type,
        }
        if isinstance(child, FunctionNode):
            common.update(
                {
                    "resolved": True,
                    "target": child.canonical_name,
                    "call_kind": child.call_kind,
                    "binding_assumption": (
                        asdict(child.bound_via) if child.bound_via else None
                    ),
                    "call_site_line": child.call_site_line,
                    "value_expression": child.call_value,
                }
            )
            out.append(common)
            _walk_edges(flow_name, child, child_path, out, aliases)
        elif isinstance(child, UnresolvedNode):
            common.update(
                {
                    "resolved": False,
                    "reason": child.reason,
                    "descriptor": child.descriptor,
                    "raw_kind": child.raw_kind,
                    "raw_subkind": child.raw_subkind,
                    "value_expression": child.value_expression,
                    "call_site": _sanitize(asdict(child.call_site), aliases),
                }
            )
            out.append(common)
        elif isinstance(child, ExternalNode):
            common.update(
                {
                    "resolved": True,
                    "followed": False,
                    "target": child.target_canonical_name,
                    "source_path": _sanitize(
                        {"source_path": child.source_path}, aliases
                    )["source_path"],
                    "value_expression": child.value_expression,
                    "call_site": _sanitize(asdict(child.call_site), aliases),
                }
            )
            out.append(common)
        elif isinstance(child, SinkNode):
            common.update(
                {
                    "resolved": True,
                    "followed": False,
                    "terminal": True,
                    "sink_kind": child.kind,
                    "descriptor": child.descriptor,
                    "value_expression": child.value_expression,
                    "call_site": _sanitize(asdict(child.call_site), aliases),
                }
            )
            out.append(common)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()
