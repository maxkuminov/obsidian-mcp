## Why

Three defects, one shape. In each of them the system holds a **binding** — this
index came from that root; this usage row was produced by that credential; this
request may write to that vault — and the binding is allowed to go stale
without anything noticing. Two of them are fixed by *recording the fact where
it happens*, which is what makes a column unavoidable in both. The third is
fixed by *re-reading the fact before acting on it*, which is what makes it a
write-path refusal rather than a column.

They ride together because two of them carry migrations, and the project's
standing rule is that migration numbers are assigned up front across the whole
wave or exactly one worktree owns schema changes. **016 and 017 are assigned
here.** One `make test-schema` run and one adversarial pass covers both.

### 1. The index does not record which vault root it was built from (#91, deferred half)

`truthful-surfaces` shipped the display half of #91 and deferred this one to
the next migration-carrying wave, preserving the analysis in
`DEFERRED-91a.md`. This is that wave. The argument below has been re-derived
against the tree at `2a430f9`; where the code has moved since `1ce4c9d` it is
noted.

`_active_user_ids()` filters `vault_path IS NOT NULL`, so an unassigned user's
index rows are frozen rather than removed. Since #66 that is deliberate: every
tool is refused meanwhile — the admission gate is in `_tracked` — so the rows
are not a leak, and keeping them is what lets a reassignment resume without
re-embedding ~16.7k chunks. `mcp-request-routing` pins it: reassignment to the
same directory SHALL still find the previously indexed rows present.

**A different directory is a different question, and nothing answers it.**
`notes_metadata.file_path` is vault-relative; no row, and no column anywhere,
records which root the index was built from. After an admin repoints a user at
another vault, the metadata-only tools — `semantic_search`, `keyword_search`,
`list_notes`, `get_recent` and every graph tool, all served from the database
filtered by `user_id` alone — answer from the *previous* vault: its paths,
titles, tags, frontmatter and chunk excerpts. `read_note` on one of those paths
then either fails or, worse, returns a genuinely different note that happens to
occupy the same relative path in the new root. CLAUDE.md names "silently wrong
search results" as one of the two expensive failures in this product, because
an agent acts on them without a human ever seeing the query.

**Say the obvious objection out loud, because it is mostly right.**
`index_vault` already prunes by relative path — `deleted_paths = set(existing)
- set(files)` at `src/services/indexer.py:180` — so every stale row whose
relative path does not exist under the new root is removed on the first pass
over that root, with none of the machinery below. Two things genuinely do not
reconcile, and they are the whole justification:

1. **The window.** The prune happens at the next pass, so between the Save and
   that pass the database-backed tools answer from the previous root. The
   column does not remove this either — see the accepted residual — but it
   narrows what is served during it from *the previous vault* to *nothing*.

2. **A note identical by relative path *and* content hash in both roots.**
   `index_vault` classes it "no change" and `continue`s (`indexer.py:158-159`),
   so its links are never re-extracted. Meanwhile the notes it *pointed at*
   were pruned, and `note_links.target_note_id` is `ON DELETE SET NULL`
   (`source_note_id` is `ON DELETE CASCADE`, which is why the source's own rows
   survive). The result is a link row that keeps its `target_path` and loses
   its resolution: a resolved link silently becomes dangling, permanently,
   because nothing will ever re-extract it — the content hash matches, so the
   note is never re-parsed again. `get_backlinks`, `get_neighborhood` and
   `find_orphans` then report an under-resolved neighbourhood for that note for
   as long as it is unedited. **This is not a window; it does not heal.** It is
   the item that makes a schema change worth carrying, and it is the one a
   reviewer will not find on their own.

**Why a column rather than a comparison in the panel.** The tempting cheap fix
is to compare old and new in `edit_user_submit` and purge when they differ. It
does not work, because of the transition an operator actually performs:

    /old  →  unassigned  →  /new

Two Saves, because the panel's vault selector is how an admin takes an account
out of service before repointing it. On the second Save the handler sees
`old_vault = None`, and `None → /new` is *exactly* the shape #66 protects — a
user restored to the directory their index came from. The handler cannot tell
"reassigned back to where the index came from" (keep) from "reassigned
somewhere else" (discard), because the only thing that distinguishes them is a
value that no longer exists anywhere by the time it is needed. What is required
is not a comparison but a **record**: a value describing the root the rows were
built from, independent of the current assignment and therefore surviving the
unassignment.

**What round 3 changed, so a reviewer can check the replacements rather than
rediscover the originals.** Round 2 replaced a lexical root comparison with
`realpath` + `st_dev:st_ino` and replaced a provenance backfill with a
re-derive. Round 2's review accepted the direction and sharpened it three ways,
all of them in the same two places — how strong the identity proof is, and
whether a re-derive really finishes. This round: (1) the second signal becomes a
**kernel file handle**, because an inode number is reusable and both round-2
signals can agree about two different directories; (2) the root is **pinned as a
descriptor** and the identity, the discovery and every read come from that
descriptor, because deriving an identity from a pathname and then scanning the
pathname is check-then-act; (3) **any per-file skip makes the re-derive
incomplete and withholds the stamp**, because the existing scan `continue`s past
unreadable files and the structural claim was therefore false for them. Nothing
else about the shape moved.

**Decision: 016 adds two columns, `users.indexed_vault_path` and
`users.indexed_vault_handle`, nullable, one marked unit, written only by the
index pass — and 016 backfills neither.** The pair is the *identity of the
directory the rows were actually scanned from*, read at the head of
`index_vault(user_id)` from a descriptor pinned before `discover_markdown_files`
opens a single file, and compared against the same two facts observed for the
assigned root now. Skipped entirely for `user_id is None` — single-user mode has
no `users` row.

**Identity is not a normalised pathname, and this is the part the first draft
got wrong.** `transfer.canonical_vault_root` is `str(Path(path))` and nothing
more — its docstring says so, deliberately, because the question *it* answers is
"is this still the string the operator saved?". Reused here it is wrong in both
directions: a symlink retargeted from `/data/A` to `/data/B` under an unchanged
`/vaults/current` yields the *same* string for a *different* directory, so a
foreign index is kept; and `/vaults/current` versus `/vaults/real-a` naming one
directory yields *different* strings for the *same* one, so a good index is
destroyed and re-embedded. So the record is two facts, not one:

