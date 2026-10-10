## ADDED Requirements

### Requirement: Logged usage parameters SHALL be rendered into a storable form before the row is inserted
Every `usage_logs` row written through `write_usage_row` SHALL have its `params` passed through one rendering function, exactly once per write, before the first insert attempt; a row whose `params` key is absent or `None` SHALL be written with it absent or `None` respectively, unchanged. The foreign-key-cleared retry SHALL reuse the rendered values. Rows written through `write_usage_row` are the tracked call's success and refusal rows, the body-exception row, and every coalesced `rate_limited` and `slot_timeout` row, immediate or deferred. The function SHALL be applied after every merge of server telemetry and concurrency observations, so that values `timing` recorded are covered as well as caller arguments.

A top-level `params` value SHALL be stored unchanged unless it contains, at any depth and in any key or value position, at least one of: a string containing U+0000, a string containing an unpaired surrogate code point, a non-finite float, or a value of a type JSON cannot represent. Such a value SHALL be *rendered*: every string inside it, keys included, SHALL be stored with this escape grammar and no other:
- a backslash SHALL become two backslashes;
- U+0000 SHALL become the four characters `\x00`;
- an unpaired surrogate SHALL become `\u` followed by its four lowercase hexadecimal digits.

A non-finite float SHALL become the string `.nan`, `.inf` or `-.inf`, from the shared #154 token helper. A tuple, set or frozenset SHALL become a list. Any other non-JSON value SHALL become its string form, escaped. A non-string mapping key SHALL become its string form (a non-finite float key its token), escaped, and on a collision after rendering the first key SHALL win.

When any top-level value is rendered, the row SHALL carry `params["rendered_params"]`, a sorted JSON array naming each rendered top-level key. When none is, the key SHALL be absent. `rendered_params` SHALL be reserved: no tool parameter, outcome marker or telemetry key SHALL use that name.

The walk SHALL be iterative with no depth limit and SHALL terminate on self-referential input, rendering a container already on the current path as `<cycle>`. The function SHALL NOT raise. A top-level value it cannot render SHALL be stored as `<unrenderable>` and listed in `rendered_params`, and every server marker on the row SHALL be preserved.

Transfer redemption rows (`upload_file`, `download_file`) are written outside `write_usage_row`. They SHALL remain outside this requirement, because their parameters are a path read from a PostgreSQL `text` column and an integer, so none of the listed values can occur in them. A test SHALL pin that the renderer leaves their parameter shape unchanged.

#### Scenario: A NUL in an argument no longer loses the row
- **WHEN** a tracked tool is called with a top-level string argument containing U+0000, against a real PostgreSQL database
- **THEN** a `usage_logs` row SHALL be written for the call, the stored argument SHALL spell the NUL as `\x00`, and `rendered_params` SHALL name that argument

#### Scenario: A nested unstorable value is rendered in place
- **WHEN** a `list_notes` call's logged `frontmatter` argument holds a NUL in a nested key and `NaN`, `Infinity` and `-Infinity` as nested values
- **THEN** the row SHALL be written, with the key escaped by the grammar, the floats stored as `.nan`, `.inf` and `-.inf`, and `rendered_params` equal to `["frontmatter"]`

#### Scenario: The unencodable-argument refusal is itself recorded
- **WHEN** a call is refused with `argument_not_encodable` because an argument the tool **logs** (one of its declared logged parameter names) holds an unpaired surrogate
- **THEN** the refusal's `usage_logs` row SHALL be written carrying the `argument_not_encodable` marker, with the surrogate stored as `\udXXX` and the argument named in `rendered_params`; an offending argument the tool does not log never enters `params`, and its row is written as before without it

#### Scenario: The grammar distinguishes a rendered NUL from a literal escape
- **WHEN** one rendered argument contains both U+0000 and the literal four characters `\x00`
- **THEN** the stored value SHALL spell the first as `\x00` and the second as `\\x00`

#### Scenario: A clean row is unchanged
- **WHEN** a call's parameters contain no unstorable value
- **THEN** the stored `params` SHALL be identical to what was stored before this requirement, and no `rendered_params` key SHALL be present

#### Scenario: Absent and null params are preserved
- **WHEN** a row is written with no `params` key, and another with `params` set to `None`
- **THEN** the first SHALL be inserted without a `params` value supplied and the second with SQL `NULL`, neither becoming an empty object

#### Scenario: Server telemetry is covered
- **WHEN** a tool records a result path containing an unpaired surrogate in its telemetry
- **THEN** the row SHALL be written, with the telemetry key rendered and named in `rendered_params`

#### Scenario: Deep and self-referential input terminates
- **WHEN** the renderer is given a list nested 10,000 levels deep, or a mapping that contains itself
- **THEN** it SHALL return without raising, without a recursion error, and with the self-reference rendered as `<cycle>`

## MODIFIED Requirements

### Requirement: The usage row SHALL be committed asynchronously, and its write contract SHALL be otherwise unchanged
`_insert_usage` SHALL issue `SET LOCAL synchronous_commit = off` as the first statement of its transaction, for both the initial insert and the foreign-key-cleared retry. The usage write SHALL otherwise behave as follows:
- `write_usage_row` SHALL return `True` only after the insert's transaction has committed and is visible to other sessions, and `False` otherwise;
- `write_usage_row_outcome` SHALL perform the identical write and SHALL return `landed`, `unstorable` or `failed` instead of a boolean. `unstorable` SHALL mean that the terminal insert attempt failed with SQLSTATE class 22, with 54000, or with a bare `UnicodeEncodeError` and no SQLSTATE, using the same classification the indexer applies to a poison note. `failed` SHALL cover every other failure, including the writer-permit refusal and connection failures. `write_usage_row` SHALL be exactly `outcome == landed`;
- the writer-concurrency lease and the single FK retry SHALL behave exactly as before. The refusal coalescer's requeue of an unconfirmed row (#193) SHALL behave exactly as before for a `failed` row, and SHALL NOT occur for an `unstorable` row, which the coalescer drops as `mcp-rate-limits` specifies.

`True` SHALL be documented as meaning "committed and visible", not "durable across a database server crash". A crash of the PostgreSQL server or of the host may lose rows committed within the preceding ~600 ms. An application crash or restart SHALL lose no committed row.

#### Scenario: A successful write still reports True
- **WHEN** a tool call completes and its usage row commits
- **THEN** `write_usage_row` SHALL return `True` and a second session SHALL be able to read the row immediately

#### Scenario: A failed write still requeues
- **WHEN** the usage insert fails for a reason other than a foreign-key violation or a data-class rejection
- **THEN** `write_usage_row` SHALL return `False`, `write_usage_row_outcome` SHALL return `failed`, and a coalesced caller SHALL requeue its unconfirmed weight, as before

#### Scenario: A data-class rejection is reported as unstorable
- **WHEN** the terminal insert attempt fails with a SQLSTATE in class 22, either on the initial attempt or on the retry after a foreign-key violation
- **THEN** `write_usage_row_outcome` SHALL return `unstorable` and `write_usage_row` SHALL return `False`

#### Scenario: The FK retry is also asynchronous
- **WHEN** the initial insert fails on a dangling credential and is retried with the credential columns cleared
- **THEN** the retry's transaction SHALL also begin with `SET LOCAL synchronous_commit = off`
