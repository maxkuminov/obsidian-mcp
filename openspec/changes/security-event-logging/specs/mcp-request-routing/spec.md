## MODIFIED Requirements

### Requirement: A refused tool call is recorded in the usage log

A tool call refused for one of the enumerated refusal causes SHALL be written to `usage_logs` like any other tool error, carrying that cause's marker and the same allow-listed parameters as a successful call, and no field outside that allow-list, the marker, and — for a call whose body raised — the exception's class name. The enumerated causes are the three decided before the body runs (a missing vault assignment, an unencodable argument, an exhausted quota), the write refused for a read-only credential (marked `permission_denied`), a body that raised (marked `tool_exception`), and the post-body markers already in the register. Other in-band refusals a tool body returns as a message — a create over an existing path, a path or size validation, a write conflict — are **not** marked by this requirement and remain ordinary rows. Each marker SHALL be classified as pre-body or post-body when it is introduced, and two branches on opposite sides of that line SHALL NOT share a marker value.

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

#### Scenario: An unenumerated in-band refusal is an ordinary row

- **WHEN** `create_note` refuses because a note already exists at the path
- **THEN** the row SHALL be written as an ordinary call with no error marker, as it is today

## ADDED Requirements

### Requirement: A tool call whose body raises MUST NOT be lost, and its audit write MUST NOT mask the failure

The tracking decorator SHALL record a tool body's exception before re-raising it, and the record SHALL consist of one ERROR log entry carrying exception information and one best-effort `usage_logs` row whose insertion reports success or failure to the handler; a failed or interrupted insertion SHALL be logged and discarded rather than raised, so the caller always receives the original exception. The decorator SHALL guard only the tool body's invocation, so that a failure of an admission gate before it, or of the parameter and logging work after a body has completed, is never recorded as a tool exception. It SHALL catch `Exception` for the body, so that a cancellation propagates without being recorded as a tool failure and without writing a row.

#### Scenario: The row is best effort, the exception is not

- **WHEN** a tool body raises and the audit insert also fails
- **THEN** the caller SHALL receive the tool body's original exception and the failed audit write SHALL appear only as a warning record

#### Scenario: A completed write is never reported as failed

- **WHEN** a write tool completes and publishes, and the usage-logging work that follows then raises
- **THEN** no row SHALL carry the `tool_exception` marker for that call and no exception record SHALL be emitted for it

#### Scenario: Cancellation writes nothing

- **WHEN** a tool call is cancelled while its body is running
- **THEN** no `usage_logs` row SHALL be written for that call and no exception record SHALL be emitted for it

#### Scenario: The refusal count on a raising tool is not inflated

- **WHEN** a tool body raises after doing real work
- **THEN** the written row SHALL NOT match the pre-body refusal predicate, so the call's duration SHALL remain in the latency aggregates
