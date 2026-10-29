"""Layer 1 compilation: thin wrapper around crytic-compile.

Per spec §9.1, this layer hard-fails on any compilation issue. There is no
auto-remediation and no partial-analysis fallback. If crytic-compile cannot
produce artifacts, we raise CompilationFailure carrying the underlying error
verbatim and let the CLI surface it.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from crytic_compile import CryticCompile
from crytic_compile.platform.solc_standard_json import SolcStandardJson
from crytic_compile.utils.naming import process_hardhat_v3_filename


class CompilationFailure(Exception):
    """Raised when crytic-compile cannot compile the target repository.

    The string form of the exception is the underlying error verbatim, so
    the CLI can print it without paraphrasing (spec §9.1).
    """


def compile_repo(path: Path | str) -> CryticCompile:
    """Compile the Solidity project rooted at ``path``.

    Returns the populated ``CryticCompile`` object. Raises ``CompilationFailure``
    on any failure — including a missing or non-directory path — with the
    underlying error preserved verbatim in the exception message.

    The returned object stays inside Layer 1; only ``RepoFacts`` crosses the
    Layer 1 / Layer 2 boundary.
    """

    repo_path = Path(path).resolve()

    if not repo_path.exists():
        raise CompilationFailure(f"path does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise CompilationFailure(f"path is not a directory: {repo_path}")

    try:
        # Defer only so provenance can be captured before the first compiler
        # invocation. CryticCompile.compile() performs the ordinary build.
        compilation = CryticCompile(
            target=str(repo_path), crytic_defer_compilation="true"
        )
        if compilation.platform.NAME == "solc":
            # A plain directory is a CLI-supported repository too. The solc
            # platform expects a file, so submit every source in one explicit
            # standard-JSON input. Keep raw newline/UTF-8 bytes unchanged.
            sources = {
                source.relative_to(repo_path).as_posix(): {
                    "content": source.read_bytes().decode("utf-8")
                }
                for source in sorted(repo_path.rglob("*.sol"))
                if source.is_file()
            }
            if not sources:
                raise CompilationFailure(
                    "plain repository contains no Solidity sources"
                )
            compilation = CryticCompile(
                target=SolcStandardJson({"language": "Solidity", "sources": sources}),
                cwd=str(repo_path),
                solc_working_dir=str(repo_path),
                crytic_defer_compilation="true",
            )
        platform = compilation.platform
        build_paths: dict[str, Path] = {}
        if platform.NAME == "Hardhat":
            get_paths = platform._get_hardhat_paths

            def record_paths(*args: Any, **kwargs: Any) -> dict:
                paths = get_paths(*args, **kwargs)
                build_paths["root"] = (repo_path / paths["root"]).resolve()
                build_paths["build_info"] = (
                    repo_path / paths["artifacts"] / "build-info"
                )
                return paths

            platform._get_hardhat_paths = record_paths

        binary = None
        binary_sha = None
        configured_version = None
        if platform.NAME in ("solc", "Solc-json"):
            binary = _direct_compiler(compilation)
            binary_sha = _binary_digest(binary)
        elif platform.NAME == "Foundry" and os.environ.get("FOUNDRY_SOLC"):
            configured = os.environ["FOUNDRY_SOLC"]
            if Path(configured).is_file():
                binary = Path(configured).resolve()
                binary_sha = _binary_digest(binary)
                result = subprocess.run(
                    [str(binary), "--version"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=15,
                )
                match = re.search(r"\b\d+\.\d+\.\d+(?:\+[^\s]+)?", result.stdout)
                if match is None:
                    raise CompilationFailure(
                        "cannot verify configured compiler identity"
                    )
                configured_version = match.group(0)
        compilation.compile()
        if binary is not None and _binary_digest(binary) != binary_sha:
            raise CompilationFailure("compiler changed during compilation")
        if platform.NAME == "Foundry":
            config = platform._config
            root = platform._project_root
            build_paths["root"] = root
            build_paths["build_info"] = root / (
                config.build_info_path
                if config and config.build_info_path
                else str(Path(config.out_path if config else "out") / "build-info")
            )
        _capture_inputs(compilation, build_paths, binary_sha)
        if configured_version is not None:
            for unit in compilation.compilation_units.values():
                if unit.compiler_version.version != configured_version.split("+", 1)[0]:
                    raise CompilationFailure("configured compiler identity mismatch")
                unit._solflow_compiler_binary_provenance = "configured_executable"
                unit._solflow_compiler_long_version = configured_version
        return compilation
    except Exception as exc:
        raise CompilationFailure(str(exc)) from exc


def _binary_digest(binary: Path | None) -> str | None:
    if binary is None:
        return None
    with binary.open("rb") as fp:
        return hashlib.file_digest(fp, "sha256").hexdigest()


def _direct_compiler(compilation: CryticCompile) -> Path | None:
    command = compilation._compilation_kwargs.get("solc", "solc")
    found = shutil.which(command)
    if found is None:
        return None  # Let the normal compiler error report the missing tool.
    binary = Path(found).resolve()
    # Resolve solc-select's Python launcher to the selected native executable,
    # then pass that executable to CryticCompile. Hashing the launcher alone
    # would attest neither the selected compiler nor its implementation.
    with binary.open("rb") as fp:
        header = fp.read(2048)
    if b"solc_select" in header:
        from solc_select.solc_select import artifact_path, current_version

        version, _ = current_version()
        binary = artifact_path(version).resolve()
    compilation._compilation_kwargs["solc"] = str(binary)
    return binary


def _capture_inputs(
    compilation: CryticCompile, paths: dict[str, Path], binary_sha: str | None
) -> None:
    for unit in compilation.compilation_units.values():
        unit._solflow_compiler_binary_sha256 = binary_sha
        unit._solflow_compiler_binary_provenance = (
            "invoked_executable" if binary_sha is not None else "unavailable"
        )
        compiler_input = None
        if "build_info" in paths:
            # Read exactly the artifact selected by CryticCompile, including
            # custom Hardhat/Foundry output directories; no repository crawl.
            unique_id = unit.unique_id
            if Path(unique_id).name != unique_id:
                raise CompilationFailure("invalid compilation-unit artifact identity")
            with (paths["build_info"] / f"{unique_id}.json").open() as fp:
                build_info = json.load(fp)
            compiler_input = build_info.get("input")
            unit._solflow_compiler_long_version = build_info.get("solcLongVersion")
        elif compilation.platform.NAME == "Solc-json":
            compiler_input = compilation.platform.to_dict()
        if compiler_input is not None:
            unit._solflow_compiler_settings = compiler_input.get("settings", {})
            unit.compiler_version.optimize_runs = (
                compiler_input.get("settings", {}).get("optimizer", {}).get("runs")
            )
            source_bytes = {}
            root = paths.get("root") or Path(compilation.working_dir)
            for filename in unit.filenames:
                names = (
                    getattr(filename, "used", ""),
                    getattr(filename, "relative", ""),
                    filename.absolute,
                )
                for name, info in compiler_input.get("sources", {}).items():
                    if compilation.platform.NAME == "Hardhat":
                        name = process_hardhat_v3_filename(name)
                    if (
                        name in names
                        or (root / name).resolve() == Path(filename.absolute).resolve()
                    ):
                        if "content" in info:
                            source_bytes[str(Path(filename.absolute).resolve())] = info[
                                "content"
                            ].encode("utf-8")
                        break
            unit._solflow_source_bytes = source_bytes
