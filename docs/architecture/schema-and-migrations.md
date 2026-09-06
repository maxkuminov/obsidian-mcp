# Schema drift and migrations

> Deep rationale extracted from `CLAUDE.md`. Read before writing or reviewing a migration.

## Schema drift is not only what `alembic check` sees

`alembic check` autogenerates against the live database and reports what it
would emit. It compares tables, columns, nullability and indexes — it does
**not** compare CHECK constraint predicates. So it is the cheap gate, not the
whole gate.

Issue #53 found both kinds at once. The models declare nine columns NOT NULL
that their migrations left nullable (`api_keys.is_active/created_at`,
`notes_metadata.indexed_at`, `oauth_clients.created_at`,
`oauth_codes.used/created_at`, `oauth_tokens.revoked/created_at`,
`usage_logs.created_at`) — `alembic check` sees that, and a freshly migrated
001→012 database has it too, so it was a migration bug. Separately, the CHECK
`ck_oauth_clients_auth_method_secret` that migration 010 creates was **absent
on the live database** while `alembic_version` read 012 — `alembic check`
cannot see that at all, and neither can a lookup by constraint name, which a
same-named `CHECK (true)` would satisfy while enforcing nothing.

Migration `013_schema_reconciliation` fixes both and enforces the *complete*
010 shape rather than just the missing piece, because how the live database
lost the constraint is unknown. Its rules, all load-bearing:

- The constraint is resolved through `pg_constraint` — `conrelid`, `contype`,
  `convalidated`, and `pg_get_constraintdef` compared against the canonical
  predicate — never by name. The canonical rendering is derived at runtime from
  an empty scratch table (`CREATE TEMP TABLE … (LIKE oauth_clients)`) so it is
  the server's own normalization, not a hand-written string pinned to one
  Postgres major.
- The server default is compared **exactly** — `pg_get_expr` on `pg_attrdef`
  against a canonical default derived the same way, by setting it on a scratch
  `TEMP` table and reading it back. A substring test would accept
  `'not_client_secret_post'`, and autogenerate does not compare server defaults
  at all, so this comparison is the only thing that sees that drift.
- **Rows are never mutated to satisfy a constraint, and nothing the constraint
  reads is written before the constraint has been verified.** The offender
  query runs over the raw rows *first*, and a NULL `token_endpoint_auth_method`
  counts as an offender. **Do not "simplify" this by backfilling the NULL to
  `client_secret_post` before the check** — that was the first draft and review
  caught it. A CHECK passes when its predicate is NULL, so such a row really
  can exist on a drifted database; backfilling it manufactures a passing row,
  i.e. invents an auth method for a client and lets it authenticate with
  whatever secret it happens to carry. Only after the check passes does 013
  touch the column's type/default/NOT NULL — declarative fixes that write no
  row value, and `SET NOT NULL` then needs no backfill because a NULL would
  already have raised. A violating row makes the migration raise, naming the
  `client_id`s; the transaction rolls back and the deploy aborts before the
  container is recreated. If the column is *absent*, 013 refuses rather than
  adding it — 010 always adds it, so its absence means guessing an auth method
  for every existing client.
- `LOCK TABLE oauth_clients IN SHARE ROW EXCLUSIVE MODE` is held from before
  the offender check to the end, so no insert can land between the check and
  `ADD CONSTRAINT`. `lock_timeout=10s` / `statement_timeout=60s` make a
  blocked migration fail fast instead of stalling the deploy; they are **per
  statement / per lock acquisition**, not a budget for the transaction. 013
  `RESET`s both at the end of `upgrade()`: `SET LOCAL` lasts for the
  transaction, and `alembic/env.py` runs *every* pending revision in one, so a
  later revision would otherwise inherit them. 014 does the same for the same
  reason.
- **Lock order is child-first.** The five other tables are backfilled and set
  NOT NULL before `oauth_clients` is locked, matching the app's own direction
  (`src/oauth/routes.py` locks `oauth_codes` `FOR UPDATE`, then reads
  `oauth_clients`, then inserts `oauth_tokens`), so a concurrent OAuth request
  queues behind the migration instead of closing a wait cycle with it. That is
  an ordering guarantee, not an absence of contention: the residual behaviour
  is rollback-and-retry — past `lock_timeout` the whole transaction aborts and
  the deploy fails before the container is recreated. Re-run when quiet.
