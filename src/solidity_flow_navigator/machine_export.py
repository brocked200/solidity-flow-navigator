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
import shutil
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
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
    aliases, sources = _source_manifest(facts, compilation)
    flow_dicts = [_sanitize(asdict(flow), aliases) for flow in ordered_flows]
    scope_dict = {
        "exclude_paths": sorted(scope.exclude_paths),
        "exclude_contracts": sorted(scope.exclude_contracts),
        "inline_libraries": sorted(scope.inline_libraries),
        "stub_paths": sorted(scope.stub_paths),
        "interface_bindings": [
            {"interface": iface, "contract": contract}
            for iface, contract in sorted(scope.interface_bindings)
        ],
    }
    core: dict[str, Any] = {
        "tool": {"name": "solflow", "version": tool_version},
        "sources": sources,
        "source_tree_sha256": _digest_json(sources),
        "compilers": _compiler_manifest(compilation, aliases),
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
    entries: list[dict[str, Any]] = []
    for path in sorted(raw_paths, key=lambda p: str(p)):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ExportError(f"cannot read compiled source {path}: {exc}") from exc
        sha = hashlib.sha256(raw).hexdigest()
        try:
            alias = path.relative_to(repo_root).as_posix()
        except ValueError:
            alias = f"external/{sha[:16]}/{path.name}"
        if alias in aliases.values():
            # Same relative name with different absolute sources is ambiguous.
            # Content-address the later path rather than silently colliding.
            alias = f"external/{sha}/{path.name}"
        aliases[str(path)] = alias
        entries.append(
            {
                "path": alias,
                "sha256": sha,
                "bytes": len(raw),
                "newline": _newline_style(raw),
            }
        )
    entries.sort(key=lambda e: e["path"])
    return aliases, entries


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
    if crlf and bare_lf:
        return "mixed"
    if crlf:
        return "crlf"
    if bare_lf:
        return "lf"
    return "none"


def _compiler_manifest(
    compilation: CryticCompile, aliases: dict[str, str]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for unit in compilation.compilation_units.values():
        version = unit.compiler_version
        files = sorted(
            aliases.get(str(Path(f.absolute).resolve()), f"unmapped/{f.short}")
            for f in unit.filenames
        )
        entries.append(
            {
                "compiler": version.compiler,
                "version": version.version,
                "optimized": version.optimized,
                "optimize_runs": version.optimize_runs,
                "via_ir": version.via_ir,
                "source_paths": files,
                "binary_sha256": _compiler_binary_sha256(
                    version.compiler, version.version
                ),
            }
        )
    entries.sort(
        key=lambda e: (
            str(e["compiler"]),
            str(e["version"]),
            _digest_json(e["source_paths"]),
        )
    )
    return entries


def _compiler_binary_sha256(compiler: str, version: str | None) -> str | None:
    candidate: Path | None = None
    if compiler == "solc" and version:
        try:
            from solc_select.constants import ARTIFACTS_DIR

            artifact = ARTIFACTS_DIR / f"solc-{version}" / f"solc-{version}"
            if artifact.is_file():
                candidate = artifact
        except (ImportError, OSError):
            pass
    if candidate is None:
        found = shutil.which(compiler)
        if found:
            candidate = Path(found).resolve()
    if candidate is None or not candidate.is_file():
        return None
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return None


def _sanitize(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, dict):
        out = {k: _sanitize(v, aliases) for k, v in value.items()}
        if "filename_absolute" in out and "filename_relative" in out:
            absolute = str(Path(out.pop("filename_absolute")).resolve())
            out["filename_relative"] = aliases.get(absolute, out["filename_relative"])
        if isinstance(out.get("source_path"), str):
            source_path = Path(out["source_path"])
            if source_path.is_absolute():
                out["source_path"] = aliases.get(
                    str(source_path.resolve()), out["source_path"]
                )
        return out
    if isinstance(value, tuple | list):
        return [_sanitize(item, aliases) for item in value]
    return value


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
                    "source_path": child.source_path,
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
