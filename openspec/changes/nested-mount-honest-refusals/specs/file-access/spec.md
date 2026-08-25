# file-access — delta for nested-mount-honest-refusals

## ADDED Requirements

### Requirement: `delete_file` inherits the accurate cross-mount soft-delete refusal

`delete_file(path, permanent=False)` SHALL refuse a soft delete whose source directory is on a different mount than the vault root's `.trash/` with the same mount-boundary error the note tools use — naming the mount layout as the cause and `permanent=True` as the workaround — because it reaches the same soft-delete primitive; it SHALL NOT report the failure as `.trash/` lacking non-replacing-rename support. `permanent=True` SHALL be unaffected: a permanent unlink crosses no mount boundary and remains the working path on such a layout.

#### Scenario: Soft-deleting a raw file on a nested mount

- **WHEN** `delete_file("M/photo.png")` runs where `M/` is a mount beneath the vault root
- **THEN** the tool SHALL refuse with the mount-boundary error naming the mount layout and `permanent=True` as the workaround
- **AND** the file SHALL be untouched and nothing SHALL be created in `.trash/`

#### Scenario: Permanent delete still works across the boundary

- **WHEN** `delete_file("M/photo.png", permanent=True)` runs on the same layout
- **THEN** the file SHALL be unlinked exactly as before this change
