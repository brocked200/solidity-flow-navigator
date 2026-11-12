"""Live binding routes apply the CLI's built-flow validity gate."""

from __future__ import annotations

import importlib
import json
import re
import tomllib
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from flask.testing import FlaskClient

from solidity_flow_navigator.analysis.types import (
    CallEdge,
    Contract,
    Function,
    RepoFacts,
    SourceLocation,
)
from solidity_flow_navigator.flow.builder import build_flows
from solidity_flow_navigator.flow.scope import DEFAULT_SCOPE, Scope
from solidity_flow_navigator.serve.app import create_app

app_module = importlib.import_module("solidity_flow_navigator.serve.app")


def _location(contract: str) -> SourceLocation:
    filename = f"src/{contract}.sol"
    return SourceLocation(f"/repo/{filename}", filename, 0, 1, (1,), 1, 2)


def _function(
    contract: str,
    name: str,
    *,
    visibility: str = "external",
    implemented: bool = True,
    entry_point: bool = False,
    calls: tuple[CallEdge, ...] = (),
) -> Function:
    return Function(
        canonical_name=f"{contract}.{name}()",
        name=name,
        full_name=f"{name}()",
        contract_declarer_name=contract,
        visibility=visibility,
        is_constructor=False,
        is_fallback=False,
        is_receive=False,
        is_modifier=False,
        is_implemented=implemented,
        is_virtual=False,
        is_entry_point=entry_point,
        payable=False,
        view=False,
        pure=False,
        parameters=(),
        returns=(),
        modifier_names=(),
        source_location=_location(contract),
        source_code=f"function {name}() {visibility} {{}}",
        calls=calls,
    )


def _contract(
    name: str,
    functions: tuple[Function, ...],
    *,
    interface: bool = False,
    bases: tuple[str, ...] = (),
) -> Contract:
    return Contract(
        name=name,
        kind="interface" if interface else "contract",
        is_interface=interface,
        is_library=False,
        is_abstract=False,
        linearized_base_contract_names=bases,
        immediate_base_contract_names=bases[:1],
        source_location=_location(name),
        functions=functions,
        modifiers=(),
    )


def _facts() -> RepoFacts:
    # Keep the interface call below an internal child: validity must inspect
    # every node, not only the Flow root or its immediate children.
    call = CallEdge(
        kind="high_level",
        subkind=None,
        target_canonical_name="IFoo.foo()",
        target_function_name="foo",
        target_contract_name="IFoo",
        is_resolved=True,
        source_location=_location("Caller"),
    )
    internal_call = replace(
        call,
        kind="internal",
        target_canonical_name="Caller.helper()",
        target_function_name="helper",
        target_contract_name="Caller",
    )
    caller = _contract(
        "Caller",
        (
            _function("Caller", "run", entry_point=True, calls=(internal_call,)),
            _function("Caller", "helper", visibility="internal", calls=(call,)),
        ),
    )
    iface = _contract(
        "IFoo",
        tuple(_function("IFoo", m, implemented=False) for m in ("foo", "bar")),
        interface=True,
    )
    implementations = tuple(
        _contract(
            name,
            tuple(
                _function(name, method, visibility=visibility)
                for method, visibility in methods
            ),
            bases=("IFoo",) if name == "FooMock" else (),
        )
        for name, methods in (
            ("FooImpl", (("foo", "external"), ("bar", "public"))),
            ("FooMock", (("foo", "external"), ("bar", "external"))),
            ("PrivateFoo", (("foo", "private"), ("bar", "external"))),
            ("PathFoo", (("foo", "external"), ("bar", "external"))),
            ("PartialFoo", (("foo", "external"),)),
        )
    )
    return RepoFacts("/repo", (caller, iface, *implementations), ())


def _scope() -> Scope:
    return replace(
        DEFAULT_SCOPE,
        exclude_paths=(*DEFAULT_SCOPE.exclude_paths, "src/PathFoo.sol"),
        interface_bindings=(("IFoo", "FooImpl"), ("IUnused", "Stable")),
    )


def _client(
    tmp_path: Path, *, scope: Scope | None = None, prebuilt_scope: Scope | None = None
) -> tuple[FlaskClient, Path]:
    active = _scope() if scope is None else scope
    facts = _facts()
    target = tmp_path / "solflow.toml"
    target.write_text('# keep this comment\n[scope]\nexclude_contracts = ["*Mock*"]\n')
    flows = build_flows(facts, prebuilt_scope or active)
    app = create_app(facts, flows, scope=active, config_path=target)
    app.config.update(TESTING=True)
    return app.test_client(), target


def _flow(client: FlaskClient) -> dict:
    response = client.get("/flow/Caller.run()")
    assert response.status_code == 200
    match = re.search(
        r'<script[^>]*id="flow-data"[^>]*>(.*?)</script>',
        response.get_data(as_text=True),
        re.S,
    )
    assert match is not None
    return json.loads(match.group(1))


