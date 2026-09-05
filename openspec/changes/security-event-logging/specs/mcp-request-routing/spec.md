## MODIFIED Requirements

### Requirement: A refused tool call is recorded in the usage log

Every tool call the server refuses SHALL be written to `usage_logs` like any other tool error, carrying an error marker and the same allow-listed parameters as a successful call, and no field outside that allow-list, the marker, and — for a call whose body raised — the exception's class name. The rule covers the refusals decided before the body runs (a missing vault assignment, an unencodable argument, an exhausted quota) and the refusals decided inside it (a write attempted with a read-only credential, marked `permission_denied`), and it covers a body that raised, marked `tool_exception`. Each marker SHALL be classified as pre-body or post-body when it is introduced, and two branches on opposite sides of that line SHALL NOT share a marker value.

#### Scenario: Refusal is auditable

- **WHEN** a tool call is refused for a missing vault assignment
- **THEN** a `usage_logs` row SHALL be written for that tool with an error marker in `params` and the tool's normal allow-listed parameters

#### Scenario: Refusal adds no new logged field

- **WHEN** that row is written
- **THEN** `params` SHALL contain no parameter outside the tool's existing allow-list plus the error marker

#### Scenario: A write refused for permission is auditable

- **WHEN** a read-only credential calls a write tool
- **THEN** a `usage_logs` row SHALL be written carrying the `permission_denied` marker, so that the row is distinguishable from a successful write by the same tool

#### Scenario: A raising body is auditable

- **WHEN** a tool body raises an exception
- **THEN** a `usage_logs` row SHALL be written carrying the `tool_exception` marker, the exception's class name, and the duration measured up to the raise

## ADDED Requirements

### Requirement: A tool call whose body raises MUST NOT be lost, and its audit write MUST NOT mask the failure

The tracking decorator SHALL record a tool body's exception before re-raising it, and the record SHALL consist of one ERROR log entry carrying exception information and one best-effort `usage_logs` row; a failure of that row's insertion SHALL be logged and discarded rather than raised, so the caller always receives the original exception. The decorator SHALL catch `Exception` only, so that a cancellation propagates without being recorded as a tool failure and without writing a row.

#### Scenario: The row is best effort, the exception is not

- **WHEN** a tool body raises and the audit insert also fails
- **THEN** the caller SHALL receive the tool body's original exception and the failed audit write SHALL appear only as a warning record

#### Scenario: Cancellation writes nothing

- **WHEN** a tool call is cancelled while its body is running
- **THEN** no `usage_logs` row SHALL be written for that call and no exception record SHALL be emitted for it

#### Scenario: The refusal count on a raising tool is not inflated

- **WHEN** a tool body raises after doing real work
- **THEN** the written row SHALL NOT match the pre-body refusal predicate, so the call's duration SHALL remain in the latency aggregates