- `indexed_vault_path` — `os.path.realpath` of the root as it was scanned,
  proven at that moment to name the descriptor the pass pinned. This resolves
  symlinks and normalises separators, `.` and `..`, so a trailing separator or
  an aliasing symlink is never read as a reassignment.
- `indexed_vault_handle` — the **kernel file handle** of that directory, taken
  from the pinned descriptor with `name_to_handle_at(fd, "", AT_EMPTY_PATH)`
  and stored as the opaque text `"<handle_type>:<hex of f_handle>"`. Compared
  by byte equality, never parsed.

**Why a file handle and not `st_dev:st_ino` — this is round 2's blocker, and
the measurement settles it.** An inode number is *reusable*: delete the
directory, create another at the same path, and the allocator can hand back the
same number, at which point a realpath comparison and an inode comparison both
agree about two different directories and a foreign index is kept on unanimous
evidence. That is not hypothetical and it is not rare. Measured on this host
(ext4, `rmdir` + `mkdir` in a loop): **the very first recreation reused the
inode** — `dev=66306 ino=19944872` before and after — while the file handle
changed from `a85530010b6f671e` to `a8553001fbe0f6ef`. The first four bytes are
the inode number; the last four are the inode **generation counter**, which the
kernel bumps precisely so that a reused inode is not mistaken for the old one.
`name_to_handle_at` is the kernel's own answer to inode reuse, and asking it is
a rule rather than a heuristic.

Three further measurements, because they decide the rest of the design:

- **The wrapper exists.** `ctypes.CDLL(None).name_to_handle_at` resolves on
  glibc 2.39 (this host) and on 2.41 (`python:3.12-slim`, the deployment base);
  glibc has exported it since 2.14. So unlike `openat2` — which the
  `atomic-beneath-root-writes` change must reach through a raw `syscall(2)`
  number table because glibc exports no wrapper *at any version* — this one
  takes the wrapper-first shape `rename_noreplace` already uses, and needs no
  architecture table. Verified by `getattr` on both, not assumed.
- **No capability is required, and a bind mount does not disturb it.** In a
  `--cap-drop ALL --user 1000:1000` container with the repository bind-mounted
  at another path, the handle for that directory was byte-identical to the
  handle read on the host — `2fb398003402e116` both times — while the kernel's
  mount id differed (6307 in the container, 31 on the host); the design
  therefore ignores the `mount_id` the call also returns. Only
  `open_by_handle_at` needs `CAP_DAC_READ_SEARCH`, and **this design never
  calls it**: a handle is an identity to compare, never a door to open. Stated
  without overclaiming the measurement: `st_dev` was *not* observed to differ
  there (a bind mount shares the superblock, so it is the same device number),
  and the reason a reboot is now a clean keep is structural rather than
  measured — on ext4 and xfs the handle is the inode number and the inode's
  generation counter, both of which live on disk, so nothing about mounting or
  restarting moves them. Round 2's fear that an unstable `st_dev` would charge
  a discard on every restart disappears with `st_dev` itself.
- **Some filesystems have no handles, and the failure is loud.**
  `name_to_handle_at` returned `EOPNOTSUPP` for `/proc`, `/sys`, and for a
  container's own overlayfs root. Some FUSE mounts do the same. That branch is
  therefore real and is specified below rather than assumed away.

**Where the filesystem gives no handle, the keep branch does not exist.** There
is then no proof available, so the pass may not assert "same directory": it
re-derives, **every pass**, and stamps nothing — a record that cannot license a
keep is indistinguishable from no record, so writing one would only invite a
later reader to treat it as evidence. The cost is declared rather than
discovered: a re-derive is parse-and-upsert plus a full link rebuild over a
vault an ordinary pass already reads and hashes in full, and it makes **zero
embedding calls** for unchanged content — which is what makes "every pass"
affordable at ~2,577 notes on a five-minute interval, where a discard would be
tens of minutes of serial embedding. It is not free: it holds the pass's parsed
bodies in memory and rewrites every `note_links` row each pass. So it is
surfaced — **once per process per root, at warning level**, and named as the
reason in every re-derive log line — rather than silently costing an operator
CPU forever. Moving the vault onto a filesystem that supports handles is the
fix, and the log says so.

**Rejected: a UUID persisted inside the vault.** It is the obvious durable
identity and it is wrong here for a reason that has nothing to do with
correctness: it writes the server's bookkeeping into the user's own data. The
vault is Max's single source of truth, synced by Obsidian across machines; a
`.obsidian-mcp-root-id` file would be copied by every duplication of the vault
(so two copies claim one identity — the exact failure mode a UUID is supposed
to prevent), deleted by anyone tidying dotfiles, and would make a *read-only*
vault mount un-identifiable. It also inverts the trust direction: the identity
of a directory would be whatever a writer of that directory says it is. The
kernel handle is not forgeable by vault content and costs one syscall.

**Rejected: keep `st_dev:st_ino` as sufficient proof.** That is round 2's
design and the measurement above is its counterexample: it certifies a
recreated directory as the original on the first try. Rejected also as a
*supplementary* proof — the pair is recorded nowhere and compared nowhere
across time in this design. `st_dev`/`st_ino` survive in exactly one role, and
it is not a match signal: at observation time the pass checks that
`os.path.realpath(assigned)` still names the inode it pinned, which is #59's
own `_require_same_directory` idiom (the pathname and the descriptor must
describe one directory *right now*, because the recorded pathname is computed
from the name). A disagreement there means the name is moving under the pass,
and it routes to **indeterminate** — assert nothing, destroy nothing. Never to
a keep.

So the pass reaches one of six verdicts, and **a keep requires proof**:

| observed vs. recorded | verdict | what the pass does |
| --- | --- | --- |
| the assigned root cannot be opened as a directory, or its realpath no longer names the pinned inode | **indeterminate** | nothing at all: no delete, no stamp; the pass fails as it does today |
| pinned, but the filesystem returns no handle | **provenance unresolved** | **re-derive**, and stamp nothing — there is no proof to record, so every pass re-derives. Logged once per process per root |
| handle obtained, and no record at all | **provenance unresolved** | **re-derive**, then stamp at the end of the pass *if the pass was complete* |
| handle obtained, realpath equal **and** handle equal | **same directory, proven** | nothing |
| handle obtained, realpath differs **and** handle differs | **different directory** | **discard**: delete the user's `notes_metadata` (embeddings and links cascade) and stamp, in one committed transaction, before any file under the new root is read |
| handle obtained, and exactly one of the two differs | **provenance unresolved** | **re-derive**, then stamp at the end of the pass *if the pass was complete* |