- Downgrade drops the constraint **only if it carries 013's COMMENT marker**.
  A fresh 001→013 database got its constraint from 010, so downgrading to 012
  must keep it. NOT NULL is never relaxed on downgrade — that would re-create
  the drift the migration exists to remove.
- The scratch `TEMP` tables mean the migration role needs the **TEMP
  privilege** on the database. The deploy's owner role has it; a locked-down
  migration role may not.

`tests/integration/test_schema_check.py` is the gate, and **`make test-schema`
is how you run it** — throwaway `pgvector/pgvector:pg16` up, module, container
removed pass or fail. Run it before any deploy that carries a migration; it
never reads the deploy `.env` or touches the live database. The target sets
`OMCP_REQUIRE_SCHEMA_INTEGRATION=1`, which turns the module's opt-in
`PGVECTOR_TEST_ADMIN_URL` skip into a hard failure — a gate that silently skips
because nobody exported the URL is not a gate. (Plain `pytest tests/` still
skips it, which is what you want on a machine with no Postgres.) It asserts
`alembic check` clean *and* the catalog directly *and* that the two forbidden
inserts are actually rejected, across fresh / drifted / impostor-constraint /
violating-row / NULL-method / missing-column / wrong-default / wrong-type /
`client_secret_hash NOT NULL` / nullable-method / stamp-back / downgrade paths.
Idempotence is exercised by `alembic stamp 012` then `upgrade head`, not by a
second `upgrade head` — the latter is a no-op at the alembic level and proves
nothing about the migration body.

The same module also gates **014**'s backfill, which `alembic check` is blind to
in a different way: it sees the column, its NOT NULL and its index, and nothing
about *which rows got which grant*. So the 014 cases assert the grouping
directly — one family per `(client_id, user_id)`, never a family spanning two
users, never a user's rows split across two families — plus stamp-back
idempotence (which must not re-stamp existing `grant_id`s) and the downgrade.

## 023: `indexer_state`, `chunks_truncated`, and why the CHECK is not tidiness

Migration 023 owns two units and backfills neither.

**`indexer_state (key varchar(64) PK, value text NOT NULL, updated_at
timestamptz NOT NULL DEFAULT now())`** holds three facts about the index as a
whole: the embedding fingerprint, the keyword fingerprint, and the embed
rotation cursor. A table rather than columns on an existing row because there
is no singleton row to hang them on — `users` is per tenant, `notes_metadata`
is per note, and `indexer_runs` is an append-only display history nothing reads
for a decision.

**The `ck_indexer_state_key` CHECK closes the key set, and that is a
correctness constraint rather than hygiene.** A key read from this table that
does not exist reads as *absent*, and absent is precisely the state that makes
the startup fingerprint guard **adopt** the current configuration instead of
refusing. So one mistyped key silently disables, for ever and on every start,
the guard whose entire job is to stop a same-dimension model swap from mixing
two vector spaces in one column. `ck_indexer_runs_trigger` exists for the
weaker version of the same argument, where a typo produces a mislabelled row.
Adding a key is therefore a migration, which is correct: every key here has a
startup or a scheduling consequence.

The constraint is resolved through `pg_constraint` — `conrelid`, `contype`,
`convalidated`, and `pg_get_constraintdef` against the server's own rendering
derived from a scratch `TEMP` table — **never by name**, per 013's rule, and it
carries 023's marker as its constraint comment. `alembic check` does not
compare CHECK predicates at all, so a same-named `CHECK (true)` would satisfy
every name-level lookup while enforcing nothing.

**Nothing is backfilled, and that is 016's argument.** Deriving a fingerprint
at migration time would assert that the stored vectors and tsvectors were
produced by the configuration the `.env` carries *now* — which is exactly the
claim the fingerprint exists to test, and exactly the reassignment-lag mistake
016 refuses to make with vault provenance. An absent fingerprint means
"unknown", the only true statement available at migration time, and the
application's startup adoption rule owns it from there. Because 023 writes no
row, a stamp-back re-run cannot erase a fingerprint the application has since
recorded.

**`notes_metadata.chunks_truncated BOOLEAN NOT NULL DEFAULT FALSE`** is 022's
shape for 022's reasons: the constant server default keeps it a catalogue-only
`ADD COLUMN` on a table carrying a `tsvector` and two GIN indexes, and `false`
is the *true* value for every pre-existing row, because every row that exists
when 023 runs was embedded by a chunker that had no cap and could not truncate.
A pre-existing object of either name is refused, not adopted, for the reason
022 refuses one: the vector tools read the column as whether a note's embedding
covers the whole note, and a wrong value either hides a capped note from an
agent or invents a cap that never happened.

