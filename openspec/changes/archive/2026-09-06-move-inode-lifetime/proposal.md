## Why

Issue #219's intermittent symlink rollback test depends on inode-number reuse.
`_pin_source_inode` closes its descriptor before the rename, so its stored
identity can be recycled and falsely certify a replacement. The existing move
contract requires a retained identity witness, not a number sampled earlier.

## What Changes

Retain the O_PATH descriptor through rename, destination verification and any
rollback, closing it on every exit. Make test swaps explicitly occur before or
after identity capture so each outcome is deterministic.

## Capabilities

### Modified Capabilities
- `vault-write`: a move's identity witness stays alive through verification.

## Impact

`src/mcp_server/tools.py`, anchored note-write tests, vault-tools architecture.
No migration, setting, dependency, or stronger atomic byte-CAS claim.

Refs #219