Every input lands somewhere, and the interesting ones land where they should.
A directory deleted and re-created at the same path: realpath equal, handle
differs — **re-derive**, which is the round-2 blocker turned into the cheap safe
branch rather than a wrong keep. A vault restored from a backup in place: same
pathname, every inode new — **re-derive**, no re-embed, which is the outcome an
operator restoring a vault would want and would not think to ask for. A
bind-mounted alias of one directory: handle equal, realpath differs —
**re-derive**, so no vault is re-embedded on account of an alias. A retargeted
symlink under an unchanged assignment: both differ — **discard**. A reboot: both
equal — **keep**, with no re-embed, because nothing in the handle moves.

**Which error the design prefers, said plainly.** CLAUDE.md ranks silently wrong
search results above expensive ones, so the design never resolves an ambiguity
in favour of keeping — and after this round, *keeping is the only verdict that
requires positive proof*. It also never resolves an ambiguity in favour of the
*destructive* branch: a discard costs a full re-embed, so it fires only when
both independent signals agree that the directory changed. Ambiguity goes to a
branch that asserts nothing and destroys nothing. The indeterminate row is the
one place the design does nothing at all, and for a reason: you cannot re-derive
from a directory you cannot read, and destroying an index because a bind mount
was briefly unavailable buys nothing and costs the full re-embed.

**The root is pinned once, and everything the pass does runs beneath that
descriptor.** Deriving an identity from a pathname and then scanning that
pathname is check-then-act, and round 2 shipped exactly that: the reviewer's
ABA is `/vault/current` pointing at A while the identity is read, retargeted to
B before `discover_markdown_files`, and retargeted back to A before the next
pass — leaving B's rows stamped as A and then permanently accepted by the keep
branch. The fix is #59's, taken wholesale rather than reinvented: **open the
assigned root once, derive the identity from that descriptor, and perform
discovery and every file read beneath it.** `os.open(vault, O_RDONLY |
O_DIRECTORY)` pins an inode; `os.fstat` and `name_to_handle_at(fd, "",
AT_EMPTY_PATH)` describe the thing pinned rather than the thing named;
`os.scandir(fd)` and `os.open(name, dir_fd=parent)` walk downward from it. A
directory descriptor keeps naming the same directory however its pathname is
later renamed or relinked, so the verdict, the scan and the stamp all describe
one inode. There is no interval left in which the pathname can decide what gets
scanned.

Two properties of that walk are deliberate, because anchoring must not quietly
change *what the index contains*:

- **The symlink policy is unchanged.** `Path.rglob` does not descend directory
  symlinks today, and the descriptor walk descends with
  `O_DIRECTORY | O_NOFOLLOW` — the same rule, now enforced by the kernel per
  descent rather than by a library's traversal habit. A symlinked *file* at a
  discovered path is read as it is today. Anchoring is about *which directory
  is scanned*, not about containment, and this change makes no containment
  claim it did not already make: a symlinked leaf can still point outside the
  root, exactly as before, and `open_mutable` remains the guard that matters
  for writes.
- **Stat and read describe one inode.** The scan currently calls
  `full_path.read_text()` and then `full_path.stat()` — two independent
  pathname resolutions, so `file_size` and `modified_at` can describe a
  different file from the one whose bytes were hashed. Under the anchored form
  the file is opened once through the parent descriptor and `os.fstat`ed on
  that descriptor. Not the reason for the change; a free consequence of it,
  and worth having.

The walk holds one descriptor per level of depth, not per file — depth-first,
each parent closed once its children are done — so it costs the process a
handful of descriptors, not thousands. It is a *read* walk and it stays inside
`src/services/indexer.py`; see the sequencing note in `tasks.md` for why it is
deliberately not a `vault_fs` helper.

**Every pass in the indexer that reads vault files is anchored the same way,
because otherwise the stamp is a claim about one pass and the rows outlive it.**
`index_vault` is the pass that stamps, but `embed_vault`, `link_backfill_pass`
and `rebuild_tsvectors` also read `vault / file_path` by pathname and also write
rows the stamp is a claim about — `note_embeddings`, `note_links` and
`content_tsvector` respectively. A user whose notes contain no links has an
empty `note_links` table forever, so `link_backfill_pass` runs on *every*
startup and would happily write link rows read through a retargeted pathname
into a state a previous pass had stamped. Anchoring all four is one helper used
four times in one file, and it lets the structural claim be stated without an
"as of" qualifier.

**And the embedding pass verifies the hash it is about to certify.**
`embed_note` sets `note.embedded_content_hash = note.content_hash` — the
*metadata* row's hash, not a hash of the bytes it just embedded. So a file that
differs from its row at embedding time is embedded and then marked as embedded
*for the row's hash*, and nothing will ever re-embed it. That matters here
because the re-derive branch keeps `note_embeddings` on precisely the argument
that a matching `content_hash` proves the vector is the right one for that file
— an inference that is only sound if every vector actually came from content
hashing to what was stamped. So `embed_vault` re-hashes what it read and skips
the note when it does not match the row it selected; the next pass, which will
have refreshed the row, picks it up. One sha256 over bytes already in memory,
and it is what makes the "keep the embeddings" argument load-bearing rather
than merely plausible.

**The re-derive, precisely — and why it is not a compromise.** For an unresolved
root the pass runs over the assigned root with **content-hash change detection
disabled**: every discovered file is parsed and upserted regardless of its hash,
the ordinary prune removes every row whose relative path is not present under
that root, and because every note counts as changed,
`_update_links_for_changed` deletes and re-extracts **every** one of that user's
link rows and re-resolves them against an index built from those notes alone. So
after the pass, every surviving row and every link row was written by that pass
from a file under the assigned root. That is a structural claim about who wrote
the rows, not an enumeration of columns that has to be re-audited whenever a
column is added.

**A skip falsifies that claim, so a skip withholds the stamp — round 2's third
blocker.** The claim above is only true if the pass actually visited every file.
The scan does not: it `continue`s past a `UnicodeDecodeError` (`indexer.py:150`)
and past any other read failure (`:153`), the tsvector loop `continue`s for a
path it has no buffered body for (`:311`), and `_update_links_for_changed`
re-reads each changed note from disk and `continue`s on
`UnicodeDecodeError | FileNotFoundError | OSError` (`:403`). Each of those
leaves the *ordinary prune* as the only thing acting on that path — and the
prune keeps a row whose relative path exists under the new root, which is
exactly the case a re-derive exists to repair. Vault A supplied `Same.md`; vault
B holds a different `Same.md` with invalid UTF-8; the path is discovered, the
read raises, the row and its links survive untouched, and round 2's design then
tail-stamps B over them. A pass can complete "successfully" and leave a foreign
row certified.

