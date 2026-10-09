"""Deriving the /mcp body-memory budget and the boot check (#322, D2).

Covers the derivation scenarios, the static ranges (safe direction only), the
lifespan-only boot check, the process-own cgroup reader against fake
proc/sys trees, and the migration-init-container regression: importing
`src.config` under a small limit must neither read a cgroup file nor fail.
"""
import json
import logging
import math
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from src import config as config_module
from src.config import Settings
from src.services import body_budget
from src.services.body_budget import (
    BodyBudgetConfigError,
    check_plan,
    derive_body_budget,
    read_cgroup_memory_limit,
)

ROOT = Path(__file__).resolve().parent.parent
MIB = 1024 * 1024
GIB = 1024 * MIB


def make(**kw):
    kw.setdefault("secret_key", "0123456789abcdef0123456789abcdef")
    return Settings(_env_file=None, **kw)


def limit(value):
    return lambda: value


# ── derivation scenarios ────────────────────────────────────────────────────

def test_reference_deployment_derives_128_mib():
    plan = derive_body_budget(make(), limit(2 * GIB))
    assert plan.source == "cgroup"
    assert plan.memory_budget == 1 * GIB
    assert plan.capacity == 128 * MIB
    assert plan.small_lane == 16 * MIB
    assert plan.large_lane == 112 * MIB
    check_plan(plan)


def test_no_limit_falls_back_to_1_gib():
    plan = derive_body_budget(make(), limit(None))
    assert plan.source == "fallback"
    assert plan.memory_budget == 1 * GIB
    assert plan.capacity == 128 * MIB
    check_plan(plan)


def test_a_failing_reader_counts_as_no_limit():
    def boom():
        raise PermissionError("no /proc")
    assert derive_body_budget(make(), boom).source == "fallback"


def test_a_container_too_small_for_one_maximum_write_refuses_to_boot():
    plan = derive_body_budget(make(), limit(512 * MIB))
    with pytest.raises(BodyBudgetConfigError) as err:
        check_plan(plan)
    for name in ("MCP_BODY_MEMORY_BUDGET_BYTES", "MCP_BODY_MEMORY_FRACTION",
                 "MCP_BODY_MEMORY_MULTIPLIER", "MAX_FILE_WRITE_BYTES"):
        assert name in str(err.value)
    with pytest.raises(BodyBudgetConfigError):
        body_budget.configure(make(), cgroup_reader=limit(512 * MIB), log_line=False)


def test_the_minimum_container_for_the_defaults_is_about_1_1_gib():
    settings = make()
    body = settings.mcp_max_request_body_bytes
    # large_lane = capacity − capacity // 8 >= body, capacity = 0.5 L // 8.
    need = math.ceil(body * 8 / 7) * 8 * 2
    check_plan(derive_body_budget(settings, limit(need + 64)))
    with pytest.raises(BodyBudgetConfigError):
        check_plan(derive_body_budget(settings, limit(need - 1024 * 64)))
    assert 1.0 * GIB < need < 1.15 * GIB


def test_an_explicit_budget_overrides_the_derivation():
    plan = derive_body_budget(make(mcp_body_memory_budget_bytes=1610612736),
                              limit(4 * GIB))
    assert plan.source == "setting"
    assert plan.capacity == 192 * MIB
    check_plan(plan)


def test_an_explicit_budget_without_a_readable_limit_is_taken_as_given():
    plan = derive_body_budget(make(mcp_body_memory_budget_bytes=3 * GIB), limit(None))
    assert plan.source == "setting" and plan.memory_budget == 3 * GIB
    assert plan.safe_allocation is None
    check_plan(plan)


def test_an_explicit_budget_above_the_safe_allocation_is_refused():
    # 1.5 GiB of a 2 GiB container: the safe allocation is 1 GiB.
    plan = derive_body_budget(make(mcp_body_memory_budget_bytes=1610612736),
                              limit(2 * GIB))
    assert plan.safe_allocation == 1 * GIB
    with pytest.raises(BodyBudgetConfigError, match="safe allocation"):
        check_plan(plan)


def test_the_safe_allocation_keeps_the_fixed_headroom():
    # 1.2 GiB: half is 614 MiB, but 1.2 GiB − (384 + 32) MiB is 812 MiB, so
    # half binds. At 700 MiB the headroom binds instead (284 MiB).
    assert body_budget.safe_allocation(700 * MIB, 32 * MIB) == 284 * MIB
    plan = derive_body_budget(make(mcp_body_memory_budget_bytes=300 * MIB),
                              limit(700 * MIB))
    problems = body_budget.plan_problems(plan)
    assert any("safe allocation" in p for p in problems)


