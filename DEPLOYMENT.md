# Deploying on a VPS

End-to-end guide for going from "I rented a VPS" to "Claude can read
and write my Obsidian vault from anywhere." Assumes basic comfort with
SSH and editing config files. If a step doesn't make sense, the README
explains the underlying components in more detail.

This is not a one-click deploy. Five moving parts have to come
together:

1. Compute. A Linux VPS running Docker.
2. Database. PostgreSQL 16 with the `pgvector` extension.
3. Vault sync. Getting your Obsidian vault onto the VPS and keeping it
   in sync with your local edits. Nextcloud is the recommended path.
4. Embeddings. OpenAI API for VPS deployments without GPUs (Ollama on
   a CPU-only VPS is too slow to be usable).
5. TLS and reverse proxy. Caddy for the simplest path, Traefik if you
   already run it, or your own external proxy (Nginx Proxy Manager,
   nginx, …) if you already terminate TLS elsewhere.

The included `docker-compose.simple.yml` bundles 1, 2, and 5 plus the
MCP server itself. You handle 3 and 4 separately. If you already run a
reverse proxy, use `docker-compose.proxy.yml` instead — see
[Already have a reverse proxy?](#already-have-a-reverse-proxy) below.

## What you need before starting

- A VPS with at least 2 GB RAM and 20 GB disk. 4 GB / 40 GB is more
  comfortable if your vault is large or you self-host Postgres on the
  same box. Any Ubuntu/Debian/Rocky/Alma image works.
- **Linux kernel 5.6 or newer.** Path containment is enforced with
  `openat2()`, and the server exits at startup if the syscall is
  unavailable — an old kernel, or a container runtime whose seccomp
  profile blocks it. `uname -r` on the VPS. Kernel **5.8** additionally
  enables the mount check the file-transfer tools need; below it the
  server starts, logs one warning, and refuses uploads and imports while
  everything else works.
- **A filesystem that supports hard links and
  `renameat2(RENAME_NOREPLACE)`** for the vault mount — ext4 and xfs do.
  Note creation, `move_note` and the soft delete refuse with a named
  error otherwise, rather than degrading to a write that could clobber.
  `O_TMPFILE` is used where available; on a mount that refuses it (some
  NFS exports, including TrueNAS SCALE's) set
  `VAULT_ALLOW_NAMED_STAGING_FALLBACK=true`. The vault must also not
  have another mount nested underneath it — the transfer tools refuse to
  publish across a mount boundary.
- **PostgreSQL 16 with pgvector 0.8.0 or newer.** Older pgvector
  silently loses recall on filtered semantic search, so the server exits
  rather than run on it. `pgvector/pgvector:pg16` (what the bundled
  compose files use) is fine.
- A domain or subdomain (e.g. `obsidian.example.com`) with an A record
  pointed at the VPS's public IP. DNS propagation takes a few minutes.
- Ports 80 and 443 open on the VPS firewall, plus 22 for SSH.
- An OpenAI API key
  ([platform.openai.com](https://platform.openai.com)). Budget around
  $0.05 per 1k notes for the first index with
  `text-embedding-3-small`. Almost free after that.
- Either a Nextcloud instance (self-hosted or paid) or another way to
  keep your vault synced to the VPS. See the Vault sync section.

## Step 1. Install Docker on the VPS

SSH into the VPS, then:

```bash
# Debian / Ubuntu
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in so the group change takes effect
```

Verify with `docker run hello-world`. It should print "Hello from
Docker!"

## Step 2. Clone and configure

```bash
git clone https://github.com/maxkuminov/obsidian-mcp.git
cd obsidian-mcp
cp .env.example .env
$EDITOR .env
```

Minimum values you need to set in `.env`:

```env
# Public hostname Caddy/Traefik will route to
MCP_HOSTNAME=obsidian.example.com

# Database. Match what docker-compose.simple.yml will create.
DATABASE_URL=postgresql+asyncpg://obsidian_mcp:CHANGE_ME@postgres:5432/obsidian_mcp

# itsdangerous signer. Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=...

# Where on the host your vault lives (set up in Step 4)
VAULT_HOST_PATH=/home/youruser/vault

# Embeddings. OpenAI is the realistic path on a CPU-only VPS.
EMBEDDING_PROVIDER=openai
OPENAI_API_KEY=sk-...
EMBEDDING_DIMENSIONS=1024
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
```

Generate a strong DB password and use it in both `DATABASE_URL` and
the Postgres service env in the compose file (Step 3).

Before starting Caddy, generate a password hash and replace the
`$2a$14$REPLACE_WITH_BCRYPT_HASH` placeholder in `Caddyfile.example`:

```bash
docker run --rm caddy:2 caddy hash-password --plaintext 'your-password'
```

The bundled configuration protects `/admin`, `/api`, and `/authorize`,
fails closed while the placeholder remains, and answers everything it
does not name with 404. Its public matcher already forwards
`/transfer*`: those routes are public by design — the capability token
in the request is what authorises them — so if you customise the file,
keep them out of the basic-auth block.

> Leave `MCP_SANDBOX_MODE` unset (it appears commented-out in
> `.env.example`). It exists only for the Glama registry's automated
> sandbox build, where it bypasses Postgres, the embedding provider,
> the vault, and `/mcp` auth so introspection can complete. Setting it
> in production disables every external dependency.

## Step 3. Bring up Postgres, MCP, and Caddy

The repo ships a `docker-compose.simple.yml` designed for fresh VPS
deployments. It runs:

- `pgvector/pgvector:pg16` (Postgres with pgvector preinstalled)
- The MCP server itself
- Caddy as the TLS-terminating reverse proxy. It auto-issues Let's
  Encrypt certs from the hostname in your `.env`.

Build the MCP image and bring everything up:

```bash
docker compose -f docker-compose.simple.yml build
docker compose -f docker-compose.simple.yml up -d
```

The first start does a database init: it creates the `obsidian_mcp`
database, the `vector` extension, and runs alembic migrations. Watch
the logs:

```bash
docker compose -f docker-compose.simple.yml logs -f obsidian-mcp
```

You should see "Application startup complete" within ~30 seconds, then
"Starting vault index scan..." (which will report 0 files until Step
4). The control panel is at `https://your-hostname/admin` and uses the
Caddy credentials configured above.

### Already have a reverse proxy?

If you already run Nginx Proxy Manager (NPM), a standalone nginx, or
another external proxy that terminates TLS, use
`docker-compose.proxy.yml` instead. It brings up Postgres and the MCP
server on plain HTTP — no bundled Caddy — and lets your existing proxy
own certificates and HTTPS:

```bash
docker compose -f docker-compose.proxy.yml build
docker compose -f docker-compose.proxy.yml up -d
```

Set `MCP_HOSTNAME` in `.env` to the public hostname your proxy serves
— the app derives `base_url` (and the OAuth discovery URLs) from it.
Then wire your proxy to the container via one of two paths:

- **Proxy in Docker (e.g. NPM):** put the proxy and this stack on a
  shared Docker network — in `docker-compose.proxy.yml`, uncomment the
  `proxy_net` blocks (under the service and under `networks:`) and
  **remove the `ports:` mapping** — then forward your proxy to
  `http://obsidian-mcp:8000`. Nothing is published to the host.
- **Proxy on the host / another machine:** keep the default
  loopback-bound `ports:` mapping and forward to
  `http://127.0.0.1:${MCP_PORT:-8000}` (same host) or widen the bind and
  firewall the port to the proxy (another host).

Either way, forward `Host` + `X-Forwarded-Proto: https` and enable
WebSocket / streaming passthrough on `/mcp`. The file's header comment
walks through both patterns and the exact headers your proxy must
forward.

If you plan to use the file-transfer tools (`request_upload`,
`request_download`), the proxy must also forward **`/transfer/*`** —
unauthenticated at the proxy, since the capability token is checked by
the app — with request buffering off so an upload streams. Two more
constraints come with it: the token travels in the URL *fragment*, so
keep header logging **off** (Traefik's default is `drop`) and don't let
an APM capture request headers, or you will log live capabilities.

> [!WARNING]
> `docker-compose.proxy.yml` trusts proxy headers and, in single-user
> mode, relies on your reverse proxy to protect `/admin`. Keep the app's
> upstream private to the proxy (shared Docker network, or a
> loopback/firewalled published port). Do not expose the MCP container
> port publicly.

## Step 4. Get your vault onto the VPS

This is the hardest design decision in the whole stack. The MCP server
needs read-write access to the vault on the VPS filesystem. You need
your edits on your laptop or phone to propagate to the VPS, and any
agent writes on the VPS to propagate back.

### Option A. Nextcloud (recommended)

The pattern: run Nextcloud somewhere, install the Nextcloud Desktop
client on every device that edits the vault (your laptop, phone with
the Obsidian mobile app + a webdav-mounted folder, etc.), and
bind-mount the synced vault folder into the MCP container.

If you self-host Nextcloud (most flexibility):

1. Run Nextcloud via the [official Docker image][nextcloud-docker]. It
   can live on the same VPS or a different host. Both work.
2. Create a user account and install the Nextcloud Desktop client on
   your laptop. Choose your Obsidian vault folder as the sync source.
   Wait for the initial sync to complete.
3. On the VPS, find the Nextcloud data path for that user, typically
   `nextcloud_data/<username>/files/<vault-folder>`. That's what you
   set as `VAULT_HOST_PATH`.
4. The Nextcloud server has to *see* file changes the MCP server
   makes. By default it scans on a cron interval and on client-driven
   events. Set up the [Nextcloud cron job][nextcloud-cron] (or use
   `occ files:scan`) so writes from the MCP container show up in the
   next client sync.

[nextcloud-docker]: https://hub.docker.com/_/nextcloud
[nextcloud-cron]: https://docs.nextcloud.com/server/latest/admin_manual/configuration_server/background_jobs_configuration.html

If you use a hosted Nextcloud (Hetzner Storage Share, your host's
offering, etc.): mount it on the VPS via WebDAV using
[davfs2](https://savannah.nongnu.org/projects/davfs2/) or
[rclone mount](https://rclone.org/commands/rclone_mount/). rclone is
generally more reliable. Point `VAULT_HOST_PATH` at the mount.

A note on conflicts. Nextcloud handles concurrent edits with
conflict-copy files (`Note (conflicted copy 2026-04-26).md`). If you
and an agent edit the same note within seconds of each other, expect
to occasionally clean these up. Practical mitigation:

- Don't run agent writes on a note while you have it open in Obsidian.
  The MCP server stages every write, flushes it, and publishes it with a
  single atomic operation, so writes either land whole or not at all —
  but Nextcloud still sees them as "remote change while local was
  dirty."
- Nextcloud's default sync interval is fast enough that this is rare
  in practice. Most agent sessions are either read-only or write in
  batches the user reviews after.

### Option B. Obsidian Sync (paid)

Obsidian's official sync product. $4/mo. Set up sync on your devices
as normal. To get the vault onto the VPS, install
[Obsidian on the VPS in headless mode][obsidian-headless] or use a
third-party tool like [obsidian-livesync][livesync] which exposes sync
data via a CouchDB endpoint you can mount.

Easier alternative if you're paying for Obsidian Sync anyway: just use
Nextcloud on top of it (sync the same folder both ways). Nextcloud
handles the VPS side, Obsidian Sync handles cross-device.

[obsidian-headless]: https://forum.obsidian.md/t/headless-obsidian-on-a-server/47558
[livesync]: https://github.com/vrtmrz/obsidian-livesync

### Option C. Git

Treat the vault as a git repo. Agents commit their writes, you pull on
your laptop. This works *only* if you're disciplined about commit
hygiene and don't mind merging. It's the most fragile option for
real-time use, but the simplest to set up.

```bash
# On the VPS
cd /path/to/vault
git init
git remote add origin git@github.com:you/private-vault.git
```

Wire the MCP server to commit after each write. See `IMPROVEMENTS.md`
"Vault revision safety" for the rationale (it was deferred because
daily backups covered the maintainer's needs, but the design notes are
there).

### Option D. rsync from local

The crudest but most reliable: run `rsync -avz --delete` from your
laptop to the VPS on a cron or before every agent session. Agent
writes don't propagate back unless you also rsync the other direction
afterwards. Acceptable for read-only agent workflows, bad for write.

```bash
rsync -avz ~/Obsidian/MyVault/ youruser@vps.example.com:/home/youruser/vault/
```

## Step 5. Initialize the database and verify

If you used `docker-compose.simple.yml`, the database is created and
migrated automatically on first start. Verify:

```bash
docker compose -f docker-compose.simple.yml exec postgres \
  psql -U obsidian_mcp -d obsidian_mcp -c '\dt'
```

You should see the tables: `api_keys`, `notes_metadata`,
`note_embeddings`, `note_links`, `oauth_clients`,
`oauth_codes`, `oauth_tokens`, `transfer_tokens`, `usage_logs`,
`users`, plus `alembic_version`.

After Step 4 the indexer will pick up your vault on the next pass
(every 5 min). To trigger immediately, click "Reindex Now" in the
panel — that is the only on-demand trigger; `POST
/admin/settings/reindex` needs an admin panel session and a CSRF
token, so there is no headless (curl) equivalent. `make reindex`
prints these instructions.

## Step 6. Lock down the control panel

The control panel is at `https://your-hostname/admin`. The bundled Caddy
configuration denies access until you replace its placeholder basic-auth
hash. Keep that protection or replace it with one of the options below.

### Caddy basic auth (simplest)

In `Caddyfile.example` (which `docker-compose.simple.yml` uses by
default), replace the placeholder bcrypt hash. Generate a hash with:

```bash
docker run --rm caddy:2 caddy hash-password --plaintext 'your-password'
```

Restart Caddy: `docker compose -f docker-compose.simple.yml restart caddy`.

### IP allowlist

Restrict `/admin` and `/api` to your home or work IPs in the Caddy
config. Easiest if you have a static IP. Doable with a dynamic-DNS
hostname.

### OAuth via traefik-forward-auth

If you already run Traefik with `traefik-forward-auth` (Google or
Authelia), use the included `docker-compose.yml` instead of
`docker-compose.simple.yml`. The Traefik labels are pre-wired for a
`chain-oauth@file` middleware.

The `/mcp` endpoint itself is *always* API-key protected at the
application layer regardless of which option you pick. The auth above
is just for the human-facing admin UI.

## Step 7. Mint an API key and connect a client

Once the panel is locked down, log in and create an API key with
`readwrite` permission. Copy the `omcp_...` token. It's shown once.

In your MCP client (Claude Desktop config, Claude Code, n8n, etc.):

```
URL:  https://your-hostname/mcp
Auth: Bearer omcp_...
```

The first call any agent should make in a new session is
`get_vault_guide()`. That's how it learns your folder structure and
conventions before writing anything.

## Sizing and cost

Approximate steady-state cost for a single-user setup with a
3,000-note vault on a small VPS:

| Component | Spec | Cost/month |
| --- | --- | --- |
| VPS (Hetzner CX22, DigitalOcean, etc.) | 2 vCPU, 4 GB RAM, 40 GB | $5–8 |
| Domain | one TLD | $1 |
| OpenAI embeddings | first index ~$0.15, ongoing minimal | <$1 |
| Nextcloud (self-hosted on same VPS) | shared compute | $0 |
| Total | | ~$6–10 |

The first deploy's embedding spend is a one-time cost. After that you
only pay for changed-note re-embeds, which for a typical edit volume
is pennies a month.

## Common pitfalls

- DNS not propagated yet. Caddy will fail to issue a cert. Check with
  `dig +short your-hostname` from the VPS. If it doesn't return the
  VPS IP, wait or fix the A record.
- Postgres extension missing. If you use a managed Postgres that
  doesn't support `pgvector`, the indexer will crash on first
  embedding insert. Check the host's pgvector support. If absent, fall
  back to self-hosted via `pgvector/pgvector:pg16`.
- Vault path empty. The MCP container starts, but `Found 0 markdown
  files` shows in the logs. Check that your `VAULT_HOST_PATH` on the
  host actually contains `.md` files and that the bind mount is
  reading from the right place (`docker compose exec obsidian-mcp ls
  /obsidian` should show your notes).
- Nextcloud not seeing agent writes. The OS-level write happens
  immediately, but Nextcloud only knows about it on its next scan.
  Either configure the Nextcloud cron, or run `php occ files:scan
  --path="/user/files/Vault"` after a write burst.
- Embedding cost surprise. Pointing `text-embedding-3-large` at a
  20k-note vault will run around $6 for the first index. The default
  model (`text-embedding-3-small`) is about 5× cheaper. The Reset
  embeddings button in the panel makes it cheap to switch.
- Container exits immediately with a `critical` log line. Three startup
  checks can do that, and each names itself: `openat2` unavailable
  (kernel older than 5.6, or a blocking seccomp profile), pgvector older
  than 0.8.0 (`ALTER EXTENSION vector UPDATE`), or `EMBEDDING_DIMENSIONS`
  disagreeing with the live embedding column (`make reset-embeddings`).
  A placeholder `SECRET_KEY`, and `MCP_SANDBOX_MODE` set together with
  any public route — `MCP_HOSTNAME`, a non-loopback `BASE_URL`, or a
  non-loopback entry in `ALLOWED_HOSTS` — are refused the same way. A
  `*` in `ALLOWED_ORIGINS` is refused outright, sandbox or not: CORS
  runs with credentials enabled, so a wildcard origin would make the
  server reflect any Origin.
- Transfer links 404 in the browser. The reverse proxy is not forwarding
  `/transfer/*` — see Step 2 for the bundled Caddy config and the
  external-proxy notes above. If the tools themselves refuse to mint,
  check that `MCP_HOSTNAME` (or `BASE_URL`) is set: without a public
  origin the server will not hand out a localhost link.
- Uploads refuse with "the filesystem does not support…". Two different
  causes. On a mount without `O_TMPFILE` (some NFS exports) the message
  names `VAULT_ALLOW_NAMED_STAGING_FALLBACK`; setting it to `true`
  accepts named staging, which is a weaker but declared guarantee, and
  `/health` then reports `vault_named_staging_fallback_active: true`
  once a write actually uses it. On a kernel below 5.8 the mount check
  is unavailable and transfer writes refuse regardless —
  `/health` reports `transfer_mount_check_available: false`. A nested
  mount underneath the vault root is refused too: publication cannot
  cross a mount boundary.
- `/admin` exposed without auth. Don't skip Step 6. The MCP endpoint
  itself is API-key gated, but the panel can mint new keys and reset
  embeddings.
- Large notes come back truncated. Expected. If your vault holds very
  large generated notes (bulk document extracts, exported archives),
  `read_note` returns a bounded window rather than the whole note:
  `MAX_READ_RESPONSE_CHARS` defaults to 40,000 characters (~10K tokens)
  so one read cannot exhaust the calling model's context. The response
  carries the offset to continue from plus an outline of the note's
  sections, so an agent can fetch just the section it needs. Raise it in
  `.env` if your clients genuinely want larger single reads — but the
  failure mode it prevents is your inference provider rejecting the
  request outright with "input exceeds the context window", which is far
  harder to diagnose than a truncation notice.

## Day-2 operations

The `Makefile` targets in the README assume the registry-and-Traefik
deployment; on a compose-file deployment the equivalents are plain
`docker compose -f <file> …` commands. Either way:

- **Health.** `curl -s https://your-hostname/health` returns `status`
  plus `transfer_mount_check_available` and
  `vault_named_staging_fallback_active` — the two capability facts that
  are otherwise only visible in the startup log.
- **Upgrades.** Pull, rebuild, and bring the stack up again; migrations
  run on start. After a release that carries one, confirm the schema
  agrees with the models:
  `docker compose -f docker-compose.simple.yml exec obsidian-mcp alembic check`
  should print "No new upgrade operations detected."
- **Backups.** `pg_dump` the database on a schedule; the vault itself is
  covered by whatever sync you chose in Step 4. Note that a soft delete
  moves files into `.trash/` inside the vault, so your sync sees them.

  `make db-backup` also **records** each dump it takes — filename and size —
  in a `backups_log` row, which is what the panel's Health page reads to
  report backup age. It has to be a database row: the container cannot see
  the backups directory, and giving it a mount into the host's backup
  storage was the alternative rejected. So the age shown on that page is
  the age of the last backup *taken through `make db-backup`*; a dump you
  take by hand with `pg_dump` will not appear there, and the page will keep
  warning after 8 days as though none had been taken.

  **On the deploy that first ships this** (migration 021): `make deploy`
  takes its backup *before* it migrates — deliberately, because the backup
  is the only way back from a bad migration — so that one dump runs against
  a database with no `backups_log` in it yet. The target prints
  `WARNING: backup taken but not recorded` and **succeeds**; the dump is on
  disk and valid. The first *recorded* backup is the next one, i.e. the next
  deploy or a manual `make db-backup`. Until then the Health page reads "no
  backup recorded yet", which is a fresh-table state and not a failure. Once
  the table exists the disposition inverts: a backup that cannot record
  itself fails the target loudly, because a missing row makes the page
  report a staler safety net than you actually have.
- **Dependency audit.** `make audit` (pip-audit) and `make trivy` (image
  CVE scan) work from a checkout regardless of which compose file runs
  the container.

## What's not covered

- High availability. Single VPS, single Postgres, no replica. If you
  want redundancy, add managed Postgres and front the MCP container
  with a load balancer. Out of scope here.
- Vault encryption at rest. Files on the VPS disk are plain text
  unless you set up an encrypted filesystem. If you need that, look at
  LUKS for the data volume.
- Multi-user vaults are supported but not detailed here. The default
  deployment is single-vault. To run multiple users with isolated
  vaults on the same container, see the **Multi-user mode** section in
  the [README](./README.md#multi-user-mode): bootstrap flow, inviting
  users, the admin role, and rollback are covered there.