@pytest.mark.parametrize("target_name", ("FooMock", "PrivateFoo", "PathFoo", "Typo"))
def test_failed_binding_preserves_current_flows_scope_and_config(
    tmp_path: Path, target_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even an over-permissive dropdown must not bypass the builder's rules.
    monkeypatch.setattr(app_module, "_candidate_contracts", lambda *_: (target_name,))
    client, config = _client(tmp_path)
    before_flow = _flow(client)
    before_config = config.read_bytes()

    response = client.get(f"/bind/IFoo?contract={target_name}&next=/%23bindings")

    assert response.status_code == 302
    location = urlsplit(response.headers["Location"])
    assert parse_qs(location.query).get("bind") == ["error"]
    assert location.fragment == "bindings"
    assert _flow(client) == before_flow
    assert 'value="FooImpl" selected' in client.get("/").get_data(as_text=True)
    assert config.read_bytes() == before_config
    saved = client.post("/bindings/save")
    assert saved.headers["Location"] == "/?saved=ok#bindings"
    data = tomllib.loads(config.read_text())
    assert data["bindings"] == {"IFoo": "FooImpl", "IUnused": "Stable"}
    assert data["scope"] == {"exclude_contracts": ["*Mock*"]}
    assert "# keep this comment" in config.read_text()


@pytest.mark.parametrize(
    "target_name", ("FooMock", "PrivateFoo", "PathFoo", "PartialFoo")
)
def test_dropdown_filters_exclusions_and_non_public_structural_methods(
    tmp_path: Path, target_name: str
) -> None:
    client, _ = _client(tmp_path)
    html = client.get("/").get_data(as_text=True)
    assert 'value="FooImpl" selected' in html
    assert f'<option value="{target_name}"' not in html


def test_cli_valid_binding_does_not_require_candidate_membership(
    tmp_path: Path,
) -> None:
    # Only foo() is called. CLI permits this binding even though PartialFoo
    # lacks IFoo.bar() and therefore is absent from structural candidates.
    client, target = _client(tmp_path)
    response = client.get("/bind/IFoo?contract=PartialFoo&next=/%23bindings")
    assert response.headers["Location"] == "/#bindings"
    bound = _flow(client)["root"]["children"][0]["children"][0]
    assert bound["bound_via"] == {
        "interface_name": "IFoo",
        "contract_name": "PartialFoo",
    }
    assert client.post("/bindings/save").headers["Location"] == "/?saved=ok#bindings"
    assert tomllib.loads(target.read_text())["bindings"]["IFoo"] == "PartialFoo"


def test_none_sentinel_clears_and_saves_a_valid_binding(tmp_path: Path) -> None:
    client, target = _client(tmp_path)
    response = client.get("/bind/IFoo?contract=__none__&next=/%23bindings")
    assert response.headers["Location"] == "/#bindings"
    unbound = _flow(client)["root"]["children"][0]["children"][0]
    assert unbound["reason"] == "interface_call_no_binding"
    assert client.post("/bindings/save").headers["Location"] == "/?saved=ok#bindings"
    assert tomllib.loads(target.read_text())["bindings"] == {"IUnused": "Stable"}


@pytest.mark.parametrize("target_name", ("FooMock", "PrivateFoo", "PathFoo", "Typo"))
def test_save_revalidates_bindings_supplied_directly_to_app(
    tmp_path: Path, target_name: str
) -> None:
    # Library callers can seed Scope separately from prebuilt flows. The Save
    # route cannot assume that every binding arrived through /bind/.
    invalid = replace(_scope(), interface_bindings=(("IFoo", target_name),))
    client, target = _client(tmp_path, scope=invalid, prebuilt_scope=_scope())
    before = target.read_bytes()
    assert client.post("/bindings/save").headers["Location"] == "/?saved=error#bindings"
    assert target.read_bytes() == before
    # A stale binding remains visible and can be cleared to recover.
    assert client.get("/bind/IFoo?contract=__none__").headers["Location"] == "/"
    assert client.post("/bindings/save").headers["Location"] == "/?saved=ok#bindings"
    assert tomllib.loads(target.read_text())["bindings"] == {}


def test_failed_index_rebuild_preserves_the_entire_previous_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app_module, "_candidate_contracts", lambda *_: ("FooImpl", "PartialFoo")
    )
    client, target = _client(tmp_path)
    before_flow = _flow(client)
    before_index = client.get("/").get_data(as_text=True)
    before_config = target.read_bytes()

    def fail_index(*_args):
        raise ValueError("index rebuild failed")

    with monkeypatch.context() as patch:
        patch.setattr(app_module, "build_index", fail_index)
        response = client.get("/bind/IFoo?contract=PartialFoo")
    assert response.headers["Location"] == "/?bind=error"
    assert _flow(client) == before_flow
    assert client.get("/").get_data(as_text=True) == before_index
    assert target.read_bytes() == before_config