def test_the_boot_check_follows_a_raised_max_file_write_bytes():
    settings = make(max_file_write_bytes=60 * MIB)
    plan = derive_body_budget(settings, limit(2 * GIB))
    assert plan.max_request_body > plan.large_lane
    with pytest.raises(BodyBudgetConfigError, match="MAX_FILE_WRITE_BYTES"):
        check_plan(plan)


def test_a_small_lane_below_one_envelope_is_refused():
    plan = body_budget.BodyBudgetPlan(
        source="setting", memory_budget=0, multiplier=8, capacity=0,
        small_lane=MIB - 1, large_lane=200 * MIB, cgroup_limit=None,
        safe_allocation=None, max_request_body=61 * MIB)
    with pytest.raises(BodyBudgetConfigError, match="small lane"):
        check_plan(plan)


def test_small_request_threshold_matches_the_envelope_allowance():
    assert body_budget.SMALL_REQUEST_MAX_BYTES == config_module._MCP_ENVELOPE_ALLOWANCE_BYTES


def test_configure_installs_the_budget_and_logs_once(caplog):
    with caplog.at_level(logging.INFO, logger="src.services.body_budget"):
        b = body_budget.configure(make(), cgroup_reader=limit(2 * GIB))
    assert body_budget.get_body_budget() is b
    assert b.small_capacity == 16 * MIB and b.large_capacity == 112 * MIB
    assert b.wait_seconds == 15 and b.max_waiters == 8
    lines = [r for r in caplog.records if "MCP body budget" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert "source=cgroup" in lines[0].getMessage()


def test_fallback_and_unchecked_setting_log_at_warning(caplog):
    with caplog.at_level(logging.INFO, logger="src.services.body_budget"):
        body_budget.configure(make(), cgroup_reader=limit(None))
        body_budget.configure(make(mcp_body_memory_budget_bytes=GIB),
                              cgroup_reader=limit(None))
    lines = [r for r in caplog.records if "MCP body budget" in r.getMessage()]
    assert [r.levelno for r in lines] == [logging.WARNING, logging.WARNING]
    assert "source=fallback" in lines[0].getMessage()
    assert "source=setting" in lines[1].getMessage()


# ── static ranges: only the safe direction ──────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("mcp_body_memory_fraction", 0.1),
    ("mcp_body_memory_fraction", 0.5),
    ("mcp_body_memory_multiplier", 8),
    ("mcp_body_memory_multiplier", 32),
    ("mcp_body_memory_budget_bytes", 64 * MIB),
    ("mcp_body_memory_budget_bytes", None),
    ("mcp_body_budget_wait_seconds", 0),
    ("mcp_body_budget_wait_seconds", 60),
    ("mcp_body_budget_waiters", 1),
    ("mcp_body_budget_waiters", 256),
])
def test_in_range_values_are_accepted(field, value):
    assert getattr(make(**{field: value}), field) == value


@pytest.mark.parametrize("field,value", [
    ("mcp_body_memory_fraction", 0.09),
    ("mcp_body_memory_fraction", 0.51),
    ("mcp_body_memory_fraction", 0.8),
    ("mcp_body_memory_fraction", float("nan")),
    ("mcp_body_memory_multiplier", 4),
    ("mcp_body_memory_multiplier", 7),
    ("mcp_body_memory_multiplier", 33),
    ("mcp_body_memory_budget_bytes", 64 * MIB - 1),
    ("mcp_body_memory_budget_bytes", 0),
    ("mcp_body_budget_wait_seconds", -1),
    ("mcp_body_budget_wait_seconds", 61),
    ("mcp_body_budget_wait_seconds", float("inf")),
    ("mcp_body_budget_waiters", 0),
    ("mcp_body_budget_waiters", 257),
])
def test_out_of_range_values_are_refused(field, value):
    with pytest.raises(ValidationError):
        make(**{field: value})


def test_the_defaults():
    s = make()
    assert s.mcp_body_memory_budget_bytes is None
    assert s.mcp_body_memory_fraction == 0.5
    assert s.mcp_body_memory_multiplier == 8
    assert s.mcp_body_budget_wait_seconds == 15
    assert s.mcp_body_budget_waiters == 8


