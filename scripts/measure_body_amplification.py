"""Measure /mcp peak-RSS growth per request-body byte (#322, task 4.1).

    python -m scripts.measure_body_amplification --token omcp_... [--body-bytes N]

Starts the app in a **fresh** uvicorn subprocess (`--workers 1`, the shipped
contract) with the caller's environment (`DATABASE_URL` of a migrated
database, `VAULT_PATH`, `SECRET_KEY`, `EMBEDDING_DIMENSIONS`, and
`ALLOWED_HOSTS` including `127.0.0.1`, which the probes connect to), warms it
with one small request, then sends, one after the other:

- a `write_file` envelope in text mode whose content is control characters,
  each JSON-escaped to six bytes (`\\u0001`): a supported write (about 10 MiB
  decoded) whose envelope is near `mcp_max_request_body_bytes`, the worst
  escaping ratio the SDK has to decode;
- an `import_from_url`-shaped envelope whose `url` is the whole body: the
  shape that produced the highest peak in the ASVS reproduction (it is now
  refused by the URL cap, after the SDK has parsed it).

For each it reports `(VmHWM − VmRSS before) ÷ body length`, read from
`/proc/<pid>/status`, resetting the peak with `/proc/<pid>/clear_refs` between
the two. The result is one JSON line on stdout. The ratio must stay at or
below `MCP_BODY_MEMORY_MULTIPLIER` (8): a higher one means raising the
multiplier's default and floor, not loosening the guard
(`tests/integration/test_body_budget_stack_pg.py`).

Linux only. The integration test imports these helpers rather than shelling
out, so the two cannot drift.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent

#: `max(2 × 25 MB, 6 × 10 MiB) + 1 MiB`, the per-request limit with the
#: default caps (`Settings.mcp_max_request_body_bytes`).
DEFAULT_MAX_REQUEST_BODY = 63_963_136

ACCEPT = "application/json, text/event-stream"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(env: dict, port: int, log_path: Path) -> subprocess.Popen:
    """uvicorn as the Dockerfile runs it: one worker, no proxy headers."""
    log = open(log_path, "wb")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--workers", "1", "--no-proxy-headers",
         "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def wait_healthy(port: int, proc: subprocess.Popen, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited with {proc.returncode} during startup")
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if response.status_code == 200:
                return
            last = f"{response.status_code} {response.text[:200]}"
        except httpx.HTTPError as exc:
            last = repr(exc)
        time.sleep(0.25)
    raise RuntimeError(f"server never became healthy (last: {last})")


def status_kib(pid: int, field: str) -> int:
    """`VmRSS` / `VmHWM` from `/proc/<pid>/status`, in KiB."""
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith(field + ":"):
            return int(line.split()[1])
    raise KeyError(field)


def reset_peak(pid: int) -> bool:
    """Reset `VmHWM` to the current RSS (`clear_refs` 5). False if refused."""
    try:
        Path(f"/proc/{pid}/clear_refs").write_text("5")
        return True
    except OSError:
        return False


def _envelope(name: str, arguments: dict, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def write_file_envelope(target: int, path: str = "amplification/write.txt",
                        request_id: int = 1) -> bytes:
    """A text-mode `write_file` whose JSON is exactly `target` bytes."""
    args = {"path": path, "content": "", "encoding": "text", "overwrite": True}
    base = len(json.dumps(_envelope("write_file", args, request_id)).encode())
    room = target - base
    escaped, plain = divmod(room, 6)
    args["content"] = "\x01" * escaped + "a" * plain
    data = json.dumps(_envelope("write_file", args, request_id)).encode()
    assert len(data) == target, (len(data), target)
    return data


def import_url_envelope(target: int, request_id: int = 1) -> bytes:
    """An `import_from_url` call whose `url` fills the body to `target` bytes."""
    prefix = "https://example.com/"
    args = {"url": prefix, "path": "amplification/import.bin"}
    base = len(json.dumps(_envelope("import_from_url", args, request_id)).encode())
    args["url"] = prefix + "a" * (target - base)
    data = json.dumps(_envelope("import_from_url", args, request_id)).encode()
    assert len(data) == target, (len(data), target)
    return data


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": ACCEPT}


def post(port: int, token: str, data: bytes, timeout: float = 120.0) -> httpx.Response:
    return httpx.post(f"http://127.0.0.1:{port}/mcp/", content=data,
                      headers=headers(token), timeout=timeout)


def tool_result(response: httpx.Response) -> dict | None:
    """The JSON-RPC message in a JSON or SSE response, or None."""
    text = response.text
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


def warm_up(port: int, token: str) -> None:
    data = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "tools/list"}).encode()
    response = post(port, token, data)
    response.raise_for_status()
    # One small write, so the write path's lazy imports are not charged to
    # the measured envelope.
    post(port, token, write_file_envelope(64 * 1024, "amplification/warm.txt"))


def measure(pid: int, port: int, token: str, data: bytes) -> dict:
    """Peak RSS growth over one envelope, as a ratio of its length."""
    reset = reset_peak(pid)
    before = status_kib(pid, "VmRSS")
    response = post(port, token, data)
    peak = status_kib(pid, "VmHWM")
    growth = max(0, peak - before) * 1024
    return {"status": response.status_code, "body_bytes": len(data),
            "rss_before_kib": before, "peak_kib": peak,
            "growth_bytes": growth, "ratio": round(growth / len(data), 3),
            "peak_reset": reset}


def run(env: dict, token: str, body_bytes: int, log_path: Path) -> dict:
    port = free_port()
    proc = start_server(env, port, log_path)
    try:
        wait_healthy(port, proc)
        warm_up(port, token)
        return {
            "write_file": measure(proc.pid, port, token,
                                  write_file_envelope(body_bytes)),
            "import_from_url": measure(proc.pid, port, token,
                                       import_url_envelope(body_bytes)),
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token", required=True, help="a readwrite omcp_ API key")
    parser.add_argument("--body-bytes", type=int,
                        default=DEFAULT_MAX_REQUEST_BODY - 4096)
    parser.add_argument("--log", default="measure_body_amplification.log")
    args = parser.parse_args(argv)
    result = run(dict(os.environ), args.token, args.body_bytes, Path(args.log))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
