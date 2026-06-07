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
4). The control panel is at `https://your-hostname/admin`. You'll set
up auth on it in Step 6.

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
  The MCP server's atomic-write path (`os.replace` onto the
  destination) means writes either land or don't, but Nextcloud still
  sees them as "remote change while local was dirty."
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
`note_embeddings`, `note_links`, etc.

After Step 4 the indexer will pick up your vault on the next pass
(every 5 min). To trigger immediately, run `make reindex` or click
"Reindex Now" in the panel.

## Step 6. Lock down the control panel

The control panel is at `https://your-hostname/admin`. By default
there is no authentication on it, anyone who knows the URL can manage
API keys and trigger destructive operations. Pick one of the options
below before you go further.

### Caddy basic auth (simplest)

In `Caddyfile.example` (which `docker-compose.simple.yml` uses by
default), uncomment the `basic_auth` block and replace the bcrypt
hash. Generate a hash with:

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
- `/admin` exposed without auth. Don't skip Step 6. The MCP endpoint
  itself is API-key gated, but the panel can mint new keys and reset
  embeddings.

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
