"""Determinism and schema tests for the machine-readable export."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from solidity_flow_navigator.analysis.types import (
    Contract,
    Function,
    RepoFacts,
    SourceLocation,
)
from solidity_flow_navigator.flow.scope import Scope
from solidity_flow_navigator.flow.types import (
    Binding,
    ExternalNode,
    Flow,
    FunctionNode,
    SinkKind,
    SinkNode,
    UnresolvedNode,
    UnresolvedReason,
)
from solidity_flow_navigator.machine_export import (
    ExportError,
    build_machine_export,
    write_machine_export,
)


def _analysis(root: Path):
    source = root / "src" / "A.sol"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"contract A {\r\n function f() external {}\r\n}\r\n")
    loc = SourceLocation(
        filename_absolute=str(source),
        filename_relative="src/A.sol",
        start=0,
        length=source.stat().st_size,
        lines=(1, 2, 3),
        starting_column=1,
        ending_column=2,
    )
    fn = Function(
        canonical_name="A.f()",
        name="f",
        full_name="f()",
        contract_declarer_name="A",
        visibility="external",
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=True,
        is_virtual=False,
        is_entry_point=True,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=loc,
        source_code="function f() external {}",
        calls=(),
    )
    contract = Contract(
        name="A",
        kind="contract",
        is_interface=False,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=(),
        immediate_base_contract_names=(),
        source_location=loc,
        functions=(fn,),
        modifiers=(),
    )
    bound = FunctionNode(
        canonical_name="Impl.run()",
        name="run",
        full_name="run()",
        declarer_contract_name="Impl",
        invoked_via_contract_name="Impl",
        invoked_via_super=False,
        is_modifier=False,
        visibility="external",
        payable=False,
        view=False,
        pure=False,
        source_code="function run() external {}",
        source_location=loc,
        builtins_used=(),
        children=(),
        call_site_line=2,
        call_kind="external",
        bound_via=Binding("IRunner", "Impl"),
    )
    unresolved = UnresolvedNode(
        reason=UnresolvedReason.LOW_LEVEL_CALL,
        descriptor="call()",
        call_site=loc,
        raw_kind="low_level",
        raw_subkind="call",
        value_expression="amount",
    )
    sink = SinkNode(
        kind=SinkKind.TRANSFER,
        descriptor="transfer(recipient)",
        call_site=loc,
        value_expression="payout",
    )
    root_node = FunctionNode(
        canonical_name="A.f()",
        name="f",
        full_name="f()",
        declarer_contract_name="A",
        invoked_via_contract_name="A",
        invoked_via_super=False,
        is_modifier=False,
        visibility="external",
        payable=False,
        view=False,
        pure=False,
        source_code=fn.source_code,
        source_location=loc,
        builtins_used=(),
        children=(bound, unresolved, sink),
    )
    flow = Flow(
        entry_point_declarer_canonical_name="A.f()",
        entry_point_invoker_canonical_name="A.f()",
        entry_point_contract_name="A",
        entry_point_function_name="f",
        root=root_node,
        unresolved_count=1,
        max_depth=1,
        value_sink_count=2,
        dynamic_dispatch_count=1,
    )
    facts = RepoFacts(
        repo_path=str(root),
        contracts=(contract,),
        free_functions=(),
    )
    version = SimpleNamespace(
        compiler="missing-test-compiler",
        version="1.2.3",
        optimized=True,
        optimize_runs=999,
        via_ir=False,
    )
    filename = SimpleNamespace(absolute=str(source), short="A.sol")
    unit = SimpleNamespace(compiler_version=version, filenames=(filename,))
    compilation = SimpleNamespace(compilation_units={"random-id": unit})
    scope = Scope(interface_bindings=(("IRunner", "Impl"),))
    return facts, (flow,), scope, compilation


def test_export_is_checkout_independent_and_complete(tmp_path: Path) -> None:
    args_a = _analysis(tmp_path / "a")
    args_b = _analysis(tmp_path / "b")
    one = build_machine_export(*args_a, tool_version="test")
    two = build_machine_export(*args_b, tool_version="test")

    assert one == two
    assert one["schema_version"] == 1
    assert one["sources"][0]["path"] == "src/A.sol"
    assert one["sources"][0]["newline"] == "crlf"
    assert one["summary"]["unresolved_nodes"] == 1
    assert one["summary"]["value_sink_nodes"] == 2
    assert one["summary"]["dynamic_dispatch_nodes"] == 1
    assert one["binding_assumptions"] == [{"interface": "IRunner", "contract": "Impl"}]
    assert [edge["resolved"] for edge in one["edges"]] == [True, False, True]
    assert one["edges"][0]["binding_assumption"] == {
        "interface_name": "IRunner",
        "contract_name": "Impl",
    }
    assert one["edges"][1]["value_expression"] == "amount"
    assert one["edges"][2]["sink_kind"] == "transfer"
    assert one["edges"][2]["value_expression"] == "payout"
    assert "filename_absolute" not in json.dumps(one)


def test_export_writer_is_byte_deterministic(tmp_path: Path) -> None:
    payload = build_machine_export(*_analysis(tmp_path / "repo"), tool_version="test")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    digest_a = write_machine_export(payload, first)
    digest_b = write_machine_export(payload, second)

    assert digest_a == digest_b
    assert first.read_bytes() == second.read_bytes()
    parsed = json.loads(first.read_text())
    assert parsed["payload_sha256"] == payload["payload_sha256"]


def test_scope_digest_preserves_gitignore_precedence(tmp_path: Path) -> None:
    facts, flows, _, compilation = _analysis(tmp_path / "repo")
    first = Scope(exclude_paths=("src/**", "!src/A.sol"))
    second = Scope(exclude_paths=("!src/A.sol", "src/**"))
    one = build_machine_export(facts, flows, first, compilation, tool_version="test")
    two = build_machine_export(facts, flows, second, compilation, tool_version="test")
    assert one["scope"]["exclude_paths"] == list(first.exclude_paths)
    assert one["scope_sha256"] != two["scope_sha256"]


def test_external_edge_paths_and_symlink_locations_are_portable(tmp_path: Path) -> None:
    facts, flows, scope, compilation = _analysis(tmp_path / "repo")
    real_source = tmp_path / "dependency" / "Lib.sol"
    real_source.parent.mkdir()
    real_source.write_text("library Lib {}")
    symlink = tmp_path / "repo" / "src" / "Lib.sol"
    symlink.symlink_to(real_source)
    loc = replace(flows[0].root.source_location, filename_absolute=str(symlink))
    external = ExternalNode(
        target_canonical_name="Lib.f()",
        target_function_name="f",
        target_contract_name="Lib",
        source_path=str(symlink),
        call_site=loc,
        value_expression="payout",
    )
    flow = replace(flows[0], root=replace(flows[0].root, children=(external,)))
    unit = compilation.compilation_units["random-id"]
    unit.filenames += (SimpleNamespace(absolute=str(symlink), short="Lib.sol"),)
    payload = build_machine_export(
        facts, (flow,), scope, compilation, tool_version="test"
    )
    serialized = json.dumps(payload)
    assert str(tmp_path) not in serialized
    assert payload["edges"][0]["source_path"].startswith("external/")


def test_external_aliases_deduplicate_identical_source_content(tmp_path: Path) -> None:
    facts, flows, scope, compilation = _analysis(tmp_path / "repo")
    unit = compilation.compilation_units["random-id"]
    for directory in ("dep-one", "dep-two", "dep-three"):
        source = tmp_path / directory / "Lib.sol"
        source.parent.mkdir()
        source.write_text("library Lib {}")
        unit.filenames += (SimpleNamespace(absolute=str(source), short="Lib.sol"),)
    payload = build_machine_export(
        facts, flows, scope, compilation, tool_version="test"
    )
    source_paths = [source["path"] for source in payload["sources"]]
    assert len(source_paths) == len(set(source_paths))
    assert len([p for p in source_paths if p.startswith("external/")]) == 1


def test_compiler_units_have_total_deterministic_order(tmp_path: Path) -> None:
    args = _analysis(tmp_path / "repo")
    facts, flows, scope, compilation = args
    first = compilation.compilation_units["random-id"]
    second = SimpleNamespace(
        compiler_version=replace_version(first.compiler_version, optimize_runs=200),
        filenames=first.filenames,
    )
    compilation.compilation_units = {"one": first, "two": second}
    one = build_machine_export(*args, tool_version="test")
    compilation.compilation_units = {"two": second, "one": first}
    two = build_machine_export(*args, tool_version="test")
    assert one == two


def replace_version(version, **kwargs):
    return SimpleNamespace(**(vars(version) | kwargs))


def test_relative_external_paths_are_content_addressed(tmp_path: Path) -> None:
    facts, flows, scope, compilation = _analysis(tmp_path / "repo")
    source = tmp_path / "dependency" / "Lib.sol"
    source.parent.mkdir()
    source.write_text("library Lib {}")
    loc = replace(
        flows[0].root.source_location,
        filename_absolute=str(source),
        filename_relative="../dependency/Lib.sol",
    )
    dep = replace(facts.contracts[0].functions[0], source_location=loc)
    facts = replace(facts, free_functions=(dep,))
    child = ExternalNode(
        target_canonical_name="Lib.f()",
        target_function_name="f",
        target_contract_name="Lib",
        source_path=loc.filename_relative,
        call_site=flows[0].root.source_location,
    )
    flow = replace(flows[0], root=replace(flows[0].root, children=(child,)))
    payload = build_machine_export(
        facts, (flow,), scope, compilation, tool_version="test"
    )
    assert payload["edges"][0]["source_path"].startswith("external/")
    assert "../dependency" not in json.dumps(payload)


def test_export_rejects_changed_source_snapshot(tmp_path: Path) -> None:
    facts, flows, scope, compilation = _analysis(tmp_path / "repo")
    source = Path(facts.contracts[0].source_location.filename_absolute)
    compilation.src_content = {str(source): source.read_text()}
    source.write_text("contract Changed {}")
    with pytest.raises(ExportError, match="changed since analysis"):
        build_machine_export(facts, flows, scope, compilation, tool_version="test")


def test_binary_digest_does_not_claim_unrelated_path_compiler(
    tmp_path: Path, monkeypatch
) -> None:
    facts, flows, scope, compilation = _analysis(tmp_path / "repo")
    binary = tmp_path / "unrelated-solc"
    binary.write_bytes(b"this is not the compiler used by Hardhat")
    monkeypatch.setenv("PATH", str(binary.parent))
    compilation.compilation_units["random-id"].compiler_version.compiler = "solc"
    compilation.platform = SimpleNamespace(NAME="Hardhat")
    payload = build_machine_export(
        facts, flows, scope, compilation, tool_version="test"
    )
    assert payload["compilers"][0]["binary_sha256"] is None


def test_export_uses_recorded_compiler_inputs_settings_and_digest(
    tmp_path: Path,
) -> None:
    facts, flows, scope, compilation = _analysis(tmp_path / "repo")
    unit = compilation.compilation_units["random-id"]
    source = Path(unit.filenames[0].absolute)
    compiler_bytes = source.read_bytes().replace(b"\r\n", b"\n")
    unit._solflow_source_bytes = {str(source): compiler_bytes}
    unit._solflow_compiler_binary_sha256 = "a" * 64
    unit._solflow_compiler_binary_provenance = "invoked_executable"
    unit._solflow_compiler_long_version = "0.8.20+commit.a1b79de6"
    unit._solflow_compiler_settings = {
        "evmVersion": "paris",
        "optimizer": {"enabled": True, "runs": 1234},
        "remappings": [f"project/={facts.repo_path}/src/"],
    }
    payload = build_machine_export(
        facts, flows, scope, compilation, tool_version="test"
    )
    compiler = payload["compilers"][0]
    assert compiler["binary_sha256"] == "a" * 64
    assert compiler["binary_provenance"] == "invoked_executable"
    assert compiler["long_version"] == "0.8.20+commit.a1b79de6"
    assert compiler["settings"]["evmVersion"] == "paris"
    assert compiler["settings"]["remappings"] == ["project/=src/"]
    assert compiler["source_inputs"] == [
        {
            "path": "src/A.sol",
            "sha256": hashlib.sha256(compiler_bytes).hexdigest(),
            "bytes": len(compiler_bytes),
        }
    ]
    assert str(tmp_path) not in json.dumps(payload)


def test_export_rejects_build_input_different_from_disk(tmp_path: Path) -> None:
    args = _analysis(tmp_path / "repo")
    unit = args[3].compilation_units["random-id"]
    unit._solflow_source_bytes = {unit.filenames[0].absolute: b"contract Different {}"}
    with pytest.raises(ExportError, match="compiler input differs"):
        build_machine_export(*args, tool_version="test")


def test_export_rejects_nonportable_absolute_scope_patterns(tmp_path: Path) -> None:
    facts, flows, _, compilation = _analysis(tmp_path / "repo")
    scope = Scope(exclude_paths=(str(tmp_path / "repo" / "private" / "**"),))
    with pytest.raises(ExportError, match="scope paths must be relative"):
        build_machine_export(facts, flows, scope, compilation, tool_version="test")


def test_export_rejects_ambiguous_flow_ids(tmp_path: Path) -> None:
    facts, flows, scope, compilation = _analysis(tmp_path / "repo")
    other = replace(flows[0], root=replace(flows[0].root, source_code="different body"))
    with pytest.raises(ExportError, match="ambiguous Flow identity"):
        build_machine_export(
            facts, (*flows, other), scope, compilation, tool_version="test"
        )


@pytest.mark.parametrize("raw, expected", [(b"a\rb\r", "cr"), (b"a\rb\n", "mixed")])
def test_source_manifest_records_bare_cr_newlines(
    tmp_path: Path, raw, expected
) -> None:
    args = _analysis(tmp_path / "repo")
    source = Path(args[0].contracts[0].source_location.filename_absolute)
    source.write_bytes(raw)
    payload = build_machine_export(*args, tool_version="test")
    assert payload["sources"][0]["newline"] == expected
