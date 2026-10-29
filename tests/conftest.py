"""Shared pytest fixtures for the integration tests.

The Solmate test repository lives outside this codebase as a sibling
``test-repos/`` directory. Discovery walks up from this file's
parents to find that sibling, so the fixtures work both from the primary
checkout and from a linked git worktree (e.g. under ``.claude/worktrees/``).

``solmate_facts`` (Layer 1) and ``solmate_flows`` (Layer 2) are
session-scoped — the expensive forge build runs once per pytest session,
and Flow construction runs once on top of it. Both fixtures live here so
either layer's tests find them via pytest's automatic conftest discovery.
"""

import subprocess
import tomllib
from pathlib import Path

import pytest

from solidity_flow_navigator.analysis.compile import compile_repo
from solidity_flow_navigator.analysis.slither_facts import extract_facts
from solidity_flow_navigator.analysis.types import RepoFacts
from solidity_flow_navigator.flow.builder import build_flows
from solidity_flow_navigator.flow.scope import DEFAULT_SCOPE, Scope
from solidity_flow_navigator.flow.types import Flow


def _discover_solmate() -> Path | None:
    """Return the path to the sibling ``test-repos/solmate``, or None.

    Walks several ancestor levels so this works in both the primary
    checkout (sibling at parents[2]) and in linked git worktrees that
    nest the project a few levels deeper (e.g. parents[5] when checked
    out under ``.claude/worktrees/<id>/``).
    """

    here = Path(__file__).resolve()
    for n in range(2, 7):
        if n >= len(here.parents):
            break
        candidate = here.parents[n] / "test-repos" / "solmate"
        if candidate.is_dir():
            return candidate
    return None


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("Solmate integration")
    group.addoption(
        "--solmate-path", type=Path, help="Path to the pinned Solmate fixture checkout"
    )
    group.addoption(
        "--require-solmate",
        action="store_true",
        help="Fail instead of skip when the Solmate integration fixture is missing",
    )


def _verify_solmate_revision(path: Path) -> None:
    lock = tomllib.loads(
        (Path(__file__).parent / "fixtures.lock.toml").read_text(encoding="utf-8")
    )
    for name, checkout in (("solmate", path), ("ds_test", path / "lib" / "ds-test")):
        try:
            revision = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "-C", str(checkout), "status", "--porcelain", "-uno"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            pytest.fail(f"Cannot verify pinned {name} fixture: {exc}", pytrace=False)
        expected = lock[name]["revision"]
        if revision != expected or dirty:
            pytest.fail(
                f"{name} fixture must be clean at {expected}; got {revision}. "
                "See docs/INTEGRATION_TESTS.md.",
                pytrace=False,
            )


@pytest.fixture(scope="session")
def solmate_facts(pytestconfig: pytest.Config) -> RepoFacts:
    """Compile Solmate via crytic-compile and extract Layer 1 facts."""

    explicit = pytestconfig.getoption("--solmate-path")
    path = explicit if explicit is not None else _discover_solmate()
    if path is None or not path.is_dir():
        message = "Solmate fixture missing; see docs/INTEGRATION_TESTS.md"
        if explicit is not None or pytestconfig.getoption("--require-solmate"):
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    path = path.resolve()
    _verify_solmate_revision(path)
    cc = compile_repo(path)
    return extract_facts(cc, path)


@pytest.fixture(scope="session")
def solmate_flows(solmate_facts: RepoFacts) -> tuple[Flow, ...]:
    """Build one Flow per entry point on the Solmate facts under DEFAULT_SCOPE.

    This is the production-behavior fixture: v0.1's default scope filters
    out ``Mock*`` contracts and anything under ``src/test/**``, so any test
    asserting on those names belongs on ``solmate_flows_unfiltered``
    instead.
    """

    return build_flows(solmate_facts, DEFAULT_SCOPE)


@pytest.fixture(scope="session")
def solmate_flows_unfiltered(solmate_facts: RepoFacts) -> tuple[Flow, ...]:
    """Build flows on the Solmate facts with NO scope filtering applied.

    Used by tests whose subject is content the default scope excludes —
    Mock contracts (``MockERC20``, ``MockOwned``, ...), helpers under
    ``src/test/**``, and the lib/ts-test reach that test contracts produce.
    Equivalent to running ``solflow`` with a ``solflow.toml`` whose
    ``exclude_paths = []`` and ``exclude_contracts = []`` clear the
    built-in defaults (the v0.10.0 Stage 2 replacement for the retired
    ``--no-default-excludes`` flag).
    """

    return build_flows(solmate_facts, Scope())
