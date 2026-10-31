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
On a Kubernetes cluster, use
[`docs/deployment-kubernetes.md`](./docs/deployment-kubernetes.md) and the
manifests in [`deploy/kubernetes/`](./deploy/kubernetes/) instead. The
vault-sync, embedding and transport sections below still apply there.

> **Upgrading an existing deployment?** A Compose install created from
> `docker-compose.simple.yml` or `docker-compose.proxy.yml` before #324
> runs its app as the database superuser and **must** follow
> [Upgrading: split database roles](#upgrading-split-database-roles)
> (one backup, one script); the new compose files do not start the app
> on an unconverted volume. Any other deployment whose `DATABASE_URL`
> names a superuser is refused at startup too (opt-out:
> `DATABASE_ALLOW_SUPERUSER=true`). The Kubernetes bundle is unaffected.
> Also read
> [Internal transport](#internal-transport-the-database-and-embedding-hops)
> and [Panel Content-Security-Policy](#panel-content-security-policy)
> before you deploy. An `http://` embedding URL to a non-loopback host
> (the `http://ollama:11434` default included) now refuses to start
> unless `.env` has `EMBEDDING_ALLOW_PLAINTEXT=true`, and a TLS
> parameter in `DATABASE_URL` or any `PGSSL*` variable is refused.
>
> **Minimum container memory (#322).** The `/mcp` body-memory budget is
> derived from the app container's cgroup memory limit (half of it, ÷ 8,
> as raw request bytes in flight), and the server **refuses to start**
> when that cannot hold one maximum request body (61 MiB with the default
> `MAX_FILE_WRITE_BYTES`). With the defaults the app container needs a
> memory limit of at least **~1.1 GiB**; the shipped compose file and the
> Kubernetes manifests set 2 GiB. A smaller container must lower
> `MAX_FILE_WRITE_BYTES`. A container with no limit uses a 1 GiB budget
> and logs a WARNING — give it a limit. Importing the settings (the
> Kubernetes `alembic` init container, 1 GiB) is unaffected: only the web
> application's startup checks. See `.env.example` and
> [rate limits](docs/architecture/rate-limits.md).

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
cp postgres.env.example postgres.env
chmod 600 .env postgres.env
$EDITOR .env postgres.env
```

The bundled stacks use **two database identities and two files** (#324):

| File | Variable | Who reads it | What it is |
| --- | --- | --- | --- |
| `.env` | `OBSIDIAN_DB_PASSWORD` | the app container and, for first-start init only, the postgres container | password of `obsidian_mcp`, a **non-superuser** role that owns the `obsidian_mcp` database. The compose file builds the app's `DATABASE_URL` from it. |
| `postgres.env` | `POSTGRES_PASSWORD` | the postgres container only | password of `postgres`, the cluster **superuser**. The app never sees it: the compose file blanks `POSTGRES_PASSWORD` in the app container even if `.env` still has one. |

Generate each with `openssl rand -hex 32`. Both must be at least 24
characters, not a placeholder, and different from each other;
`OBSIDIAN_DB_PASSWORD` may use only `A-Z a-z 0-9 . _ ~ -` because it is
placed into a URL unencoded. The postgres container checks all of this
**before** it initialises a volume and refuses to start otherwise, naming
the variable and file (never the value). `docker compose config` already
fails if `OBSIDIAN_DB_PASSWORD` is unset or `postgres.env` is missing.
`make init` generates both files with random values.

Do not set `DATABASE_URL` in `.env` for the bundles: `.env.example`
leaves it commented out, and the compose file's own value would win
anyway. It is for other deployments (an external or shared PostgreSQL,
the `make init` / `make db-init` compose-host path, where `make init`
uncomments it with the generated password). `make init` also creates
`postgres.env` on every path; a deployment against an external
PostgreSQL never reads it, and you can delete it there.

Minimum values you need to set in `.env`:

```env
# Public hostname Caddy/Traefik will route to
MCP_HOSTNAME=obsidian.example.com

# The application role's database password: openssl rand -hex 32
# (The bundled compose files build DATABASE_URL from it; do not set
# DATABASE_URL or POSTGRES_PASSWORD in .env for them.)
OBSIDIAN_DB_PASSWORD=...

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

And in `postgres.env`:

```env
# The database superuser's password: openssl rand -hex 32 (different from
# OBSIDIAN_DB_PASSWORD)
POSTGRES_PASSWORD=...
```

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

The first start does a database init: as the superuser `postgres` it
creates the role `obsidian_mcp` (`NOSUPERUSER NOCREATEDB NOCREATEROLE
NOREPLICATION NOBYPASSRLS`), the `obsidian_mcp` database owned by it,
and the `vector` extension; then the app connects as `obsidian_mcp` and
runs the alembic migrations, which need no superuser privilege. The
server refuses to start if its database session is a superuser (see
[Upgrading: split database roles](#upgrading-split-database-roles)).
If that first init fails part-way (the postgres log says why), the
postgres container refuses the half-initialised volume on its next
start; the volume holds no data yet, so remove it
(`docker compose -f docker-compose.simple.yml down -v`, which also
removes Caddy's certificate volumes, or `docker volume rm
<project>_pg_data`) and start again. Watch the logs:

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

**Which peers may set those headers is one setting: `TRUSTED_PROXY_IPS`.**
The default trusts loopback and the three private (RFC 1918) ranges, which
covers a loopback-published port and a shared Docker network without any
configuration — and also trusts every *other* container on that network.
Narrow it to your proxy's own address —
`TRUSTED_PROXY_IPS=<your proxy's address>` — once the proxy has a fixed
one. The value is a CSV or JSON list whose entries are each a single
address or one CIDR network (`10.0.0.5`, `10.0.0.0/24`); read your
proxy's address off your own deployment rather than copying an example,
since a literal here that is not your proxy trusts the wrong peer. A
proxy outside the default ranges must be listed or every request will
appear to come from the proxy, and every per-address rate limit will be
shared. The value is validated at boot, the canonical list is logged
once at startup, and uvicorn's own `--forwarded-allow-ips` /
`FORWARDED_ALLOW_IPS` is switched off in the image and in both reference
compose files (`--no-proxy-headers`), so there is no second control to
keep in step.

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

### Plaintext HTTP is refused, not redirected, on machine-facing paths

Both bundled reverse-proxy configurations answer a plaintext `http://`
request for a machine-facing path with **403 and no `Location` header**,
instead of redirecting it to HTTPS.

This looks unhelpful and is deliberate. An MCP client configured with an
`http://` server URL has already put its `Authorization: Bearer omcp_…`
on the wire in cleartext by the time the proxy sees it. A redirect on a
`POST` is a 307/308 that preserves the method and body, and MCP client
stacks follow redirects with the `Authorization` header intact — httpx,
which the MCP Python SDK uses, deliberately keeps it across a direct
http→https hop. So the call succeeds over TLS and **every request keeps
working**, which means nobody ever finds out that the credential was
sent in the clear. The same applies to `POST /token`, which carries the
authorization code, the `code_verifier` and the client secret in the
body. A hard failure on the first request is the only outcome that
reaches the operator.

| Plaintext request | Answer |
| --- | --- |
| `/mcp*`, `/transfer*`, `/health`, `/.well-known*`, `/register`, `/token`, `/revoke` | 403, no `Location` |
| `/` **with** an `Authorization: Bearer` header, any method | 403, no `Location` |
| `/admin*`, `/api*`, `/authorize` | redirect to `https://` |
| `/` with no `Authorization` header | redirect to `https://` |
| `/.well-known/acme-challenge/*` | not refused — certificate issuance must work |

The asymmetry is the control, not an oversight. A person who typed a
hostname into a browser leaks nothing by being redirected; a machine
holding a long-lived bearer token benefits from a failure. The root path
is split by the *header* rather than by the path, because there the
credential — not the URL — is what makes the request machine-facing.
`/health` is on the refusing side: it is machine-facing and has no
browser use, and the container's own healthcheck calls
`http://localhost:8000/health` **inside** the container, bypassing the
proxy entirely. If you point an external uptime monitor at this host,
configure it with `https://`; the monitor is corrected, not the rule.

**Traefik (`docker-compose.yml`).** The rule is a router on the `http`
entrypoint with an explicit `priority=200`, carrying an `ipAllowList`
middleware whose source range is `192.0.2.0/32`.

That range is RFC 5737 TEST-NET-1 — reserved for documentation, never
routable — so the allow-list matches nobody and `ipAllowList` refuses
every request with its default 403, in the proxy, without contacting the
application. **It is not a placeholder. Do not "fix" it to a real
address**; giving it anything matchable reopens the leak. `ipAllowList`
was chosen over the `errors` middleware because `errors` only fires on a
status the backend already returned, so it forwards the request first
and cannot refuse at all. The priority is written out rather than left
to Traefik's default, which is derived from the length of the rule
string — that would outrank the `http-catchall` redirect today, but a
control resting on a string length is not a contract.

**Caddy (`Caddyfile.example`).** An `http://{$MCP_HOSTNAME}` site block
`respond`s 403 for the same set and `redir`s everything else. Note that
defining it disables Caddy's automatic HTTP→HTTPS redirect for that
host, which is why the block ends with an explicit `redir`.

**Bring-your-own proxy (`docker-compose.proxy.yml`).** There is no proxy
in that file for this repository to configure, so this is prose and
nothing else: on your own proxy, add a plaintext (port 80) rule that
returns 403 for the paths in the table above — including `/` when the
request carries an `Authorization: Bearer` header, for every method —
exempts `/.well-known/acme-challenge/`, and redirects the rest. If you
skip it, a misconfigured client leaks its bearer token silently and the
deployment still appears to work.

**Rollback** is reverting the label block (or the Caddy site block) and
redeploying. There is no migration, no database row and no cached state
to unwind — plaintext requests simply go back to being redirected.

### Internal transport: the database and embedding hops

The app also opens two connections of its own: to PostgreSQL and to the
embedding endpoint. Both are held to a transport policy at startup.

**Upgrade note.** If the active embedding URL (`OLLAMA_URL`, or
`OPENAI_BASE_URL` with the OpenAI provider) is `http://` to anything other
than loopback — the `http://ollama:11434` default included — add this to
`.env` **before** deploying, or the server refuses to start with a message
naming it:

```bash
EMBEDDING_ALLOW_PLAINTEXT=true
```

It acknowledges that every chunk and search query crosses that hop
unencrypted. Remove it once the endpoint is `https`. Also move any TLS
parameter out of `DATABASE_URL` (`?ssl=…`, `?sslmode=…`) into
`DATABASE_SSL_MODE`, and unset any `PGSSL*` variable: both are refused.
Embedding clients ignore `HTTP(S)_PROXY`, `SSL_CERT_FILE` and `.netrc`.

**Reading the startup lines.** Each start logs one line per hop:

```text
Database transport: mode=prefer encrypted=False tls_version=- server_verified=False
Embedding transport: provider=ollama scheme=http host=ollama port=11434 verify=n/a plaintext_override=True
```

and one `internal_transport_plaintext` security event for each hop that is
still cleartext (`reason` = `database` or `embedding`, `outcome` = the mode or
`override`). A deployment with neither event is encrypted on both hops.

**Going strict.**

1. Create an internal CA (or reuse one) and mount its root certificate
   read-only into the app container. Issue certificates whose SANs include
   the names the app dials (the host in `DATABASE_URL`, the embedding host).
2. **PostgreSQL:** install the server certificate and key, set `ssl = on` and
   `ssl_min_protocol_version = 'TLSv1.2'`, and in `pg_hba.conf` put a
   `hostssl` row for this database and role **above** a `hostnossl … reject`
   row for the same pair; reload. Then set `DATABASE_SSL_MODE=verify-full`
   and `DATABASE_SSL_CA_FILE=<mounted root>`, deploy, and confirm the line
   reads `encrypted=True … server_verified=True`. Under a strict mode the app
   refuses a Unix-socket `DATABASE_URL` and exits if the session is not
   encrypted.
3. **Embeddings:** put the endpoint behind TLS with a certificate from the
   CA, then set its URL to `https://…`, set `EMBEDDING_CA_FILE=<mounted
   root>`, and remove `EMBEDDING_ALLOW_PLAINTEXT`.

If the database requires a client certificate, set
`DATABASE_SSL_CERT_FILE` and `DATABASE_SSL_KEY_FILE` (PEM, mounted
read-only). They apply to the strict modes only (`require`, `verify-ca`,
`verify-full`), and must be set together or not at all; either one
alone, or both with `disable`/`prefer`, refuses to start.

### Panel Content-Security-Policy

The admin panel, the login/register pages and the OAuth consent page
send a per-response nonce Content-Security-Policy with no inline script.
`PANEL_CSP` in `.env` selects it:

| Value | Effect |
| --- | --- |
| `enforce` (default) | `Content-Security-Policy` header; violations are blocked |
| `report-only` | the same policy as `Content-Security-Policy-Report-Only`: violations appear in the browser console, nothing is blocked |
| `off` | neither header |

`report-only` and `off` are rollback settings for a policy that breaks a
panel control: edit `.env` and recreate the container (`docker compose
-f <file> up -d`, or `make deploy`); no rebuild. Any value other than
`enforce` is logged at WARNING on every start, so a forgotten rollback
shows up. The `/transfer` pages keep their own policy under every value,
and any other value refuses to start.

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
  psql -U postgres -d obsidian_mcp -c '\dt' -c '\du obsidian_mcp'
```

You should see the tables: `api_keys`, `notes_metadata`,
`note_embeddings`, `note_links`, `note_tasks`, `oauth_clients`,
`oauth_codes`, `oauth_tokens`, `transfer_tokens`, `usage_logs`,
`users`, plus `alembic_version`, all owned by `obsidian_mcp`, and
`obsidian_mcp` listed with no attributes beyond login (in particular,
not `Superuser`). `exec postgres psql` connects over the container's
local socket, which the image trusts, so it needs no password.

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
  /obsidian` should show your notes). If the index already holds notes,
  the pass refuses to delete them: it fails with "vault root is empty
  but the index holds N note(s); nothing was deleted", the run is
  recorded as failed and `/health` turns `degraded` after three such
  passes. Fix the mount and the next pass carries on. If you emptied the
  vault on purpose, confirm it in the panel (Settings → Danger zone, or
  the user's page in multi-user mode); that lets the next pass prune
  once. A vault root that cannot be listed at all, or a subfolder the
  server cannot read, likewise deletes nothing: the subfolder's notes
  keep their index rows and the run record names the folder. A mount
  that is wrong but *not* empty (a different, smaller directory) is not
  detected, and its pass prunes whatever is missing from it.
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
- `make reset-embeddings` or `make rebuild-tsvectors` appears to hang.
  Both take the index generation lock, and the periodic index pass holds
  that lock for the whole of its transaction — minutes on a large vault.
  They are waiting for the pass to commit, which is the required
  behaviour: a reset that lands mid-pass is what stores vectors from one
  configuration under a fingerprint naming another. Wait it out, or pause
  the indexer from the panel first. Do not give either command a short
  lock timeout.
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
- Writes refuse with `precondition_required`. `WRITE_PRECONDITION_REQUIRED`
  defaults to `false`; enable it only after clients send the `content_hash`
  from `read_note` or `read_file(hash_only=True)` as `expected_hash`.
  Creation remains exempt. A `stale_precondition` requires a fresh read and
  reconsideration of the edit; `precondition_unavailable` means the file
  exceeds the tool's read cap and an operator must resolve that limit.
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
  are otherwise only visible in the startup log — and an `indexer`
  object. `status` is `"degraded"` when the indexer task has died, a
  counter (a scope's consecutive failed index or embed passes, its
  incomplete re-derives, or the tick's user enumeration) has reached
  `INDEXER_DEGRADED_AFTER_FAILURES` (default 3), or a note is
  quarantined. **The HTTP code is 200 either way**, deliberately: the
  Kubernetes manifests use `/health` as the liveness probe, and a
  restart loop cannot fix vault content or a provider outage. So point
  your uptime monitor at the **`status` field** — a keyword check for
  `"status":"ok"` or a JSON-path check — not at the status code, or a
  failing indexer stays invisible (#308: 8.5 days of failed passes
  under a `200 ok`). The body carries counts only; the offending path
  is in the log and on `/admin/health`.
- **Upgrades.** Check the upgrade notes below first, then pull and
  rebuild. **When the release carries a migration, stop the app before
  migrating and start it after** —
  `docker compose -f docker-compose.simple.yml stop obsidian-mcp`, then
  `docker compose -f docker-compose.simple.yml run --rm obsidian-mcp alembic upgrade head`,
  then `up -d` (the Makefile's `make deploy` does this by itself when
  `alembic current` is behind `alembic heads`). A migration that rewrites
  rows in place — 031 below and 035 are two — must not race an old-image process
  that still writes them. After a release that carries one, confirm the
  schema agrees with the models:
  `docker compose -f docker-compose.simple.yml exec obsidian-mcp alembic check`
  should print "No new upgrade operations detected."

  **Upgrading to `hybrid_search` (#354).** No migration and nothing
  re-derived or re-embedded. The release adds a 28th tool, `hybrid_search`,
  and one optional setting, `HYBRID_RRF_K` (default 60, 1–1000; an
  out-of-range value refuses to start). `hybrid_search` joins the
  `embedding` concurrency class beside `semantic_search`, so
  `CLASS_MAPPING_VERSION` moves to 5 and starts a new readiness epoch for
  `make concurrency-report`. The panel's search-analytics page gains a
  `hybrid_search` query table. Rollback is a redeploy of the previous image:
  the tool disappears from the listing, its usage rows stay (harmless), and
  the mapping version moving back starts another epoch.

  **Upgrading across migration 033 (change feed, #351).** 033 adds
  `notes_metadata.content_changed_at`, backfilled in one UPDATE from each
  note's `modified_at` (or `indexed_at` where that is NULL), and three
  change-feed tables. Nothing is re-embedded and nothing is re-derived:
  the extraction version stays 4. The feed starts with the first pass
  after the upgrade, and nothing before 033 has events. The three feed
  tables are **durable** and are dumped with their rows by `make db-backup`
  and the Kubernetes backup Job. `get_changes` joins the `scan`
  concurrency class, so `CLASS_MAPPING_VERSION` moves to 4 and starts a new
  readiness epoch. There are four new settings, all with working defaults:
  `CHANGE_FEED_RETENTION_DAYS`, `CHANGE_FEED_MAX_EVENTS_PER_SCOPE`,
  `CHANGE_FEED_MAX_EVENTS_PER_PASS` and `CHANGE_FEED_LOCK_TIMEOUT_MS`
  (README, Configuration). Rollback: the previous image reads none of these
  objects, and `alembic downgrade 032` drops them, losing only the feed
  history.

  **Upgrading across migration 032 (task listing, #350).** 032 adds the
  `note_tasks` table and two `notes_metadata` bits; it writes no row. The
  extraction version moves to 4, so the first pass after the upgrade
  re-derives every note once (the startup full-hash pass) and stores its
  checkbox tasks for `list_tasks`. Nothing is re-embedded: version 4
  shares version 3's embedding cleaner. Until that pass finishes,
  `list_tasks` counts the notes not yet scanned in its header and
  `move_note(rewrite_links=True)` returns `index_not_ready`. `list_tasks`
  joins the `scan` concurrency class, so `CLASS_MAPPING_VERSION` moves to
  3 and starts a new readiness epoch: `make concurrency-report` evidence
  from before the upgrade does not carry over. **Rollback is
  roll-forward:** a bare redeploy of the previous image re-extracts every
  note and, having no version-4 cleaner, clears every note's embedding
  certification; its embed pass then re-chunks every note and reuses every
  stored vector (#281), so no provider calls are made, while
  `semantic_search` marks those notes stale until it catches up. Rolling
  forward again is one more re-derive. No new setting.

  **Upgrading across migration 031 (keyword ranking, #344).** 031
  rewrites every keyword vector so a note's title and frontmatter
  `aliases` rank above its body; ranking is right the moment the new
  version serves. Snippets (`keyword_search`'s `snippet:` line) come
  from an excerpt that is stored only when a note's vector is written,
  so notes indexed before 031 show `head:` or no second line, the result
  header counts them, and startup logs the count. Run
  `make rebuild-tsvectors` (or, on a compose-file deployment,
  `docker compose -f docker-compose.simple.yml run --rm obsidian-mcp python -m scripts.rebuild_tsvectors`)
  **once** after the upgrade; it stores an excerpt for every note,
  touches no embeddings and makes no API calls. On Kubernetes use
  `deploy/kubernetes/base/rebuild-tsvectors-job.example.yaml` with the
  Deployment scaled to zero (the RWO vault volume), as the
  [Kubernetes guide](docs/deployment-kubernetes.md#upgrades-and-migrations)
  describes. Three optional settings arrived with it — `RANK_WEIGHTS`,
  `KEYWORD_EXCERPT_CHARS`, `KEYWORD_SNIPPET_ROWS` — documented in the
  README's Configuration table and `.env.example`.
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

  Dumps hold every tenant's note text and every credential hash, so
  `make db-backup` writes them under `umask 077` into a `0700` directory,
  verifies each one with `gzip -t`, and then prunes dumps older than
  `BACKUP_RETAIN_DAYS` (default 30) while always keeping the newest
  `BACKUP_RETAIN_MIN` (default 7). Override either on the command line or in
  `Makefile.local`. The backups directory is **never** mounted into the
  container — `make deploy` and `make status` fail if a mount at
  `/app/backups` exists — and `.env` should be `0600` (`make init` sets it).

  **What a dump contains.** Of the eighteen tables, six are a cache of the
  vault plus settings — `notes_metadata`, `note_embeddings`, `note_links`,
  `note_tasks`, `indexer_runs`, `indexer_state` — and their rows are the bulk of every
  dump (one 1024-dimension vector per chunk). `make db-backup` dumps their
  *schema only* (`pg_dump --exclude-table-data=<table>` for each), so a
  restore followed by `alembic check` is clean and the first indexer pass
  rebuilds the rows from the vault. The other twelve — users, API keys,
  OAuth clients, codes and tokens, transfer tokens, panel sessions, usage,
  quotas, `backups_log`, the concurrency counters and runs — are not
  derivable from anything and are always dumped. `BACKUP_FULL=1 make
  db-backup` dumps everything; a weekly line of that beside the daily
  default is worth it when restore *time* matters more than dump size, since
  a full dump restores a warm index instead of rebuilding one. The
  pre-migration dump that `make deploy` takes is always full for the same
  reason: it exists for an instant rollback. `make
  db-restore` accepts either kind.

  **Restore runbook.** `make db-restore FILE=<dump.sql.gz>` loads a dump
  into an *empty* `obsidian_mcp` database: it verifies the whole file first
  (`gzip -t`, pg_dump's end-of-dump marker) and loads it in one transaction
  with `ON_ERROR_STOP`, so the database is fully restored or untouched.

  1. Stop the app container so nothing writes during the load (`make down`,
     or `docker compose -f <your compose file> stop obsidian-mcp`). Leave
     PostgreSQL running.
  2. Give the restore an empty database. On a fresh volume `make db-init`
     creates it with the `vector` extension; on an existing one, as the
     superuser: `DROP DATABASE obsidian_mcp; CREATE DATABASE obsidian_mcp
     OWNER obsidian_mcp;` then `\c obsidian_mcp` and `CREATE EXTENSION
     vector;` (the app role cannot create the extension itself).
  3. `make db-restore FILE=<path>/backup_YYYYMMDD_HHMMSS.sql.gz`.
  4. Start the app (`make up`). Migrations run on start and bring an older
     dump up to the image's schema; `make db-check` must then print "No new
     upgrade operations detected." — it does for a schema-only dump too,
     because `--exclude-table-data` keeps every table's DDL, the `halfvec`
     index and `alembic_version`.
  5. **If the dump was taken without `BACKUP_FULL`, the index is empty and
     the indexer rebuilds it; there is nothing to run by hand.** An empty
     index over a non-empty vault is the ordinary first-start case, not the
     refused one (the "empty root over an existing index" guard is the
     inverse: a vault that *vanished* under a populated index). The startup
     pass is a full-hash pass over every note — metadata, keyword vectors,
     links — and the embed pass that follows it in the same tick sends every
     chunk to the embedding provider. Both take time proportional to vault
     size: the scan is minutes for a few thousand notes, the embed stage is
     chunks × provider latency (minutes on a local Ollama, longer on a
     metered API). With more than one active user the embed stage is also
     budgeted per user per pass (`EMBED_CHUNK_BUDGET_PER_USER`,
     `EMBED_TIME_BUDGET_SECONDS_PER_USER`), so a large multi-user rebuild
     spans several `INDEX_INTERVAL_SECONDS` ticks. `indexer_state` is empty
     too, so startup *adopts* the current embedding and keyword
     configuration instead of refusing on a mismatch; `make
     reset-embeddings` is only needed if you also changed
     `EMBEDDING_DIMENSIONS` or the model at the same time. Meanwhile
     `/health` reports the indexer (`status: "degraded"` only if a pass
     actually fails) and the panel dashboard shows the pending count;
     semantic search returns partial results until the embed pass finishes,
     keyword search is complete after the first pass.
  6. Take a `make db-backup` once the index is back, so `backups_log` has a
     post-restore row and the Health page stops dating the last backup from
     the dump you restored.

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

### Measuring search quality

Any change to the search path (ranking, chunking, the embedding model or
provider, query parsing, fusion, a reranker) is measured with `make
search-eval` on judgments mined from your own usage log: a search followed,
within two minutes, by a successful `read_note` of one of the paths it
returned, by the same API key or OAuth grant. The design and every rule are
in [search](docs/architecture/search.md#search-evaluation-harness-356).
`make search-eval` with no arguments runs on a committed synthetic fixture
and needs nothing; the real run is this path, from a checkout with
`requirements-dev.txt` installed:

1. **Export.** `make search-eval-export [DAYS=90] USER_ID=<your user id>`.
   It runs one read-only transaction holding one `SELECT` through the same
   channel as `make db-backup` (`DB_BACKEND=docker` or `cnpg`, set in
   `Makefile.local`), and writes the `keyword_search`, `semantic_search`,
   `hybrid_search` and `read_note` rows of the last `DAYS` days (1 to 365) to
   `.search-eval/usage-export.jsonl` (`SEARCH_EVAL_DIR` to move it): a `0700`
   directory, a `0600` file, git-ignored. **It is tenant data**: query text,
   the paths agents read, owner ids. Never commit it, upload it or paste it
   into an issue. On a multi-user deployment `USER_ID=` is required for a
   report you will attach to a PR; an unfiltered export covers every user
   and is for your private use only. `SEARCH_EVAL_DB_NAME` (default
   `obsidian_mcp`, the name `make db-backup` dumps) names the database to
   export from.
2. **Back up with the derived tables.** `BACKUP_FULL=1 make db-backup`. The
   default dump stores `notes_metadata` and `note_embeddings` as schema
   only, and replay needs their rows.
3. **Restore it into a scratch database**, never over the live one. For
   example, on a throwaway pgvector container bound to loopback:

   ```sh
   docker run --rm -d --name omcp-eval -e POSTGRES_PASSWORD=<scratch password> \
       -p 127.0.0.1:55499:5432 pgvector/pgvector:pg16
   docker exec omcp-eval createuser -U postgres obsidian_mcp
   docker exec omcp-eval createdb -U postgres eval_copy
   gunzip -c <the backup>.sql.gz | docker exec -i omcp-eval psql -q -U postgres -d eval_copy
   ```

   `SEARCH_EVAL_DATABASE_URL` is then
   `postgresql+asyncpg://postgres:<scratch password>@127.0.0.1:55499/eval_copy`.
   It is the only replay target the harness documents; it never reads
   `DATABASE_URL`.
4. **Snapshot, before any candidate.**
   `make search-eval-snapshot SEARCH_EVAL_DATA=.search-eval SEARCH_EVAL_DATABASE_URL=<scratch copy>`
   records which indexed notes each arm can reach. Taken after a candidate
   was applied, it would hide that candidate's lost notes as drift; the
   harness cannot check the order. An existing snapshot is never replaced unless
   you pass `FORCE=1`.
5. **Apply the candidate to the copy**, if it changes indexed data (a
   migration, a re-chunk, a re-embed).
6. **Score.**
   `make search-eval SEARCH_EVAL_DATA=.search-eval SEARCH_EVAL_DATABASE_URL=<scratch copy> ARMS="keyword semantic <candidate>" BASELINE=keyword`.
   It mines the export (plus `.search-eval/manual.jsonl`, operator-written
   judgments, when present), replays every arm against the copy and prints
   a table; `report.json` lands next to the export. The built-in arms call
   the server's own search services, so run it in an environment that
   carries the server's search settings (`FTS_CONFIGS`, the embedding
   provider settings, and a `SECRET_KEY`, which the settings module needs
   to import). `MIN_QUERIES` (50) and `SEED` (0) are overridable.
   `ARM_OPTS="key=value …"` passes one `--arm-opt` per pair to the replay;
   every arm of the run sees the same options. The fusion arms of
   `hybrid_search` (#354) use it: `ARMS="keyword semantic
   scripts.search_eval_arms.hybrid:make_arm
   scripts.search_eval_arms.hybrid:make_arm_alt" ARM_OPTS="hybrid_k=60
   hybrid_alt_k=20"` scores two values of `HYBRID_RRF_K` in one report
   (`make_arm` defaults `hybrid_k` to the setting; `make_arm_alt` refuses to
   run without `hybrid_alt_k`). A hybrid arm fails the run if the embedding
   provider is down during replay, because a keyword-only answer is not
   evidence for the fusion.
   The rerank arms (#367) are separate factories, so the incumbent
   `semantic` arm stays unreranked in the same report: `ARMS="semantic
   scripts.search_eval_arms.rerank:make_semantic_arm
   scripts.search_eval_arms.rerank:make_hybrid_arm" BASELINE=semantic`,
   run with `RERANK_URL` pointing at the reranker you mean to measure. Each
   probes the reranker before the first query (a failed probe exits 64)
   and fails the run on any answer that was not reranked, a timeout
   included (a query whose search finds nothing is scored as empty and
   counted per arm as `empty_candidates` in the manifest): raise `RERANK_TIMEOUT_MS` for replay if the reranker is slow,
   and say so next to the report.
7. **Attach `report.json` to the PR only if it says `attachable: true`.** It
   carries metrics, counts and hashes, no query text and no note paths. A
   `CORPUS_LOSS` verdict means the candidate lost searchable notes for that
   arm, and the run is not evidence. `INSUFFICIENT_DATA` means too few judged
   queries for a verdict. A failed or running manifest means the run did not
   finish: re-run it; `score` never falls back to an earlier run. There is no
   pass/fail threshold: the reviewer reads the deltas against the baseline.

Drop the scratch container afterwards (`docker rm -f omcp-eval`).

#### Before and after a re-chunk or re-embed

A change that alters what is embedded (the chunking format, the context
prefix, the embedding model) needs the incumbent code on the pre-change copy
for the baseline and the new code on the *same* copy after a re-embed. The
harness's `runfile:` arms carry the baseline across:

1. Steps 1–4 above: export, `BACKUP_FULL=1` backup, restore into a scratch
   copy, snapshot.
2. **Baseline generation**, from a checkout of the incumbent release:
   `make search-eval … ARMS="keyword semantic"`. Keep that generation's
   staged `semantic` arm file (`.search-eval/runs/<generation>/semantic.json`).
   **The incumbent runs first, on a drained copy.** If the copy still holds
   notes of an older chunking version (production had not finished the
   previous transition when it was backed up), first run the *incumbent*
   image over it as in step 3 until the drain query reads 0 at the
   incumbent's version, then take the baseline. For chunking version 2
   (#390), also record the **before distribution**: run the candidate
   checkout's harness with `ARMS="semantic"` on that drained copy, before
   re-embedding, and keep its `chunk_lengths` (its `legacy_chunked_notes` is
   non-zero, as expected; that run is not the candidate).
3. **Upgrade and re-embed the copy** with the candidate image, as a one-off
   container: `DATABASE_URL` is the scratch copy, the vault is mounted
   **read-only** at the path the copy's `users.vault_path` names, no port is
   published, and the provider settings are production's. Its startup runs
   the migrations and its indexer re-embeds. To finish in one sitting, set
   `EMBED_TIME_BUDGET_SECONDS_PER_USER=0` and `EMBED_CHUNK_BUDGET_PER_USER=0`
   **on that container only**. Wait until
   `SELECT count(*) FROM notes_metadata WHERE embedded_content_hash IS DISTINCT FROM content_hash OR embedded_chunking_version IS DISTINCT FROM 2`
   reads 0 (the literal is the candidate's `CHUNKING_VERSION`, 2 since #390),
   then stop it. A residue that does not drain is notes whose bytes
   changed since the backup: report it, do not chase it. Record
   `SELECT count(*) FROM note_embeddings` and the pass durations in
   `indexer_runs`; that is your production estimate. Also sum the
   `(S submitted, R reused)` counts over the container's
   `Embedding complete…` log lines: `S` is the provider work the transition
   costs, `R` the vectors it reused.
4. **Candidate generation**, from the candidate checkout:
   `make search-eval … ARMS="keyword semantic runfile:<baseline semantic file>" BASELINE=runfile:<baseline semantic file>`.
   Each semantic arm in `report.json` carries its `retrieval_fingerprint`
   (the embedding settings, the chunking version and a digest of
   `EMBEDDING_QUERY_INSTRUCTION`, never the text) and the copy's
   `legacy_chunked_notes`, counted for the export's owner. That count must
   be 0 for the report to be evidence. Since #390 it also carries
   `chunk_lengths` (`chunks`, `p10`/`p50`/`p90`, `whole_short`, `under_min`,
   `raw_over_window`), and `under_min` must be 0 too. The report's count is
   scoped to the export's owner; the statement below is the same predicate
   **tenant-wide** (every owner on the copy), so a non-zero value there with
   a zero in the report is another tenant's chunks. It is exactly this
   statement, with the effective minimum (200 at the default `CHUNK_SIZE`):
   `SELECT count(*) FROM note_embeddings ne WHERE length(btrim(regexp_replace(ne.chunk_text, '[\t\n\v\f\r ]+', ' ', 'g'))) < 200 AND (SELECT count(*) FROM note_embeddings o WHERE o.note_id = ne.note_id) > 1`
5. **The release rule for a chunking change (#390).** For the candidate
   arm's label, read
   `deltas[<label>][<slice>][<metric>]["ci95"]` for `<slice>` in `overall`
   and `class:prose` and `<metric>` in `recall@10` and `ndcg@10`. **Accept**
   when all four have `ci95[1] >= 0` (the interval contains zero or lies
   above it); **reject** when any has `ci95[1] < 0`. **Re-measure, never
   accept,** when the `verdict` is `INSUFFICIENT_DATA` or `CORPUS_LOSS`, or
   any of the four `ci95` is null.
6. Attach `report.json` only if `attachable: true`, as above.

### Calibrating search confidence

`semantic_search` and `hybrid_search` put `confidence: ok | low` on every
page (#389). `low` means no keyword note matched all the terms and the best
similarity of the call's candidates is strictly below
`SEARCH_LOW_CONFIDENCE_SIMILARITY`. The setting ships at **0, which is off**
(every page reads `ok`). A false `low` on a good page pushes an agent away
from the right notes, so the value is chosen from your own usage, not
guessed. bge-m3's candidate is 0.50; another model needs its own number.

1. **Accrue evidence at 0.** Run the release with the threshold at 0 for at
   least **14 days and** until **each** tool has at least **200**
   clicked-through pages (a page one of whose notes the agent then read).
   Every page's usage row records `confidence`, `top_similarity` and, on
   `hybrid_search`, `keyword_all`, whatever the threshold.
2. **Tabulate a single-user export.**
   `make search-eval-export USER_ID=<your user id>` (step 1 of "Measuring
   search quality" above), then
   `python -m scripts.search_eval mine --export .search-eval/usage-export.jsonl --out .search-eval`
   (it writes `confidence-rows.jsonl` beside the judgments) and
   `python -m scripts.search_eval confidence --data .search-eval --threshold 0.50`.
   It reads only the mined files and connects to no database. It prints one
   line per tool,
   `<tool>: false-low <rate> (<n>/<m> clicked-through pages, <d> days) — PASS|FAIL|INSUFFICIENT_DATA at < 1/50`,
   then counts per query class and the `top_similarity` deciles of clicked
   and unclicked pages. Rows logged before the release carry no
   `confidence` and are excluded; keyword-only pages are included.
3. **Pick and accept.** Pick the threshold at the noise ceiling of the
   unclicked distribution and re-run step 2 at that value. **Accept it only
   if both lines read PASS**: under 1 in 50 clicked-through pages would
   read `low`. Fewer than 14 days or 200 clicked-through pages (zero
   included) is `INSUFFICIENT_DATA`, never a pass. One setting enables both
   tools, so both lines must pass; a deployment that rarely uses one tool
   cannot enable either until it does.
4. **Set it** (`SEARCH_LOW_CONFIDENCE_SIMILARITY=<value>` in `.env`),
   recreate the container, and re-run the tabulation after a further
   period. Re-calibrate from step 1 whenever the embedding model changes.

The output (`.search-eval/confidence-report.json` and the table) carries
the mine run's `provenance` and `attachable`. **Only attachable output may
leave `.search-eval/`**: a multi-user tabulation begins with `NOT
ATTACHABLE (multi-user provenance)` and stays private. The reference
deployment sets a non-zero value only after both lines pass. `mine` binds the rows to
its report by digest, so `confidence` refuses (exit 64) when
`confidence-rows.jsonl` and `mine-report.json` come from different `mine`
runs — re-run `mine` instead of mixing files.

### Reranker (optional)

`semantic_search` and `hybrid_search` accept `rerank=true`, which asks a
cross-encoder reranker to reorder the candidates the call already fetched
(at most 50 notes, each sent as its title and its best-matching chunk, cut
to 2,048 characters). Nothing uses it until you set `RERANK_URL`, and the
default setup does not. **It is not a recommended default**: on a laptop CPU
scoring 10 documents takes several seconds, so 50 candidates time out at the
1.5 s default on every call. Treat it as a GPU-only option, and measure it
with the harness (above) on your own judgments before telling agents to use
it. The reranker receives the query and candidate note text, so give it the
same trust as your embedding endpoint.

One wire format is supported: the Jina-compatible `/v1/rerank` that
`llama-server --reranking` serves (vLLM and Infinity serve it too). As an
example only, with a `bge-reranker-v2-m3` GGUF of your choice:

```sh
llama-server --reranking -m <path to bge-reranker-v2-m3 GGUF> \
    --host 127.0.0.1 --port 8081 -ngl 99
```

or the same in a container on the server's network. Then, in `.env`:

```sh
RERANK_URL=http://127.0.0.1:8081/v1/rerank
# RERANK_TIMEOUT_MS=1500
```

A reranker on another host needs `https`, or `RERANK_ALLOW_PLAINTEXT=true`
for plaintext (the embedding override does not cover it). Restart, then
check:

- the log has one `Rerank transport:` line and `Reranker probe passed`;
- `/health` reports `"rerank": "enabled"` (`disabled` means the probe
  failed: the log has one CRITICAL naming the reason, and the server keeps
  re-probing every 60 s, doubling up to 10 minutes, so a reranker that
  starts after the server is picked up on its own);
- `semantic_search(query, rerank=true)` says `reranked: true (candidates: N)`.

A reranker that is slow, down or answering garbage never fails a search:
the call returns the unreranked page marked `reranked: false (<reason>)`,
and the usage row records `rerank_reason`. TEI's native `/rerank` and
Cohere's API are not supported in this release, and there is no
authentication header.

**Upgrading to this release:** no migration and nothing to re-index. The
argument is new and optional. Rolling back is a redeploy of the previous
image; a client still sending `rerank` is then refused by the
unknown-argument rule.

## OAuth CIMD and the resource gate (#357)

**Migration 036** adds `oauth_clients.client_id_metadata_url` (nullable) and
the CHECK `ck_oauth_clients_cimd_shape`. There is no backfill and no rewrite,
and it runs in seconds. Run `make test-schema` before merging, as for every
migration, and `alembic check` must be clean afterwards. The downgrade
(`alembic downgrade 035`) **refuses while any CIMD client row exists**. Delete
those clients on the panel's OAuth page first; their grants go with them, and
their users reconnect.

**What changes on the first request.** The AS metadata advertises
`client_id_metadata_document_supported: true`. On their next fresh connection
Claude, Claude Code and ChatGPT send an `https` `client_id` instead of calling
`/register`. Existing DCR grants are untouched. The rollback lever is
`OAUTH_CIMD_ENABLED=false` plus a recreate (no rebuild). Clients then fall back
to DCR, and CIMD grants keep refreshing.

**Outbound access.** The server fetches each client's metadata document over
`https` from the pod/container (Claude's documents are on `claude.ai`). The
fetch uses the `import_from_url` SSRF policy, so an egress policy that blocks
public HTTPS breaks CIMD. Clients then see `invalid_client`, and the log shows
`oauth_authorize_refused reason=client_metadata.fetch_failed` or
`client_metadata.ssrf_refused`.

**Verifying a connection took the CIMD path.** Look for one record per approval:

```bash
kubectl logs deploy/obsidian-mcp | grep '"msg": "oauth_consent_granted"' \
  | jq -c '{registration_kind, cimd_host, outcome}'
# {"registration_kind":"cimd","cimd_host":"claude.ai","outcome":"created"}
```

A DCR registration shows `oauth_client_registered` with
`registration_kind: dcr`. On a compose host use `docker logs obsidian-mcp`.

**The 14-day `resource` gate (follow-up #404).** RFC 8707 `resource` is classified on every
`/authorize` and `/token` request and never refused in this release. A later
change may refuse a foreign value with `invalid_target`, but only after **14
consecutive days with no `resource_class: unexpected`** on any record. The
`unclassified_rate_limited` and `unclassified_malformed` classes are excluded.
Count per day:

```bash
kubectl logs deploy/obsidian-mcp --since=24h \
  | jq -r 'select(.resource_class != null) | [.resource_class, (.resource_host // "")] | @tsv' \
  | sort | uniq -c
```

Any `unexpected` line names only the host the client sent. Record it in
#404 before anything is enforced.

## Upgrading across migration 035 (tags in keyword search, #391)

Migration 035 puts a note's tags into its keyword vector, so
`keyword_search` (and `hybrid_search`'s keyword arm) finds a note by a word it
carries only as a tag — frontmatter `tags` or an inline `#tag`, and for a
nested `#a/b` also `a` and `b`. The vector becomes `A(title) || A(aliases)
|| B(tags) || D(body)`: a tag hit ranks below a title hit and above a body
hit under the unchanged `RANK_WEIGHTS` default. `matched_in` gains `tags`.
No new setting, no schema object, nothing re-embedded, no extraction-version
bump.

- **What it does.** Under the index generation lock, in SQL from the stored
  `title`, `frontmatter` and `tags` columns (no file access), it adds the
  tags part to every vector that has none yet and whose tags produce a word.
  Untagged notes are not touched. It rewrites each tagged vector once:
  measured at about 30–40 seconds for 4,265 synthetic notes of 1,800 words
  with three tags each on a local PostgreSQL 16 (most of it the rewrite, the
  rest the preflight); shorter notes take less. On Kubernetes that time is
  added to the `Recreate` gap.
- **Stop the app before migrating** (stop → migrate → start). An old-image
  process that writes a vector after 035 commits writes it without tags, and
  035 never runs again. `make deploy` stops the app by itself when a
  migration is pending; Kubernetes `Recreate`s. If you migrate a compose
  stack by hand and get this wrong, `make rebuild-tsvectors` repairs it.
- **All or nothing, with a size preflight.** Before writing anything, 035
  estimates every rewritten vector against PostgreSQL's 1 MiB tsvector limit
  and aborts, naming the row ids, if any would exceed it; any error during
  the rewrite also fails the whole migration naming the row id and SQLSTATE.
  Nothing is committed, so the database is still at 034. **Under Kubernetes
  `Recreate` an abort is downtime**: the old pod is already gone and the new
  pod's `migrate` initContainer fails, so nothing serves until you revert the
  image digest.
- **Check before merging.** Run this read-only statement against the live
  database (034) first. It reports counts and ids only. It shows one FTS
  config (`english`); the multi-config form follows it.
  `flagged` must be `0` before you upgrade.

  ```sql
  WITH tagged AS (
    SELECT id, content_tsvector AS v, tags
    FROM notes_metadata
    WHERE content_tsvector IS NOT NULL AND cardinality(tags) > 0
      AND length(ts_filter(content_tsvector, '{b}')) = 0
  ), toks AS (
    SELECT t.id, x.lexeme, sum(cardinality(x.positions)) AS npos
    FROM tagged t
    CROSS JOIN LATERAL unnest(t.tags) AS g(tag)
    CROSS JOIN LATERAL (VALUES (translate(g.tag, chr(2) || chr(3), '')),
                               (replace(translate(g.tag, chr(2) || chr(3), ''), '/', ' '))) AS f(txt)
    CROSS JOIN LATERAL unnest(to_tsvector('english', f.txt)) AS x
    GROUP BY t.id, x.lexeme
  ), b AS (
    SELECT id, sum((octet_length(lexeme) + 1) / 2 * 2 + 2 + 2 * least(npos, 256)) AS bytes
    FROM toks GROUP BY id
  ), est AS (
    SELECT t.id,
      (SELECT coalesce(sum((octet_length(lexeme) + 1) / 2 * 2
         + CASE WHEN cardinality(positions) > 0 THEN 2 + 2 * cardinality(positions) ELSE 0 END), 0)
       FROM unnest(ts_filter(t.v, '{a}')))
    + (SELECT coalesce(sum((octet_length(lexeme) + 1) / 2 * 2
         + CASE WHEN cardinality(positions) > 0 THEN 2 + 2 * cardinality(positions) ELSE 0 END), 0)
       FROM unnest(ts_filter(t.v, '{d}')))
    + b.bytes AS bytes
      , length(ts_filter(t.v, '{a}')) = 0 AS d_only
    FROM tagged t JOIN b USING (id)
  )
  SELECT count(*) AS tagged_rows,
         count(*) FILTER (WHERE bytes > 1048511 OR d_only) AS flagged,
         array_agg(id ORDER BY id) FILTER (WHERE bytes > 1048511 OR d_only) AS ids,
         count(*) FILTER (WHERE d_only) AS d_only,
         max(bytes) AS largest
  FROM est;
  ```

  **With several FTS configs** (the stored fingerprint names them; for
  example `english,simple`), use this form instead: one `to_tsvector(cfg,
  txt)` branch per config, combined with **`UNION ALL`** and grouped by id
  and lexeme afterwards. Plain `UNION` is wrong: it removes duplicate rows,
  and the same tag word parsed under two configs gives two identical
  `(id, lexeme, npos)` rows, so `UNION` counts it once while 035's head
  holds it under both configs — an under-estimate that lets a near-limit row
  pass (Codex impl r3: 1,048,060 under `UNION` against the preflight's
  1,048,828).

  ```sql
  WITH tagged AS (
    SELECT id, content_tsvector AS v, tags
    FROM notes_metadata
    WHERE content_tsvector IS NOT NULL AND cardinality(tags) > 0
      AND length(ts_filter(content_tsvector, '{b}')) = 0
  ), toks AS (
    SELECT id, lexeme, sum(npos) AS npos FROM (
      SELECT t.id, x.lexeme, cardinality(x.positions) AS npos
      FROM tagged t
      CROSS JOIN LATERAL unnest(t.tags) AS g(tag)
      CROSS JOIN LATERAL (VALUES (translate(g.tag, chr(2) || chr(3), '')),
                                 (replace(translate(g.tag, chr(2) || chr(3), ''), '/', ' '))) AS f(txt)
      CROSS JOIN LATERAL unnest(to_tsvector('english', f.txt)) AS x
      UNION ALL
      SELECT t.id, x.lexeme, cardinality(x.positions) AS npos
      FROM tagged t
      CROSS JOIN LATERAL unnest(t.tags) AS g(tag)
      CROSS JOIN LATERAL (VALUES (translate(g.tag, chr(2) || chr(3), '')),
                                 (replace(translate(g.tag, chr(2) || chr(3), ''), '/', ' '))) AS f(txt)
      CROSS JOIN LATERAL unnest(to_tsvector('simple', f.txt)) AS x
    ) AS per_config
    GROUP BY id, lexeme
  ), b AS (
    SELECT id, sum((octet_length(lexeme) + 1) / 2 * 2 + 2 + 2 * least(npos, 256)) AS bytes
    FROM toks GROUP BY id
  ), est AS (
    SELECT t.id,
      (SELECT coalesce(sum((octet_length(lexeme) + 1) / 2 * 2
         + CASE WHEN cardinality(positions) > 0 THEN 2 + 2 * cardinality(positions) ELSE 0 END), 0)
       FROM unnest(ts_filter(t.v, '{a}')))
    + (SELECT coalesce(sum((octet_length(lexeme) + 1) / 2 * 2
         + CASE WHEN cardinality(positions) > 0 THEN 2 + 2 * cardinality(positions) ELSE 0 END), 0)
       FROM unnest(ts_filter(t.v, '{d}')))
    + b.bytes AS bytes
      , length(ts_filter(t.v, '{a}')) = 0 AS d_only
    FROM tagged t JOIN b USING (id)
  )
  SELECT count(*) AS tagged_rows,
         count(*) FILTER (WHERE bytes > 1048511 OR d_only) AS flagged,
         array_agg(id ORDER BY id) FILTER (WHERE bytes > 1048511 OR d_only) AS ids,
         count(*) FILTER (WHERE d_only) AS d_only,
         max(bytes) AS largest
  FROM est;
  ```

  The estimate counts the title part, the body part and the tags part
  separately, as 035 does (a word in both a note's title and its body is
  stored once but rebuilt twice), strips the U+0002/U+0003 characters
  from tags as 035 does, and counts the tag words one tag at a time, so it
  is never smaller than the tags part 035 builds. A row is flagged when its estimate is over the limit, **or** when its
  vector has no title part at all (`d_only`: a note 031 left at the size
  limit). 035 builds such a row's whole title-and-aliases head, which the
  stored vector does not show, so the statement flags every one of them
  rather than guess. A flag on a short note whose title is only stop words
  (`The`) is harmless. Fix a flagged row before upgrading: shorten the note
  (its body or its tags), or let the current image rewrite it through its
  size retreat — edit the note, or run `make rebuild-tsvectors` — after which
  it has a title part and the size check applies to it; then run the
  statement again.
- **If 035 fails anyway.** The database is still at 034: serve the previous
  image (on Kubernetes, revert the digest), shorten the named note's body or
  tags so that its vector is smaller, let the previous image re-index it,
  and deploy again.
- **Optional afterwards.** A tagged note that already had a title part keeps
  its body positions shifted by the old head's length; matching and
  single-part ranking are exact, and a cover spanning the tags and the body
  scores marginally lower until the note is next written. `make
  rebuild-tsvectors` makes every vector byte-exact; it is not required.
- **Rollback.** `alembic downgrade 034` (with the new image) strips every tags
  part, then deploy the previous image.
- Afterwards, `alembic check` should print "No new upgrade operations
  detected."

## Upgrading to migration 034 (heading-aware chunks)

Migration 034 adds five nullable columns (the chunk's heading path, section
ordinal and context prefix on `note_embeddings`; the chunking version and an
input digest on `notes_metadata`) and two CHECKs. It is metadata-only plus one
constraint validation over the embedding table, which takes seconds. Nothing
is backfilled.

- **Startup does not refuse.** The stored embedding fingerprint is
  unchanged, so there is no `make reset-embeddings`. The startup log gains an
  `Embedding retrieval: {…}` line.
- **The vault is re-embedded in the background.** Every note embedded before
  034 is selected again, note by note, after any real edit, and the work is
  held to `EMBED_TIME_BUDGET_SECONDS_PER_USER` / `EMBED_CHUNK_BUDGET_PER_USER`
  per pass **in every mode, single-user included**. Heading-aware chunking
  makes more, smaller chunks (expect roughly 1.3× to 2.5× the old count), so
  on a vault of a few thousand notes and a GPU Ollama this is tens of minutes
  of provider time spread over a few hours of passes. Measure it on a
  restored copy first (above) if you want a number.
- **What you see meanwhile.** Search keeps answering from the old vectors
  until each note's new ones land. Old rows have previews but no `§`
  locator. Every `semantic_search` / `find_related` response carries an
  `index_transition: N notes not yet re-chunked; …` line until your notes have
  drained, because ranking across the two generations is approximate. The
  dashboard's pending count starts near your note count and falls each pass;
  a budget stop logs one WARNING per tenant per pass and is not a run error.
  `/health` stays `ok`; its `indexer.rechunk` field reads `"in_progress"`
  and then `"complete"` (a state, never a count). Queries share Ollama with
  the re-embed batches, so a search can wait up to one batch (2–3 s).
- **Rollback.** Roll forward if you can. Otherwise run `alembic downgrade 033`
  with the **new** image first: it deletes the heading-era vector rows and
  un-certifies their notes (the old build would otherwise reuse vectors of
  prefixed input as if they were the bare chunk's), then deploy the old
  image, which re-embeds those notes. Until it does, they are missing from
  semantic search.

## Upgrading to chunking version 2 (#390, minimum chunk length)

No migration and no new setting. **This is a second gradual re-embed of the
whole vault, right after the 034 one above**: version 2 stops storing chunks
shorter than 200 characters (a short section is grouped with its
neighbours), cuts windows at a line or word start, and sends long whitespace
runs collapsed. The minimum is a constant tied to the chunking version, not
a setting, so every previously valid `CHUNK_SIZE` still starts (below 100 the
minimum is half the window).

- **Startup does not refuse**, and the startup `Embedding retrieval:` line
  shows `"chunking_version":2`.
- **What you see.** The same transition as 034's: every note is selected
  again in the background under the per-tenant budgets; the
  `index_transition: N notes not yet re-chunked; …` line returns on
  `semantic_search`, `find_related` and `hybrid_search` until your notes
  drain; `/health` reads `indexer.rechunk: "in_progress"` and then
  `"complete"`; the dashboard's pending count jumps to about your note count
  and falls each pass. Each pass logs
  `Embedding complete: N notes, X chunks (S submitted, R reused)`: a section
  that did not change keeps its vector, so expect fewer provider calls than
  034's transition. Previews now start on a word, and a merged chunk's
  preview can include an inner heading line.
- **Rollback.** Deploy the previous image. There is no schema to undo, but
  that build re-embeds every note stamped 2 in full (it reuses nothing
  across the versions).

## Upgrading: split database roles

**Who needs this:** a deployment from `docker-compose.simple.yml` or
`docker-compose.proxy.yml` whose database volume was created before
#324. Those bundles created the database with `POSTGRES_USER=obsidian_mcp`,
which the PostgreSQL image makes the **cluster superuser**, and the app
connected as it. A compromised app could then read every database,
run shell commands in the postgres container (`COPY … PROGRAM`) and
change roles. Fresh installs now get a separate superuser `postgres` and
a non-superuser `obsidian_mcp` that owns only its database; this
procedure converts an existing volume to exactly that shape, keeping
all data. It is manual and one-time: nothing converts a cluster
automatically.

**Who does not:** the Kubernetes bundle (`deploy/kubernetes/`) already
has this shape, and a deployment pointed at an external PostgreSQL where
`make db-init` created the role is not a superuser either. If the
server refuses to start with "is a PostgreSQL superuser" on such a
deployment, the role in `DATABASE_URL` really is one: switch it to a
non-superuser owner of the database (or, as a stop-gap, set
`DATABASE_ALLOW_SUPERUSER=true`).

### What happens if you skip it

The new compose files will not serve on an unconverted volume:

- With a **new** `OBSIDIAN_DB_PASSWORD` (the expected case), the
  migrations fail to authenticate and the app container logs
  `database authentication failed for role obsidian_mcp; a Compose install
  created before #324 must run docker/upgrade-split-db-roles.sql. See
  DEPLOYMENT.md 'Upgrading: split database roles'`, then restarts.
- With `OBSIDIAN_DB_PASSWORD` set to the **old** password, the app
  connects as the still-superuser role and refuses to start with
  `Database role obsidian_mcp is a PostgreSQL superuser. Refusing to
  start …`.

The postgres container itself starts normally on the old volume (once
both new passwords are configured), which is what lets you run the
script.

### Steps

Run every step from the repository checkout that holds your bundle, with
`docker-compose.simple.yml` replaced by `docker-compose.proxy.yml` if that
is your bundle.

1. **Stop the app first,** with the compose file you are running today, so
   nothing is connected as the old superuser while you work:

   ```bash
   docker compose -f docker-compose.simple.yml stop obsidian-mcp
   ```

2. **Back up.** Nothing below deletes data, and the conversion is one
   transaction that rolls back entirely if its self-check fails, but take
   a dump first:

   ```bash
   docker compose -f docker-compose.simple.yml exec -T postgres \
     pg_dump -U obsidian_mcp obsidian_mcp | gzip > obsidian_mcp-pre-324.sql.gz
   ```

3. **Pull** the new version of the repository.
4. **Create `postgres.env`** from `postgres.env.example` (`chmod 600`)
   with a new `POSTGRES_PASSWORD` (`openssl rand -hex 32`). This becomes
   the superuser's password.
5. **Edit `.env`:** add `OBSIDIAN_DB_PASSWORD` with a **new** value
   (`openssl rand -hex 32`, different from the superuser's). Do not reuse
   the old password: it was the superuser's, and nothing checks that you
   changed it. Remove `POSTGRES_PASSWORD` and `DATABASE_URL` from `.env`;
   the compose file now builds `DATABASE_URL` itself and blanks
   `POSTGRES_PASSWORD` in the app container, but neither belongs there.
6. **Start the database only,** so it is recreated with the new
   entrypoint and `postgres.env` (the app stays stopped):

   ```bash
   docker compose -f docker-compose.simple.yml up -d postgres
   ```

7. **Run the conversion script.** It takes both passwords from the
   postgres container's own environment, so they never appear on the host
   command line, and connects over the container's local socket:

   ```bash
   docker compose -f docker-compose.simple.yml exec -T postgres sh -c \
     'psql -X -v ON_ERROR_STOP=1 -U obsidian_mcp -d obsidian_mcp \
           -v admin_pw="$POSTGRES_PASSWORD" -v app_pw="$OBSIDIAN_DB_PASSWORD"' \
     < docker/upgrade-split-db-roles.sql
   ```

   It renames the bootstrap superuser to `postgres` (with the
   `postgres.env` password), creates `obsidian_mcp` as a non-superuser
   (with the `.env` password), and gives it ownership of the database,
   every table, sequence, view, function, type and schema in it, and any
   default privileges the old role had set. The `vector` extension stays
   with `postgres`, as on a fresh install. After the commit it terminates
   every session still connected under the old superuser identity (a
   session that logged in as the old `obsidian_mcp` would otherwise keep
   superuser rights under the new name) and fails if any is left. It
   prints `upgrade-split-db-roles: done …` on success.

   Running it again checks the cluster against the fresh-install shape:
   if everything matches it prints `already split` and changes nothing
   (it also removes a temporary role an interrupted earlier run may have
   left behind); if the cluster is only partly split (the database still
   owned by `postgres`, `obsidian_mcp` with extra attributes such as
   `CREATEDB`, objects or default privileges still owned by the
   superuser) it exits non-zero with `… is not in the fresh-install
   shape: …` and lists each problem, for you to fix by hand as `postgres`.

8. **Restart the database** before the app connects, so no session from
   before the conversion survives in any form:

   ```bash
   docker compose -f docker-compose.simple.yml restart postgres
   ```

9. **Start everything:**

   ```bash
   docker compose -f docker-compose.simple.yml up -d
   ```

The script relies on the image's default `trust` for local-socket
connections; if you hardened `pg_hba.conf`, run the same `psql` with
explicit credentials for the existing superuser. It turns statement and
error-statement logging off for its own sessions, so a failing step does
not write a password into the postgres container's log. A logging
extension you added yourself (such as `pgaudit`) is outside that switch
and may still record the `ALTER ROLE … PASSWORD` statements.

**If the script fails,** it prints why and its transaction rolls back:
the cluster is as it was, apart from a temporary role that the next run
removes. A missing password reads `admin_pw is not set` / `app_pw is
empty`; a cluster that is neither the old nor the new shape is refused
with a description of what it found. The one failure that comes after
the commit is `… session(s) of the old superuser identity are still
connected`: the conversion is done, and step 8's restart clears them.

**Rollback.** The script changes roles, not data, and the previous
release works with either shape of cluster:

- **After converting:** check out the release you ran before (its
  compose files build the image from the checkout), restore its `.env`
  without `POSTGRES_PASSWORD`, and set `DATABASE_URL` in it to
  `postgresql+asyncpg://obsidian_mcp:<OBSIDIAN_DB_PASSWORD>@postgres:5432/obsidian_mcp`
  (the new runtime password). Then `docker compose -f <bundle> up -d
  --build`. The app runs as the non-superuser; nothing else changes.
- **Without converting** (to keep running on the old superuser for a
  while): check out the release you ran before and its `.env`
  unchanged, and `up -d --build`. Nothing on the volume has changed.
  Keeping the **new** files on an unconverted volume instead
  (`DATABASE_ALLOW_SUPERUSER=true` and `OBSIDIAN_DB_PASSWORD` set to the
  old password) works only when the old password passes the postgres
  container's checks: at least 24 characters, only `A-Z a-z 0-9 . _ ~
  -`, not a placeholder, and different from `POSTGRES_PASSWORD`. An
  install on the old `changeme` default or any short password cannot do
  this; use the previous release.

## What's not covered

- High availability. Single VPS, single Postgres, no replica. If you
  want redundancy, add managed Postgres. The MCP server itself runs as
  exactly one process (its rate limits are in-process), so a load balancer
  over several replicas is not an option. This holds on Kubernetes too (see
  [deployment-kubernetes.md](./docs/deployment-kubernetes.md#constraints-the-manifests-encode)).
- Vault encryption at rest. Files on the VPS disk are plain text
  unless you set up an encrypted filesystem. If you need that, look at
  LUKS for the data volume.
- Multi-user vaults are supported but not detailed here. The default
  deployment is single-vault. To run multiple users with isolated
  vaults on the same container, see the **Multi-user mode** section in
  the [README](./README.md#multi-user-mode): bootstrap flow, inviting
  users, the admin role, and rollback are covered there.