**023 pins `SET LOCAL search_path TO public`** and asserts afterwards that the
unqualified name really resolves to `public.indexer_state`. 021 introduced the
pin and `RESET`s its own at the end of `upgrade()`, so a later revision in the
same transaction inherits nothing — the gate's redirected-`search_path` case
found 023 creating its table in the decoy schema before the pin was added.
Pinning rather than passing `schema="public"` to each `op.*` call is
deliberate: a schema-qualified table in alembic's eyes does not match a model
that declares no schema, and `alembic check` would report drift for ever after.

**The gate's asserted head moves from `022` to `023`.** Raising it is a
required part of adding a migration, not a chore beside it: the assertion is
the only thing that makes "head" a value somebody chose. Left at `022` the
module would pass on a database migrated to `023`, and its guarantee — that the
revisions it exercises are the revisions that will run — would quietly become a
guarantee about a *prefix* of them. **The earlier waves' cases stay.** 013's,
014's, 016's, 017's and 022's cases assert facts about those migrations' bodies
that no later revision restates; a gate rewritten around only the newest wave
stops testing the reconciliations the earlier ones exist to perform.

## 024: `user_sessions`, and why no cookie was grandfathered

Migration 024 creates one table, `user_sessions`, and **writes no row on any
path**. It chains from **023** (`down_revision = "023"`, the
`index-integrity-hardening` migration) and must not be merged or migrated ahead
of it; the `schema-integrity` spec delta names that ordering, and the gate's
asserted `HEAD_REVISION` moves `017 → 024` as a *modification* of the existing
head requirement rather than a second requirement beside it.

**`id` is `sha256(sid)`, and that is a schema decision, not an application
one.** The signed panel cookie carries a `secrets.token_urlsafe(32)`
identifier; the column stores its hex digest, `varchar(64)` — byte for byte the
shape `api_keys.key_hash` and `transfer_tokens` already use. The reason lives
in this note: **a `pg_dump` of this database is protected data**, taken before
every migration and retained thirty days, and the invariant is that it holds
password, API-key, OAuth and transfer-token *hashes* and deliberately no
plaintext credential. Storing the identifier verbatim would make every retained
dump a file full of live panel sessions — the invariant inverted, by one
column. The digest is unkeyed on purpose: 256 bits of CSPRNG output has nothing
to brute-force, and an HMAC under `SECRET_KEY` would make the table unreadable
after a rotation an operator may need to perform.

**The `ON DELETE CASCADE` is verified through `pg_constraint.confdeltype`,
never by constraint name.** A permanent user delete removes that user's
sessions with no handler code at all, and `User.sessions` declares
`passive_deletes=True` so the *database* cascade is what fires rather than a
per-row ORM delete that would leave the schema's cascade untested. A same-named
FK pointing at another table, or one that deletes with `SET NULL` against a NOT
NULL column, satisfies every name-level lookup while being a different
constraint. Both indexes — `ix_user_sessions_user_id` for the revocation
predicate and `ix_user_sessions_expires_at` for the purge — are resolved
through `pg_index` as column lists plus uniqueness, validity and whether they
are partial or over an expression, which is 019's and 021's rule: a name
recreated on another column keeps the name an existence check looks for while
the scan has nothing to lean on.

**Marker-owned, in 016/017/022/023's shape — a bare `create_table` is not the
house shape.** A module-level `MARKER` is stamped as a `COMMENT ON TABLE` in
the same transaction as the create and mirrored in `src/models/db.py` as
`_USER_SESSIONS_TABLE_MARKER`, so `alembic check` compares it like any other
attribute. Where the table already exists, 024 **verifies the complete shape it
would have created** — every column's type and nullability, the primary key,
the `created_at` server default, the FK's delete action, each index — and
**refuses**, naming what disagreed, rather than patching it.

