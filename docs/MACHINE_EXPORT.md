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

The artifact includes source bodies and may contain private code. Store and
share it under the target's confidentiality policy.