def test_settings_construction_never_reads_the_cgroup(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Settings read the cgroup")
    monkeypatch.setattr(body_budget, "read_cgroup_memory_limit", boom)
    make()
    make(mcp_body_memory_budget_bytes=64 * MIB, max_file_write_bytes=500 * MIB)


# ── the cgroup reader, against fake trees ───────────────────────────────────

V2_MOUNT = "30 1 0:26 / /sys/fs/cgroup rw,nosuid,nodev shared:4 - cgroup2 cgroup2 rw,nsdelegate\n"
V1_MEMORY_MOUNT = ("35 25 0:30 / /sys/fs/cgroup/memory rw,nosuid shared:13 - "
                   "cgroup cgroup rw,memory\n")
V1_CPU_MOUNT = "34 25 0:29 / /sys/fs/cgroup/cpu rw shared:12 - cgroup cgroup rw,cpu,cpuacct\n"
ROOT_MOUNT = "25 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"


def tree(tmp_path, *, cgroup, mountinfo, files):
    (tmp_path / "proc/self").mkdir(parents=True)
    if cgroup is not None:
        (tmp_path / "proc/self/cgroup").write_text(cgroup)
    if mountinfo is not None:
        (tmp_path / "proc/self/mountinfo").write_text(mountinfo)
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_private_namespace_v2_reads_the_mount_root(tmp_path):
    root = tree(tmp_path, cgroup="0::/\n", mountinfo=ROOT_MOUNT + V2_MOUNT,
                files={"sys/fs/cgroup/memory.max": "2147483648\n"})
    assert read_cgroup_memory_limit(root) == 2 * GIB


def test_nested_v2_takes_the_smallest_ancestor(tmp_path):
    root = tree(tmp_path, cgroup="0::/a/b/c\n", mountinfo=ROOT_MOUNT + V2_MOUNT,
                files={"sys/fs/cgroup/a/b/c/memory.max": "max\n",
                       "sys/fs/cgroup/a/b/memory.max": "1073741824\n",
                       "sys/fs/cgroup/a/memory.max": "4294967296\n"})
    assert read_cgroup_memory_limit(root) == 1 * GIB


def test_nested_v2_below_a_namespaced_mount_root(tmp_path):
    # The mount's root is /kubepods/pod1, and /proc/self/cgroup names a path
    # beneath it: only the part below the mount root is under the mount point.
    mount = "30 1 0:26 /kubepods/pod1 /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n"
    root = tree(tmp_path, cgroup="0::/kubepods/pod1/ctr\n", mountinfo=mount,
                files={"sys/fs/cgroup/ctr/memory.max": "805306368\n",
                       "sys/fs/cgroup/memory.max": "2147483648\n"})
    assert read_cgroup_memory_limit(root) == 768 * MIB


def test_v2_with_max_everywhere_and_no_v1_is_no_limit(tmp_path):
    root = tree(tmp_path, cgroup="0::/user.slice/app.service\n",
                mountinfo=ROOT_MOUNT + V2_MOUNT,
                files={"sys/fs/cgroup/user.slice/app.service/memory.max": "max\n",
                       "sys/fs/cgroup/user.slice/memory.max": "max\n"})
    assert read_cgroup_memory_limit(root) is None


def test_v1_reads_the_controller_root(tmp_path):
    root = tree(tmp_path, cgroup="4:memory:/\n3:cpu,cpuacct:/\n",
                mountinfo=ROOT_MOUNT + V1_CPU_MOUNT + V1_MEMORY_MOUNT,
                files={"sys/fs/cgroup/memory/memory.limit_in_bytes": "2147483648\n"})
    assert read_cgroup_memory_limit(root) == 2 * GIB


def test_v1_unlimited_is_no_limit(tmp_path):
    root = tree(tmp_path, cgroup="4:memory:/\n", mountinfo=ROOT_MOUNT + V1_MEMORY_MOUNT,
                files={"sys/fs/cgroup/memory/memory.limit_in_bytes":
                       "9223372036854771712\n"})
    assert read_cgroup_memory_limit(root) is None


def test_hybrid_falls_through_to_v1(tmp_path):
    # The unified hierarchy carries no memory controller (no memory.max).
    v2_unified = "31 25 0:27 / /sys/fs/cgroup/unified rw - cgroup2 cgroup2 rw\n"
    root = tree(tmp_path, cgroup="0::/x\n4:memory:/x\n",
                mountinfo=ROOT_MOUNT + v2_unified + V1_MEMORY_MOUNT,
                files={"sys/fs/cgroup/memory/memory.limit_in_bytes": "1610612736\n"})
    assert read_cgroup_memory_limit(root) == 1536 * MIB


def test_nothing_readable_is_no_limit(tmp_path):
    root = tree(tmp_path, cgroup=None, mountinfo=None, files={})
    assert read_cgroup_memory_limit(root) is None


def test_unreadable_mountinfo_uses_the_default_mount_points(tmp_path):
    root = tree(tmp_path, cgroup="0::/svc\n", mountinfo=None,
                files={"sys/fs/cgroup/svc/memory.max": "1073741824\n"})
    assert read_cgroup_memory_limit(root) == 1 * GIB


def test_garbage_values_are_no_limit(tmp_path):
    root = tree(tmp_path, cgroup="0::/\n", mountinfo=V2_MOUNT,
                files={"sys/fs/cgroup/memory.max": "-5\n"})
    assert read_cgroup_memory_limit(root) is None


def test_a_path_outside_the_mount_root_reads_only_the_mount_point(tmp_path):
    mount = "30 1 0:26 /other /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n"
    root = tree(tmp_path, cgroup="0::/../../escape\n", mountinfo=mount,
                files={"sys/fs/cgroup/memory.max": "1073741824\n"})
    assert read_cgroup_memory_limit(root) == 1 * GIB


def test_an_escaped_mount_point_is_decoded(tmp_path):
    mount = "30 1 0:26 / /sys/fs/my\\040cgroup rw - cgroup2 cgroup2 rw\n"
    root = tree(tmp_path, cgroup="0::/\n", mountinfo=mount,
                files={"sys/fs/my cgroup/memory.max": "1073741824\n"})
    assert read_cgroup_memory_limit(root) == 1 * GIB


# ── the migration init container (Codex spec review finding 4) ─────────────

INIT_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import src.services.body_budget as bb

calls = []
def small_container(*a, **k):
    calls.append(1)
    return 512 * 1024 * 1024
bb.read_cgroup_memory_limit = small_container

import src.config
from src.config import Settings
Settings()
# Everything alembic/env.py imports from this repo (pinned by the AST test
# below), plus the engine module.
import src.models.db
import src.services.vector_index
import src.services.transport_security
import src.database
imports_read = len(calls)
try:
    bb.configure(src.config.settings, log_line=False)
    refused = False
except bb.BodyBudgetConfigError:
    refused = True
print("RESULT " + json.dumps({"imports_read": imports_read, "refused": refused}))
"""


def test_importing_settings_and_alembic_under_a_small_limit_does_not_fail(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "SECRET_KEY": "0123456789abcdef0123456789abcdef",
        "EMBEDDING_ALLOW_PLAINTEXT": "true",
        "VAULT_PATH": str(vault),
        "MCP_HOSTNAME": "",
    }
    result = subprocess.run(
        [sys.executable, "-c", INIT_PROBE, str(ROOT)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(l for l in result.stdout.splitlines() if l.startswith("RESULT "))
    outcome = json.loads(line.removeprefix("RESULT "))
    # Nothing on the import/migration path read the cgroup, and the same limit
    # does refuse the web application's own startup.
    assert outcome == {"imports_read": 0, "refused": True}


def test_alembic_env_imports_only_what_the_probe_imports():
    """The probe above imports what `alembic/env.py` imports. If env.py ever
    imports more of the app (say `src.main`), this fails and the probe must
    grow with it."""
    import ast

    module = ast.parse((ROOT / "alembic/env.py").read_text())
    imported = set()
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert {m for m in imported if m.split(".")[0] == "src"} == {
        "src.config", "src.models.db", "src.services.vector_index",
        "src.services.transport_security"}


# ── the lifespan is where the check runs ────────────────────────────────────

class _Sentinel(Exception):
    pass


async def test_the_lifespan_refuses_a_too_small_container_before_any_other_guard(monkeypatch):
    from src import main

    def later_guard():
        raise _Sentinel("the body budget check did not run first")

    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", False)
    monkeypatch.setattr(body_budget, "read_cgroup_memory_limit", limit(512 * MIB))
    monkeypatch.setattr(main, "_check_openat2_support", later_guard)
    with pytest.raises(SystemExit):
        async with main.lifespan(main.app):
            pass


async def test_the_lifespan_installs_the_derived_budget(monkeypatch):
    from src import main

    def later_guard():
        raise _Sentinel("stop after the body budget")

    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", False)
    monkeypatch.setattr(body_budget, "read_cgroup_memory_limit", limit(2 * GIB))
    monkeypatch.setattr(main, "_check_openat2_support", later_guard)
    body_budget.reset_body_budget(None)
    with pytest.raises(_Sentinel):
        async with main.lifespan(main.app):
            pass
    installed = body_budget.get_body_budget()
    assert installed.plan.source == "cgroup"
    assert installed.large_capacity == 112 * MIB
