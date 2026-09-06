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
  contract: every substitution is exactly as long as what it replaces — in
  **code points**, which is what every consumer stores and reports — because
  heading positions and `ExtractedLink.position` index the unmasked text.
  Since #150 the masker is a line scanner rather than a regex, and
  `clean_for_embedding` consumes it too: see "The fence grammar every consumer
  shares" below.
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
  mechanism was judged not worth building **for that artifact**, and that
  judgement stands for it. #150 built one anyway, for a larger blast radius —
  a masker grammar change re-derives every note's links and tags — and it is
  the general mechanism now: `notes_metadata.extraction_version`, described in
  [indexing and embeddings](indexing-and-embeddings.md#re-deriving-after-a-grammar-change-150).
  A future parser revision should bump that marker rather than declare
  staleness again.
- **The round-trip guarantee is scoped, and both layers' docstrings say so:**
  it covers a complete, unwindowed whole-note read only (`section=None`,
  `offset=0`, and `truncated` false in the structured result). A truncated read
  must be paged to the end first. A `read_note(section=…)` response carries the
  matched heading line in its **`heading` field** and the body in **`content`**,
  and `edit_note(section=…)` takes exactly that `content` — the pre-#149 shape,
  where the response was one rendered string that *included* the heading line
  and the caller had to strip it, is gone; see "The read→write round trip is
  field-based" below for why no docstring may describe recovering the body from
  a rendered response.

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
- The parent is opened from that already-open root fd by **one** kernel-enforced beneath-root lookup — `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)` in `vault_fs.open_dir_beneath` (#87). Resolution of the *pathname* happens first, so in-vault symlinked folders keep working; the lookup itself stays strict, so a component that *became* a link in the interval is refused rather than followed. Relaxing it to follow in-vault links was the other option and was rejected — it re-derives containment per component, which is the check-then-act this change removes, and it would weaken the primitive `/transfer/*` depends on. Two flags it deliberately does not carry: `RESOLVE_NO_XDEV`, which would refuse lookups *through* a mount point beneath the root and so break every read and write that works across a nested mount — a vault that spans mounts is fine for one tenant and stays fine — while fixing none of the three symlink races this section is about; and `RESOLVE_IN_ROOT`, which scopes `..` chroot-style rather than refusing it — `_split` stays in front and refuses `..`, absolute paths and NUL bytes lexically, because `RESOLVE_BENEATH` *scopes* `..` too (`A/../A` succeeds at the kernel) and nothing here is normalised on the caller's behalf.
- **The `RESOLVE_NO_XDEV` omission is a containment non-issue for *one* tenant, and it is exactly what makes a nested *second* tenant reachable (#199).** The paragraph above used to stop at "a vault that spans mounts is fine", which is true and was read as the whole story. It is not: when two active users' `vault_path` values name overlapping directories — an ancestor/descendant pair, or a bind mount or symlink aliasing one root as two — the inner tenant's files genuinely are beneath the outer tenant's root, `RESOLVE_BENEATH` agrees they are contained, and `read_note`, `edit_note`, `write_file` and `delete_note` reach across the boundary while the containment check says everything is in order. **The fix is not this flag.** Turning `RESOLVE_NO_XDEV` on would convert a two-tenant misconfiguration into a one-tenant outage and still miss the same-filesystem cases; the roots are compared instead, by inode identity and canonical containment, at assignment time and at every pass entry point, and an affected pair is refused before any tool body runs. See "The vault-root overlap guard" in [vault-roots-and-tenancy.md](vault-roots-and-tenancy.md).
- **A *grafted* second tenant is still reachable, and that is a declared residual.** `mount --bind /vaults/b /vaults/a/inner` leaves both root inodes distinct and both canonical real paths outside each other, so neither of those two checks sees it — and `RESOLVE_BENEATH` will happily walk into the graft. User A can then read, overwrite and delete every note in B's vault through the ordinary write tools, and A's index pass files B's notes under A's `user_id`. That is accepted limitation **L1** of the overlap guard (with **L2**, an accessible alias of a root that could not be examined, as the same class); the owner decision is pending and it is tracked in the follow-up issue `vault-root-mount-graft-detection`. Treat `/vaults/` and the compose file's mounts as an admin-trust boundary.
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
- a **read-modify-write overwrite** (`edit_note`, `set_frontmatter`, and `move_note`'s link rewrites) is optimistic, not linearizable: `expected=` compares the current bytes immediately before the rename and a writer landing inside that window is still overwritten — the same guarantee level as the transfer fingerprint check, declared rather than implied. **`expected=` is server-internal and its window is the tool's own read→rename interval, not the caller's.** The bytes it compares are the ones this call read; no write tool accepts an expected hash, version or mtime, and `read_note` surfaces none, so an `edit_note` whose `content` was computed from an earlier `read_note` overwrites anything that landed in between without a word — the whole note under full replacement and `replace_frontmatter=True`, only the addressed region under `append`/`find`/`section`. That there is no caller-visible write precondition is an accepted limitation, tracked in #205; the `edit_note` docstring states it, and any wording that lets an agent read `File changed while editing` as covering its own read→edit round trip is a bug in the docstring.
- **`write_file(overwrite=True)` has no conflict detection at all** — it is an unconditional replace, and deliberately so: the raw byte tool takes whole-file content from the caller and has no prior read to compare against. Do not read `expected=` into it. `request_upload(overwrite=True)` is the path that *does* bind to the incumbent's fingerprint; use it when the caller cares that the file has not changed.
- The **no-clobber** publish (`create_note`, `write_file` by default) has no window at all (`link()` is kernel-atomic), and neither does the soft delete or `move_note`'s own publication (one `renameat2(RENAME_NOREPLACE)`).

All of these are properties of concurrent editing, of an attacker who already holds the destination directory, or of what the kernel offers — not of path resolution: the write always lands on the path the caller named, in the directory that was validated.

An in-vault `..` (`Folder/../note.md`) is also refused by `validate_mutable_path` — a mutating tool never resolves a component away — but with a message naming the normalised path rather than "Path traversal denied", which would be a lie the caller cannot act on. Reads still resolve `..`.

## File-access tools (non-markdown)
Raw read/write/browse of arbitrary vault files, distinct peers to the note tools (note tools stay markdown-only). Pure byte transport — no server-side PDF/text extraction, no embedding or indexing of non-markdown files.
- `read_file(path, encoding="auto", offset=0, limit=None)` — `auto` resolves text-like MIME → text, image → inline MCP image content block (renders in-client), everything else → base64 string. `text` forces UTF-8 decode (errors on non-UTF-8); `base64` forces raw-bytes base64. Capped by `MAX_FILE_READ_BYTES` (default 10 MB), checked against on-disk size before reading. Text results are additionally bounded by `MAX_READ_RESPONSE_CHARS` and page via `offset`; base64 and image results are not windowed. Base64 reads are token-heavy — check size with `list_files` first.
- `write_file(path, content, encoding="base64", overwrite=False)` — `base64` decodes `content` to raw bytes; `text` writes UTF-8. No-clobber by default (`overwrite=True` to replace), auto-creates parent dirs, atomic via `vault.write_file`. Capped by `MAX_FILE_WRITE_BYTES` (default 25 MB) on decoded length — **except for a `.md` destination**, which is capped at `min(MAX_NOTE_BYTES, MAX_FILE_WRITE_BYTES)`; see "The `.md` cap follows the extension, not the tool" below.
- `list_files(folder=".", pattern="*", recursive=False, limit=200)` — `ls`-style: immediate children (subdirs + files) by default, each file with size + mtime; glob-filterable; capped at `limit` with a truncation note. `pattern` is refused over `MAX_LIST_PATTERN_CHARS` (1,024) — see "The `list_files` pattern cap" below.

- `delete_file(path, permanent=False)` — soft-deletes to `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>` through the anchored helper; `permanent=True` unlinks. Refuses `.md` (pointing at `delete_note`), directories and symlinks. The `.md` refusal runs on the **canonical** final component, so `note.md/.`, `a//note.md` and `NOTE.MD` are refused too — the caller's string is not the path.

`write_file` additionally goes through `validate_mutable_path`, so it refuses a symlinked final component the way the note tools do (see "Mutations act on the path as named" above) — `overwrite=True` cannot clobber a file through an alias. `read_file` and `list_files` still follow links.

All four enforce the same traversal guard and the same dot-dir guard (`is_hidden_path`, rejecting any path component starting with `.` — the indexer's visibility rule, keeping `.obsidian`/`.git`/`.trash`/`.smart-env` out of reach), but **not through the same validator**: `read_file` and `list_files` use `validate_visible_path` (which resolves, so links are followed), `write_file` uses `validate_mutable_path` (parent resolved, symlinked leaf refused), and `delete_file` canonicalises lexically and resolves through `vault_fs`'s beneath-root lookup. Vault helpers (`read_bytes`, `write_bytes`, `list_dir`, MIME classification) live in `src/services/vault.py`. MIME detection uses stdlib `mimetypes` plus a magic-byte sniff for PNG/JPEG/GIF/WebP. `read_file` is the first tool returning a non-`str` MCP content object.

### The `list_files` pattern cap (#204)

`list_dir` refuses a `pattern` longer than `MAX_LIST_PATTERN_CHARS` (1,024)
with a `ValueError` — the exception `list_files_impl` already maps to an
in-band refusal — and the message names the constant.

Two things about that check are load-bearing and must not be "tidied":

- **It runs before `fnmatch` is touched.** The cost is the compile itself.
  `fnmatch.translate` + `re.compile` is linear at ~10 µs/char (Python 3.12
  emits atomic groups, so there is no backtracking to cap), and it runs
  synchronously on the one event loop this server has: a 500 KB pattern was a
  5.4-second stall for every other tenant, and the transport body limit
  admitted about ten minutes of one. A cap applied after the compile caps
  nothing.
- **It runs before `validate_visible_path` and before `is_dir`.** An over-long
  pattern against a folder that does not exist must still be refused *for the
  pattern*. Told "Not a directory" instead, a caller fixes the folder and
  repeats the same stall with a valid one.

1,024 characters is far beyond any real glob and compiles in about 5 ms. A
separate wildcard-count cap would be redundant; running the walk in a thread
is defence in depth and is deliberately not part of this.

### The `.md` cap follows the extension, not the tool (#203)

Every *note*-writing path is capped at `MAX_NOTE_BYTES`. `write_file`, the
`PUT /transfer/upload` route and `import_from_url` were the exceptions,
because they are byte transport with no extension allowlist — so a 25 MiB
`.md` could be landed by a tool the note tools would have refused, and the
indexer then reads it as a note. All three now cap a `.md` destination
(case-insensitive) at

    min(MAX_NOTE_BYTES, MAX_FILE_WRITE_BYTES)

and the refusal **names the limit that applied** — `MAX_NOTE_BYTES` or
`MAX_FILE_WRITE_BYTES`. The smaller of the two, so an operator who lowers
`MAX_FILE_WRITE_BYTES` below 10 MiB is not surprised by a *more* permissive
markdown limit; and named, because a caller told only "max 10,485,760" cannot
tell which knob to reach for. Non-markdown destinations keep
`MAX_FILE_WRITE_BYTES` unchanged. `src/mcp_server/tools.py:_write_cap_for` is
the one place that decides; the transfer route applies the same bound where it
applies the stream cap, so an oversized `.md` upload aborts with 413 at cap+1
exactly as an oversized file does.

## Three kinds of size cap — don't confuse them

There are **byte** caps, a **character** cap, and a **transport** cap, and they protect different things:

- `MAX_FILE_READ_BYTES` / `MAX_FILE_WRITE_BYTES` bound what the **server** reads into or writes out of memory. They refuse the operation.
- `MAX_READ_RESPONSE_CHARS` (default 40,000 ≈ 10K tokens) bounds what `read_note` / `read_file` **return to the caller**, whose context the result consumes. It truncates rather than refusing.
- The MCP streamable-HTTP **request body limit** bounds what the transport accepts at all, before any tool runs. It is derived, not configured: `max(2 × MAX_FILE_WRITE_BYTES, 6 × MAX_NOTE_BYTES) + 1 MiB` (61 MiB with the defaults), passed to `FastMCP(max_request_body_size=)` from `Settings.mcp_max_request_body_bytes`. The SDK's own default is 4 MiB, which would silently reject writes far below our documented 25 MB cap. The formula guarantees — for a *canonical* envelope, i.e. JSON-RPC framing plus non-content arguments under 1 MiB — that a base64 `write_file` at the cap (base64 is `4·⌈n/3⌉ ≤ 2n + 2`) and any note write up to `MAX_NOTE_BYTES` (JSON escaping expands a byte at most 6×) always reach the tool, which then decides. Unsupported shapes are bounded by the transport with a bare HTTP 413 and no tool error: text-mode `write_file` whose escaping exceeds the limit (send base64 — always safe), an envelope over 1 MiB, and arguments that are large but discarded.

**An argument that is not UTF-8 never reaches a tool body.** `_tracked` screens
every bound argument — strings, and an **iterative, depth-unlimited** walk into
list and mapping arguments like `set_frontmatter(updates=…)` — and refuses the
call with
`_UNENCODABLE_ARG_MARKER` in `usage_logs`, distinct from the vault markers so an
operator can tell "this credential has no vault" from "this client is emitting
bad JSON escapes". The refusal names the parameter and never repeats the value,
which is the point: the value is precisely the one that cannot be encoded into
the response carrying the complaint.

The screen sits in the shared decorator rather than in each tool because
auditing interpolations one at a time is how this class stayed open for two
audit rounds. `read_note`'s `path` and `section` were closed individually first;
the very next review found `edit_note(section=…)` — the shared resolver's
not-found listing quotes the selector straight back — and `edit_note(find=…)`
beside it. Anything a future tool accepts is covered by construction. `path` is
*also* refused at the vault layer (`validate_path`, `_mutable_parts`), because a
path that is not UTF-8 cannot name a file and that is true regardless of which
decorator ran.

**The walk had a six-level depth cap and that was a bug, not a bound.**
`{"deep": [[[[[["\ud800"]]]]]]}` walked past it and the value was written to
the note. It is an explicit stack now, with no depth constant: total argument
size is already bounded by the transport's request-body limit, so depth was the
only unbounded axis and a loop removes it. Visited containers are skipped, so a
self-referential argument (impossible over the wire, reachable from an
in-process caller) terminates.

**Every note write tool caps its own result.** `create_note`, `edit_note` (all modes, `dry_run` included), and `set_frontmatter` refuse a result over `MAX_NOTE_BYTES` with a tool-level error and no write, via `_note_size_error()` in `tools.py`. That is what keeps the tool, not the transport, in charge of every supported write — the transport limit sits deliberately above every tool cap. `MAX_NOTE_BYTES` lives in `src/config.py` (not `tools.py`) because the transport formula needs it.

**Precisely: it is a per-component budget, not a single ceiling on the whole response.** Since #149 `read_note` returns fields, and each note-controlled field carries its own budget:

| field | budget |
| --- | --- |
| `content` | `MAX_READ_RESPONSE_CHARS`, lowered by `limit`, windowed by `_window()` |
| `outline` | its own independent `MAX_READ_RESPONSE_CHARS` (also lowered by `limit`), measured over the **serialized** outline object |
| `title`, `tags`, `frontmatter_yaml` + its JSON view, `heading` | one shared **metadata budget**, `MAX_READ_RESPONSE_CHARS` and *not* lowered by `limit` — `limit` is documented as bounding returned content, and letting `limit=1` silently strip a note's title made a cheap probe useless |
| `path` | none needed: it is returned **exactly**, bounded instead at admission by `vault.MAX_PATH_CHARS` (1,024, matching `notes_metadata.file_path`) and by `vault.is_encodable` (a path that is not UTF-8 cannot name a file, and cannot be serialized into the response either) |
| `error`, `notice` | fixed server prose plus bounded interpolations only — the path (1,024) and the section selector (`_NOTICE_SELECTOR_MAX`); the whole `error` string is additionally cut to the metadata budget |

**The worst-case serialized response**, stated for the new shape: `(content cap + outline budget + metadata budget + the fixed path allocation and prose) × 2` for the structured-content-plus-text-block duplication `× up to 6` for JSON string escaping of note-controlled text (`\u0001` is six characters for one). At the 40,000 default that is roughly 1.4 M characters in the pathological case — a note whose body is 40,000 control characters *and* whose frontmatter is over-budget *and* which is heading-dense. The old "≈ `2 × cap`" figure was retired with the envelope. Every component must have a budget; if you add a fourth, give it one, and update this table, the worst case, and the end-to-end test in `tests/test_read_response_cap.py`.

**Why the metadata budget exists at all.** `read_note` goes through `read_file()`, which has no byte cap, so a note can carry a multi-megabyte frontmatter block above a one-character body: without a third budget the response is governed by the content cap and is still megabytes wide.

**Overflow drops a field WHOLE, in a fixed order, and reports it out of band.** The order is the lossy `frontmatter` JSON view (the raw block says everything it does), then `frontmatter_yaml`, then `tags`, then `heading`, then `title`. It is a **priority list, not an optimizer**: each step runs only if the remainder still does not fit, and it stops as soon as it does — so an oversized `title` costs the `heading` first, deterministically, rather than the server searching for the cheapest set to drop.

**No field is ever cut short.** Not truncated in place, not elided, not replaced by an in-band marker — a shortened or marked value inside a note-controlled field is indistinguishable from note content, which is the forgery class #149 exists to end. That covers the two short fields where an in-place cut looks harmless: an earlier revision elided `heading` and `title`, and it also computed the heading's room from the *un-dropped* title, so an oversized title silently zeroed `heading` to `""` — a present-but-empty note-controlled field, colliding with this tool's own convention that `""` is an *answer* (an empty section body) rather than an absence. Both are gone; `tests/test_issue_149_read_note_framing.py` pins all five steps and the `""` case.

Every drop is reported in the server-controlled `metadata_omissions` list, naming the field, a stable reason code, and how to read the value anyway (the note's raw bytes). `frontmatter_yaml` in particular is dropped rather than cut because half a YAML block still parses: a truncated one is a *corrupt* block that looks valid.

Satisfying the byte caps says nothing about the response. A 3 MB note is well inside the 10 MB read cap and will still exhaust a context window — that is exactly how this bit us: `read_note` had no cap at all and returned a 3.4 M-char tool result, which the caller's inference provider rejected as "input exceeds the context window". `read_note` goes through `read_file()` in the vault service, not `read_bytes()`, so it never even had the byte cap.

**If you add a tool that returns file or note content, it needs the character cap too.** `_window()` and `_capped_text()` in `tools.py` are the shared helpers.

Over-cap reads return the first window plus truncation **as data** — `truncated`, `offset`, `next_offset` (absent at the end), `total_chars` — a server-authored `notice` carrying the guidance prose, and, for a whole-note read, the `outline` object. `limit` may lower the cap for one call but never raise it; raising is an operator decision via the env var.

## Section addressing

`read_note(section=…)` and `edit_note(section=…)` share one resolver in `src/services/vault.py`, so a selector names the same section for both. Three forms:

1. Ordinal — `"#7"`, the 7th ATX heading in document order, 1-based. Checked **first**.
2. Path-style chain — `"Parent/Child"`, ancestors outermost-first. A selector containing `/` never takes the ordinal branch.
3. Exact heading text — `"Tasks"`.

The ordinal exists because **path-style cannot disambiguate duplicate siblings**: two `## Report.xlsx` under the same parent share every ancestor, so no chain separates them. Bulk-extraction notes are full of these.

**A bare `#N` always wins over a heading literally titled `#N` — don't "fix" this by preferring text.** The outline we emit on truncation advertises ordinals as the reliable selector; if note content could shadow one, the section we just told the caller to fetch by `#2` would be unreachable by `#2`. Text-first was the original implementation and pre-merge review caught it. The literal heading loses nothing: it stays reachable by the path form (`Parent/#2`) and by its own ordinal, so under ordinal-first every section is addressable, which is not true the other way round.

Ambiguity stays an error that names the resolving ordinals; it never silently picks the first match (that is how an agent edits the wrong section and reports success).

Helpers: `_resolve_section_index` (selector → index), `_section_body_span` (index → body span), `extract_section_parts` (heading line and body **separately** — what `read_note` returns as its `heading` and `content` fields), `extract_section` (the same two concatenated, for callers that want one string), `replace_section` (body only, for writes), `outline_sections` (depth/text/size/ordinal per section).

**Selector parity is about resolution on writes this tool admits, not about
admission.** Two shapes read fine by section and refuse every section write —
a defective frontmatter block, and the unmatched indented fence opener below.
Both docstring layers say so; do not restore the unqualified "a selector that
names a section for reading names the same section for writing".

### The fence grammar every consumer shares (#150)

`_scan_headings` scans `mask_code(text)`, so **what counts as fenced code is
what decides which lines are headings** — and therefore which sections exist,
which ordinals they carry, and what a section write replaces. That grammar
lives in one place, `src/services/links.py`, and is the `code-masking`
capability (`openspec/specs/code-masking/spec.md`). Its consumers are heading
resolution, `extract_links`, `extract_tags`, `move_note` link rewriting, and
`clean_for_embedding`. **No consumer may carry a private grammar.**
`clean_for_embedding` did, and the two disagreed: semantic search embedded code
the masker hid.

The pinned CommonMark subset, in one place so a future widening has something
to be checked against:

- **Opener** — 0–3 leading U+0020 spaces, a run of ≥3 backticks or ≥3 tildes,
  an info string. A backtick fence's info string may not contain a backtick, so
  a one-line `` ```code``` `` inline span never opens a block.
- **Closer** — 0–3 leading spaces, a run of the **same character at least as
  long as the opener's**, then nothing but U+0020 / U+0009. A shorter run does
  not close; the other fence character does not close; an NBSP after the run
  does not close.
- **Span** — the opening line's first character through the closing line's last
  character, **excluding the closing line's terminator**. Masking is a
  same-length substitution and the heading pattern matches only at a line
  start, so swallowing that terminator would leave an immediately following
  heading with no line boundary in front of it and hide it from the read and
  write sides alike. Terminators *inside* the span are masked like anything
  else. This is the rule that keeps `# B` a heading in
  ``` `# A\n```\n# Hidden\n```\n# B\n` ```.
- **Unterminated at column zero** — masks to end of note; a document is
  CommonMark's outermost container.
- **Unterminated, indented 1–3 spaces** — **not a fence**, and reported to the
  caller instead. See the refusal below.
- **Terminators** — LF, CRLF as a unit, or a lone CR, matching the partition
  and the heading scan.
- **Frontmatter** — a valid line-1 block is opaque to fence recognition, and
  the partition runs **at most once per note**. The recognizer therefore takes
  its context explicitly: `FULL_NOTE` discovers and skips the block,
  `BODY` scans from character zero and never re-partitions. Both mistakes are
  real: `BODY` on a raw note lets a fence-shaped YAML scalar swallow the body
  for `move_note`'s rewriter; `FULL_NOTE` on a stripped body eats a
  mapping-shaped body prefix as a phantom second block, hiding an unmatched
  opener from the refusal that must fire on it. **At most once** is enforced by
  handing the recognizer already-partitioned text wherever the caller has it:
  `extract_tags` takes the **body**, not the raw note — it is already holding
  the parsed mapping, so a `#` inside a *valid* block is structured data rather
  than an inline tag — and `move_note`'s preflight takes one `FULL_NOTE` scan
  per source and passes it to the rewriter via `apply_fence_mask` instead of
  masking the same bytes a second time.

Deliberate divergences, kept: container blocks are not parsed, so a *matched*
fence's extent is computed flat even when its opener sits in a list item;
4+-space indented code blocks are not masked; ATX headings stay column-zero
only; inline-code masking remains a single-line approximation that never
crosses a terminator.

A single `(?s)` regex cannot express "closer at least as long as the opener"
or the indented-unterminated exclusion. The widened regex that tried is exactly
what hid this bug class, so the recognizer is a line scanner whose clauses can
be audited one at a time. `tests/test_issue_150_fence_grammar.py` is one case
per spec scenario, with exact span offsets.

**Two refusals, both naming the opener and writing nothing.** An indented
opener with no closer is the one shape the flat grammar genuinely cannot
decide: under CommonMark the block may end where an enclosing list item does,
so any flat reading either splits a code block or extends a section over real
content, and fabricating an end-of-note extent would let one stray line swallow
every later section — a *new* destructive class, worse than the one this
grammar closes.

- `edit_note(section=…)` refuses such a note, naming the opener's line and
  character position (re-based onto the whole file when the note carries a
  frontmatter block, since that is the only coordinate the caller can act on).
- `move_note(rewrite_links=True)` preflights **every** source it would rewrite,
  the moved note's own body included, and refuses the whole move **before the
  rename**, naming each offending source. Rewriting mutates note text, and a
  link under such an opener may be inside a list item's code block.
  `rewrite_links=False` is unaffected and is the way to move such a note.

`move_note(rewrite_links=True)` also refuses, before the rename and naming the
source, when one source plans two rewrites over **overlapping spans** — a
wikilink to the moved note sitting inside a markdown link's anchor that also
names it. That note has no correct rewritten form; see the splice section
below.

`move_note(rewrite_links=True)` carries a **third**, unrelated refusal from the
same change: it aborts while any note in the caller's owner scope still carries
a stale `extraction_version`, because its rewrite-source inventory is
`note_links` and a stale-grammar extraction may have omitted rows. That one is
transitional and clears itself — see
[the transition window](indexing-and-embeddings.md#the-transition-window-two-controls-150).

Reads stay asymmetric on purpose, the same doctrine as defective frontmatter:
`read_note(section=…)` and the truncation outline keep resolving under the
not-a-fence reading, because a read destroys nothing. **The guarantee on such
a note is the refusal, not the round trip.**

**The declared re-addressing break.** Widening the masker changes which lines
are headings, so on a note containing a newly recognised shape — an indented
opener or closer, a longer closer, an unterminated column-zero fence — `#N`
ordinals emitted before #150 may shift and previously-selectable sections
disappear. Accepted, and small: the heading that vanishes was inside code, and
writing to it was already destructive (that is issue #150). Outlines are
per-response, so the exposure is an agent holding a pre-deploy outline.
`tests/test_issue_140_section_round_trip.py` keeps the pre-#150 bytes beside
the current ones for the two shapes #140 froze as out of scope.

Derived state does not heal on its own — the bytes on disk are unchanged, so
`content_hash` cannot see the grammar move. See the `extraction_version`
mechanism in
[indexing and embeddings](indexing-and-embeddings.md#re-deriving-after-a-grammar-change-150).

### The link grammar every link consumer shares (#180)

Four grammars parse links, and they must agree:

| Grammar | Where | Used by |
| --- | --- | --- |
| `_WIKILINK_RE` (regex), `scan_md_links()` | `src/services/links.py` | `extract_links` → `note_links`, `get_links`, `get_backlinks` |
| `_WIKILINK_REWRITE_RE` (regex), `scan_md_links(**MDLINK_REWRITE_FLAGS)` | `src/mcp_server/tools.py` | `move_note(rewrite_links=True)` |

**Every character class in all four is closed.** Not "the two that were
reported": an open class that can swallow the rest of a line before the tail
fails is quadratic, and closing only some of them moves the burn to the next
one. Measured at 40 KB before the fix, on the production host: `[[` 18 s,
`[[a#` 11.8 s, `[[a|` 4.9 s, `[a](` 3.6 s, `[a](x` 2.4 s — all of it on the
single event loop, inside the indexer, from a note any authenticated tenant
can write. The rules:

- **Wikilink target, anchor and alias exclude `[` and `]`** — Obsidian's link
  *syntax* forbids both inside `[[...]]`, so no well-formed wikilink changes.
- **Markdown link text excludes `[`** (it already excluded `]`).
- **Markdown hrefs cannot exclude brackets** — `[t](Foo [draft].md)` is a legal
  link to a legal filename; Obsidian forbids brackets in wikilink syntax, not
  in filenames. They are **length-bounded** instead, at `MDLINK_HREF_MAX`
  (2,048), which exceeds `MAX_PATH_CHARS` (1,024) plus any anchor.
- **Every quantifier is possessive** (`++`, `*+`), so no class can be
  re-entered by backtracking.

**The markdown half is a hand-written scanner, not a regex, and that is a
an availability fix rather than a style choice.** Closing the classes made the
markdown regex linear — but linear *with a 2,048× constant*, because every
`](` re-scanned up to 2 KiB looking for a `.md` that was not there: ~4.7 s per
512 KiB of `[a](`, so ≈ 90 s for a 10 MiB note, from a body any authenticated
tenant can write.

**`asyncio.to_thread` does not fix that, and it is important to know why.**
CPython releases the GIL *between* `re` steps, never inside one, and a scan
that matches nothing is a single step. So all 90 s ran with the GIL held and
every other tenant's request stopped dead anyway. Dispatching off the loop
bounds the stall at the longest single scan; **the linear scan time is the
real bound**, and shortening it is the actual fix.

`scan_md_links()` in `src/services/links.py` is that scanner. It reproduces
the two retired regexes exactly — they are kept as differential oracles and
fuzzed against it in `tests/test_asvs_mdlink_scanner.py` — while answering
every "where is the next `)` / newline / `>` / `.md#`" question from a
**monotone cursor**, so the total scanning is one forward pass rather than
O(n × 2048), and pruning the whole candidate loop on a `.md#`/`.md)`/`.md>`
tetragram prefilter. Measured after: **1 MiB of `[a](` in 0.8 ms and of
`[a](.md` in 2.9 ms**, against ~9.4 s for the first of those before. The
scanner's own worst case — a body dense in *both* `](` candidates and `.md`
tetragrams, where nothing can be pruned and each `](` costs one loop
iteration — is ~200 ms per MiB, and is pinned with its own ceiling in that
test file rather than left unmeasured.

**The accepted differences**, enumerated and asserted in
`tests/test_asvs_link_grammar.py`, which is where any future change to this
grammar has to argue its case:

| Input | Before | Now |
| --- | --- | --- |
| `[[Note\|see [1]]]`, `[[Note#Sec [x]]]` | a row with a mangled alias/anchor | no row — **and `move_note(rewrite_links=True)` no longer rewrites it either** |
| `[[[Foo]]` | target `[Foo` | target `Foo` (the match starts at the second `[`) |
| `[a[b](x.md)` | `link_text` `[a[b](x.md)` | still a row to `x.md`; `link_text` is `[b](x.md)` |
| an href over 2,048 characters | a row | no row — it cannot name a note |

The first row's second half is the one accepted difference with a **write**
consequence, so it is spelled out rather than left to be inferred: the rewrite
grammar closed the same classes as extraction, so a link like
`[[Old#Results [draft]]]` is invisible to `move_note` as well as to the index.
Move `Old.md` and that link is left on disk still naming `Old`, with no
warning — the rewrite reports the number of links it changed, and this one was
never a candidate. Accepted because the open anchor and alias classes were two
of the five quadratic blowups (`[[a#` 11.8 s and `[[a|` 4.9 s at 40 KB),
because Obsidian's wikilink syntax forbids `[`/`]` inside `[[...]]` so nothing
well-formed is affected, and because the previous behaviour was not *correct*
either — it extracted the link with a mangled anchor and rewrote that. The
difference is that both halves now agree. **Do not close it by re-admitting
brackets into those classes**; the fix for a link that must survive a move is
to rename the anchor. Pinned in `tests/test_asvs_link_grammar.py`.

**Parity between the two grammars is a rule, not a coincidence.** A corpus of
single-line links is run through both in the same test, and both must accept
and reject the same members. Two divergences are **pre-existing** and
deliberately left alone, recorded in that test as known gaps rather than
silently inherited — closing either changes what `move_note` mutates, which
needs its own change and its own adversarial pass:

1. the rewrite scanner has no CommonMark `<href>` alternative, so
   `[a](<Old.md>)` is indexed but never rewritten;
2. its anchor class is `[^)]` where extraction's is `[^)\n]`, so a `#anchor`
   running past a line break is rewritable but not extractable.

Since both markdown halves are now one scanner, those two are its only two
keyword arguments — `angle=False` and `anchor_crosses_newlines=True`, spelled
once as `MDLINK_REWRITE_FLAGS` in `src/mcp_server/tools.py`. They were
reproduced rather than closed on purpose: healing either would make
`move_note` rewrite links it currently leaves broken, which is a change to
what a destructive tool mutates on disk and needs its own proposal and its own
adversarial pass. Keeping them exact is what makes the scanner swap a pure
performance change with an empty behaviour delta.

**Extraction is bounded per note** at `MAX_LINKS_PER_NOTE` (10,000), applied in
**document order** — the two extraction loops (wikilinks, then markdown links)
are merged by position before the cut, so a note with 20,000 wikilinks does not
lose every markdown link in the file. A capped note is a *declared*
degradation, not a skip: see the link cap in
[indexing and embeddings](indexing-and-embeddings.md).

**A link-grammar change has the same staleness mechanics as a fence-grammar
change.** The bytes on disk do not move, so `content_hash` cannot see it and
stale `note_links` rows would persist until each note is next edited. Bump
`CURRENT_EXTRACTION_VERSION` in the same change — and `move_note(rewrite_links=True)`
refuses until the re-derivation completes, which is the correct disposition.

`move_note`'s rewrite computation runs through `asyncio.to_thread`: it is a
pure function of a string, and a hub note's backlink sources are processed one
after another. That dispatch bounds the stall at the longest single scan step,
**not at zero** — see the GIL note above — which is why the scan itself had to
get fast.

**Both halves of the per-source work are dispatched, not just the rewrite.**
The preflight takes one `FULL_NOTE` fence scan per source and hands it to the
rewriter (`_scan_rewrite_source`); that scan is the *larger* half — a full pass
over up to `MAX_NOTE_BYTES`, 1.6–11.3 s per 10 MiB — and it originally ran on
the loop nine lines above the `to_thread` that dispatched the rewrite. Both go
through `to_thread`, and the dispatch test asserts both names: asserting only
`_rewrite_links_in_text` is what let the larger half sit on the loop.

**The splice that applies the rewrites is linear, and that is load-bearing.**
It used to be `out = out[:start] + replacement + out[end:]` per rewrite over a
descending sort — a fresh copy of the whole note per link. Measured on
`[[Old]] ` repeated: 0.072 s at 64 KiB, 0.176 s at 128 KiB, 0.723 s at 256 KiB,
5.315 s at 512 KiB. Clean O(n²), ≈ 35 minutes extrapolated to `MAX_NOTE_BYTES`
— **while holding `_MOVE_REWRITE_LOCK`**, so every other tenant's
link-rewriting move waits behind it. That is the same cross-tenant stall the
scanner work removed from the other half of the same function, reintroduced
after the scan finished. `_splice_rewrites` walks the spans once with a cursor
and joins once: 0.04 s for the 131,072 rewrites of a 1 MiB body, against ~25 s
for the retired shape.

The cursor walk is equivalent to any per-span splice **only while the spans do
not overlap**, and each scanner is already non-overlapping within itself. They
were believed unable to overlap *each other* — a markdown link's text class
excludes `[` and `]`, so no `[text](` can begin inside a wikilink span and run
past its `]]` — and that is wrong, because the markdown **anchor** class is
`[^)]`: `[x](Old.md#anchor[[Old]])` is one markdown link whose anchor contains
a whole wikilink, and a move of `Old.md` plans both. `_splice_rewrites`
therefore still *checks*, and an overlap is now a **refusal**
(`MoveRewriteOverlap`), handled by `_move_note_locked` exactly as
`MoveRewriteCapExceeded` is: the whole move aborts before the rename, naming
the source note.

It used to fall back to the retired reverse splice instead, on the theory that
the retired implementation defined the answer. It does not define a *correct*
one, which is the whole lesson of this hunk (#211): applying the inner
replacement first changes the string's length, so the outer one then splices
at a stale `end`. `[x](Old.md#anchor[[Old]])TAIL` came back as
`[x](N.md#anchor[[Old]])IL` — two characters eaten past the end of the link,
reported as two successful rewrites. A fallback that produces *some* bytes is
worse than a refusal whenever nobody can say those bytes are right. The
equality of the cursor walk with the retired splice is still tested as an
oracle over a randomized corpus of non-overlapping spans; the overlap path is
tested both through the splice directly and through `move_note` end to end,
where the assertion is that nothing was renamed and nothing was written.

**The recognizers read the masked copy; every byte written back is sliced from
the unmasked note (#211).** The two scans run over `apply_fence_mask(content,
…)` because a link inside a fence or inside backticks is text *about* a link
and must not be rewritten — that is the whole reason the mask exists. But the
replacement text is spliced into `content`, and it used to be assembled from
pieces read off the mask: the wikilink `rest` (`#anchor|alias`) and the
markdown link's text and anchor. Inline code inside any of those is a run of
spaces in the mask, so ``See [the `foo` option](Old.md)`` was published as
`See [the       option](New.md)` — a silent destructive write on an ordinary
note, on the path whose entire job is to leave everything except the target
alone. The fix is the same property the masker already promises everywhere
else: masking is a **same-length** substitution, so a span found in the masked
copy indexes the identical region of `content`, and the rewriter re-slices
`rest`, `text` and `anchor` out of `content` at that span
(`MdLinkMatch.text_start` / `anchor_start` exist for exactly this). What the
recognizers *see* is unchanged. Anything later added to the replacement — a
new alias form, a title, a display component — takes its bytes from `content`
too, never from `masked`.

**And a candidate whose deciding span carries masked bytes is skipped.** The
same bug has a second half, on the read side of the same loop: *which* links
get rewritten was still decided from the masked wikilink target and markdown
href, where the filler is spaces. ``[[`x`Old]]`` reached `resolve_target` as
`"   Old"`, stripped to `Old`, and a move of `Old.md` published `[[New]]` over
a link that named a different note — the destructive write again, this time on
a link the author never pointed at the moved note. So each candidate's target
or href span is compared between `masked` and `content` first
(`MdLinkMatch.href_start` is there for the markdown half), and a difference
skips it. Not "unmask it and resolve that": the mask exists because this server
does not know what the hidden bytes mean, and on a destructive path the safe
answer is to leave the link exactly as written. **Extraction is deliberately
NOT changed to match** — it records a slightly wrong target for such a link,
which is a graph inaccuracy the reindex can correct, not bytes lost from a
note; changing it would need a `CURRENT_EXTRACTION_VERSION` bump and a full
re-derivation of every note's links.

**One source's rewrites are bounded at `MAX_LINKS_PER_NOTE`, and over the cap
is a refusal.** The read side has always been bounded — extraction stops at
10,000 links — and the write side was not, so a 10 MiB note of `[[Old]] `
planned ~1.7 million rewrites in a single source. Refused rather than
truncated, and refused *before the rename*, the same disposition as the
`MAX_NOTE_BYTES` per-source and `MAX_MOVE_REWRITE_BYTES` aggregate preflights:
rewriting the first N and stopping leaves the remainder pointing at the path
the move just vacated, and reports the move as a success. The error names the
source and the cap so the agent knows which note to split. Note that the cap
does **not** make the splice's linearity redundant — 10,000 rewrites of a
10 MiB note is still 100 GB of copying under the old shape.

### Where a section's body begins, and what a section write destroys (#140)

**A section's body begins at the first byte of the line immediately after the
heading line, and ends immediately before the next heading of equal-or-shallower
depth, or at end of file.** At most one terminator (LF, CRLF as a unit, or a
lone CR) separates the two; a heading at EOF with no terminator has an empty
body. There is no third region: no whitespace, no blank line, and no fenced
code block sits between the heading line and the body. `extract_section`
returns the heading line, its terminator, and exactly the span `replace_section`
writes — by construction, not by coincidence — and `extract_section_parts`
returns those two halves apart, which is what `read_note` puts in `heading` and
`content` (#149).

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

### The read→write round trip is field-based, because every textual one was forgeable (#149)

**`read_note` returns a structured result, not a rendered document**
(`ReadNoteResult` in `src/mcp_server/read_result.py`). The round trip is a
*field*:

- a section read's `content` **is** the body `edit_note(section=…)` replaces —
  the same `_section_body_span` computes both, via `extract_section_parts`;
- a complete, unwindowed whole-note read's `content` is the body
  `edit_note(path, content)` full replacement accepts;
- the matched heading line is its own `heading` field and is never part of
  `content`, which ends the old read-returns-heading / write-takes-body
  asymmetry an agent had to compensate for.

**No docstring may instruct a caller to extract the body from a rendered
response** — no "split on the separator", no "drop the first line". That
prohibition survives the fix, because the reason for it does. Here it is.

Before #149 `read_note` built every response as an envelope — `# <title>`,
`**Path:**`, optional `**Tags:**` and `**Frontmatter:**` — then `"\n---\n"`
and the selected content. The response's first line was the *title*, not the
heading: "strip the first line and write it back" makes an agent write
``**Path:** `n.md` `` into the note. The obvious repair, "split on the first
`\n---\n`", is also forgeable, because every component of that envelope is
note-controlled and a *valid* note can make one emit a line that mimics the
separator. Both reproduced during #140's audit:

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
on a line. **The rejected alternative** was to keep the string and reversibly
escape each component, validating the composed envelope and refusing on
forgery: that is a permanent availability hole for notes that merely *contain*
`---` shapes, the escaping has to be specified and taught to agents, and the
validation is a denylist over renderings — the class this history says is
unclosable. JSON supplies the reversible escaping instead, at the protocol
layer, where nobody has to invent it.

Both forgeries are pinned end to end in
`tests/test_issue_149_read_note_framing.py`, including the write-back through
`edit_note(section=…)` that used to clobber the section.

**Three SDK realities shape the implementation** (MCP 1.29 FastMCP, verified in
the pinned wheel — `mcp/server/fastmcp/utilities/func_metadata.py`):

1. The text block is rendered from the **returned object**
   (`pydantic_core.to_json(result, fallback=str, indent=2)`) and
   `structuredContent` from the **validated dump**
   (`model_validate(result).model_dump(mode="json")`). So every value must be
   made JSON-safe *when the model is built* — never inside a serializer, or the
   two renderings diverge. A parity test pins them equal.
2. Optional pydantic fields serialize as `null`. `null` in `content` is not the
   same statement as no `content`, so the model's `_OmitNone` serializer drops
   `None` from both renderings. `""`, `[]` and `False` are answers and survive.
3. A result with `error` set is an MCP **success** (`isError=false`) — in-band
   errors are this server's application convention, unchanged from before. And
   `_tracked`'s admission-refusal path must return a typed
   `ReadNoteResult(error=…)` for this tool (via `_tracked(refusal_result=…)`):
   a bare string from a tool with an output schema fails FastMCP's output
   validation and reaches the agent as a *protocol* error instead of the
   in-band refusal the vault-root gate promises.

**Frontmatter twice, and only one of them is authoritative.** `frontmatter_yaml`
is the block's YAML source, fence lines excluded — content-lossless whenever
present, and dropped whole rather than cut under budget pressure. `frontmatter`
is a best-effort JSON view built by a depth-, node- and size-bounded,
cycle-safe walk, and it is **omitted entirely** (with the reason stated in
`metadata_omissions`) whenever it cannot be honest, because a *valid* YAML
block has shapes JSON does not:

- recursive aliases — `x: &X [*X]` loads into a self-referential list that
  crashes or hangs a naive walk;
- non-string keys — `1:` and `"1":` are two YAML keys and one JSON key, so
  rendering the view silently loses one of them;
- dates and timestamps — no JSON form, so they become ISO strings, which is
  lossy in the other direction;
- **values that will not become text** — two shapes, both *valid YAML*, both
  reachable from a note a user can save, and `vault.coerce_text` is the one
  predicate for both (see "The representability boundary" below for where it
  runs):
  - `title: "\uD800"` decodes to a lone surrogate: a valid Python `str`, not a
    Unicode scalar value, so it cannot be UTF-8-encoded. `pydantic_core`, which
    renders *both* halves of the MCP result, raises
    `PydanticSerializationError` on it — note-controlled frontmatter
    manufacturing a **protocol error**, a whole class above the in-band errors
    this tool promises. Reason code `unpaired_surrogate`.
  - `title: 0x<5000 hex digits>` is *constructed* by PyYAML without complaint
    — CPython's integer-string digit limit guards decimal parsing, not hex
    literals — and then `str()` on the value raises `ValueError`. Reason code
    `not_json_representable`.

  `frontmatter_yaml` is unaffected in both cases: in the file the value is
  ordinary text. `heading`, `content` and the outline's titles are slices of
  `Path.read_text(encoding="utf-8")`, which is strict, so they cannot carry a
  surrogate at all.

A partial view is never emitted: a caller cannot tell a pruned map from the
real one. **Mutation goes through `set_frontmatter`, or through the raw block
with `edit_note(find=…)` — never a round trip of the JSON view**, and all four
docstrings say so.

### The representability boundary: scrub once, at the parse (#149)

**`_partition_frontmatter` returns a mapping every consumer can carry.** The
scrub (`_scrub_frontmatter`) runs there, once per parse, and every reader of a
parsed block inherits it: `read_note`'s fields, `extract_tags`, the indexer's
`title` column and its JSONB `frontmatter` column, the control panel's note
viewer, `move_note`'s title refresh. A key whose value nothing can render is
**dropped**, and the loss is recorded on the diagnosis (`lossy`, keyed by the
top-level frontmatter key it happened under).

**Why the boundary and not the consumers.** Three review rounds closed this
class one consumer at a time and it came back each time: `read_note`'s fields,
then `edit_note`'s selector, then — in the same review — the indexer's
`_note_title`, JSONB serialization of `notes_metadata.frontmatter`, and
`extract_tags`' *scalar-string* `tags:` branch, which the previous round's fix
to its list branch had walked straight past. Screening per consumer is a list
you have to remember to add to. A predicate at the parse cannot be forgotten by
the next consumer, because the next consumer never sees the value. Both crashes
Codex reproduced are pinned in `tests/test_issue_149_read_note_framing.py`
against the *unmodified* indexer helpers, which is the check that the
inheritance is real.

**What it removes, and what it deliberately leaves.** It removes only what
*nothing* can render: a string that is not UTF-8-encodable, an integer whose
`str()` raises, a container that contains itself, and a subtree nested deeper
than `_SCRUB_MAX_DEPTH` (64). It does **not** unify how the renderable is
rendered — dates and non-string keys come through exactly as
PyYAML built them, because each consumer's own coercion of those is
load-bearing (an indexed frontmatter value is what
`keyword_search(frontmatter=…)` matches against, and `read_note`'s JSON view
ISO-formats a date where the indexer `str()`s it). Widening the scrub into a
normalizer would silently re-key the index.

**The walk is iterative, with an explicit stack**, so the walk itself cannot
blow the stack however deep the input goes. Total work is bounded by a node
budget derived from the block's own length (a YAML document of N characters
cannot construct more than N nodes), and the frame stack carries the ids on the
current descent, so `x: &X [*X]` is recognised as recursion rather than looped
on.

**Depth is bounded too, and it is a bound on the predicate, not on the walk.**
`_SCRUB_MAX_DEPTH` (64) exists because the consumers this boundary protects are
recursive and always will be: `indexer._sanitize_value`, `_note_title`,
`copy.deepcopy` and `yaml.safe_dump` all descend a frontmatter value frame by
frame. A structure deeper than they can descend is a structure *nothing can
render*, which is precisely what this scrub removes — reached structurally
instead of scalar by scalar, and reported as `excessive_depth`.

Getting this wrong once is instructive. **The node budget bounds size and says
nothing about depth**, and the two are independent axes. A 550 KB alias chain
(`a0: &a0 {k: 1}` / `a1: &a1 {n: *a0}` / …) parses cleanly — PyYAML composes
every `a<i>` at depth one, so its own composer never recurses even though the
constructed graph is thousands deep — passed the budget with a 1,045-deep
subtree intact, and came back `valid=True, lossy={}`. `_sanitize_value` then
raised `RecursionError` on every index pass, forever, because nothing commits
and the content hash never advances: #126's failure mode, reached by a new
route. The empty loss record also walked straight through `set_frontmatter`'s
refusal, where `deepcopy` and `safe_dump` raised in turn.

**Do not fix that by converting the consumers to iterative walks.** That is
four rewrites and a standing obligation on the fifth consumer, against one
bound here that all of them inherit — the same argument that moved the whole
predicate to this boundary. 64 is generous for a real vault (Obsidian's own
frontmatter is flat; the deepest thing anyone writes by hand is a few levels of
nested mapping) and sits far below CPython's ~1,000-frame limit with room for
the several frames each consumer spends per level.

**Consequences that are not obvious:**

- A subtree past the depth bound is pruned at that point, so the levels above
  it survive and the note stays readable; only the tail nobody could render is
  gone.
- A container all of whose contents were dropped survives *pruned*
  (`{"nested": {}}`). That is the right answer for a consumer that only needs
  not to crash. It is also exactly why `read_note` omits its `frontmatter` JSON
  view for **any** loss anywhere in the block, however deep and under whatever
  unrelated key: a pruned mapping is indistinguishable from the real one, and
  this tool does not emit a partial view. `frontmatter_yaml` still carries the
  block verbatim.
- **`set_frontmatter` refuses a note whose block lost something**, by name,
  pointing at `edit_note(find=…)` or `replace_frontmatter=True`. It rewrites
  the block *from the parsed mapping*, so serializing the pruned one would
  delete those keys as a side effect of setting an unrelated one — a
  destructive write reported as a success. The refusal precedes the no-op check
  for the same reason D6 gives for the defective-block refusal. This is a
  deliberate behaviour change: `a: &A [*A]` notes used to accept a
  `set_frontmatter` and now do not, and
  `tests/test_issue_128_set_frontmatter_refusals.py` records why.
- Section writes and default full-replace are unaffected — neither goes through
  the mapping; both reattach `diagnosis.block` byte-identically.

**`frontmatter_yaml` is LF-normalized, not byte-exact — declared, not fixed.**
The read path hands the shared parser `Path.read_text()` output, which has
already applied universal-newline translation, so a CRLF or lone-CR block comes
back with LF terminators. It is the same residual a section body carries and it
has the same cause. Re-reading the file as bytes purely to serve one response
field would give `read_note` a *second* partition of the same note, which is
exactly what D3 exists to prevent, and the two could disagree. `edit_note` is
unaffected — it works from raw bytes and still reattaches a CRLF block
byte-identically — so the residual is a read-side rendering detail, stated in
both `read_note` docstrings.

**A block the parser refuses to load is a `yaml_error`, on both sides.**
`yaml.YAMLError` is not PyYAML's whole failure surface. Two shapes a user can
save escape it: `n: <6000 digits>` raises a bare `ValueError` from CPython's
integer-string digit limit, inside the *constructor*, and a few thousand nested
flow collections raise `RecursionError` out of the composer. Uncaught, both took
out `read_note` **and** every write path that diagnoses a block, on a note
nobody could then repair. `_partition_frontmatter` catches all three and
classifies them as `yaml_error` — the class that already means "the parser
refused this region" — so the consequences compose the way every other defect
does: the block stays in the body (the read still returns every byte of the
note, in `content`), section writes and `set_frontmatter` refuse by name with
the `replace_frontmatter=True` repair, and default full-replace rewrites the
file wholesale. **The rejected alternative** was to call such a block *valid*
with an empty mapping, so the read could still expose `frontmatter_yaml`: that
hands `set_frontmatter` a `{}` to merge into and `safe_dump` over the top of a
block it never parsed — a destructive write on a note whose only defect is that
it is large.

### Resolved residual: the masker's narrower-than-CommonMark fence grammar (#150)

This heading used to declare a live residual: `_FENCE_RE` recognised only a
column-zero opener closed by a run of exactly the same length, so an indented
or longer-closed fence was not masked and a heading inside it was selectable.
**#150 closed it** — see "The fence grammar every consumer shares" above for
the grammar that replaced it, the two write refusals, and the declared
re-addressing break the widening caused. Nothing here is a live constraint any
more; the history is kept only so the phrase #140's spec uses — "fenced code
block **as recognised by the shared masker**" — still has a referent.

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
