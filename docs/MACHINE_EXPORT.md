# Machine export schema

`solflow --export-json PATH <repo>` compiles the target, builds the same Flow
IR used by the UI, writes canonical JSON atomically, prints the emitted-byte
SHA-256 to stderr, and exits without starting Flask.

Schema version `1` contains:

- raw source paths, byte lengths, newline modes, SHA-256 values, and a source
  tree digest;
- compiler identity/settings and compiler-binary SHA-256 when locally
  resolvable;
- normalized scope configuration, interface-binding assumptions, and digest;
- per-entry-point Flow trees;
- a flat edge inventory containing resolved calls, unresolved/dynamic calls,
  external stubs, explicit sinks, and native-value expressions;
- orthogonal summary counts for unresolved nodes, value sinks, and dynamic
  dispatch;
- an embedded payload digest.

Absolute checkout paths and timestamps are intentionally excluded so the same
source, compiler, scope, and tool version produce byte-identical output.

The artifact includes source bodies and may contain private code. Store and
share it under the target's confidentiality policy.
