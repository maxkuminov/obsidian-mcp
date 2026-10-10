## ADDED Requirements

### Requirement: A dropped refusal row SHALL be recorded once, with the weight it carried
The server SHALL invoke the security-event emitter exactly once for `usage_refusal_row_dropped`, at warning level, each time the refusal coalescer drops a planned row whose write was `unstorable`. The record SHALL carry only `tool`, `reason` (the row's marker, `rate_limited` or `slot_timeout`), `count` (the row's `1 + suppressed`) and `user_id` when the row is attributed. Its suppression subject SHALL be the subject `usage_log_failed` uses for the same row. Whether the record reaches the sink SHALL remain subject to the global per-subject suppressor. These field names already carry bounds in the formatter's allow-list, and this requirement SHALL NOT add or widen a field bound. The record SHALL NOT carry the row's parameters, the exception's message or a traceback, because the driver's message quotes the bound parameters. A failure to emit SHALL NOT raise into the request path or the housekeeping tick. The event SHALL be catalogued in `EVENT_FIELDS` and in the architecture note's event table.

#### Scenario: A drop is recorded with its weight
- **WHEN** the coalescer drops an unstorable planned row that represented four refusals
- **THEN** exactly one `usage_refusal_row_dropped` emission SHALL occur, with `count` 4 and the row's tool and marker

#### Scenario: Nothing outside the allow-list is written
- **WHEN** the dropped row's parameters contain note content and a vault path
- **THEN** the record SHALL contain neither, and no field outside its declared four

#### Scenario: A requeued row is not a drop
- **WHEN** a planned row's write fails for a non-data reason and is requeued
- **THEN** no `usage_refusal_row_dropped` emission SHALL occur
