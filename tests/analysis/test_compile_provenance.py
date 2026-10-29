"""Compiler provenance stays tied to the build that produced the facts."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from solidity_flow_navigator.analysis import compile as compile_module


@pytest.mark.parametrize("absolute_artifacts", [True, False])
@pytest.mark.parametrize("source_name", ["A.sol", "project/contracts/A.sol"])
def test_hardhat_custom_build_info_captures_exact_inputs_and_settings(
    tmp_path: Path, monkeypatch, absolute_artifacts: bool, source_name: str
) -> None:
    relative = source_name.removeprefix("project/")
    source = tmp_path / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"contract A {\r\n}\r\n")
    build_dir = tmp_path / "custom-artifacts" / "build-info"
    build_dir.mkdir(parents=True)
    compiler_input = {
        "language": "Solidity",
        "sources": {source_name: {"content": "contract A {\r\n}\r\n"}},
        "settings": {
            "optimizer": {"enabled": True, "runs": 1234},
            "evmVersion": "paris",
            "viaIR": True,
        },
    }
    (build_dir / "exact-unit.json").write_text(
        json.dumps(
            {"input": compiler_input, "solcLongVersion": "0.8.20+commit.a1b79de6"}
        )
    )
    unit = SimpleNamespace(
        unique_id="exact-unit",
        filenames=(
            SimpleNamespace(absolute=str(source), relative=relative, used=relative),
        ),
        compiler_version=SimpleNamespace(optimize_runs=None),
    )

    class Platform:
        NAME = "Hardhat"

        def _get_hardhat_paths(self, *_args):
            artifacts = (
                build_dir.parent
                if absolute_artifacts
                else build_dir.parent.relative_to(tmp_path)
            )
            return {"root": tmp_path, "artifacts": artifacts}

    class Compilation:
        def __init__(self, **kwargs):
            self.deferred = kwargs.get("crytic_defer_compilation") == "true"
            self.platform = Platform()
            self.compilation_units = {"exact-unit": unit}
            self._compilation_kwargs = {}

        def compile(self):
            assert self.deferred
            self.platform._get_hardhat_paths([], {})

    monkeypatch.setattr(compile_module, "CryticCompile", Compilation)
    cc = compile_module.compile_repo(tmp_path)
    captured = cc.compilation_units["exact-unit"]
    assert captured._solflow_source_bytes == {str(source): source.read_bytes()}
    assert captured._solflow_compiler_settings == compiler_input["settings"]
    assert captured.compiler_version.optimize_runs == 1234
    assert captured._solflow_compiler_long_version == "0.8.20+commit.a1b79de6"


def test_direct_compiler_is_recorded_before_use(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "solc"
    binary.write_bytes(b"test native compiler binary")
    binary.chmod(0o700)
    unit = SimpleNamespace(filenames=(), compiler_version=SimpleNamespace())

    class Compilation:
        def __init__(self, **kwargs):
            self.platform = SimpleNamespace(
                NAME="Solc-json", to_dict=lambda: {"settings": {}, "sources": {}}
            )
            self._compilation_kwargs = {"solc": str(binary)}
            self.compilation_units = {"unit": unit}
            self.working_dir = tmp_path

        def compile(self):
            assert self._compilation_kwargs["solc"] == str(binary)

    monkeypatch.setattr(compile_module, "CryticCompile", Compilation)
    compile_module.compile_repo(tmp_path)
    assert (
        unit._solflow_compiler_binary_sha256
        == hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    assert unit._solflow_compiler_binary_provenance == "invoked_executable"


def test_changed_compiler_binary_fails_closed(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "solc"
    binary.write_bytes(b"compiler before build")
    binary.chmod(0o700)

    class Compilation:
        def __init__(self, **kwargs):
            self.platform = SimpleNamespace(NAME="Solc-json")
            self._compilation_kwargs = {"solc": str(binary)}
            self.compilation_units = {}

        def compile(self):
            binary.write_bytes(b"compiler after build")

    monkeypatch.setattr(compile_module, "CryticCompile", Compilation)
    with pytest.raises(compile_module.CompilationFailure, match="compiler changed"):
        compile_module.compile_repo(tmp_path)


@pytest.mark.parametrize("reported_version", ["0.8.25", "0.8.24"])
def test_foundry_explicit_compiler_is_verified(
    tmp_path: Path, monkeypatch, reported_version: str
) -> None:
    binary = tmp_path / "solc"
    binary.write_text("#!/bin/sh\nprintf 'Version: 0.8.25+commit.b61c2a91\\n'\n")
    binary.chmod(0o700)
    monkeypatch.setenv("FOUNDRY_SOLC", str(binary))
    info = tmp_path / "out" / "build-info"
    info.mkdir(parents=True)
    (info / "unit.json").write_text(
        json.dumps({"input": {"sources": {}, "settings": {}}})
    )
    unit = SimpleNamespace(
        unique_id="unit",
        filenames=(),
        compiler_version=SimpleNamespace(version=reported_version),
    )

    class Compilation:
        def __init__(self, **kwargs):
            self.platform = SimpleNamespace(
                NAME="Foundry",
                _project_root=tmp_path,
                _config=SimpleNamespace(build_info_path=None, out_path="out"),
            )
            self.compilation_units = {"unit": unit}

        def compile(self):
            pass

    monkeypatch.setattr(compile_module, "CryticCompile", Compilation)
    if reported_version == "0.8.24":
        with pytest.raises(
            compile_module.CompilationFailure, match="compiler identity mismatch"
        ):
            compile_module.compile_repo(tmp_path)
    else:
        compile_module.compile_repo(tmp_path)
        assert (
            unit._solflow_compiler_binary_sha256
            == hashlib.sha256(binary.read_bytes()).hexdigest()
        )
        assert unit._solflow_compiler_binary_provenance == "configured_executable"


def test_plain_directory_compiles_all_sources_with_exact_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "src").mkdir()
    one = tmp_path / "src" / "A.sol"
    two = tmp_path / "B.sol"
    one.write_bytes("contract A { /* đ */ }\r\n".encode())
    two.write_bytes(b"contract B {}\n")
    filenames = tuple(
        SimpleNamespace(
            absolute=str(p),
            relative=p.relative_to(tmp_path).as_posix(),
            used=p.relative_to(tmp_path).as_posix(),
        )
        for p in (one, two)
    )
    unit = SimpleNamespace(filenames=filenames, compiler_version=SimpleNamespace())

    class Compilation:
        def __init__(self, target, **kwargs):
            self.platform = (
                SimpleNamespace(NAME="solc") if isinstance(target, str) else target
            )
            self._compilation_kwargs = {}
            self.compilation_units = {"unit": unit}
            self.working_dir = tmp_path

        def compile(self):
            assert self.platform.NAME == "Solc-json"
            assert set(self.platform.to_dict()["sources"]) == {"src/A.sol", "B.sol"}

    monkeypatch.setattr(compile_module, "CryticCompile", Compilation)
    monkeypatch.setattr(compile_module, "_direct_compiler", lambda _: None)
    compile_module.compile_repo(tmp_path)
    assert unit._solflow_source_bytes == {
        str(one): one.read_bytes(),
        str(two): two.read_bytes(),
    }