**The rule is the simplest one that keeps the claim true: any per-file
discovery, read, stat, parse or link-extraction skip makes the re-derive
incomplete, and an incomplete re-derive is not stamped.** The pass still does
all the work it can — an unreadable file does not abort the pass, and every
readable file is still repaired — but it logs which paths kept it unstamped
(the first twenty and a count, the same offender-report shape 013 and 015 use)
and the next pass re-derives again.

Three reasons this is the right rule rather than the conservative one.
**It fails toward re-work, never toward wrongness.** The alternative shape the
reviewer offered — transactionally delete the stale rows for a skipped path, as
a fresh index would — is also correct, and it is strictly more machinery: a
second deletion path for index contents, which is the thing #64 and this
document's own panel-purge argument both refuse, and it destroys a row that may
well be the *right* row (the file was merely unreadable this second). **It keeps
provenance honest.** A permanently undecodable file leaves that user in
re-derive mode indefinitely, and that is the cost, stated plainly: the pass
never asserts an identity it could not establish. The alternative is stamping a
claim the pass cannot prove, which is the whole defect being fixed — a foreign
row silently laundered into a certified one. **And the cost is bounded and
visible.** Re-derive mode is parse-and-upsert plus a link rebuild with zero
embedding calls, on a vault an ordinary pass already reads in full; the log
names the offending paths on every pass, so an operator who is paying for it is
told which file to fix, by name, rather than left to notice a CPU bill.

**And the link rebuild reads no file at all.** `_update_links_for_changed`
currently re-reads each changed note from disk, which is both a second read of
bytes the scan already parsed and a second window in which the file can vanish —
a disappearance between the scan and the rebuild silently drops that note's
links while the scan's row stands. The scan already buffers each changed note's
parsed body in `path_to_content` for the tsvector loop, for exactly this reason
(issue #18); the link rebuild takes the same buffer. That removes the window
rather than classifying it, and a changed path absent from the buffer becomes a
skip like any other — incomplete, unstamped, retried.

`note_embeddings` are **not** deleted, and that is where the cost goes. An
embedding is a pure function of chunk text and `notes_metadata.content_hash`
proves content equality, so a vector attached to a row whose hash still matches
the file under the assigned root is *provably* the right vector for that file.
`embed_vault` already selects on `embedded_content_hash != content_hash`, so it
re-embeds exactly the notes whose content differs and nothing else.

The marginal cost over an ordinary pass is consequently small: an ordinary pass
already reads and hashes **100%** of the vault's files, so the re-derive adds
only the parse-and-upsert of the notes an ordinary pass would have `continue`d
past, plus one link re-extraction — and **zero embedding calls** for unchanged
content. Against that, a discard on this vault means re-embedding ~16.7k chunks
serially through Ollama bge-m3; CLAUDE.md's own figures (a 14 s cold provider
reload, a ≈0.47 s warm `semantic_search` that includes one embed) put a single
chunk in the hundreds of milliseconds, so 16.7k of them is **tens of minutes per
assigned user**, during which `semantic_search` answers from a shrinking
fraction of the vault. Charging that on every upgrade, to every assigned user,
is a real availability event; charging it on a deliberate reassignment the
operator just performed is not.

**Why 016 backfills nothing.** `indexed_vault_path = vault_path` looks free and
is not: "assigned now" is not "indexed from what is assigned now", and the
reassignment lag it ignores is the exact defect this change exists to close. An
admin who reassigns and deploys before the next pass gets rows from vault A
stamped as B; the next pass then sees both signals equal, takes the no-op
branch, and the identical-path/identical-hash case — the one that never heals —
is *guaranteed* suppressed rather than merely possible. NULL is the only value
that asserts nothing, and under the classification above NULL is not "stamp and
move on": it is the unresolved branch, so every legacy user is repaired once,
cheaply, on the first pass after the upgrade, and stamped only then.

This **deletes a hole rather than costing one**. The earlier draft carried a
"one-time backfill hole" for accounts already unassigned at migration time,
which got exactly one reassignment without reconciliation. Those accounts are
now NULL like everybody else and get the same repair. There is no special case
left to document.

**The stamp is written where the state it describes is established, which means
two different places.** On the discard branch the state is "this user has no
index rows", which is true the instant that transaction commits, so the stamp
goes with it at the head of the pass — and a pass that then fails while scanning
retries cleanly, because the next one finds both signals equal and simply
indexes. On the re-derive branch the state is "every row was derived from this
root", which is not true until the pass finishes, so the stamp is committed
**after** the pass's last write and only if the pass raised nothing **and
skipped nothing**. A crash mid-repair leaves no stamp and the next pass repairs
again — bounded, idempotent, and never a stamp over a half-repaired index; and
after this round, never a stamp over an index the pass could not fully visit
either. Head-stamping a re-derive would be exactly the false provenance this
section is about, written by our own code instead of by the migration.

**Deploy ordering, and what actually holds.** `make deploy` runs
`docker compose run --rm obsidian-mcp alembic upgrade head` in a one-off
container **while the old container is still running**, and only then
`docker compose up -d --force-recreate`. So an old-code index pass can be
mid-flight when 016 commits and can go on committing `notes_metadata` rows from
the old root afterwards. **Nothing serialises the two.** `index_pass_lock` is an
in-process `asyncio.Lock`; there is no advisory lock, no row lock and no
cross-container coordination, and claiming otherwise is exactly the kind of
thing this document exists to not do.

What makes it safe is not a lock but the absence of a backfill: **016 writes no
provenance, so an old pass's writes have nothing to contradict.** Old code
cannot write either column — they are not on its models and no code path sets
them — so every row is NULL when the new container starts, whatever the old pass
wrote, and the new container's first pass per user takes the unresolved branch
and re-derives from the assigned root. Overlap between the two indexer loops is
prevented by `docker compose up -d --force-recreate` being stop-then-start for
one service, not by anything in the code. The residual is therefore a property
of the deploy command: **a deploy that runs two indexing containers of this
service concurrently — a second replica, a rolling deploy, a manually started
container — can let an old pass commit rows from the old root after a new pass
has tail-stamped the new one.** `make deploy` does not do that; an operator who
changes it must quiesce the old container before migrating.

