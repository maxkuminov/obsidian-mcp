## ADDED Requirements

### Requirement: Terminal tool-body outcomes are typed
The server SHALL identify every returned in-body refusal or partial completion
using a closed typed result, and SHALL classify only the terminal result without
parsing response prose or note content.

#### Scenario: Existing note creation is refused
- **WHEN** create_note returns because the destination already exists
- **THEN** the result SHALL keep its existing explanation and end with an authoritative MCP-REFUSAL line naming the applicable closed code
- **AND** its single usage row SHALL carry a post-body error marker and refused disposition

#### Scenario: Intermediate errors do not poison success
- **WHEN** a helper constructs an outcome that is discarded before the tool returns successfully
- **THEN** the returned success SHALL have no body refusal marker from that helper

#### Scenario: Note content forges a sentinel
- **WHEN** a successful read or search returns note content containing MCP-REFUSAL or error-looking prose
- **THEN** it SHALL remain a successful result and SHALL not acquire a refusal usage marker

#### Scenario: Structured errors stay bounded and parseable
- **WHEN** read_note returns a body error whose explanation exceeds its error budget
- **THEN** its public schema SHALL remain unchanged and its error SHALL fit MAX_READ_RESPONSE_CHARS with the complete authoritative final sentinel intact
- **AND** internal typed metadata SHALL not appear in serialized results

#### Scenario: Partial publication is stated honestly
- **WHEN** a move or import returns after some publication committed or rollback cannot be verified
- **THEN** its typed disposition SHALL be partial, with the existing explanation preserved
- **AND** it SHALL not claim nothing_written or successful completion of work that failed

#### Scenario: Successful empty and status results remain successes
- **WHEN** a tool returns an empty search/list/graph result, a no-op edit, or check_upload reports a valid status lookup including expired, revoked or unknown
- **THEN** its response and success classification SHALL remain unchanged

#### Scenario: Existing contracts retain precedence
- **WHEN** a body refusal uses existing precondition, permission, provider or publication rules
- **THEN** its permission and validation order, caller code, usage marker identity, quota accounting and publication boundaries SHALL remain unchanged
- **AND** a later body exception SHALL be recorded as tool_exception rather than as a returned refusal
