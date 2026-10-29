# Reproducible integration gate

Public upstream through `b4a697affb6647fd79d09ecbf954857761cb84e8` never
pinned the sibling Solmate checkout: `tests/conftest.py` was introduced in
`3ab895f2a0da7d86328a1bcc466a1af3e8aeb4d4`, and public CI intentionally
omits the fixture. A normal upstream checkout can therefore skip these tests.
The custom branch's merge gate must run them with `--require-solmate`.

`tests/fixtures.lock.toml` selects immutable Solmate revision
`89365b880c4f3c786bdd453d4b8e8fe410344a69` and its pinned ds-test submodule.
This tree preserves the ERC20-typed SafeTransferLib API, LibString's
`toStringOZ(uint256)` free function, and Solidity 0.8.15 configuration that
the integration assertions expect. The fixture verifies both checkouts'
revisions and tracked-file cleanliness before compiling. Generated Foundry
`out/` and `cache/` artifacts are ignored by Solmate and may be regenerated.

Set up the public dependency separately from execution:

```sh
git clone --no-checkout https://github.com/transmissions11/solmate.git ../test-repos/solmate
git -C ../test-repos/solmate checkout --detach 89365b880c4f3c786bdd453d4b8e8fe410344a69
git -C ../test-repos/solmate submodule update --init --recursive
```

Install Foundry 1.3.1 and solc 0.8.15 before blocking the build's network.
The lock records the reviewed macOS release archive/binary SHA256 values;
other operating systems require their matching official release artifacts.
The compiler can be supplied using `FOUNDRY_SOLC=/absolute/path/to/solc-0.8.15`
or preinstalled under the isolated HOME's `.svm/0.8.15/solc-0.8.15`.

Create a clean virtual environment, install the reviewed dependency versions
with `pip install -r tests/requirements-review.txt`, and then install this
checkout with `pip install --no-deps --no-build-isolation -e .`.
Run with
an empty environment, using disposable HOME/TMPDIR, no credentials, and an
OS sandbox that denies network and reads outside the toolchain and disposable
checkouts. Dependency downloads happen before entering that sandbox. Target
build configuration can execute code; an empty environment alone does not
prevent filesystem access to secrets. The port-selection tests monkeypatch
the bind probe, so they need no open
localhost ports and can run in the same network-blocked sandbox.

```sh
python -m pytest -q --require-solmate --solmate-path ../test-repos/solmate
```

The merge gate requires zero missing-fixture skips. `--solmate-path` also
rejects a missing explicit fixture path even without `--require-solmate`.
