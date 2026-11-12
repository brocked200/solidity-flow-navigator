"""A process-global IR hook must not report a different caller's failure."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from slither.core.declarations.function import Function

from solidity_flow_navigator.analysis.slither_facts import _capture_ir_failures


@pytest.mark.parametrize("include_owner_failure", [False, True])
def test_ir_capture_keeps_only_its_own_thread(monkeypatch, include_owner_failure):
    def failing_ir(function):
        raise RuntimeError(function.canonical_name)

    monkeypatch.setattr(Function, "generate_slithir_and_analyze", failing_ir)
    with _capture_ir_failures() as failures:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                Function.generate_slithir_and_analyze,
                SimpleNamespace(canonical_name="Foreign.probe()"),
            )
            with pytest.raises(RuntimeError, match=r"Foreign\.probe"):
                future.result(timeout=5)
        if include_owner_failure:
            with pytest.raises(RuntimeError, match=r"Owner\.probe"):
                Function.generate_slithir_and_analyze(
                    SimpleNamespace(canonical_name="Owner.probe()")
                )
        expected = ["Owner.probe(): RuntimeError: Owner.probe()"]
        assert failures == (expected if include_owner_failure else [])
    assert Function.generate_slithir_and_analyze is failing_ir