**Accepted residual.** The reconciliation runs in the indexer, so a
reassignment is honoured at the *next* pass, not at the Save. The window is
bounded by `INDEX_INTERVAL_SECONDS` (default 5 minutes) plus the duration of a
pass already in flight, and during it the metadata-only tools still answer from
the previous root. Closing it entirely means either purging inside the panel's
POST transaction — a second writer of index contents, for a five-minute
improvement — or refusing every tool for the whole interval, which breaks the
disk-backed tools that are already correct against the new root. Neither is
worth it. Same optimistic level as `edit_note(expected=…)` and the transfer
fingerprint check, and the same "takes effect at the next authenticated
request" shape as an OAuth revocation. Declared, not discovered. And note that
the *re-derive* branch does not narrow that window even to "nothing served": its
rows are replaced as the pass proceeds rather than deleted up front. That is the
price of not asserting a provenance nobody recorded, and it is paid only by the
one-time legacy population and by genuinely ambiguous identity.

**Rejected: age-based pruning.** Dropping index rows untouched for N days
invents a retention policy nobody asked for, deletes exactly the rows #66
preserves on purpose, and costs the full re-embed #66 exists to avoid when it
is wrong. Reassignment to a different root is a real event with a real trigger;
"this index is old" is not an event, and an unassigned account later restored
to its own directory is the *normal* case.

**Rejected: backfill `indexed_vault_path = vault_path`.** The failing input is
decisive and is carried into the spec as a scenario: vault A holds `Same.md`
linking to `OnlyA.md`; vault B holds a byte-identical `Same.md` and no
`OnlyA.md`; the user is indexed on A, reassigned to B, and the deploy runs
before the next pass. The backfill stamps B over rows built from A, both signals
then agree, and `Same.md`'s link is dangling forever — the never-heals case
turned from *possible* into *guaranteed* by the migration meant to prevent it.

**Rejected: force a full discard for every assigned legacy row.** Correct, and
the most expensive correct thing available. It charges tens of minutes of serial
embedding per assigned user on the upgrade, for a population in which the
genuinely drifted accounts — reassigned but not yet reindexed at migration time
— are a small minority, and it destroys vectors whose validity a content hash
already proves. The re-derive is correct by the same structural argument and
costs no embedding call for unchanged content.

**Rejected: quiesce and discard inside the migration.** It makes 016 a second
deleter of index contents, which is the rule this section already invokes
against a panel-side purge and which #64 argued for grant families; it cannot in
fact quiesce a container it does not control, since `make deploy` migrates from
a *separate* one-off container while the old app is still up; and it pays the
same full re-embed as the option above.

**Rejected: delete the skipped path's rows instead of withholding the record.**
The reviewer offered this as the other way to keep the structural claim true,
and it is correct — a fresh index would remove those rows. It is rejected for
two reasons and neither is effort. It makes the reconciliation a *second*
deletion path for index contents, keyed on a transient read failure, which is
the shape this document already refuses for a panel-side purge and which #64
argued against for grant families. And its failure direction is wrong: a file
that could not be decoded *this second* is very often the right file for the row
being deleted, so the rule would destroy valid rows — and, with them,
embeddings — in exchange for a stamp nobody is waiting for. Withholding the
stamp costs a repeat of a pass that makes no embedding calls.

**Rejected: treat an unreadable file as a fatal pass error.** Simpler to state,
and it converts one bad file into a total outage of index maintenance for that
user: no new note is indexed, no deletion is pruned, and nothing is embedded
until a human intervenes. The chosen rule does every repair it can and declines
only to *certify*.

**Rejected: infer provenance by overlapping the recorded relative paths with the
files found under the assigned root.** A threshold on a heuristic — high overlap
"means" the same root — and CLAUDE.md is explicit that a heuristic pretending to
be a rule should ship behind a flag or in shadow mode rather than absorb another
round of threshold tuning. Its failure direction is also the wrong one: two
vaults that share a directory layout produce a high overlap and a silent keep.

### 2. Transfer-route usage rows lose attribution when the credential is deleted (#92, item 2)

#77 made every MCP tool call carry a denormalised actor — `actor_kind`,
`actor_label`, `actor_ref` on `usage_logs`, bound by `APIKeyMiddleware` into
`current_actor` and written by `_log_usage` — because both credential FKs are
allowed to lose their target while the log row stays, and both do so on the
operator's most urgent path.

**The transfer routes were not covered, and CLAUDE.md records the gap as
known.** `src/transfer/routes.py::_log_row` builds its own `UsageLog` from the
*minting* identity carried on the `transfer_tokens` row: `key_id`,
`oauth_token_id`, `user_id`, and nothing else. There is no request-scoped actor
to read — the redemption request is session-less and authenticates with a
capability, not a credential — so those rows are attributed by LEFT JOIN
exactly as every row was before #77. Delete the OAuth client and every
`upload_file` / `download_file` line it produced renders "unknown"; NULL a
key's `usage_logs.key_id` before deleting it and the same thing happens. These
are the rows an operator reviewing a suspect connector most wants: the ones
where bytes entered or left the vault.

**Decision: record the actor on `transfer_tokens` at mint, copy it into the
usage row at redemption.** Migration 017 adds `actor_kind` / `actor_label` /
`actor_ref` to `transfer_tokens`, mirroring 015's types exactly
(`String(20)` / `String(255)` / `String(64)`, nullable, no server default), and
`_log_row` copies the three fields onto the `UsageLog` it builds.

In the #77 register, and for the #77 reasons:

