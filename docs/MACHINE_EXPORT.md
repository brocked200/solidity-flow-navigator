# Machine export schema

`solflow --export-json PATH <repo>` compiles the target, builds the same Flow
IR used by the UI, writes canonical JSON atomically, prints the emitted-byte
SHA-256 to stderr, and exits without starting Flask.

Schema version `1` contains:

- raw source paths, byte lengths, newline modes, SHA-256 values, and a source
  tree digest;
- compiler identity, exact standard-JSON settings/input-source digests when
  supplied by Hardhat/Foundry, and compiler-binary SHA-256 with explicit
  provenance;
- normalized scope configuration, interface-binding assumptions, and digest;
- an `analysis` object recording analyzer losses and capture diagnostics;
- per-entry-point Flow trees;
- a flat edge inventory containing resolved calls, unresolved/dynamic calls,
  external stubs, explicit sinks, and native-value expressions;
- orthogonal summary counts for unresolved nodes, value sinks, and dynamic
  dispatch;
- an embedded payload digest.

Absolute checkout paths and timestamps are intentionally excluded so the same
source, compiler, scope, and tool version produce byte-identical output.
Scope pattern order is preserved because gitignore negations are order-sensitive;
binding assumptions contain the effective, last-wins interface assignments.
Absolute scope patterns are rejected; use repository-relative patterns.

Source entries describe raw files (`lf`, `crlf`, `cr`, `mixed`, or `none`). The
compiler's `source_inputs` separately identify the exact UTF-8 compiler input,
including its newline bytes. Export fails when source content has changed since
analysis or disagrees with a captured compiler input. External/symlink sources
use full-content-SHA aliases, and identical external content is deduplicated.

`binary_provenance` is `invoked_executable` for a directly selected solc binary,
or `configured_executable` when `FOUNDRY_SOLC` names a binary whose digest is
stable across the build and whose reported version matches the artifacts.
Framework-selected compilers without this evidence have `binary_sha256: null`
and `binary_provenance: "unavailable"`; an unrelated local solc is never substituted.
`settings_provenance: "unavailable"` likewise means the framework did not expose
its complete settings. These are explicit reproducibility gaps, not a claim that
the artifact fully identifies the build.

Summary node counts sum per-entry-point metrics. Shared code reachable from
different entry points contributes to each Flow; these are not globally unique
source-site counts. Unresolved nodes, binding assumptions, and dynamic dispatch
remain audit limitations even when a Flow has zero other unresolved calls.
Duplicate entry-point identities are rejected before UI routing or export can
silently overwrite or conflate a Flow.

## Analyzer errors and partial output

By default an observed analyzer error or unverified diagnostic capture makes
the CLI exit `1` before writing an export or starting the UI. Compilation
errors are always fatal. To deliberately inspect a partial result, pass
`--allow-partial-analysis` and treat its reported losses as **UNREAD**.

`analysis` contains:

- `complete`: whether this extraction observed no losses and its diagnostic
  capture remained verified;
- `degraded_functions`: ordered, deduplicated known functions whose IR
  generation failed;
- `diagnostics`: ordered, deduplicated analyzer and capture messages.

The `analysis.complete` signal describes the analyzer run, not the completeness
of the runtime call graph. Unresolved targets, stubs and binding assumptions
remain separate limitations. An empty function list is insufficient: an empty
AST or unresolved parser reference can lose information without identifying a
function. Inspect both `complete` and `diagnostics`.

SolFlow applies scoped declaration-ID compatibility handling during Slither
construction. Contract identifiers, including namespace members and Contract
types, resolve through the owning compilation unit's complete declaration
table. Enum, struct and value-type declarations retain their exact compiler
identity; nested declarations are associated with models by owner and source
span. Function IDs keep the contextual and virtual-dispatch rules. The
compatibility hooks are restored after extraction and do not modify installed
Slither files or Solidity sources. Missing or contradictory identity evidence
fails closed instead of selecting an unrelated same-name declaration.

This integration is checked against Slither 0.11.6. Upstream private-interface
changes can require a compatibility update; an unsupported identity must not
be treated as a successful repair. Legacy or synthesized inputs without an
authoritative declaration ID retain Slither's original behavior.

Corrected resolution can restore previously missing calls and replace a
wrong but callable dependency target even when no IR error was logged. It can
therefore change edges, source-bearing Flow trees and `payload_sha256` without
any change to Solidity input. Record the SolFlow Git revision alongside audit
artifacts when comparing analyzer revisions.

For Slither 0.11.6, IR-generation aborts, empty-AST handling and unresolved
exported references have direct observation hooks scoped to extraction. Those
paths remain visible when logging is disabled entirely within construction.
Other analyzer errors still rely on log capture and its integrity checks;
this is not a guarantee that every future or internally suppressed diagnostic
will be observed.

## Scope and receiver identity

`scope.dependency_paths` records the effective, ordered dependency-root
patterns. The default is `["**/node_modules/**"]`. These patterns suppress
Flow roots only; they do not affect compilation or explicit binding
eligibility. `--dependency-path` appends patterns; a config value of `[]`
clears the default. `exclude_paths` is stricter and also gates binding targets.

Each function node adds optional `invoked_via_contract_file`, the portable
source alias of its receiver's declaring file. Together with
`invoked_via_contract_name`, it identifies the receiver across same-name
contracts in distinct files, including cycle terminals. Consumers of older
artifacts may encounter an absent value; synthetic nodes may carry `null`.

Schema version remains `1` because these are additive fields. The always-present
`analysis` and `scope.dependency_paths` additions changed `payload_sha256` for
all artifacts, even complete runs. The receiver-file field also changes the
hashed Flow trees. Old and new payload digests are therefore not comparable
as unchanged evidence; regenerate artifacts with the same sources, compiler,
scope and tool revision when comparing bytes.

The artifact includes source bodies and may contain private code. Store and
share it under the target's confidentiality policy.
