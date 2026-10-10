"""The /mcp body-memory budget on the real stack (#322, task 4.2).

A real uvicorn process (`--workers 1`), a real migrated Postgres, a real
readwrite API key and a temp vault:

- **the multiplier guard**: one near-limit `write_file` and one
  `import_from_url`-shaped envelope, sent one after the other to a fresh
  process; each one's peak RSS growth ÷ body length must be at most
  `MCP_BODY_MEMORY_MULTIPLIER`. A failure means raising the multiplier's
  default and floor (`src/config.py`), never loosening this test;
- **the six-writer burst** at a 1 GiB memory budget: the process survives,
  its peak RSS stays within baseline + 1 GiB + 32 MiB (replay) + 128 MiB, and
  every request either succeeds or gets the `body_memory` 429, at least one
  succeeding.

Opt-in twice: `PGVECTOR_TEST_ADMIN_URL` (like the rest of this directory) and
`BODY_BUDGET_RSS_TESTS=1`, which `make test-integration` sets and CI's `tests`
job does not (owner decision, Codex spec review finding 6): a peak-RSS bound
on a shared CI runner measures the runner.
"""
import asyncio
import hashlib
import importlib.util
import json
import os
import secrets
import sys
from pathlib import Path

import pytest

import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.skipif(os.environ.get("BODY_BUDGET_RSS_TESTS") != "1",
                       reason="set BODY_BUDGET_RSS_TESTS=1 (make test-integration) "
                              "to run the peak-RSS measurements"),
    pytest.mark.skipif(not sys.platform.startswith("linux"),
                       reason="reads /proc/<pid>/status"),
]

ROOT = Path(__file__).resolve().parent.parent.parent
MIB = 1024 * 1024
GIB = 1024 * MIB
DIMENSIONS = 64
MULTIPLIER = 8  # the default and floor of MCP_BODY_MEMORY_MULTIPLIER

_spec = importlib.util.spec_from_file_location(
    "measure_body_amplification", ROOT / "scripts/measure_body_amplification.py")
measure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("body_budget_322", DIMENSIONS)


@pytest.fixture(scope="module")
def api_token(migrated_url):
    import asyncpg

    token = "omcp_" + secrets.token_hex(24)

    async def insert():
        conn = await asyncpg.connect(_harness.asyncpg_dsn(migrated_url))
        try:
            await conn.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix, permission, is_active) "
                "VALUES ($1, $2, $3, 'readwrite', true)",
                "body-budget-322", hashlib.sha256(token.encode()).hexdigest(), token[:12])
        finally:
            await conn.close()

    asyncio.run(insert())
    return token


def server_env(migrated_url, tmp_path, **extra):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT),
        "DATABASE_URL": migrated_url,
        "SECRET_KEY": secrets.token_hex(32),
        "VAULT_PATH": str(vault),
        "EMBEDDING_DIMENSIONS": str(DIMENSIONS),
        "EMBEDDING_ALLOW_PLAINTEXT": "true",
        # Nothing listens here: the indexer and the warm-up fail and are
        # counted, which none of these measurements touch.
        "OLLAMA_URL": "http://127.0.0.1:9",
        "MCP_HOSTNAME": "",
        # The probes connect to 127.0.0.1; the default list is `localhost`.
        "ALLOWED_HOSTS": '["127.0.0.1", "localhost"]',
        "MULTI_USER_MODE": "false",
        "LOG_LEVEL": "WARNING",
    }
    env.update({k: str(v) for k, v in extra.items()})
    return env


def test_the_multiplier_covers_the_measured_amplification(migrated_url, api_token, tmp_path):
    env = server_env(migrated_url, tmp_path)
    body = measure.DEFAULT_MAX_REQUEST_BODY - 4096
    result = measure.run(env, api_token, body, tmp_path / "server.log")
    print("\nbody-amplification " + json.dumps(result, sort_keys=True))
    for shape, row in result.items():
        assert row["status"] == 200, (shape, row, (tmp_path / "server.log").read_text()[-3000:])
        assert row["peak_reset"], "clear_refs refused: the peak is not this envelope's"
        assert row["ratio"] <= MULTIPLIER, (
            f"{shape}: peak RSS growth is {row['ratio']}x the body, above "
            f"MCP_BODY_MEMORY_MULTIPLIER={MULTIPLIER}. Raise the multiplier's "
            "default and floor; do not loosen this guard.")


def test_six_maximum_size_writes_stay_within_a_1_gib_budget(migrated_url, api_token, tmp_path):
    import httpx

    env = server_env(migrated_url, tmp_path,
                     MCP_BODY_MEMORY_BUDGET_BYTES=1 * GIB)
    port = measure.free_port()
    log_path = tmp_path / "server.log"
    proc = measure.start_server(env, port, log_path)
    try:
        measure.wait_healthy(port, proc)
        measure.warm_up(port, api_token)
        body = measure.DEFAULT_MAX_REQUEST_BODY - 4096
        envelopes = [measure.write_file_envelope(body, f"burst/w{i}.txt", request_id=i + 1)
                     for i in range(6)]
        assert measure.reset_peak(proc.pid)
        baseline_kib = measure.status_kib(proc.pid, "VmRSS")

        async def burst():
            async with httpx.AsyncClient(timeout=180) as client:
                async def one(data):
                    return await client.post(f"http://127.0.0.1:{port}/mcp/", content=data,
                                             headers=measure.headers(api_token))
                return await asyncio.gather(*(one(d) for d in envelopes))

        responses = asyncio.run(burst())
        peak_kib = measure.status_kib(proc.pid, "VmHWM")
        assert proc.poll() is None, "the server died: " + log_path.read_text()[-3000:]
        assert httpx.get(f"http://127.0.0.1:{port}/health", timeout=5).status_code == 200

        statuses = [r.status_code for r in responses]
        print(f"\nbody-budget burst: statuses={statuses} baseline_kib={baseline_kib} "
              f"peak_kib={peak_kib} growth_mib={(peak_kib - baseline_kib) / 1024:.1f}")
        for r in responses:
            if r.status_code == 429:
                assert r.json()["code"] == "body_memory"
            else:
                assert r.status_code == 200, (r.status_code, r.text[:500])
                message = measure.tool_result(r)
                assert message is not None and "result" in message, r.text[:500]
                assert not message["result"].get("isError"), message
        assert statuses.count(200) >= 1
        bound_kib = baseline_kib + (1 * GIB + 32 * MIB + 128 * MIB) // 1024
        assert peak_kib <= bound_kib, (peak_kib, bound_kib)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except Exception:  # noqa: BLE001
            proc.kill()