- **Bound at mint from the credential the request already loaded.** The minting
  call is an ordinary authenticated MCP tool call — `request_upload` /
  `request_download` run under `_tracked`, inside a request `APIKeyMiddleware`
  has already resolved — so `current_actor` is *already set* and already holds
  the OAuth `client_name` that `_load_credential` alone would not (the OAuth
  branch's token lookup `outerjoin`s `oauth_clients` for exactly this). `mint_token`
  reads that ContextVar itself rather than taking a parameter, through the
  **same single reader** `_log_usage` uses, so the two cannot drift in shape or
  truncation and **no path gains a query**. This is the `plan_mint_window`
  discipline applied to a second field: the mint reads what it needs in its own
  transaction rather than trusting a caller-supplied value.
- **A snapshot, never re-derived.** The label is what the credential was called
  when the capability was minted. Re-reading it at redemption would rewrite
  history on every rename, and would fail entirely in the case the scheme
  exists for — the credential deleted.
- **One owned unit with a COMMENT marker.** 017 stamps each column with
  `denormalised actor, recorded at mint (017_transfer_token_actor)`, completes
  only a set that is all present, exactly typed, nullable, default-free **and
  marked**, and refuses anything else — a partial set, a `NOT NULL` column, a
  foreign one — naming what it found. `downgrade()` drops only marked columns,
  all-or-nothing. The same string is declared on the model
  (`TransferToken._ACTOR_COLUMN_MARKER`) so `alembic check` compares it. Type
  and width are a coincidence anyone could reproduce; the marker is the only
  evidence that *this* scheme wrote the values, which is the entire basis for
  showing them to an operator as an audit trail.
- **The backfill labels what its own FK still points at, and nothing else.**
  017 backfills `transfer_tokens` from `api_keys` and from `oauth_tokens` →
  `oauth_clients`, guarded on `actor_kind IS NULL` so a re-run cannot rewrite a
  value minting has since recorded. Worth stating precisely, because it differs
  from 015: `transfer_tokens.key_id` and `.oauth_token_id` are **`ON DELETE
  CASCADE`**, so a row whose minting credential is gone does not exist to
  label. The rows the backfill leaves NULL are therefore the ones that carry no
  credential FK at all — a single-user or sandbox mint — and they render as
  unattributed rather than as a guess.
- **A label beside a NULL `actor_kind` is drift, and 017 refuses it — 015's
  `_assert_no_orphan_labels` rule, which the first draft of this proposal
  omitted while claiming to follow 015 exactly.** The backfill's only guard is
  `actor_kind IS NULL`, so a row that already carries an `actor_label` or an
  `actor_ref` under a NULL kind would be *relabelled* from whatever credential
  its FK points at now — rewriting a recorded attribution, which is the one
  thing these columns must never do. It is reachable by a stamp-back re-run
  over a database that drift or a faulty writer has put in that state, so 017
  runs the same offender query 015 runs, before the backfill, and raises naming
  the offending ids while changing nothing. Cheap, and it is the invariant that
  makes the marker pattern safe on re-run rather than merely well-typed.
- **017 writes nothing to `usage_logs`, and that is a decision.** A transfer
  usage row written before 017 carries no link back to the token that produced
  it — there is no `transfer_token_id` on `usage_logs` and adding one to label
  history would be inventing a join that never existed. The only other
  available backfill is a re-run of 015's own credential join, which 015 owns
  and guards; two migrations writing the same three columns of the same table
  by the same rule is precisely the second resolution path #64 argued against.
  So rows in the 015→017 gap keep join-only attribution, render through the
  panel's existing pre-015 fallback, and show "unknown (credential deleted)"
  when their credential is gone. That is a bounded, closed set that only
  shrinks — the same shape as 016's one-time hole, stated rather than left to
  be found.
- **Nothing about redemption's authorisation changes.** The label is display
  and audit only and is never read for authorization, exactly as on
  `usage_logs`. `_credential_ok`, `resolve_root_ok` and the publish gate are
  untouched.

### 3. A vault reassignment is not seen by a write already in flight (#88)

Surfaced by the adversarial audit of the panel slice (PR #80) and accepted as a
documented limitation in PR #81. In multi-user mode: an MCP request
authenticates, `APIKeyMiddleware` warms the cache and binds
`current_vault_root = (user_id, /vaults/old)`; before the tool body runs an
admin commits a reassignment to `/vaults/new` and the panel reports success;
the request's snapshot still says `/vaults/old`, so its `create_note` /
`edit_note` / `write_file` lands in the *former* vault after the reassignment
was reported complete. The snapshot is deliberately immutable — that is what
makes #66's admission gate fail closed under a concurrent bulk warm — so the
staleness is a property of the design, not a bug in it. The bound is one
request's lifetime, which for a write tool includes the whole tool body.

**Decision (already taken; not re-litigated here): re-read the current root
immediately before the publish, and refuse on change. Not a `SELECT … FOR
UPDATE` gate held across the filesystem publish.** The transfer path holds row
locks across its publish because it has a token row, a bounded byte stream and
an already-open session doing nothing else; a note mutation has none of those.
Holding the credential and user rows `FOR UPDATE` across `move_note`'s link
rewrites, or across an `edit_note` on a note near `MAX_NOTE_BYTES`, would put
arbitrary vault I/O inside a lock every authenticated request contends for. The
re-read narrows the window from *one request's lifetime* to *the publish phase
of one call*, at the same optimistic level as `edit_note(expected=…)` and the
transfer fingerprint check, and says so.

**The seam.** `atomic-beneath-root-writes` — which lands before this change —
leaves the mutation path as: `open_mutable(rel, user_id)` yields a
`MutableTarget` holding a beneath-root parent descriptor, and every publish
(`_atomic_write_at`'s `linkat`/`renameat`, `move_file_no_clobber`'s
`rename_noreplace`, `soft_delete_at`) runs through that target. This change
puts the confirmation on the target: a fresh read of the assignment **stamps
every target the call is about to publish through**, and the publish helpers
**refuse a target that carries no stamp from this call**. That is what makes
inheritance structural rather than conventional — a mutating tool added later
cannot publish without one, the way a tool added later cannot skip the
admission gate.

**One confirmation per publishing operation, not one per call — the first draft
had this wrong and the correction simplifies it.** "Once per call" is fine for
the five tools that publish exactly once, and false for `move_note`. That tool's
single confirmation would have been taken before the `renameat2` and then reused
across an `async with async_session()` metadata transaction — an await of
unbounded duration — and across an arbitrary number of separate
`write_file_at` publications, one per planned link rewrite. Reusing one stamp
there is the same staleness the change exists to narrow, just relocated: an
admin who reassigns during that await sees every remaining rewrite land in the
former vault under a confirmation taken before the reassignment committed. So
the rule is the simpler one — **the confirmation is taken immediately before
each publishing operation and covers exactly that operation** — which is what
makes the residual below ("staging, flush and one publishing call") *true for
every tool* rather than true for six of seven. The extra cost is one indexed
primary-key read per rewrite, against one file read and one file write per
rewrite that the tool already performs.

**And the permanent unlink goes behind a helper, because otherwise the
structural claim is false.** `delete_note(permanent=True)` currently reaches a
bare `os.unlink(target.name, dir_fd=target.dir_fd)` — a mutation through a
`MutableTarget` that no publish helper mediates, so nothing could refuse it for
a missing stamp. It is the only bare mutating syscall left on that path. A
`MutableTarget`-based permanent-unlink helper joins `_atomic_write_at`,
`move_file_no_clobber` and `soft_delete_at` on the seam, and the direct call is
replaced by it. Without that, "the publish helpers refuse an unstamped target"
would be an accurate description of five sixths of a destructive-write surface
and a false description of the whole.

**What a mid-sequence refusal leaves, and what the caller is told.** For
`move_note(rewrite_links=True)` a per-publication confirmation can refuse
*after* the move has committed. There is nothing to roll back: the `renameat2`
happened, and `notes_metadata` and `note_links` were updated to match it, which
is correct — refusing the metadata update would leave the database describing a
note that is no longer there. So the tool **stops at the first refusal** —
every remaining rewrite would write into a vault the caller no longer has,
through descriptors pinned before the reassignment — and reports the partial
outcome explicitly: the move completed in the previous root, the assignment
changed mid-call, and these sources were left unrewritten. That reuses the
existing `failed_rewrite_sources` idiom, which already carries per-source
rewrite failures into the result string as a named warning; the reassignment is
a new reason, not a new mechanism. Silence here would be the worst option: a
half-rewritten link graph reported as a clean move is precisely the "graph
asserting a link the vault bytes do not contain" the preflight exists to
prevent.

**What "changed" means.** The database's current `users.vault_path` for the
acting user, canonicalised the way `transfer.canonical_vault_root` canonicalises
a root, compared against the root this request bound at admission
(`current_vault_root`). Refuse when it differs, when it is now NULL, when the
`users` row is gone, or when `is_active` is false — the same four conditions
`APIKeyMiddleware` and `_credential_ok` already treat as loss of entitlement.
Comparison is on the canonical *pathname*, not on a `resolve()`d form: resolving
is itself a filesystem read that a concurrent rename can change, and the fact
being checked is what the operator saved, not what the disk currently looks
like.

**Two questions, two normalisers, and they must not be merged.** Item 1 above
replaces a lexical comparison with a realpath-plus-inode identity precisely
because *its* question is "is this the directory those rows were scanned from?".
This one's question is "is this still the assignment the operator saved?", and
for it the lexical form is not a weakness but the definition: #88's harm is a
write landing in a vault the operator has moved the caller out of, and that is a
change to the record, not to the disk. A symlink retarget under an unchanged
assignment is deliberately outside #88 — #59 pins the parent descriptor exactly
so a pathname relinked mid-call cannot redirect a write, and re-resolving here
would reintroduce the check-then-act #59 removed. So `canonical_vault_root`
stays exactly as it is and gains no `resolve()`; item 1's identity helper is a
*second, separate* function, and neither may be refactored into the other.
**A fresh read, not a cache hit — and the honest cost.** Reading
`_user_vault_cache` or `current_vault_root` would be a tautology: those are the
values being checked. So this is one `SELECT users.vault_path, users.is_active
WHERE id = :uid`, and it reintroduces exactly the per-call query that #66
forbade in `_vault_root` ("Keep it a pure cache lookup … a DB query here would
be a query on every tool call"). The reconciliation is that #66's rule is about
*every tool call*, and this query runs only on **mutations**. Search, read,
list and the graph tools — which dominate the call mix by a wide margin — are
untouched, and a mutation already does far more expensive work than one indexed
primary-key lookup. Stated as a trade, not hidden: the admission gate stays a
pure cache lookup, and the mutation path gains one query.

**Scope, and what is deliberately outside it.** The six tools that publish
through a `MutableTarget` — `create_note`, `edit_note`, `move_note`,
`delete_note`, `set_frontmatter`, `write_file` — plus `delete_file`, which does
**not** go through `open_mutable` (it resolves via `_vault_context` and walks
from `vault_fs.open_root(root)`), and therefore needs its own confirmation
before `soft_delete` / `remove`. Naming that asymmetry rather than letting a
reviewer find it: the structural stamp covers six tools, and the seventh is
covered explicitly. `import_from_url` and `PUT /transfer/upload` already hold a
**stronger** gate — `lock_identity_for_publish` and `before_publish()` lock the
credential and user rows `FOR UPDATE` across the publish and re-check the root
against the one captured at mint — and SHALL NOT be weakened to this optimistic
form.

**The residual, precisely.** A reassignment that commits after the confirming
read and before the publish syscall — including one that commits while the
syscall is running — still lands in the former root, and the tool reports
success. Nothing short of a lock held across the publish closes that, and that
is the option this change rejected on purpose. What changes is the size of the
window: from the whole tool body (a read, a diff, a section resolve, an
embedding-sized payload) down to staging, `fsync` and one publishing call.

Because the confirmation is per publishing operation rather than per call, that
bound holds for `move_note(rewrite_links=True)` too — each rewrite carries its
own window rather than inheriting one taken before the move — at the cost that
the tool has *several* such windows and can therefore be refused part way
through. That partial outcome is specified above and reported, not swallowed.
The metadata transaction between the move and the first rewrite is not covered
by any confirmation and does not need to be: it writes no vault bytes, and it
describes a publication that has already happened.

## What Changes

- **`users.indexed_vault_path` and `users.indexed_vault_handle`** (migration
  016, nullable `varchar(1024)` and `varchar(320)`, no server defaults, one
  marked unit): the realpath and the opaque kernel **file handle**
  (`"<handle_type>:<hex>"`, from `name_to_handle_at`) of the directory a user's
  index was actually scanned from, written only by `index_vault` and never by an
  operator-facing handler. **016 backfills neither.**
- **The pass pins the assigned root as a descriptor**, derives both facts from
  that descriptor, and performs discovery and every vault-file read beneath it —
  in `index_vault`, `embed_vault`, `link_backfill_pass` and `rebuild_tsvectors`
  alike — so no pathname resolution can redirect what is scanned after the
  verdict is reached.
- **`index_vault` classifies the root before it scans**, and a keep requires
  proof. Realpath and handle both equal → no-op; both differ → delete the user's
  `notes_metadata` (embeddings and links cascade) and stamp, in one committed
  transaction, before any file under the new root is read; no handle available
  from the filesystem → re-derive every pass and stamp nothing, logged once per
  process per root; anything else, including no record at all → re-derive the
  index from the assigned root (change detection off, prune as usual, every link
  row re-extracted and re-resolved from the scan's own buffer, embeddings kept
  where the content hash still matches) and stamp after the pass completes;
  assigned root unopenable, or its realpath no longer naming the pinned inode →
  nothing at all. Never for `user_id is None`.
- **A re-derive that skipped any file is incomplete and is not stamped.** Any
  per-file discovery, read, stat, parse or link-extraction skip withholds the
  tail stamp; the pass logs the paths that kept it unstamped and the next pass
  re-derives again.
- **`embed_vault` verifies the hash it certifies**, re-hashing what it read
  against the row it selected and skipping on mismatch, so
  `embedded_content_hash` cannot certify a vector built from other bytes — which
  is what the re-derive's retention of `note_embeddings` rests on.
- **`transfer_tokens.actor_kind` / `actor_label` / `actor_ref`** (migration 017,
  nullable, marked): the denormalised actor, read from `current_actor` inside
  `mint_token` through the same single reader `_log_usage` uses, and copied onto
  the `UsageLog` by `_log_row` at redemption.
- **017 backfills `transfer_tokens` only**, from its own surviving FKs, guarded
  on `actor_kind IS NULL` and preceded by 015's orphan-label check (a label
  beside a NULL kind aborts the migration, changing nothing). It writes nothing
  to `usage_logs`.
- **Every vault mutation confirms the assignment before each publishing
  operation.** A fresh read stamps the target that operation publishes through;
  the publish helpers — including a new `MutableTarget`-based permanent-unlink
  helper that replaces `delete_note(permanent=True)`'s bare `os.unlink` — refuse
  an unstamped target; a changed, cleared or deactivated assignment refuses with
  nothing written and `usage_logs.params.error = "vault_reassigned"`.
- **`move_note(rewrite_links=True)` confirms before the move and before each
  link rewrite**, stops at the first refusal, and reports the partial outcome:
  which root the move landed in and which sources were left unrewritten.
- **`delete_file` confirms the same way** before its soft delete or unlink,
  since it does not publish through a `MutableTarget`.
- **The schema gate covers both migrations.** `tests/integration/
  test_schema_check.py` gains the 016 and 017 cases at the 013/014/015 bar —
  fresh shape and marker, the absence of a 016 backfill, 017's backfill
  grouping and orphan-label refusal, stamp-back idempotence, foreign and
  partial-column refusals, downgrade — and `HEAD_REVISION` becomes `017`.

## Capabilities

### Modified Capabilities
- `index-integrity`: the index records the identity of the directory it was
  scanned from — a real path plus a kernel file handle, both taken from a
  descriptor the pass pins and then scans beneath; a pass reconciles against
  that identity before it scans, keeping an index only on proof, discarding one
  whose directory demonstrably moved, and re-deriving one whose provenance it
  cannot resolve — recording the result only when the re-derive visited every
  file.
- `schema-integrity`: migrations 016 and 017, each owning its columns as a
  marked unit, with `alembic check` clean at head.
- `file-transfer`: a transfer capability records the actor that minted it, and
  the redemption's usage row carries that actor; `delete_file` confirms the
  vault assignment before it deletes.
- `vault-write`: a mutation confirms the caller's vault assignment immediately
  before each publishing operation, and refuses when it has moved; every
  destructive syscall on a mutation target, the permanent unlink included, goes
  through a helper that enforces it.

No new capability. The two migration requirements go to `schema-integrity`
rather than beside their behaviour (015 put its migration requirement in
`mcp-request-routing`) because this wave carries two migrations that
`make test-schema` gates as one unit, and splitting them across capabilities
would put one gate's contract in two places. The behaviour each migration
enables stays in `index-integrity` and `file-transfer` respectively.

The #88 requirement is **ADDED to `vault-write`, not a modification of "Note
mutations are anchored to the parent directory opened at validation"** — that
requirement is being rewritten by `atomic-beneath-root-writes`, which lands
first, and two changes modifying one requirement's text is a merge conflict
dressed up as a spec. The refusal's usage-log marker is a scenario on the new
requirement rather than a modification of `mcp-request-routing`'s "A refused
tool call is recorded in the usage log", which is about the admission gate and
stays about the admission gate.

## Impact

- `alembic/versions/016_indexed_vault_identity.py` — new
- `alembic/versions/017_transfer_token_actor.py` — new (`down_revision = "016"`)
- `src/models/db.py` — `User.indexed_vault_path` / `User.indexed_vault_handle`
  with 016's marker; `TransferToken.actor_kind` / `actor_label` / `actor_ref`
  with 017's marker
- `src/services/indexer.py` — the `name_to_handle_at` binding (wrapper-first
  `ctypes`, the `rename_noreplace` shape), the root-identity helper (pin,
  realpath, handle — its own function, **not** a change to
  `transfer.canonical_vault_root`), the descriptor-anchored discovery and read
  helpers used by all four passes, the classification at the head of
  `index_vault`, the re-derive mode with its completeness accounting, the link
  rebuild reading from the scan buffer, `embed_vault`'s hash verification, and
  the tail stamp
- `src/auth/session.py` — the one shared reader of `current_actor`, extracted
  from `tools.py::_actor_columns` so mint and log cannot drift
- `src/services/transfer.py` — `mint_token` records the actor; the pre-publish
  root confirmation helper
- `src/transfer/routes.py` — `_log_row` copies the three columns
- `src/services/vault.py` — the confirmation stamp on `MutableTarget`, the
  publish helpers' refusal of an unstamped target, and a new permanent-unlink
  helper on the same seam
- `src/mcp_server/tools.py` — the confirmation before each publishing operation,
  `move_note`'s per-rewrite confirmation and partial-outcome reporting,
  `delete_note(permanent=True)` routed through the new helper, `delete_file`'s
  own confirmation, and the `vault_reassigned` marker
- `tests/integration/test_schema_check.py` — 016 and 017 cases;
  `HEAD_REVISION = "017"`
- `tests/test_issue_91_indexed_root.py`,
  `tests/test_issue_92_transfer_actor.py`,
  `tests/test_issue_88_root_confirmed_before_publish.py` — new

Carries two migrations, so `make test-schema` is a required gate and
`make db-check` must report "No new upgrade operations detected" after deploy.
Both a migration and a write-path refusal are in the mandatory
adversarial-review category; the Codex framing is destructive writes and
silently wrong search results, and this change touches both.
