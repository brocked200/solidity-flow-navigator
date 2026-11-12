"""The public extractor must activate exact resolution with its loss hooks."""

import importlib
import os
import shutil

from crytic_compile import CryticCompile
from crytic_compile.platform.solc_standard_json import SolcStandardJson

from solidity_flow_navigator.analysis import slither_facts


def test_extractor_repairs_import_collision_without_hiding_ir_loss(
    monkeypatch, tmp_path
):
    compiler = os.environ.get("SOLFLOW_TEST_SOLC") or shutil.which("solc")
    assert compiler, "A preinstalled solc is required for the integration gate"
    sources = {
        "Local.sol": (
            "pragma solidity ^0.8.26; library Math {"
            " enum Rounding { Up, Down }"
            " function mulDivUp(uint a,uint b,uint c) internal pure returns(uint)"
            " { return (a*b+c-1)/c; } }"
        ),
        "Vendor.sol": (
            "pragma solidity ^0.8.26; library Math {"
            " enum Rounding { Floor, Ceil }"
            " function max(uint a,uint b) internal pure returns(uint)"
            " {return a>b?a:b;} }"
        ),
        "Carrier.sol": (
            'pragma solidity ^0.8.26; import {Math} from "Vendor.sol";'
            " contract Carrier { function vendor(uint a,uint b) external pure"
            " returns(uint) {return Math.max(a,b);} }"
        ),
        "Target.sol": (
            'pragma solidity ^0.8.26; import {Math} from "Local.sol";'
            ' import {Carrier} from "Carrier.sol";'
            " contract Target is Carrier {"
            " function run(uint a,uint b,uint c) external pure returns(uint)"
            " {return Math.mulDivUp(a,b,c);}"
            " function choice(Math.Rounding r) external pure returns(uint)"
            " {return uint(r);} }"
        ),
    }
    for name, body in sources.items():
        (tmp_path / name).write_text(body)
    cc = CryticCompile(
        SolcStandardJson(
            {
                "language": "Solidity",
                "sources": {name: {"content": body} for name, body in sources.items()},
            }
        ),
        solc=compiler,
        solc_working_dir=str(tmp_path),
        cwd=str(tmp_path),
    )
    module = importlib.import_module("slither.slither")
    original_update = module._update_file_scopes

    def polluted_scope(parser):
        original_update(parser)
        wrong = next(
            c
            for c in parser.contracts_by_id.values()
            if c.name == "Math"
            and str(c.source_mapping.filename.absolute).endswith("Vendor.sol")
        )
        for scope in parser.compilation_unit.scopes.values():
            if str(scope.filename.absolute).endswith("Target.sol"):
                scope.contracts["Math"] = wrong

    monkeypatch.setattr(module, "_update_file_scopes", polluted_scope)
    instances = []
    original_slither = slither_facts.Slither

    def capture(compilation):
        instance = original_slither(compilation)
        instances.append(instance)
        return instance

    monkeypatch.setattr(slither_facts, "Slither", capture)
    facts = slither_facts.extract_facts(cc, tmp_path)
    assert facts.analysis.complete, facts.analysis
    target = next(c for c in facts.contracts if c.name == "Target")
    run = next(f for f in target.functions if f.name == "run")
    library_call = next(c for c in run.calls if c.kind == "library")
    assert library_call.target_contract_uid == ("Local.sol", "Math")
    model = next(c for c in instances[0].contracts if c.name == "Target")
    choice = next(f for f in model.functions_declared if f.name == "choice")
    enum = choice.parameters[0].type.type
    assert str(enum.source_mapping.filename.absolute).endswith("Local.sol")
    assert enum.values == ["Up", "Down"]
    assert module._update_file_scopes is polluted_scope
