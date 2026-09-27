# Deploying on Kubernetes

This guide runs obsidian-mcp on a Kubernetes cluster with the kustomize
manifests in [`deploy/kubernetes/`](../deploy/kubernetes/). It assumes you
already know how to operate a cluster. For a single VPS with Docker, see
[`DEPLOYMENT.md`](../DEPLOYMENT.md). Most of the application-level material
there (vault sync, embedding choice, plaintext refusal, internal transport)
applies unchanged. This guide covers what is different on Kubernetes and the
properties of the app that decide how it must be deployed.

- [Layout](#layout)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Constraints the manifests encode](#constraints-the-manifests-encode)
- [Configuration](#configuration)
- [Vault storage and ownership](#vault-storage-and-ownership)
- [Database](#database)
- [TLS to Postgres and the embedding endpoint](#tls-to-postgres-and-the-embedding-endpoint)
- [The front door](#the-front-door)
- [Client addresses and `TRUSTED_PROXY_IPS`](#client-addresses-and-trusted_proxy_ips)
- [Network policy](#network-policy)
- [Upgrades and migrations](#upgrades-and-migrations)
- [Backups](#backups)
- [Troubleshooting](#troubleshooting)

## Layout

```text
deploy/kubernetes/
  kustomization.yaml            -> base/ (so `kubectl apply -k deploy/kubernetes` works)
  base/                         the app
    namespace.yaml              namespace, Pod Security Admission `restricted`
    configmap.yaml              non-secret settings (envFrom)
    secret.example.yaml         the Secret's shape. NOT applied, placeholders only
    pvc-vault.yaml              the vault volume
    vault-init-job.example.yaml one-shot ownership fix for a NEW empty vault volume
    deployment.yaml             1 replica, Recreate, migration initContainer, probes
    service.yaml                ClusterIP :8000
    networkpolicy.yaml          default deny + the app's allow-list
  postgres/                     optional: single-instance Postgres 16 + pgvector 0.8.2
  components/db-backup/         optional: nightly pg_dump CronJob recorded in backups_log
  ingress/                      front-door examples, never applied by kustomize
    gateway-api.example.yaml    Gateway API HTTPRoutes
    traefik.example.yaml        Traefik IngressRoute + Middlewares
  overlays/example/             an overlay combining base + postgres + db-backup
```

Treat `base/`, `postgres/` and `components/` as upstream and keep your changes
in an overlay copied from `overlays/example/`.

## Prerequisites

- **Linux nodes, kernel 5.6 or newer.** Every path below the vault root is
  opened with `openat2(RESOLVE_BENEATH | …)`. The server probes it at startup
  and exits if it is missing, whether because of the kernel or because a
  seccomp profile blocks it. The default seccomp profiles of containerd and
  CRI-O (`RuntimeDefault`, which the manifests request) allow it. **Kernel
  5.8** is also needed for the file-transfer tools. Below it the server starts,
  and uploads and imports refuse.
- **A container image you can pull.** The project publishes no public image.
  Build one from the repository's `Dockerfile` and push it to a registry your
  cluster can reach:

  ```bash
  docker build -t registry.example.com/obsidian-mcp:$(git rev-parse --short HEAD) .
  docker push registry.example.com/obsidian-mcp:$(git rev-parse --short HEAD)
  ```

  Then set `images:` in your overlay. Pin by digest in production.
- **PostgreSQL 16 with pgvector 0.8.0 or newer.** Use your own database, or the
  optional [`postgres/`](#database) component. On an older pgvector the server
  exits, because filtered semantic search would silently lose results.
- **A storage class, or a pre-provisioned PV, for the vault.** It must be
  read-write, owned by the pod's uid, and support hard links and
  `renameat2(RENAME_NOREPLACE)` (ext4 and xfs do). See
  [Vault storage](#vault-storage-and-ownership).
- **An ingress controller or Gateway that can split routes by path and, for
  one route, by header,** plus an SSO / forward-auth provider for the panel.
  See [The front door](#the-front-door).
- **An embedding endpoint.** OpenAI (or anything that speaks its embeddings API)
  is the easy choice on a cluster without a GPU. Ollama works too.
- `kubectl` 1.27+ (built-in kustomize with `components` and `labels`).

## Quick start

```bash
# 1. Namespace (the base also declares it; creating it first lets you add
#    the Secrets before the Deployment exists).
kubectl create namespace obsidian-mcp
kubectl label namespace obsidian-mcp pod-security.kubernetes.io/enforce=restricted

# 2. Optional bundled database: its Secret, then the StatefulSet.
kubectl -n obsidian-mcp create secret generic obsidian-mcp-postgres \
  --from-literal=superuser-password="$(openssl rand -hex 24)" \
  --from-literal=app-password="$(openssl rand -hex 24)"
kubectl apply -k deploy/kubernetes/postgres

# 3. The app's Secret. With the bundled database the password is `app-password`.
APP_PW=$(kubectl -n obsidian-mcp get secret obsidian-mcp-postgres \
  -o jsonpath='{.data.app-password}' | base64 -d)
kubectl -n obsidian-mcp create secret generic obsidian-mcp-secrets \
  --from-literal=DATABASE_URL="postgresql+asyncpg://obsidian_mcp:${APP_PW}@postgres:5432/obsidian_mcp" \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=OPENAI_API_KEY='sk-...'

# 4. Your overlay: image, hostname, ingress namespace, vault storage.
cp -r deploy/kubernetes/overlays/example ../my-obsidian-mcp   # outside the repo
$EDITOR ../my-obsidian-mcp/kustomization.yaml
#    (a copy outside the repo must point its `resources:` at the repo's
#     deploy/kubernetes/base and postgres, or at a git URL of them)

# 5. Apply and watch the migration initContainer, then the server.
kubectl apply -k ../my-obsidian-mcp
kubectl -n obsidian-mcp logs deploy/obsidian-mcp -c migrate
kubectl -n obsidian-mcp rollout status deploy/obsidian-mcp

# 6. Front door: adapt one of deploy/kubernetes/ingress/*.example.yaml.
```

`kubectl apply -k deploy/kubernetes` applies only the app base. It works as-is
only if you have already created the Secret, bound a writable vault volume and
edited the placeholders (image, `MCP_HOSTNAME`, ingress namespace). Missing
pieces fail closed. With no Secret the pod stays in
`CreateContainerConfigError`. With the placeholder `SECRET_KEY` the app
refuses to start. With the placeholder ingress namespace, no traffic is let in.

Once the pod is Ready, open `https://<host>/admin` through your SSO, create an
API key, and connect a client to `https://<host>/mcp` exactly as in
[DEPLOYMENT.md, Step 7](../DEPLOYMENT.md#step-7-mint-an-api-key-and-connect-a-client).

## Constraints the manifests encode

These come from the application, not from taste. Each one is a comment in the
manifest where it applies.

**Exactly one replica, `Recreate`, one uvicorn worker.** Several things live
only in the process's memory and are deliberately not shared: the `/mcp`
rate-limit token buckets, the per-address failed-authentication budget, the
panel's per-account login budget, concurrency admission, the refusal
coalescer, and the vault-root overlap snapshot. A second pod, a second worker,
or the brief two-pod overlap of a `RollingUpdate` gives each process a full
set of limits and multiplies every effective rate. Two pods would also run two
indexers against the same vault. So: `replicas: 1`, `strategy: Recreate`, no
HorizontalPodAutoscaler. The image's `CMD` carries `--workers 1`. The
manifests do not override `command`, and if you do, keep `--workers 1
--no-proxy-headers`. See [rate limits](architecture/rate-limits.md). The
consequence is a short outage on every rollout, which is the intended
trade-off. There is no high-availability mode.

**No `fsGroup` on the app pod.** The kubelet applies `fsGroup` by recursively
changing the group and mode of every file on a supporting volume at mount
time. For a vault of thousands of notes that other tools (sync clients,
backups) also write, that is slow and destructive. The pod runs as the uid
that owns the vault instead: `runAsUser/runAsGroup: 1000` (the image's own
user) by default, changed in your overlay if your files belong to another
uid. See [Vault storage](#vault-storage-and-ownership).

**`readOnlyRootFilesystem: true`, with `/tmp` as an `emptyDir`.** The server
writes only inside the vault (note writes and uploads stage there as
`O_TMPFILE` inodes or in `.transfer-tmp/`) and, through python-multipart's
spooling of large form bodies, to `/tmp`. The image's `/app` is root-owned, so
bytecode caches are never written even without this setting. This was checked
by running the image with a read-only root filesystem, all capabilities
dropped and `no-new-privileges`, against a real Postgres. Migrations, startup,
the index pass and an MCP `create_note` all succeeded, and `/tmp` stayed empty.

**seccomp `RuntimeDefault`, every capability dropped, non-root,
`allowPrivilegeEscalation: false`, no service-account token, no service
links.** The namespace enforces Pod Security `restricted`, and every pod in the
manifests (app, migration, Postgres, backup) satisfies it. Service links are
off because the app reads its settings from the environment and checks them
(`PGSSL*` is refused outright; `PGHOST`-style variables are refused under
strict TLS modes). Keep the environment to what is declared.

**Probes send `Host: localhost`.** The app rejects any `Host` header outside
`ALLOWED_HOSTS` (derived from `MCP_HOSTNAME`, plus `localhost`) with **400**.
The kubelet's default `Host` for an HTTP probe is the pod IP, so a probe
without the header can never pass. `/health` is cheap and never touches the
vault. The startup probe allows five minutes for the database checks and the
first vault-root snapshot. The readiness and liveness probes use the same
endpoint.

**One volume per vault, mounted at the vault root.** See the next sections.

## Configuration

Settings are environment variables, read by `src/config.py` (pydantic-settings,
names case-insensitive). The base puts non-secret values in the ConfigMap
`obsidian-mcp-config` and credentials in the Secret `obsidian-mcp-secrets`,
both loaded with `envFrom`. Anything unset takes the default below. Invalid
values refuse startup with a message naming the setting. The server does not
silently fall back.

List-valued settings: `TRUSTED_PROXY_IPS`, `FTS_CONFIGS` and
`OAUTH_KNOWN_REDIRECT_HOSTS` accept CSV or a JSON list. `ALLOWED_ORIGINS`,
`ALLOWED_HOSTS` and `EMBEDDING_EXCLUDE_PATTERNS` accept **JSON lists only**
(`["a","b"]`). Nullable limits are turned off with an empty value, `null` or
`none`. Zero is refused.

### Core

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://obsidian_mcp:changeme@postgres:5432/obsidian_mcp` | **Secret.** Must use `postgresql+asyncpg://`. URL-encode reserved characters in the password. TLS parameters (`ssl`, `sslmode`, …) are refused. |
| `SECRET_KEY` | `changeme` (refused) | **Secret.** Signs session cookies and CSRF tokens. The app refuses to start on a placeholder. `openssl rand -hex 32`. |
| `MCP_HOSTNAME` | unset | Public hostname. Derives `BASE_URL=https://<host>`, `ALLOWED_ORIGINS=["https://<host>"]` and `ALLOWED_HOSTS=[<host>, "localhost"]`. Required (or `BASE_URL`) for the transfer tools to mint links. |
| `BASE_URL` | derived | Explicit public origin (scheme + host, no path). HTTPS except for loopback. When `MCP_HOSTNAME` is set it must be `https://` on that same host, or startup is refused. |
| `ALLOWED_ORIGINS` | derived | CORS origins (JSON list). `*` is refused, since credentials are allowed. |
| `ALLOWED_HOSTS` | derived | Accepted `Host` headers (JSON list). `localhost` is always added, which is what the probes rely on. |
| `VAULT_PATH` | `/obsidian` | Vault mount inside the container. In multi-user mode, the bootstrap admin's vault. |
| `MULTI_USER_MODE` | `false` | In-app login, per-user vaults, admin role. See the README's [Multi-user mode](../README.md#multi-user-mode). |
| `BOOTSTRAP_ADMIN_USERNAME` | `max` | Only pre-fills the username field on the one-time `/admin/register` bootstrap form (multi-user mode). |
| `TRUSTED_PROXY_IPS` | `127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` | The peers allowed to set `X-Forwarded-For/-Proto`. See [below](#client-addresses-and-trusted_proxy_ips). |
| `LOG_LEVEL` | `INFO` | Root log level. An unknown name is refused. |
| `LOG_FORMAT` | `json` | `json` (one line per record) or `text`. |
| `PANEL_CSP` | `enforce` | Panel Content-Security-Policy: `enforce`, `report-only`, `off` (the last two are rollback settings). |
| `MCP_SANDBOX_MODE` | `false` | Registry-sandbox only. **Never in production.** Bypasses auth and all dependencies. It is refused together with any public hostname. |
| `MCP_REJECT_UNKNOWN_ARGUMENTS` | `true` | Refuse tool calls with undeclared arguments. `false` is a client-compatibility rollback. |

### Database TLS

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_SSL_MODE` | `prefer` | `disable`, `prefer`, `require`, `verify-ca`, `verify-full`. The only database TLS control. Any `PGSSL*` variable is refused. Strict modes refuse Unix sockets and exit if the session is unencrypted. |
| `DATABASE_SSL_CA_FILE` | unset | PEM CA bundle. Required by `verify-ca`/`verify-full` and refused otherwise. |
| `DATABASE_SSL_CERT_FILE` / `DATABASE_SSL_KEY_FILE` | unset | Client certificate and key, strict modes only, both or neither. |

### Embeddings

| Variable | Default | Meaning |
| --- | --- | --- |
| `EMBEDDING_PROVIDER` | `ollama` | `ollama` or `openai`. Switching after the first index needs `reset-embeddings` (below). |
| `EMBEDDING_DIMENSIONS` | `1024` | Vector size. Must match the stored column. The server refuses to start on a mismatch. |
| `OPENAI_API_KEY` | unset | **Secret.** Required for `openai`. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible embeddings endpoint. |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Model name. A change needs a reset. |
| `OLLAMA_URL` | `http://ollama:11434` | Ollama endpoint. Plaintext to a non-loopback host needs `EMBEDDING_ALLOW_PLAINTEXT=true`. |
| `EMBEDDING_MODEL` | `bge-m3` | Ollama model. A change needs a reset. |
| `OLLAMA_KEEP_ALIVE` | `-1` | How long Ollama keeps the model loaded (`-1` = forever, or a Go duration). |
| `OLLAMA_EMBED_BATCH_SIZE` | `16` | Chunks per `/api/embed` request (1 to 256). |
| `EMBEDDING_ALLOW_PLAINTEXT` | `false` | Acknowledge a plaintext `http://` embedding URL to a non-loopback host. |
| `EMBEDDING_CA_FILE` | unset | PEM CA for an `https` embedding endpoint behind a private CA. Replaces the default bundle. |
| `EMBEDDING_EXCLUDE_PATTERNS` | `["*.excalidraw.md","Excalidraw/*"]` | fnmatch globs that are keyword-indexed but not embedded (JSON list). |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `512` / `0` | Chunking (tokens). Overlap must be below size. A change needs a reset. |

### Indexer

| Variable | Default | Meaning |
| --- | --- | --- |
| `INDEX_INTERVAL_SECONDS` | `300` | Periodic index pass. |
| `INDEX_STAT_SHORTCUT` | `true` | Skip unchanged files by `(size, mtime, ctime, inode)`. **Set `false` for NFS, SMB, FUSE or FAT vault volumes.** |
| `INDEX_FULL_HASH_INTERVAL_HOURS` | `24` | Backstop full-hash pass. One also runs at startup and on panel Reindex. |
| `FTS_CONFIGS` | `english` | Postgres text-search configs for keyword search. A change needs `rebuild-tsvectors`. |
| `EMBED_CHUNK_BUDGET_PER_USER` | `5000` | Chunks embedded per user per pass (multi-user fairness). |
| `EMBED_TIME_BUDGET_SECONDS_PER_USER` | `300` | Seconds of embedding per user per pass. |
| `VAULT_ROOT_OBSERVE_TIMEOUT_SECONDS` | `10` | How long the overlap check waits on one vault root before quarantining that account (multi-user). |

### Vault I/O and transfer

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAX_FILE_READ_BYTES` | `10485760` | Largest file `read_file` will read. |
| `MAX_FILE_WRITE_BYTES` | `26214400` | Largest decoded upload / `write_file`. Size your ingress body limit to it. |
| `MAX_READ_RESPONSE_CHARS` | `40000` | Characters one read returns (bounds the caller's context). |
| `WRITE_PRECONDITION_REQUIRED` | `false` | Require `expected_hash` on edits, moves and deletes. |
| `VAULT_ALLOW_NAMED_STAGING_FALLBACK` | `false` | Accept named staging on filesystems without `O_TMPFILE` (some NFS). |
| `TRANSFER_TOKEN_TTL_SECONDS` | `600` | Default life of a transfer link (60 to 3600). |
| `TRANSFER_MAX_UPLOAD_SECONDS` | `600` | Longest a claimed upload may stream. |
| `TRANSFER_MAX_CONCURRENT_UPLOADS` | `4` | Simultaneous `PUT /transfer/upload`. |
| `IMPORT_ALLOW_HTTP` | `false` | Let `import_from_url` fetch plain http (the base NetworkPolicy allows only 443 egress). |

### Rate limits, quotas and concurrency

All of these live in the one process. See
[rate limits](architecture/rate-limits.md) and the README's
[Rate limits](../README.md#rate-limits) and
[Concurrency admission](../README.md#concurrency-admission) before changing
them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCP_RATE_LIMIT_PER_MINUTE` / `MCP_RATE_LIMIT_BURST` | `120` / `30` | General per-principal bucket. |
| `MCP_WRITE_RATE_LIMIT_PER_MINUTE` / `MCP_WRITE_RATE_LIMIT_BURST` | `60` / `15` | Write bucket (the vault-mutating tools and uploads). |
| `MCP_AUTH_FAILURE_LIMIT` / `MCP_AUTH_FAILURE_WINDOW_SECONDS` | `60` / `300` | Failed `/mcp` authentications per client address per window. |
| `MCP_AUTH_FAILURE_TABLE_SIZE` | `4096` | Slots in that address table. |
| `MCP_LIMITER_MAX_TRACKED_PRINCIPALS` | `10000` | Principals with their own bucket before overflow sharing. |
| `MCP_REFUSAL_LOG_INTERVAL_SECONDS` | `10` | Coalescing window for refusal rows. |
| `DEFAULT_DAILY_REQUEST_LIMIT` | `5000` | Daily quota given to newly created API keys (null = unlimited). |
| `PANEL_LOGIN_FAILURE_LIMIT` / `PANEL_LOGIN_FAILURE_WINDOW_SECONDS` | `10` / `900` | Failed panel logins per account per window (multi-user). |
| `MCP_CONCURRENCY_MODE` | `shadow` | `off`, `shadow`, `queue`, `enforce`. Promote only on a passing concurrency report. |
| `MCP_CONCURRENCY_WAIT_SECONDS` / `MCP_CONCURRENCY_TRANSPORT_WAIT_SECONDS` | `5` / `2` | Tool-stage wait (max 10) and transport-stage deadline (max 5). |
| `MCP_CONCURRENCY_REQUESTS`, `_FINGERPRINT`, `_REQUEST_WAITERS`, `_FINGERPRINT_WAITERS` | `64`, `20`, `64`, `16` | Request-envelope slots and waiters. |
| `MCP_CONCURRENCY_AUTH`, `_AUTH_WAITERS` | `2`, `32` | Authentication permits. |
| `MCP_CONCURRENCY_TOOLS`, `_TENANT`, `_PRINCIPAL` | `6`, `4`, `3` | Tool slots: global, per tenant, per principal. |
| `MCP_CONCURRENCY_EMBEDDING`, `_VECTOR`, `_WRITE`, `_SCAN`, `_LIGHT` | `1`, `1`, `1`, `2`, `4` | Per-class tool ceilings. |
| `MCP_CONCURRENCY_WAITERS`, `_TENANT_WAITERS`, `_PRINCIPAL_WAITERS` | `64`, `32`, `16` | Tool-stage waiters. |
| `MCP_CONCURRENCY_WRITERS`, `_WRITER_WAITERS`, `_WRITER_WAIT_SECONDS` | `1`, `64`, `0.25` | Database writer permits. |
| `MCP_CONCURRENCY_REGISTRY_SIZE` | `1024` | Tracked identities. |
| `MCP_CONCURRENCY_REPLAY_BUDGET_BYTES` | `33554432` | Bytes of waiting requests held for replay (1 to 256 MiB). |
| `MCP_CONCURRENCY_OTHER` | unset | Retired. `1` is ignored with a warning, and anything else is refused. |

The connection-pool budget is validated at boot: `AUTH + tool demand +
WRITERS + 4 ≤ 15`.

### Sessions and OAuth

| Variable | Default | Meaning |
| --- | --- | --- |
| `SESSION_MAX_AGE` | `604800` | Panel session lifetime, seconds (multi-user). |
| `SESSION_COOKIE_NAME` | `omcp_session` | Panel session cookie name. |
| `SESSION_TOUCH_INTERVAL_SECONDS` | `60` | How stale a session's last-seen stamp may get. |
| `SESSION_PURGE_RETAIN_DAYS` | `7` | Retention of ended session rows. |
| `OAUTH_KNOWN_REDIRECT_HOSTS` | `claude.ai,chatgpt.com` | Redirect hosts the consent page marks as known connectors. |
| `OAUTH_CLIENT_UNUSED_EXPIRY_DAYS` | `30` | Delete dynamically registered OAuth clients never used for this long (null disables). |

`OMCP_SECURITY_EVENTS_STRICT` exists for the test suite only. Do not set it.
`FORWARDED_ALLOW_IPS` is uvicorn's own variable, and the image switches that
layer off. Do not set it either.

## Vault storage and ownership

The vault is the product's data, and the server is its *editor*. The
write tools create, edit, move and delete notes, and uploads publish into it.
The volume must be read-write.

**One volume per vault, mounted exactly at the vault root.** Mount the claim
at `VAULT_PATH` (`/obsidian`). Do not use a `subPath` of a bigger volume, and
do not mount anything underneath the vault. A write publishes by linking a
staged inode into place, which cannot cross a mount boundary, and the transfer
tools refuse a destination on a different mount than the staging directory. In
multi-user mode, each user's vault is its own volume, mounted at
`/vaults/<name>` (commented example in `deployment.yaml`) and assigned in the
panel. **Never mount one tenant's vault, or anything inside it, at a path
inside another tenant's vault.** The server detects overlapping paths and
aliased roots, but by design it does not detect bind-mount grafts. The
consequence would be cross-tenant read, overwrite and delete (see
[vault roots and tenancy](architecture/vault-roots-and-tenancy.md#accepted-limitations)).

**Filesystem.** It must be case-sensitive and support hard links and
`renameat2(RENAME_NOREPLACE)`. ext4 and xfs, the usual block-storage
filesystems, do. `O_TMPFILE` is preferred. On filesystems without it (some
NFS servers) set `VAULT_ALLOW_NAMED_STAGING_FALLBACK=true`. On NFS, SMB, FUSE
or FAT, also set `INDEX_STAT_SHORTCUT=false`.

**Ownership without `fsGroup`.** The app pod never sets `fsGroup` (see
[constraints](#constraints-the-manifests-encode)), so the volume must already
be writable by the pod's uid:

- *Existing vault data.* Run the pod as the uid/gid that owns the files. Patch
  `runAsUser`/`runAsGroup` in your overlay. Do not chown the vault to suit
  the pod.
- *A new, empty, dynamically provisioned volume.* Its root is usually
  `root:root 0755`. Run
  [`vault-init-job.example.yaml`](../deploy/kubernetes/base/vault-init-job.example.yaml)
  once before the first start. That one-shot Job, and only that Job, carries
  `fsGroup`. It makes the root group-writable for gid 1000 and exits. Never
  point it at a volume that already holds a vault.

**Bringing an existing vault.** Bind the claim to a PV you create. Two
common shapes:

```yaml
# NFS export (set INDEX_STAT_SHORTCUT=false; check O_TMPFILE support)
apiVersion: v1
kind: PersistentVolume
metadata:
  name: obsidian-mcp-vault
spec:
  capacity: { storage: 50Gi }
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  claimRef: { namespace: obsidian-mcp, name: obsidian-mcp-vault }
  nfs:
    server: nfs.example.internal
    path: /exports/vault
---
# A directory on one node (the pod is pinned to that node by the PV)
apiVersion: v1
kind: PersistentVolume
metadata:
  name: obsidian-mcp-vault
spec:
  capacity: { storage: 50Gi }
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  claimRef: { namespace: obsidian-mcp, name: obsidian-mcp-vault }
  local:
    path: /srv/vault          # the vault root itself, not a parent
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - { key: kubernetes.io/hostname, operator: In, values: [node-1] }
```

In the overlay, set the claim's `storageClassName: ""` and `accessModes` to
match the PV. Use `persistentVolumeReclaimPolicy: Retain` for anything
holding real notes. Note that the kubelet *does* apply `fsGroup` to `local`
volumes, which is one more reason the app pod carries none.

Keeping the vault in sync with your devices (Nextcloud, Obsidian Sync, git,
rsync) is outside the cluster's concern and works as described in
[DEPLOYMENT.md, Step 4](../DEPLOYMENT.md#step-4-get-your-vault-onto-the-vps).
Whatever syncs must write to the same filesystem the PV exposes.

## Database

Requirements: PostgreSQL 16 (what CI tests) with **pgvector ≥ 0.8.0** installed
in the app database. The app role needs to own its database. It does not need
to be a superuser if the `vector` extension is created in advance by one. The
first migration runs `CREATE EXTENSION IF NOT EXISTS vector`, which is then a
no-op.

**Bring your own.** Point `DATABASE_URL` at it, create the extension once as a
superuser (`CREATE EXTENSION vector;` in the app database), and add an egress
rule for it to the NetworkPolicy.

**The optional `postgres/` component** is a single-instance StatefulSet on
`pgvector/pgvector:0.8.2-pg16`. It was chosen over an operator because it adds
no cluster-wide dependency and matches the image family the compose files and
CI use. The tag pins the pgvector version the app checks for. It runs as the
image's `postgres` user (uid 999) with a read-only root filesystem. On first
start it creates a **non-superuser** `obsidian_mcp` role that owns the
`obsidian_mcp` database, and installs `vector` as the superuser. It has no
TLS, no replica, no point-in-time recovery and no scheduled backups of its own
(add `components/db-backup`). The app therefore runs with
`DATABASE_SSL_MODE=prefer`, which logs one `internal_transport_plaintext`
event per start. The hop stays inside the namespace, restricted by
NetworkPolicy.

**For production, prefer [CloudNativePG](https://cloudnative-pg.io/).** Its
standard PostgreSQL images ship pgvector, and it gives you TLS with a
cluster CA out of the box (so `verify-full` is easy), WAL archiving and PITR,
and failover. A minimal cluster for this app:

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: obsidian-mcp-pg
  namespace: obsidian-mcp
spec:
  instances: 1
  imageName: ghcr.io/cloudnative-pg/postgresql:16-standard-bookworm  # includes pgvector
  storage: { size: 10Gi }
  bootstrap:
    initdb:
      database: obsidian_mcp
      owner: obsidian_mcp
      postInitApplicationSQL:
        - CREATE EXTENSION IF NOT EXISTS vector
```

Check the pgvector version in whichever image tag you pin (`SELECT extversion
FROM pg_extension WHERE extname = 'vector'` must say ≥ 0.8.0). Then set
`DATABASE_URL` to `…@obsidian-mcp-pg-rw:5432/obsidian_mcp` with the password
from the `obsidian-mcp-pg-app` Secret, mount the `obsidian-mcp-pg-ca` Secret's
`ca.crt`, set `DATABASE_SSL_MODE=verify-full` and
`DATABASE_SSL_CA_FILE=<mount path>/ca.crt`, and allow egress to the CNPG pods
(`cnpg.io/cluster: obsidian-mcp-pg`).

## TLS to Postgres and the embedding endpoint

The app enforces one transport policy per internal hop at startup. The
details, and how to read the two startup lines (`Database transport: …`,
`Embedding transport: …`), are in
[DEPLOYMENT.md, Internal transport](../DEPLOYMENT.md#internal-transport-the-database-and-embedding-hops).
On Kubernetes:

- Mount CA bundles (and any client certificate) as files from a ConfigMap
  or Secret, **into both the `migrate` initContainer and the app container**,
  because alembic applies the same policy. Point `DATABASE_SSL_CA_FILE` /
  `EMBEDDING_CA_FILE` at the mounted paths.
- `verify-full` checks the host in `DATABASE_URL` against the server
  certificate. Use the exact Service DNS name the certificate carries
  (`<svc>`, `<svc>.<ns>.svc`, …).
- A client key (`DATABASE_SSL_KEY_FILE`) must be readable by the pod's uid.
  Secret volume files are owned by root, and the pod has no `fsGroup` to
  change that, so a `0400`/`0600` mode makes the key unreadable to uid 1000.
  Mount that Secret with `defaultMode: 0444` in a volume used only by this pod
  (the app checks readability, not mode). The Secret's RBAC is what protects
  the key. Mount the same volume into the `migrate` initContainer.
- Never put `sslmode`/`ssl` into `DATABASE_URL` and never set `PGSSL*`. Both
  refuse startup.
- An in-cluster Ollama over `http://ollama.<ns>:11434` refuses to start
  unless `EMBEDDING_ALLOW_PLAINTEXT=true`. Either accept that explicitly or
  front Ollama with TLS (a cert-manager certificate plus a TLS sidecar or
  mesh) and set `EMBEDDING_CA_FILE`.

## The front door

The app is designed to sit behind a proxy that splits its routes. Neither
example in [`deploy/kubernetes/ingress/`](../deploy/kubernetes/ingress/) is
applied by kustomize, because both need your hostname, gateway and SSO.
[`gateway-api.example.yaml`](../deploy/kubernetes/ingress/gateway-api.example.yaml)
uses standard Gateway API `HTTPRoute`s.
[`traefik.example.yaml`](../deploy/kubernetes/ingress/traefik.example.yaml) is
the same split in Traefik's CRDs, and mirrors the reference Docker labels in
`docker-compose.yml`, whose comments carry the full rationale.

| Request (on `https`) | Proxy auth | Why |
| --- | --- | --- |
| `/admin*`, `/api*`, `/authorize` | **Your SSO** | The panel, its REST API and the OAuth consent page. In single-user mode the panel has **no login of its own**, so SSO is the only thing protecting a page that mints API keys. In multi-user mode it is defence in depth in front of the app's login. |
| `/mcp*`, `/transfer*`, `/health`, `/.well-known*`, `/register`, `/token`, `/revoke` | none | The app authenticates each one: API keys or OAuth tokens on `/mcp`, capability tokens on `/transfer`, and the OAuth endpoints and discovery documents are public by protocol. SSO here breaks every MCP client. |
| exactly `/` **with** `Authorization: Bearer …` | none | Some clients strip `/mcp`, and the app rewrites exactly this to `/mcp/`. The header match *and* the exact-path scope are both required. A header-matched route without the path scope, at a high priority, would let any `/admin` request carrying a bearer header bypass SSO. |
| anything else (`/`, `/docs`, `/openapi.json`, …) | – | Not routed: 404 at the proxy. |

And on plaintext `http` (:80):

| Request | Answer |
| --- | --- |
| the machine paths above, and `/` with a bearer header in any case | **refused**, no redirect |
| `/admin*`, `/api*`, `/authorize`, bare `/` | redirect to https |
| `/.well-known/acme-challenge/*` | left to your certificate issuer |

A redirect is the wrong answer for a machine path. A client configured with
`http://` has already sent its token in cleartext, and a 307/308 retry over
TLS succeeds, so nobody notices. The full reasoning is in
[DEPLOYMENT.md](../DEPLOYMENT.md#plaintext-http-is-refused-not-redirected-on-machine-facing-paths).
How the examples refuse:

- **Traefik:** a priority-200 router on `web` with an `ipAllowList` of
  `192.0.2.0/32` (TEST-NET-1, matches nobody), which answers 403 in the proxy.
  **Check your static configuration.** An entry-point redirection on `web`
  generates a router at priority 9223372036854775806 by default, which
  outranks the refusal. Set that redirection's `priority` to `1`, or remove it
  and redirect with a low-priority catch-all router.
- **Gateway API:** there is no standard "respond 403". The refusal rule
  targets a Service that deliberately does not exist, and the spec requires a
  500 from the gateway without contacting any backend. The route reports
  `ResolvedRefs=False`, and that is intended. Make sure no catch-all
  http→https redirect route outranks it for your hostname.

The Gateway API has no standard authentication filter, so the SSO route
carries an `ExtensionRef` placeholder. By the spec, an unresolvable
`ExtensionRef` must produce an error response, so the route fails closed until
you replace it with your implementation's auth filter, or attach an auth
policy to *that HTTPRoute only* (for example an Envoy Gateway
`SecurityPolicy`). The Traefik example's `forwardAuth` address is in the
reserved `.invalid` domain and fails closed the same way.

Other proxy settings:

- **Streaming.** `/mcp` holds long-lived SSE responses. Disable or raise route
  and idle timeouts for the machine routes.
- **Body size.** Uploads are up to `MAX_FILE_WRITE_BYTES` (25 MiB default).
  Raise the proxy's request-body limit and turn request buffering off so
  uploads stream.
- **Headers.** Forward `Host` unchanged. It must be `MCP_HOSTNAME`, or the
  app answers 400. Send `X-Forwarded-Proto: https`. Do not log request
  headers or full URLs of `/transfer` requests, because capability tokens
  travel in them.
- **No proxy rate limiting on the machine routes.** It is intentional: the app's own
  per-principal buckets and failed-auth budget are finer-grained than a proxy
  can express.

Verify the matrix after every change to the front door:

```bash
H=obsidian-mcp.example.com
curl -si https://$H/admin | head -1            # SSO redirect or refusal, never the panel
curl -si https://$H/api/x | head -1            # same
curl -si https://$H/mcp | head -1              # 401 from the app
curl -si https://$H/health | head -1           # 200
curl -si -H 'Authorization: Bearer x' https://$H/ | head -1   # 401 from the app
curl -si https://$H/ | head -1                 # 404
curl -si http://$H/mcp | grep -iE '^HTTP|^location'           # refused, NO Location
curl -si http://$H/admin | grep -iE '^HTTP|^location'         # redirect to https
```

## Client addresses and `TRUSTED_PROXY_IPS`

Every per-address control keys on the client address the app resolves: the
failed-authentication budget, the panel's address limits and the logs.
`TRUSTED_PROXY_IPS` is the only setting that decides which peers may set
`X-Forwarded-For` and `X-Forwarded-Proto`. uvicorn's own layer is off
(`--no-proxy-headers` in the image), so there is no second list to keep in step.

What the code does (`src/config.py`, then uvicorn's `ProxyHeadersMiddleware`):

- Entries are single addresses **or CIDR networks**, IPv4 or IPv6, given as
  CSV or a JSON list. A malformed entry refuses startup and names it.
  **There is no wildcard**: `*` is refused. An empty value (or `null`/`none`)
  trusts nobody, which is right only when nothing proxies the app.
- CIDRs are stored **canonicalised**: `10.42.0.7/16` becomes `10.42.0.0/16`,
  because uvicorn would otherwise treat a host-bit CIDR as a literal matching
  nothing. The effective list is logged once at startup:
  `Proxy header trust: TRUSTED_PROXY_IPS = …`.
- The headers are honoured only when the **direct TCP peer** is in the list.
  Then `X-Forwarded-For` is walked **from the right**, skipping entries that
  are themselves trusted, and the first untrusted address is the client. If
  every hop is trusted, the left-most one is used. So when several proxies are
  chained, each hop that appends to the header must be in the list.

On Kubernetes the direct peer is your ingress controller or gateway **pod**
(or the node, with some host-network controllers). The default
(`127.0.0.1` + the three RFC 1918 ranges) covers most pod CIDRs, but it also
trusts every other pod in the cluster. Only the NetworkPolicy's ingress rule
(controller namespace only) stops another pod from forging the header. Narrow
it to the pod CIDR, or better to the controller's own range. A CNI that
allocates pods from `100.64.0.0/10` or public space **must** be listed, or the
app sees every request as coming from the controller pod and all clients share
one failed-auth budget.

The controller itself must see the real client. Behind a cloud load balancer
that usually means `externalTrafficPolicy: Local` on the controller's
Service, or PROXY protocol, and configuring the controller to trust the load
balancer's forwarded headers. Check by making a request from a known address
and reading the app's log for that address, not for a pod or node address.

## Network policy

`base/networkpolicy.yaml` puts a default deny on the namespace, then allows:

- **ingress** to the app on 8000 from the ingress controller's namespace only
  (`change-me-ingress-namespace`, which you must set);
- **egress** to cluster DNS (`kube-system`, `k8s-app: kube-dns`), to the
  bundled Postgres pods on 5432, and to **public** addresses on 443 (the
  OpenAI-compatible API and `import_from_url`), with private, CGNAT and
  link-local ranges excluded.

Add rules for an external database, an in-cluster Ollama (examples are
commented in the file), NodeLocal DNSCache (`169.254.20.10`), or port 80 if
you enable `IMPORT_ALLOW_HTTP`. If a separate SSO proxy (for example
oauth2-proxy running as an upstream proxy) sits between the gateway and the
app, admit its namespace too, and add its pod range to `TRUSTED_PROXY_IPS`.
The Postgres and backup components ship matching policies.

## Upgrades and migrations

The `migrate` initContainer runs `alembic upgrade head` with the new image
before the new server starts. With `Recreate` and one replica, the old pod is
gone first and only one migrator ever runs. The app takes no migration lock of
its own. If you run migrations anywhere else as well (a CI job, a second
Deployment), serialize them yourself, for example with a wrapper holding
`pg_advisory_lock` on a dedicated connection. The whole upgrade runs in one
transaction (PostgreSQL has transactional DDL, and `alembic/env.py` does not
use `transaction_per_migration`), so a failed migration leaves the schema
where it was.

The procedure:

1. Read the release's notes and the README's
   [Upgrading](../README.md#upgrading) section.
2. **Take a backup first.** A migration is the one step a restore is for.
   With the backup component:
   `kubectl -n obsidian-mcp create job --from=cronjob/obsidian-mcp-db-backup pre-upgrade-$(date +%s)`
   and wait for it to complete.
3. Bump the image in your overlay (`images:` entry `name: obsidian-mcp`, by
   digest) and apply.
4. Watch `kubectl logs deploy/obsidian-mcp -c migrate`, then the server's
   startup lines.
5. Confirm the schema matches the models:
   `kubectl -n obsidian-mcp exec deploy/obsidian-mcp -- alembic check` should print
   "No new upgrade operations detected."

If a migration fails, the pod stays in `Init:Error` and the old version is
already stopped. Either fix forward, or restore the backup and roll the image
back. Alembic downgrades are not a supported rollback path.

**Index maintenance** (`make reset-embeddings`, `make rebuild-tsvectors`) must
run in a *fresh* container that reads the *new* settings, not through
`kubectl exec` into the running pod. A running pod's environment was fixed
when it started, so after you change `EMBEDDING_DIMENSIONS` or `FTS_CONFIGS`
in the ConfigMap an exec would reset the column at the old dimension or
rebuild under the old configs (#142). And a pod whose dimension disagrees
with the stored vectors exits at startup, so there may be nothing to exec
into. `rebuild_tsvectors` also re-reads every note, so it needs **every
vault volume mounted at the same paths as the app** (`/obsidian`, and each
`/vaults/<name>` in multi-user mode), plus any TLS files the app mounts.

1. Update the ConfigMap.
2. `kubectl -n obsidian-mcp scale deploy/obsidian-mcp --replicas=0`. This
   stops the indexer and frees a `ReadWriteOnce` vault volume for the Job.
3. Run a one-off Job from the same image and settings:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: obsidian-mcp-rebuild-tsvectors     # or -reset-embeddings
  namespace: obsidian-mcp
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:  # reuses the app's NetworkPolicy (DNS, Postgres, 443)
        app.kubernetes.io/name: obsidian-mcp
        app.kubernetes.io/component: server
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      enableServiceLinks: false
      securityContext:           # same uid/gid as the Deployment, no fsGroup
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: task
          image: registry.example.com/obsidian-mcp:<same tag as the Deployment>
          command: ["python", "-m", "scripts.rebuild_tsvectors"]   # or scripts.reset_embeddings
          envFrom:
            - configMapRef: { name: obsidian-mcp-config }
            - secretRef: { name: obsidian-mcp-secrets }
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          volumeMounts:
            - { name: vault, mountPath: /obsidian }
            - { name: tmp, mountPath: /tmp }
            # + every /vaults/<name> and TLS mount the Deployment has
      volumes:
        - name: vault
          persistentVolumeClaim: { claimName: obsidian-mcp-vault }
        - { name: tmp, emptyDir: {} }
```

4. When it has completed, `kubectl -n obsidian-mcp scale deploy/obsidian-mcp --replicas=1`.

Both tasks take the index generation lock. With the app scaled down nothing
else holds it. If you run one with the app up (possible only for
`reset_embeddings` on a node that can share the volume), it waits for the
running index pass to commit. That wait is expected. Do not give it a short
timeout. See [indexing and embeddings](architecture/indexing-and-embeddings.md).
An on-demand reindex is the panel's "Reindex Now" button.

## Backups

Two things hold state: the **vault** and the **database**.

- **The vault** is your notes. Back it up with whatever protects that
  storage: volume snapshots, restic/Velero, or the sync service it came from.
  Soft deletes move files into `.trash/` inside the vault, so they are covered
  too.
- **The database** holds the index (rebuildable from the vault, at an
  embedding cost), and also things that are not rebuildable: API keys, OAuth
  clients and grants, users, usage history and quotas. A dump contains every
  note's text and every credential hash, so treat it as sensitive.

[`components/db-backup`](../deploy/kubernetes/components/db-backup/) is a
nightly CronJob that follows `make db-backup`'s contract. It writes a
`pg_dump` under `umask 077`, gzips and verifies it, prunes past 30 days while
keeping the newest 7, and records the dump in `backups_log`. A dump is
accepted only if the archive verifies *and* the SQL ends with pg_dump's
end-of-dump trailer, because an empty or truncated dump still gzips to a valid
file. That table is
where the panel's Health page reads the last backup's age, so a dump taken by
any other means leaves the page warning. The backup volume is mounted only by
the backup Job, never by the app container. The Job uses only
`DATABASE_URL` from the app's Secret, and mirrors `DATABASE_SSL_*` onto libpq
(mount the CA into the Job if you use strict TLS). Keep its image's major
version at or above the server's, since `pg_dump` refuses a newer server.

The dumps sit on a volume in the same cluster. Copy them off-site (a volume
snapshot schedule, restic, Velero) if the cluster is not itself backed up.
With CloudNativePG, prefer its own WAL-archive backups and keep this
component for the `backups_log` record, or record your own backups in that
table.

**Restore** (tested against the bundled Postgres, as the non-superuser app
role):

1. Stop writers: `kubectl -n obsidian-mcp scale deploy/obsidian-mcp --replicas=0`,
   and `kubectl -n obsidian-mcp patch cronjob obsidian-mcp-db-backup -p '{"spec":{"suspend":true}}'`.
2. As a superuser, recreate the database empty, owned by the app role, with
   the extension installed. The app role cannot create `vector` itself.

   ```sql
   DROP DATABASE obsidian_mcp;
   CREATE DATABASE obsidian_mcp OWNER obsidian_mcp;
   \c obsidian_mcp
   CREATE EXTENSION vector;
   ```

3. Load the dump as the app role, in one transaction, stopping at the first
   error. The dump's `COMMENT ON EXTENSION vector` line is filtered out
   because only the extension's owner (the superuser) may set it. It is only
   the extension's description. Run it from a pod with the postgres client,
   for example the backup image with the backup volume mounted:

   ```bash
   set -euo pipefail
   F=/backups/backup_YYYYMMDD_HHMMSS.sql.gz
   URL="postgresql://${DATABASE_URL#postgresql+asyncpg://}"
   gzip -t "$F"
   gzip -dc "$F" | grep -v '^COMMENT ON EXTENSION ' \
     | psql -X -q -v ON_ERROR_STOP=1 --single-transaction "$URL"
   ```

   A failure rolls the whole restore back and leaves the database empty. Do
   not start the app on it.
4. Scale the app back to 1. Its migration initContainer brings an older dump
   up to the image's schema. Resume the CronJob.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Pod `CreateContainerConfigError` | The Secret `obsidian-mcp-secrets` (or the ConfigMap) is missing. |
| `ErrImagePull` / `ImagePullBackOff` | The `images:` placeholder was not replaced, or the registry needs an `imagePullSecret`. |
| Startup, readiness or liveness probe fails with **400** | The probe lost its `Host: localhost` header, or `ALLOWED_HOSTS` was set explicitly without `localhost` (it is always added, so check the override). |
| Every request through the ingress gets **400** | The ingress rewrites `Host`. It must reach the app as `MCP_HOSTNAME`. |
| Container exits at start with a `critical` log line | Each startup check names itself: `openat2` unavailable (kernel or seccomp), pgvector < 0.8.0, `EMBEDDING_DIMENSIONS` or model disagreeing with the stored vectors, a placeholder `SECRET_KEY`, a plaintext embedding URL without `EMBEDDING_ALLOW_PLAINTEXT`, a TLS key in `DATABASE_URL`. |
| `migrate` initContainer fails with `permission denied to create extension "vector"` | Create the extension once as a superuser in the app database. |
| Writes fail with "permission denied"; `Found 0 markdown files` | The vault volume is not owned by / writable for the pod's uid. See [ownership](#vault-storage-and-ownership). |
| Uploads refuse "the filesystem does not support …" | No `O_TMPFILE` (set `VAULT_ALLOW_NAMED_STAGING_FALLBACK=true`), kernel < 5.8 (`/health` shows `transfer_mount_check_available: false`), or a mount nested under the vault root. |
| All clients share one failed-auth budget; logs show a pod or node address | `TRUSTED_PROXY_IPS` does not cover the controller's pod range, or the controller does not see the real client. |
| `/admin` reachable without SSO | The SSO filter/policy is not attached to the SSO route, or a header-matched root route lacks its exact-path scope. Stop and fix before anything else. |
| `http://…/mcp` redirects instead of being refused | A higher-priority redirect (Traefik entry-point redirection, a catch-all route) wins. See [The front door](#the-front-door). |
| MCP sessions drop after ~15–60 s | Proxy route or idle timeout on `/mcp`. |
| Panel Health page says no backup | Backups taken outside `components/db-backup` / `make db-backup` are not recorded in `backups_log`. |
| The reset-embeddings / rebuild-tsvectors Job seems to hang | It is waiting for the running index pass to commit. Wait, or pause the indexer in the panel. |