**Complete, not minimal, and that distinction is the whole check.** The
constraint set and the index set are compared as *sets*: a table that has
everything 024 makes **plus** something it does not is not 024's table, so any
`UNIQUE`, `CHECK` or `EXCLUSION` constraint and any index beyond the two named
ones (and the primary key's own, read from the catalogue rather than assumed by
name) is refused and named. A subset check looked sufficient and was not — the
damaging addition is precisely the one every other check passes. `UNIQUE
(user_id)` added by hand leaves the marker, the columns, the primary key, the
cascading foreign key and both indexes exactly as 024 created them, and then
the **second** session a user opens, and every re-issue that follows their own
password change, fails on a constraint no handler has a branch for. `CHECK
(revoked_at IS NULL)` is the same trap one layer down: it makes revocation
itself raise, so logout, the administrative reset and the password change all
fail. And a bare `CREATE UNIQUE INDEX` creates no `pg_constraint` row at all,
which is why the index set is checked as well as the constraint set rather than
either one standing in for the other. `IF NOT EXISTS`
would be worse than raising: it adopts *any* table of that name, and the
session validator would then be authorizing browsers against a schema nothing
verified. The primary key and the server default are read for a reason
autogenerate makes necessary — it compares neither. A table whose PK has been
dropped reports as being in perfect agreement with the model while two rows
could claim one session-identifier hash, and validation reads exactly one row
per hash; a wrong `created_at` default is quieter still, leaving every column,
constraint and index exactly as 024 made them while the recorded age of every
session is wrong. `downgrade()` drops the table **only if it carries 024's
marker**.

**Nothing is backfilled, and a stamp-back re-run cannot sign anyone out.**
There is nothing to backfill *from*: a session that predates the table has no
identifier the registry could resolve, and inventing rows for the cookies
currently in flight would grandfather exactly the credentials the change exists
to invalidate. Because the reconciliation path writes and deletes nothing, the
gate's `alembic stamp 023` then `upgrade head` preserves existing rows — a gate
exercise must not log a live user out.

**Why grandfathering was rejected, in one line:** `get_active_session_user`
requires the cookie to carry both `user_id` and `sid`, so a correctly-signed
pre-deploy cookie is refused rather than accepted. Accepting it would keep
#198's replay window open for a further seven days after the fix shipped. The
cost is that the first deploy of this migration signs every live panel session
out exactly once — two production users, two logins.

024 pins `SET LOCAL search_path TO public` and asserts afterwards that the
unqualified name really resolves to `public.user_sessions`, for 021's reason
and 023's repetition of it: 021 and 023 both `RESET` the path at the end of
their own `upgrade()`, so a later revision in the same transaction inherits
nothing and **024 needs its own pin**. Creating the registry in a decoy schema
would leave the validator finding no row for any cookie, i.e. every user locked
out of the panel. `lock_timeout` / `statement_timeout` are set and `RESET` for
013's reason.

## Backups are protected data, not just a rollback tool

A `pg_dump` of this database is the complete text of every tenant's notes
(`note_embeddings.chunk_text`), every search query ever logged, and every
password, API-key, OAuth and transfer-token hash. Before 2026-09-04 the
dumps were written with the caller's umask (0664) into a 0775 directory, were
never pruned (95 files, 7.4 GB, back to March), and the directory was
bind-mounted read-write into the application container in direct
contradiction of the "container cannot see backups" invariant that
`control-panel.md` documents for `backups_log` (ASVS assessment findings
#181, #186, #187). The rules now:

- **`make db-backup` runs under `umask 077`, `chmod 600`s the dump, keeps the
  directory `0700`, and verifies the archive (`gzip -t`) before recording
  it.** A dump that does not verify is deleted and the target fails, so
  `make deploy` refuses to migrate.
- **Retention is bounded:** dumps older than `BACKUP_RETAIN_DAYS` (30) are
  pruned after the new one is verified and recorded, never below
  `BACKUP_RETAIN_MIN` (7) most-recent files and never the one just taken.
  Retention is the operator's, not the container's: nothing in `src/`
  touches the directory.
- **The directory is never mounted into the container.** `docker-compose.yml`
  carries no `/app/backups` volume, and `make deploy` / `make status` run
  `check-no-backups-mount`, which fails if `docker inspect` shows one. This
  is the enforcement of the invariant `control-panel.md` states; do not add
  the mount back "for convenience" — `docker/record-backup.sh` exists
  precisely so the container never needs it.
- **`.env` is `0600`.** It carries `DATABASE_URL` (with the password) and
  `SECRET_KEY`; `make init` sets the mode, and a second local account on the
  host is the reason this matters.

Encryption at rest for the dumps is not implemented; if it is added, keep a
restore-tested copy with separately managed keys before switching it on, or
the backup stops being a backup.

