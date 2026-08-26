# Vault tools: writes, reads, section addressing, size caps

> Deep rationale extracted from `CLAUDE.md`. Read before touching any note or file tool — this is the destructive-write surface.

## Write tools
- `create_note(path, content)` — create a new note (atomic write).
- `edit_note(path, content, append=False, operation=None, find=None, section=None, replace_all=False, dry_run=False, replace_frontmatter=False)` — four mutually exclusive modes (full-replace, append, find/replace, section). `dry_run` returns a unified diff without writing; `replace_all` lifts the single-match guard for `find`. Section mode matches ATX headings only and supports `Parent/Child` path-style and `#N` ordinal disambiguation (see "Section addressing" below). Full-replace **preserves an existing valid frontmatter block** and section mode never touches one — see "Frontmatter is preserved unless the caller says otherwise" below.
- `move_note(from_path, to_path, rewrite_links=False)` — rename or relocate a note. Updates `notes_metadata.file_path` and `note_links.target_path` rows for the moved note. With `rewrite_links=True`, also rewrites `[[Old]]` / `[[Old|alias]]` / `[[Old#anchor]]` / `![[Old]]` / `[[folder/Old]]` **and markdown `[text](Old.md)`** forms (the markdown href is regenerated relative to the *linking* note's folder), in every backlink source **and in the moved note's own body** — `rewrite_sources` starts as `[from_rel]`. The rewrites run *after* the `renameat2` commits, so one that fails leaves a **partial success** naming the unrewritten sources (`_rewrite_failure_warning`); the move is not rolled back. The docstring must say so — "the link graph never disagrees with the vault bytes" is only true of the preflight refusal, not of the whole call.
- `delete_note(path, permanent=False)` — soft-delete to `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>` by default (the same non-replacing `renameat2` `delete_file` uses; the hex suffix is what makes two same-second deletes distinct); `permanent=True` unlinks through the parent descriptor. The indexer skips dot-dirs, so search/embedding cleanup happens on the next reindex pass.
- `set_frontmatter(path, updates, remove=[])` — structured YAML frontmatter mutation. Round-trips via `yaml.safe_dump` (does not preserve YAML comments). Leaves the body byte-identical. **Refuses a malformed block by name** rather than prepending a second one; only an effective mutation writes.

All write tools route through `src/services/vault.py::_atomic_write_at`, which stages the payload, `fsync`s it, publishes it **relative to the parent descriptor opened at validation**, and `fsync`s that directory afterwards — a crash mid-write cannot truncate the destination, and nothing that happens to the pathname meanwhile can redirect the write. The two publish shapes are not the same and the docstrings must not conflate them: an **overwrite** (`edit_note`, `set_frontmatter`, `write_file(overwrite=True)`) stages a named temp file in the destination's own directory and publishes it with a same-directory `os.replace`, while a **no-clobber** write (`create_note`, `write_file` by default) stages an unnamed `O_TMPFILE` inode and publishes it with `linkat`, refusing on a filesystem without `O_TMPFILE` unless `VAULT_ALLOW_NAMED_STAGING_FALLBACK` is set.

### Frontmatter is preserved unless the caller says otherwise

Issue #128. `read_note` strips the YAML block and full-replace wrote exactly
what it was given, so the natural agent read-modify-write — read a note, edit
the content portion, pass it back — **silently deleted the frontmatter**. In
the same family, `set_frontmatter` over a malformed block prepended a *second*
`---` block above the broken one and reported success, and `remove=` no-oped.
Destructive and silently-wrong writes, the class this product ranks highest.

- **`content` is the new body; a valid line-1 block is kept byte-identically
  ahead of it.** The separator is one `\n`, inserted **only** when the block
  slice does not end in a newline (a metadata-only note whose closing fence
  sits at EOF) and `content` is non-empty. The slice is the parser's own
  computed span — never `raw[:-len(body)]`, which is wrong for an empty body.
- **`content` is never classified, and that is the whole design.** Three audit
  rounds broke every attempt to infer intent from content shape: a line-1 `---`
  test breaks on bodies opening with a thematic break, a complete-valid-block
  test breaks on a stripped body that itself opens with a mapping-shaped fenced
  block — which is exactly what `read_note` returns for such a note. Intent is
  asked for instead: **`replace_frontmatter=True`** replaces the whole file
  (today's behaviour, now opt-in) and is the only way to drop or repair a
  block. It is in `edit_note`'s `_tracked` allow-list, because it is the
  difference an operator needs to see after a block goes missing. Combined with
  `append`/`find`/`section` it is the multi-mode error; with
  `operation="replace"` it is not, since both name full replacement.
- **A note with no valid block — absent *or* defective — is replaced wholesale
  by default.** There is nothing valid to preserve, and this keeps the repair
  path open without the flag.
- **Section mode resolves over the frontmatter-stripped body and reattaches the
  block**, restoring the read/write selector parity the spec already promised:
  a YAML `#` comment is never selectable and never counted by an ordinal, which
  it was on the write side. Over a **defective** block a section write is
  **refused by name** — resolving over raw bytes there lets a `#` line inside
  the broken block be selected and lets the replacement span swallow the
  closing fence. Reads are deliberately asymmetric: `read_note` still extracts
  from such a note, because a read destroys nothing.
- **`set_frontmatter` diagnoses before the empty-`updates`/`remove` no-op**, so
  a broken note is reported broken even for a call that would have changed
  nothing. Unclosed fence, YAML error and non-mapping (list, scalar, `null`,
  `~`, comment-only) each refuse naming the defect and the
  `edit_note(replace_frontmatter=True)` repair; `remove=` refuses identically.
- **Only an *effective* mutation reaches the serializer, compared
  type-sensitively.** Plain `==` conflates `True` with `1`, which would report
  a real type change as a no-op. The guard is also what stops a
  remove-of-nothing from dropping a valid **empty** block —
  `serialize_frontmatter({}, body)` emits no fences, so on a note whose body
  opens with a mapping-shaped fenced block that drop would promote the body
  prefix into active frontmatter. The guard compares the **final** mapping
  against an untouched deep copy, not the per-key bookkeeping: an update and a
  removal that cancel (`updates={"temp": 1}, remove=["temp"]`) record two steps
  and arrive back where they started, and the step-counting version waved that
  through into the serializer. Removing the last *actual* key does remove the
  block: no fences, no separator, exactly the prior body — and **if the body's
  own first lines are a mapping-shaped fenced block, that prefix becomes the
  note's active frontmatter.** Spec-mandated and caller-requested, unlike the
  accidental version above, but declared here because the outcome is the same
  shape.
- **Equality is type-sensitive, order-sensitive and signed-zero-sensitive.**
  `True == 1` and `-0.0 == 0.0` are both True in Python and both write
  different YAML, so either would report a real change as a no-op; floats go
  through `float.hex()`, which is exact for finite values and renders every NaN
  as `'nan'` (two NaNs are therefore the same value — YAML round-trips both to
  `.nan`). Mappings compare in order, because `safe_dump` runs with
  `sort_keys=False` and key order is part of the note's bytes. The walk
  memoizes visited `(id(a), id(b))` container pairs and treats a revisit as
  equal — a recursive YAML alias (`a: &A [*A]`) is valid input that
  `safe_load` returns as a self-referencing list, and without that even a
  remove-of-nothing raised `RecursionError`. Sound because `all()`
  short-circuits: an unequal pair propagates out before anything revisits it.
- **One partition, shared by read and write.** `parse_frontmatter` and
  `parse_frontmatter_diagnose` both call `_partition_frontmatter`, so a block
  `read_note` strips can never be diagnosed differently by a tool about to
  write. `parse_frontmatter` gained **exactly one** behaviour change:
  whitespace-only fenced YAML is a valid empty mapping for *every* consumer.
  That has to be shared — leaving the read side treating `---\n---\n` as
  absent while the write side preserved it makes the read-body round trip
  *duplicate* the block. The predicate is whitespace, tested **before** the
  YAML call: PyYAML refuses a bare tab, so asking it would make `---\n \n---\n`
  valid and `---\n\t\n---\n` a parse error.
- **A line ends at LF, CRLF or a lone CR, in the partition and in the heading
  scan alike.** `read_file` applies universal-newline translation, so a
  classic-Mac note reaches the read parser as LF and its block is recognized;
  the write paths read raw bytes. While either predicate knew only `\n`, the
  same file was stripped on read and diagnosed *absent* on write — full-replace
  deleted the block and `set_frontmatter` prepended a second one — and `.`
  matching `\r` made the whole file one line, so `## A\rold\r## B` scanned as a
  single heading running to EOF. `(?m)^…$` cannot express either rule in
  Python; both use explicit `(?:\A|(?<=\n)|(?<=\r))` / `(?=\r|\n|\Z)`
  boundaries, and CRLF is always matched before the bare CR alternative so a
  terminator is never split down the middle. **All three must move together** —
  the partition, the heading scan, and the fenced/inline code masker the scan
  runs on (`src/services/links.py`): fixing the partition alone turns section
  mode's safe "no ATX headings" refusal into a write against a bogus heading,
  and leaving the masker behind lets a heading inside a `~~~` block be selected
  on the write side while `read_note` hides it — the replacement then deleting
  the closing fence — while inline code, whose class ran across `\r`, joined
  two lines and masked a *real* heading. The masker is offset-stable by
  contract: every substitution is exactly as long as what it replaces, because
  heading positions and `ExtractedLink.position` index the unmasked text.
- **Widening the terminator rule must never narrow a `\s`.** The heading
  separator is `[^\S\r\n]+` — all whitespace except the three terminators —
  because `[ \t]+` drops a heading whose marker is followed by an NBSP, and a
  dropped heading shifts every later `#N` ordinal on an existing vault. The
  closing code fence still keeps the original `\s*`, line-crossing included.
  Only `$` is generalized, to "before any terminator, or end". A 45,630-input
  LF/CRLF differential over masking, parsing, outlines, links, extraction and
  replacement holds at **0 divergences**; that is the compatibility envelope,
  and it is what any future change to these patterns has to re-establish.
- **The heading's *trailing* run no longer keeps `\s*` — that constraint was
  deliberately lifted in #140 (2026-08), and this bullet is its history, not a
  live prohibition.** It used to read: the trailing run keeps the original
  `\s*`, line-crossing included, because `_section_body_span` compensates for
  it and narrowing it would change the bytes `edit_note(section=…)` writes on
  ordinary LF notes. That reasoning held the zero-divergence envelope above
  and was wrong about the compensation: the trailing run is what set
  `line_end`, so a heading followed by blank lines — or by a fenced block,
  which the masker turns into a run of *spaces* the run happily crossed —
  produced a `line_end` far past the heading line. The read
  (`extract_section`, from `line_start`) returned that region and the write
  (`replace_section`, from `body_start`) could not replace it, so a
  read-modify-write round trip duplicated it: a fence came back twice, blank
  lines grew +1, +2, +4 over three round trips. The run is now
  `[^\S\r\n]*`, and the load-bearing invariant is stated once: **a section's
  body begins at the first byte of the line immediately after the heading
  line.** See "Section addressing" below for the compat break that bought,
  and re-establish the differential envelope before touching these patterns
  again.
- **Declared staleness.** Notes already indexed under the old empty-block
  partition (the block surfacing as literal body text, and `note_links.position`
  measured against it) do not self-heal on an ordinary pass — change detection
  hashes the raw bytes before parsing, so unchanged bytes skip the reparse. The
  artifact is cosmetic and vanishingly rare; it heals on the note's next
  hash-changing edit or under the explicit per-index rebuilds
  (`make rebuild-tsvectors`, reset/re-embed). A parser-revision invalidation
  mechanism is not worth building for it.
- **The round-trip guarantee is scoped, and both layers' docstrings say so:**
  it covers a complete, unwindowed whole-note read only (`section=None`,
  `offset=0`, no `[TRUNCATED]`). A truncated read must be paged to the end
  first, and a `read_note(section=…)` response **includes the heading line**
  while `edit_note(section=…)` takes the body only.

### Mutations act on the path as named — never through a symlink

`validate_path` returns `(vault / rel).resolve()`, which **follows symlinks**. Every mutating tool used to act on that resolved path, so an in-vault alias `alias.md -> important.md` made `edit_note("alias.md", …)` rewrite `important.md` and report success for `alias.md` — a destructive write on a path nobody named (#54).

`open_mutable(rel, user_id)` in `src/services/vault.py` is the guard (`validate_mutable_path` is its single-shot form, kept for callers that only need the verdict), and it is what `write_file` / `write_bytes` validate with, so every mutation entry point is covered:

- the **parent** is resolved and must stay inside the vault. Symlinked *directories* inside the vault (shared attachment folders — a common Obsidian setup) therefore keep working; an ancestor pointing out of the vault is still the traversal error.
- the **final component is taken as named** and `os.lstat`-ed. A symlink — dangling included — is refused with an error naming the link's canonical vault-relative target ("`outside the vault`" when it escapes), so the agent can operate on the real note.
- it returns `resolved_parent / name` as `path` (and its vault-relative form as `rel`): the real directory entry the indexer sees. `move_note` takes `from_rel` / `to_rel` from the targets, so `notes_metadata.file_path`, `note_links` and backlink discovery agree with the filesystem for notes under a symlinked folder.

Applies to `create_note`, `edit_note` (all modes — `dry_run` refuses too, rather than diffing a note the caller did not name), `set_frontmatter`, `move_note` (source and destination), `delete_note` and `write_file`. `delete_file` already refused, via the anchored `vault_fs` walk.

**Reads are deliberately unchanged.** `read_note`/`read_file`/`list_*`/graph tools still follow links — an alias reading as its target is what a user expects from an alias, and a read cannot destroy anything.

### Resolve once, open the parent, then act on the descriptor (#59)

Resolving the parent at validation only helps if the rest of the tool never asks the kernel to walk that pathname again — and a `Path` *is* a pathname. `validate_mutable_path` returning one left a live race: a process that renamed the resolved parent directory and dropped a symlink at its name, or repointed the directory behind a symlinked vault root, **between two syscalls of a single write** sent the write to a directory nobody validated. `expected=` cannot catch it, because the decoy may hold byte-identical bytes.

So `open_mutable(rel, user_id)` is now the entry point for every mutation. It runs the same guard and additionally hands back a **`MutableTarget`**: the resolved `path`, the vault-relative `rel`, the final component `name`, and an **open parent directory descriptor**. Staging, the `expected=` read, publication, the permanent unlink and the `.trash` rename all run relative to that descriptor, so no pathname is resolved after validation. A directory descriptor keeps naming the same directory however its pathname is later renamed or relinked.

- **The root descriptor is opened first and never reopened by name.** Resolving the root to a pathname and only then opening that pathname left the whole guard resting on a name: the resolved root could be renamed away and a symlink left at its name in between, and the descriptor everything else anchors to would be a directory containment never saw. Pinning first inverts it — the root is an inode from the start — and `_require_same_directory` then checks that `vault.resolve()` still names that inode, because `rel` and the containment check are computed against the pathname and must describe the directory we pinned.
- The parent is opened from that already-open root fd by **one** kernel-enforced beneath-root lookup — `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)` in `vault_fs.open_dir_beneath` (#87). Resolution of the *pathname* happens first, so in-vault symlinked folders keep working; the lookup itself stays strict, so a component that *became* a link in the interval is refused rather than followed. Relaxing it to follow in-vault links was the other option and was rejected — it re-derives containment per component, which is the check-then-act this change removes, and it would weaken the primitive `/transfer/*` depends on. Two flags it deliberately does not carry: `RESOLVE_NO_XDEV`, which would refuse lookups *through* a mount point beneath the root and so break every read and write that works across a nested mount while fixing none of the three that do not; and `RESOLVE_IN_ROOT`, which scopes `..` chroot-style rather than refusing it — `_split` stays in front and refuses `..`, absolute paths and NUL bytes lexically, because `RESOLVE_BENEATH` *scopes* `..` too (`A/../A` succeeds at the kernel) and nothing here is normalised on the caller's behalf.
- `MutableTarget.dir_fd` creates a missing parent on first use; `parent_fd` never does. Deferring creation out of validation keeps a refused write (an over-cap body) from littering the vault with empty folders, and stops a read helper from `mkdir`ing.
- `write_file_at` / `write_bytes_at` / `read_bytes_at` take the target, not a `Path`. The string-taking `read_bytes` / `write_file` / `write_bytes` are single-shot conveniences that open and close their own target; if you find yourself calling one *after* `open_mutable`, you have reintroduced the bug.
- `_atomic_write_at` stages with `O_CREAT|O_EXCL|O_NOFOLLOW` (a planted symlink on the temp name is an `EEXIST` we step around, never a decoy we truncate) and **`fsync`s the payload before publication** — without that, a crash just after the rename can publish a note whose data blocks never landed.
- **The destination directory is `fsync`ed *after* publication too, and so is the parent of every directory the call created** (#97). The payload flush makes the contents durable and says nothing about the entry that names them, so without this half a crash loses the write entirely; and flushing the note's own parent while the entry naming *that* parent is still only in the page cache loses the whole new folder, so `create_note("New/Folder/x.md")` flushes `New/Folder`, `New` and the root — outward to the first directory that already existed. `ensure_parent` records what it created (a component another writer won the race to is not ours to flush). **A failure of any of those flushes is logged and the write reported as the success it is** — see the asymmetry below; do not "fix" it into an error.
- **The no-clobber publish never exposes a staging name at all — on either write path** (#59 for notes, #92 item 1 for transfers). It stages into an `O_TMPFILE` inode — no directory entry, nothing to observe, replace or race — and publishes it with `linkat` through `/proc/self/fd/<fd>`. That also means there is nothing to clean up: a *named* staging file has to be unlinked, and an unlink is by name, so it can only be guarded by an identity check followed by the removal, which is check-then-act and could delete a substitute planted in between. `O_TMPFILE` removes the step rather than guarding it. **Do not add `O_EXCL`**: with `O_TMPFILE` it means "this file may never be linked into the filesystem", which makes the publish `ENOENT` — the opposite of its usual meaning. Two kernel details that look like blockers and are not: `linkat`'s `AT_EMPTY_PATH` form needs `CAP_DAC_READ_SEARCH` (an ordinary container has none) while the `/proc` magic link does not, and the "cannot link a zero-link inode" rule applies to an inode whose names were *removed*, not to one created `O_TMPFILE`. Verified on the deployment kernel at `CapEff=0`. `vault_fs.create_nameless_temp` / `vault_fs.link_staged_inode` are the one implementation of both halves — `vault.py` holds `_link_staged_inode` / `_proc_fd_available` as aliases, because a second copy is how the two paths drifted apart before. **This describes the mode the probe selects where `O_TMPFILE` works**; where it does not, `UnsupportedFilesystem` names `VAULT_ALLOW_NAMED_STAGING_FALLBACK` and only that flag buys the by-name form back (below).
- The **overwrite** publish is `renameat`, which is inherently by name, so its staged inode must acquire one. The two paths differ in *when*. A note write stages under a name for the whole call (`_create_temp_exclusively`, beside the destination), which is right for a write that completes in one call. A transfer stages unnamed and materialises a transient name **inside the publish gate**, immediately before the fingerprint check and the rename, in `.transfer-tmp` and never in the destination directory — the name then exists for two syscalls in a `0700` directory owned by this process, instead of for a multi-minute body plus an unbounded wait on the gate's row locks (D20). Both then run the same guards: an identity check immediately before the rename that the name still refers to the inode we wrote (`vault._require_staged_name`, `vault_fs.require_staged_name`), narrowing the substitution window to that one syscall; and a discard that unlinks **only** while the name still refers to our inode, otherwise leaving it in place and logging (`vault._discard_temp`, `vault_fs.discard_staged_name`). The failure direction is to leave litter, never to remove something we cannot prove is ours — answering a substitution by deleting the substitute is the same destructive-write class, just aimed elsewhere. The check narrows the window; it does not close it, and a substitution landing between the check and the rename is still published — declared, not a gap.
- **`VAULT_ALLOW_NAMED_STAGING_FALLBACK` is one flag for both write paths, default off** (#103, D27), **and both paths honour it.** Some servers refuse `O_TMPFILE` outright: TrueNAS SCALE's NFS export answers `EOPNOTSUPP` as root, on a second export, under NFSv4.1 and NFSv4.2, and still after a NAS upgrade, while named staging with a `link()` publish works on the same mount. It is deliberately one knob rather than two, because two would permit a deployment with a working `create_note` and a refusing upload — a state nobody chose and nobody can diagnose from either symptom alone. There is no `TRANSFER_*` variant and no per-path override.
  - **Both halves have landed** (the transfer path with the original change; the note path with #103's contributor PR, gate-hardened in #114 and archived in #116). Unset, both paths refuse on such a mount with a message naming the flag; set, the transfer path stages under a name in `.transfer-tmp` and the note path's no-clobber writes (`create_note`, `write_file(overwrite=False)`) stage under a name beside the destination. Note overwrites always staged under a name — a replacing rename has no by-descriptor form — so the flag never governed them. The archived `vault-write` spec states the fallback clauses; the interim wording that only the transfer path honoured the flag is gone with it.
  - **The probe selects the mode, once per root, and it never flips.** `probe_publication` exercises unnamed staging and by-descriptor publication alongside the hard link and the two flushes, and its cached per-root result *records the staging mode*; every publication on that root reads it back. A root that staged one upload without a name and the next one under a name would make the window each upload ran in unknowable after the fact. Flag off and unnamed staging unavailable → the probe raises, so **no token is minted and no body is streamed**. Flag on → the named mode is selected, but only after the primitives *it* needs have been established too; the flag buys back named staging and nothing else, so a root that cannot flush a directory is still refused.
  - **The fallback carries two guards the pre-change transfer path did not have.** Today's publish ran no staged-name identity check and unlinked the staging name unconditionally. The fallback inherits the transient name's guards rather than reverting past them — a name that lives for the whole streaming window needs them more, not less.
  - **The window it reopens is declared, and the two fallbacks are not equal.** A named staging file carries a directory entry for the whole streaming window, so the substitution the unnamed mode closes structurally is open again for that window, narrowed by the identity check. The transfer path stages in `.transfer-tmp` — `0700`, owner-checked, dot-prefixed, skipped by the indexer and refused by every tool's hidden-path guard — so no agent, no capability and no vault tool can reach a staged name and the residual adversary is a process running as this uid, which can rewrite the destination directly and needs no race. The note path stages beside the destination, in a directory the vault's own tools can write to. The transfer fallback's window is the **narrower** of the two; do not document them as equivalent.
  - **It announces itself once, on first exercise.** One `WARNING` per process the first time a call actually stages under a name — not when the flag is set, not when the probe selects the mode — plus `vault_named_staging_fallback_active` on `/health`, one field for both paths. That distinction is the whole value of the signal: it separates an operator who enabled the flag defensively from a mount that is taking the fallback. `/health` reads process state and **never re-probes**; a probe writes.
  - One consequence for the sweep: `.transfer-tmp/.tmp-*` older than 24 hours has nothing *new* to collect in the unnamed mode (the kernel frees an unnamed inode when the last descriptor closes), but an abandoned or killed upload in fallback mode leaves a staged file exactly as the pre-change path did. The sweep stays for both that and pre-change litter.
- No-clobber never degrades to a replacing rename: `EPERM`/`EOPNOTSUPP`/`EXDEV` raise `UnsupportedFilesystem`.
- **Moves that rewrite links are serialised process-wide** (`_MOVE_REWRITE_LOCK`, held across preflight *and* rewrites — exactly the span descriptors are pinned for). Two moves each inside their own budget can still exhaust the table between them, so the bound has to hold for the process, not per call. Plain moves pin two descriptors and are not serialised.
- **Anchoring costs descriptors, and `move_note(rewrite_links=True)` is the one place that matters.** Each *planned* rewrite pins one open parent fd from its preflight read until its post-move write — that single descriptor is what makes the two provably the same directory — and the preflight must finish before the move commits so an over-cap rewrite can still abort it. Sources that turn out to need no rewrite are released at once, and each planned one is released as soon as its write lands, so the peak tracks planned rewrites rather than backlink count. Beyond that the plan is bounded by `config.max_move_rewrite_sources()`, derived from `RLIMIT_NOFILE` (soft limit minus `MOVE_REWRITE_FD_RESERVE`) — **with no floor**, because a floor guarantees the exhaustion the cap exists to prevent on exactly the processes that cannot afford it. Exceeding the budget aborts the move *before any mutation*, and so does an actual `EMFILE`/`ENFILE` during the preflight: running out of descriptors says the plan is too big for this process, not that one source failed, and treating it as a per-source failure moved the note while silently dropping the rest of the rewrites. Same shape as `MAX_MOVE_REWRITE_BYTES`, same reason.
- `move_note` publishes with one `renameat2(RENAME_NOREPLACE)`, not `link` + `unlink`. The old shape could unlink a *different* inode than the one it linked, destroying a file that replaced the source in between. `delete_note` soft-deletes through `vault_fs.soft_delete_at`, sharing that primitive with `delete_file`. **Both parent directories are `fsync`ed after either rename lands** (#97) — the trash directory counts as one of them — as is the parent of a permanent unlink; every one of those flushes is logged and swallowed, never reported.
- **`move_note` identifies what it moved, before it moves it.** `renameat2` relocates whichever inode is at the source when it runs — the property that keeps a replacement from being destroyed — so the regular-file check that ran before the preflight does not bind the commit. `_pin_source_inode` takes an `O_PATH|O_NOFOLLOW` fd of the source first (it works for a link or a directory too, and has no side effects), and `_verify_the_moved_inode` compares it with an `lstat` of the destination through its parent fd. Three outcomes, and the last distinction is the point: our inode and a regular file → the move stands; **our** inode but a directory or a symlink → roll back with a second `RENAME_NOREPLACE` (as `soft_delete_at` does) and refuse; **not** our inode, or unidentifiable → report where things are and roll back *nothing*, because moving it away would relocate a third party's file on the strength of a name. The database is never updated on any refusal, and every post-rename failure becomes an explicit result — by then the file has been published somewhere and a traceback would leave the caller with no idea where. Unlike the soft delete, a **symlink** is refused too: a link is inert in `.trash` but not at a move destination.
- The leaf is re-`lstat`ed through the parent fd, and a leaf that became a symlink between validation and the act is **named as one** by every mutating tool. The read-modify-write tools (`edit_note`, `set_frontmatter`, `delete_note`, `move_note`'s source) and `write_file` check before acting, via `_leaf_state_error`; the creating tools (`create_note`, `write_file`, `move_note`'s destination) check on the no-clobber refusal, because `link`/`renameat2` reject a plain file, a directory and a symlink with the same `EEXIST`. Neither "not found" (which invites the agent to create it, over the link) nor a bare "already exists" nor a silent success is acceptable: `write_file(overwrite=True)` would otherwise replace the link and report "Wrote N bytes" for an alias the caller still believes in.

**The accepted residual, precisely.** The claim this is entitled to, in the words every artifact of #87 uses: **every below-root directory descriptor a call uses as a pathname anchor comes from a lookup the kernel proved beneath the vault root at the moment it resolved, and no directory descriptor retained from a creation descent is ever returned to a caller or used as a pathname anchor — so no operation is ever redirected into a directory that was never beneath the root.** Scope that exactly: it is about **directory** descriptors used as pathname anchors. A call's own staged payload descriptor is created by that call and published through by descriptor, and never anchors a pathname, so the broader "no descriptor whose containment the kernel did not check is ever acted through" is false and must not be written anywhere. Nor is "nothing outside the root is ever written" — that was the claim review rejected, and the two bullets below say why. What remains is substitution at the leaf, plus what a lookup structurally cannot promise:

- the leaf can be swapped for a symlink between the guard's `lstat` and the read or write. `O_NOFOLLOW` turns that into `ELOOP`, which the tools report; the link is never followed and nothing outside the named directory is touched.
- **an adversary who can write to the destination directory itself** can still win the `renameat` race on an overwrite publish. Say it plainly: that adversary can also just edit the note directly, so it is outside the threat #59 addresses — redirection through an *ancestor* or the *root*, where the attacker never had access to the destination at all. The no-clobber publish has no such window (nothing it stages is ever named), unless `VAULT_ALLOW_NAMED_STAGING_FALLBACK` is set, which is the operator's declared decision to take it back.
- **creating a missing directory has no beneath-root form** (#87, D22). The *lookup* window is gone: `vault_fs.open_dir_beneath` is one `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)`, so there is no interval between components for an ancestor rename to exploit. But `mkdirat` has no such form, and no syscall creates a directory *and* proves the path it created it under stayed beneath a root. So creation is still one component at a time — carrying **no** descriptor across a creation: each `mkdirat` goes through a fresh beneath-root lookup of the prefix that already exists, dropped at once, and the descriptor the caller finally gets comes from a fresh lookup of the whole path performed *after* the creation. What a race can still cost is at most one **empty** directory per component, **per creation descent**, in a place the winner already controls — never a file, never file content, never something a tool reports success about. The bound is per descent and one call can have more than one: an upload walks its destination twice with creation enabled (a cheap up-front walk so a bad path costs one syscall rather than a whole body, and the authoritative walk inside the publish gate), a note write once. **Do not try to clean it up** — an `rmdir` by a name the caller chose is the same delete-the-substitute hazard `_discard_temp` and `soft_delete` refuse.
- **a lookup proves containment when it resolves, not afterwards** (#87, D26). `RESOLVE_BENEATH` proves the path stayed beneath the root *during that resolution* and says nothing about the future — it cannot: the descriptor it returns keeps naming the same directory however that directory's pathname is later renamed, which is the entire reason #59 anchors to a descriptor rather than a name. So between the final lookup and the publish — the transfer gate's destination walk to its `linkat`/`renameat`, a note tool's `open_mutable` to its publish — a process that can rename a vault ancestor can move the resolved directory out of the vault, and the call publishes into it, wherever it now is, and reports success. Nothing was *redirected*: the bytes went to the directory the caller named, which somebody else moved. Excluding it would need an operation the kernel does not offer, and re-verifying by walking `..` upward from the descriptor is check-then-act one level up — the class of bug #59 exists to remove. **Retained, not introduced**: the per-component walk had this interval too, underneath the larger window it did not close.
- a **read-modify-write overwrite** (`edit_note`, `set_frontmatter`, and `move_note`'s link rewrites) is optimistic, not linearizable: `expected=` compares the current bytes immediately before the rename and a writer landing inside that window is still overwritten — the same guarantee level as the transfer fingerprint check, declared rather than implied.
- **`write_file(overwrite=True)` has no conflict detection at all** — it is an unconditional replace, and deliberately so: the raw byte tool takes whole-file content from the caller and has no prior read to compare against. Do not read `expected=` into it. `request_upload(overwrite=True)` is the path that *does* bind to the incumbent's fingerprint; use it when the caller cares that the file has not changed.
- The **no-clobber** publish (`create_note`, `write_file` by default) has no window at all (`link()` is kernel-atomic), and neither does the soft delete or `move_note`'s own publication (one `renameat2(RENAME_NOREPLACE)`).

All of these are properties of concurrent editing, of an attacker who already holds the destination directory, or of what the kernel offers — not of path resolution: the write always lands on the path the caller named, in the directory that was validated.

An in-vault `..` (`Folder/../note.md`) is also refused by `validate_mutable_path` — a mutating tool never resolves a component away — but with a message naming the normalised path rather than "Path traversal denied", which would be a lie the caller cannot act on. Reads still resolve `..`.

## File-access tools (non-markdown)
Raw read/write/browse of arbitrary vault files, distinct peers to the note tools (note tools stay markdown-only). Pure byte transport — no server-side PDF/text extraction, no embedding or indexing of non-markdown files.
- `read_file(path, encoding="auto", offset=0, limit=None)` — `auto` resolves text-like MIME → text, image → inline MCP image content block (renders in-client), everything else → base64 string. `text` forces UTF-8 decode (errors on non-UTF-8); `base64` forces raw-bytes base64. Capped by `MAX_FILE_READ_BYTES` (default 10 MB), checked against on-disk size before reading. Text results are additionally bounded by `MAX_READ_RESPONSE_CHARS` and page via `offset`; base64 and image results are not windowed. Base64 reads are token-heavy — check size with `list_files` first.
- `write_file(path, content, encoding="base64", overwrite=False)` — `base64` decodes `content` to raw bytes; `text` writes UTF-8. No-clobber by default (`overwrite=True` to replace), auto-creates parent dirs, atomic via `vault.write_file`. Capped by `MAX_FILE_WRITE_BYTES` (default 25 MB) on decoded length.
- `list_files(folder=".", pattern="*", recursive=False, limit=200)` — `ls`-style: immediate children (subdirs + files) by default, each file with size + mtime; glob-filterable; capped at `limit` with a truncation note.

- `delete_file(path, permanent=False)` — soft-deletes to `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>` through the anchored helper; `permanent=True` unlinks. Refuses `.md` (pointing at `delete_note`), directories and symlinks. The `.md` refusal runs on the **canonical** final component, so `note.md/.`, `a//note.md` and `NOTE.MD` are refused too — the caller's string is not the path.

`write_file` additionally goes through `validate_mutable_path`, so it refuses a symlinked final component the way the note tools do (see "Mutations act on the path as named" above) — `overwrite=True` cannot clobber a file through an alias. `read_file` and `list_files` still follow links.

All four enforce the same traversal guard and the same dot-dir guard (`is_hidden_path`, rejecting any path component starting with `.` — the indexer's visibility rule, keeping `.obsidian`/`.git`/`.trash`/`.smart-env` out of reach), but **not through the same validator**: `read_file` and `list_files` use `validate_visible_path` (which resolves, so links are followed), `write_file` uses `validate_mutable_path` (parent resolved, symlinked leaf refused), and `delete_file` canonicalises lexically and resolves through `vault_fs`'s beneath-root lookup. Vault helpers (`read_bytes`, `write_bytes`, `list_dir`, MIME classification) live in `src/services/vault.py`. MIME detection uses stdlib `mimetypes` plus a magic-byte sniff for PNG/JPEG/GIF/WebP. `read_file` is the first tool returning a non-`str` MCP content object.

## Three kinds of size cap — don't confuse them

There are **byte** caps, a **character** cap, and a **transport** cap, and they protect different things:

- `MAX_FILE_READ_BYTES` / `MAX_FILE_WRITE_BYTES` bound what the **server** reads into or writes out of memory. They refuse the operation.
- `MAX_READ_RESPONSE_CHARS` (default 40,000 ≈ 10K tokens) bounds what `read_note` / `read_file` **return to the caller**, whose context the result consumes. It truncates rather than refusing.
- The MCP streamable-HTTP **request body limit** bounds what the transport accepts at all, before any tool runs. It is derived, not configured: `max(2 × MAX_FILE_WRITE_BYTES, 6 × MAX_NOTE_BYTES) + 1 MiB` (61 MiB with the defaults), passed to `FastMCP(max_request_body_size=)` from `Settings.mcp_max_request_body_bytes`. The SDK's own default is 4 MiB, which would silently reject writes far below our documented 25 MB cap. The formula guarantees — for a *canonical* envelope, i.e. JSON-RPC framing plus non-content arguments under 1 MiB — that a base64 `write_file` at the cap (base64 is `4·⌈n/3⌉ ≤ 2n + 2`) and any note write up to `MAX_NOTE_BYTES` (JSON escaping expands a byte at most 6×) always reach the tool, which then decides. Unsupported shapes are bounded by the transport with a bare HTTP 413 and no tool error: text-mode `write_file` whose escaping exceeds the limit (send base64 — always safe), an envelope over 1 MiB, and arguments that are large but discarded.

**Every note write tool caps its own result.** `create_note`, `edit_note` (all modes, `dry_run` included), and `set_frontmatter` refuse a result over `MAX_NOTE_BYTES` with a tool-level error and no write, via `_note_size_error()` in `tools.py`. That is what keeps the tool, not the transport, in charge of every supported write — the transport limit sits deliberately above every tool cap. `MAX_NOTE_BYTES` lives in `src/config.py` (not `tools.py`) because the transport formula needs it.

**Precisely: it is a per-component budget, not a single ceiling on the whole response.** The content window is bounded by the cap, and the heading outline is *independently* bounded by the cap. A truncated whole-note read can therefore carry both, so the worst-case response is about `2 × cap` plus the fixed notice prose — not `cap`. Every component must have a budget; if you add a third, give it one, and update the worst case here and in the end-to-end test.

Satisfying the byte caps says nothing about the response. A 3 MB note is well inside the 10 MB read cap and will still exhaust a context window — that is exactly how this bit us: `read_note` had no cap at all and returned a 3.4 M-char tool result, which the caller's inference provider rejected as "input exceeds the context window". `read_note` goes through `read_file()` in the vault service, not `read_bytes()`, so it never even had the byte cap.

**If you add a tool that returns file or note content, it needs the character cap too.** `_window()` and `_capped_text()` in `tools.py` are the shared helpers.

Over-cap reads return the first window, a `[TRUNCATED]` notice with the exact continuing `offset`, and — for a whole-note read — a section outline. `limit` may lower the cap for one call but never raise it; raising is an operator decision via the env var.

## Section addressing

`read_note(section=…)` and `edit_note(section=…)` share one resolver in `src/services/vault.py`, so a selector names the same section for both. Three forms:

1. Ordinal — `"#7"`, the 7th ATX heading in document order, 1-based. Checked **first**.
2. Path-style chain — `"Parent/Child"`, ancestors outermost-first. A selector containing `/` never takes the ordinal branch.
3. Exact heading text — `"Tasks"`.

The ordinal exists because **path-style cannot disambiguate duplicate siblings**: two `## Report.xlsx` under the same parent share every ancestor, so no chain separates them. Bulk-extraction notes are full of these.

**A bare `#N` always wins over a heading literally titled `#N` — don't "fix" this by preferring text.** The outline we emit on truncation advertises ordinals as the reliable selector; if note content could shadow one, the section we just told the caller to fetch by `#2` would be unreachable by `#2`. Text-first was the original implementation and pre-merge review caught it. The literal heading loses nothing: it stays reachable by the path form (`Parent/#2`) and by its own ordinal, so under ordinal-first every section is addressable, which is not true the other way round.

Ambiguity stays an error that names the resolving ordinals; it never silently picks the first match (that is how an agent edits the wrong section and reports success).

Helpers: `_resolve_section_index` (selector → index), `_section_body_span` (index → body span), `extract_section` (heading line **plus** body, for reads), `replace_section` (body only, for writes), `outline_sections` (depth/text/size/ordinal per section).

### Where a section's body begins, and what a section write destroys (#140)

**A section's body begins at the first byte of the line immediately after the
heading line, and ends immediately before the next heading of equal-or-shallower
depth, or at end of file.** At most one terminator (LF, CRLF as a unit, or a
lone CR) separates the two; a heading at EOF with no terminator has an empty
body. There is no third region: no whitespace, no blank line, and no fenced
code block sits between the heading line and the body. `extract_section`
returns the heading line, its terminator, and exactly the span `replace_section`
writes — by construction, not by coincidence.

Before #140 the two disagreed, and the gap was **readable but unwritable**:
whatever the heading regex's trailing `\s*` swallowed came back from a section
read and lay outside the section write's span, so an agent that read a section
and wrote it back duplicated it. Two symptoms, one cause — see the terminator
bullet above for the mechanism.

**The break this bought is declared, and it is destructive rather than
cosmetic.** A section write replaces the **whole body**, so content the caller
does not resend is **deleted**:

- A blank separator that used to survive is gone unless `content` carries it.
  `edit_note(section="Tasks", content="- x")` on `## Tasks\n\n- old\n` now
  writes `## Tasks\n- x\n`. The blank line is the caller's to send
  (`content="\n- x"`).
- **A fenced code block directly under the heading is deleted.** Given this
  note:

  ````markdown
  # A
  ```
  important
  ```
  old
  ````

  `edit_note(section="A", content="new")` previously kept the block and
  replaced only `old`; it now yields exactly `# A\nnew`. Leaving the block
  behind *was* the duplication bug, so replacing it is the fix — but a caller
  who does not round-trip loses content, and both `edit_note` docstrings say
  so in those words.

  Note the exact bytes: **no trailing newline**. `A` is the last heading, so
  there is no following heading to separate the new body from, and the
  trailing separator exists only to prevent that gluing (the rule
  `tests/test_issue_5_replace_section_eof_heading.py` has pinned since #5).
  #140's proposal, design and tasks originally rendered this example as
  `# A\nnew\n`; the extra terminator was an illustration slip, caught during
  implementation and corrected in the same change.
  `tests/test_issue_140_section_round_trip.py` pins the real bytes.

What bounds it: no note is rewritten until someone writes to that section, and
the read→modify→write path an agent actually takes comes out strictly better —
it now preserves what it used to duplicate.

**A separator newline is inserted only for a non-empty replacement body.**
Both insertions — one before the body when the retained prefix does not end in
a terminator (an EOF heading), one after it when a following heading would
otherwise be glued to it — are conditional on `new_body` being non-empty. They
used to be unconditional, which meant an *empty* section was not round-trip
stable even with the regex fixed: `# A\n# B\nb\n` gained a blank line and `# A`
gained a newline on every pass. Two consecutive headings are the commonest
degenerate shape in an outline-heavy note, so without this the headline
property was simply false. The non-empty behaviour is untouched and
`tests/test_issue_5_replace_section_eof_heading.py` pins it.

**What did NOT change, and why that made the narrowing admissible.** The
heading capture is `([^\r\n]+?)` with `.strip()` applied either way, so heading
depth, trimmed text, `line_start` and document order are bit-identical before
and after — and therefore so is every `#N` ordinal and every
`outline_sections` `size` (`body_end - line_start`; the change moves neither
endpoint). **No selector changes meaning; no existing note is re-addressed.**
`tests/test_issue_140_section_round_trip.py` pins this against explicit values
captured from the pre-change tree, not by comparing two regexes.

### No documented way to recover a section body from a response — on purpose (#149)

The round-trip guarantee is stated over **note text**, verified against the
shared section helpers. It is deliberately *not* stated over a rendered
`read_note` response, and **no docstring may instruct a caller to extract the
body from one** — no "split on the separator", no "drop the first line". If
you are the next author who notices the docstrings stop short of telling the
agent how to get the body, and you are about to helpfully add a split rule:
don't. This is why.

`read_note` builds every response as an envelope — `# <title>`, `**Path:**`,
optional `**Tags:**` and `**Frontmatter:**` — then `"\n---\n"` and the selected
content. So the response's first line is the *title*, not the heading: "strip
the first line and write it back" makes an agent write ``**Path:** `n.md` ``
into the note. The obvious repair, "split on the first `\n---\n`", is also
forgeable, because every component of that envelope is note-controlled and a
*valid* note can make one emit a line that mimics the separator. Both
reproduced during #140's audit:

| forged via | frontmatter | what the rule then extracts |
| --- | --- | --- |
| the title | `title: \|-` with a `---` line in the block scalar | ``**Path:** `n.md`…`` |
| a key | `"safe\n---\nforged": value` | `\n---\n# A\nold\n` |

Sanitising the named fields does not close the class — one audit round patched
the title, the next found the key — and collapsing terminators to make
components safe is itself lossy: the distinct paths `a\nb.md` and `a b.md`
would render identically, trading a destructive write for a silently wrong
read. Nor does a per-component invariant compose: a component whose value is
exactly `---` forges a separator the moment any future field is rendered alone
on a line.

Making the selected content unambiguously recoverable needs **structural**
framing — separate metadata and section-body fields in the MCP result — which
changes `read_note`'s response shape and every consumer of it. That is filed
as **#149**. Until it lands, the docstrings state the relationship (a section
response carries the heading line and the body; `edit_note(section=…)` takes
the body) and stop there.

### Declared residual: the masker's fence grammar is narrower than CommonMark (#150)

`_FENCE_RE` in `src/services/links.py` recognises only a **column-zero** opener
closed by a fence of **exactly** the same length. CommonMark allows up to three
spaces of indentation and a closer at least as long as the opener. So an
indented fence, or one closed by a longer run, is not masked: a heading inside
such a block is selectable, and a section write there already deletes the
opening fence and orphans the contents.

That is a genuine destructive-write hazard, and it is **not #140's**. Measured
before and after the narrowing on both shapes, the bytes written are
**identical** — pinned by `tests/test_issue_140_section_round_trip.py` against
explicit expected bytes, which is the evidence for this claim. Widening the
masker changes which lines count as headings, which shifts every `#N` ordinal
on an affected note — a re-addressing break strictly larger than #140's, and
one that must not ride along on it. It is filed as **#150** and needs its own
proposal. This is why #140's spec says "fenced code block **as recognised by
the shared masker**" rather than claiming CommonMark coverage the code does not
have.

### Declared residual: a section round trip normalises the body's terminators

Beside the whole-note newline residual #128 already declared: `read_note`
applies universal-newline translation before the caller ever sees a section,
while `edit_note` reads and rewrites raw bytes. A section round trip therefore
rewrites *that section's* terminators as LF and leaves everything else alone.
Measured, and pinned as bytes:

- `# A\r\nold\r\n# B\r\nkeep\r\n` → `# A\r\nold\n# B\r\nkeep\r\n`
- mixed endings, since the normalisation is per-terminator and not per-note:
  `# A\r\none\r\ntwo\nthree\r# B\rkeep\r` → `# A\r\none\ntwo\nthree\n# B\rkeep\r`

Content is preserved; bytes are not. Normalising on write would rewrite
terminators the caller never touched, so this is accepted rather than fixed:
the byte-identity guarantee is scoped to LF-bodied notes and both `edit_note`
docstrings say so.
