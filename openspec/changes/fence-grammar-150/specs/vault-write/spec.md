## ADDED Requirements

### Requirement: Section addressing hides code per the code-masking grammar

Section resolution for `edit_note(section=…)` and `read_note(section=…)` SHALL scan text masked by the shared masker under the `code-masking` capability's CommonMark-subset fence grammar, so no heading inside any recognised fenced block is selectable, occupies a `#N` ordinal, or bounds a neighbouring section. The re-addressing consequence is declared: on a note containing a fence shape the previous masker missed (indented opener or closer, longer closer, unterminated fence), `#N` ordinals emitted before this change MAY shift, because a line inside code stops counting as a heading.

#### Scenario: An indented fence no longer exposes a heading to section writes

- **WHEN** a note is `# A\n   ```\n# Hidden\ntext\n   ```\n# B\nb\n` and a client calls `edit_note(section="#1", content="new")`
- **THEN** the section `A` SHALL be the entire span through the masked block (its body ending before `# B`), the write SHALL replace that whole body, and `# Hidden` SHALL NOT be selectable by any selector nor occupy an ordinal — `#2` SHALL resolve to `# B`

#### Scenario: A longer-closed fence is one opaque span

- **WHEN** a note is `# A\n```\n# Hidden\n````\n# B\nb\n`
- **THEN** section resolution SHALL treat the fenced span as body of `A`, and a write to `A` SHALL replace it whole rather than splitting at `# Hidden`

#### Scenario: No heading below an unterminated fence is selectable

- **WHEN** a note opens a fence that is never closed, with `#`-prefixed lines below it
- **THEN** none of those lines SHALL be selectable sections, and the truncation outline SHALL NOT list them

#### Scenario: The outline and the resolver agree after the grammar change

- **WHEN** `read_note` emits a truncation outline for a note containing any newly-recognised fence shape
- **THEN** every `#N` in that outline SHALL resolve, via `read_note(section=…)` and `edit_note(section=…)`, to exactly the heading the outline listed
