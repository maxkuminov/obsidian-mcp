"""Pytest configuration.

Forces an in-process default config so importing `src.config` succeeds even
when `.env` is missing on the test host. Individual tests can monkeypatch
specific settings as needed.
"""
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Every `Settings` field, upper-cased — i.e. every environment variable that can
# change how the app is wired. Kept as a static list because it has to be known
# *before* `src.config` is imported. `tests/test_conftest_env_hermeticity.py`
# asserts it still matches `Settings.model_fields`, so adding a setting without
# adding it here is a test failure rather than a silent hole.
SETTINGS_ENV_KEYS = (
    "DATABASE_URL",
    "OLLAMA_URL",
    "OLLAMA_KEEP_ALIVE",
    "VAULT_PATH",
    "SECRET_KEY",
    "INDEX_INTERVAL_SECONDS",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "CHUNK_SIZE",
    "CHUNK_OVERLAP",
    "EMBED_CHUNK_BUDGET_PER_USER",
    "EMBED_TIME_BUDGET_SECONDS_PER_USER",
    "VAULT_ROOT_OBSERVE_TIMEOUT_SECONDS",
    "EMBEDDING_EXCLUDE_PATTERNS",
    "MCP_HOSTNAME",
    "BASE_URL",
    "ALLOWED_ORIGINS",
    "ALLOWED_HOSTS",
    "FTS_CONFIGS",
    "EMBEDDING_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_EMBEDDING_MODEL",
    "MAX_FILE_READ_BYTES",
    "MAX_FILE_WRITE_BYTES",
    "MAX_READ_RESPONSE_CHARS",
    "WRITE_PRECONDITION_REQUIRED",
    "TRANSFER_TOKEN_TTL_SECONDS",
    "TRANSFER_MAX_UPLOAD_SECONDS",
    "TRANSFER_MAX_CONCURRENT_UPLOADS",
    "IMPORT_ALLOW_HTTP",
    "VAULT_ALLOW_NAMED_STAGING_FALLBACK",
    "MULTI_USER_MODE",
    "SESSION_MAX_AGE",
    "SESSION_COOKIE_NAME",
    "SESSION_TOUCH_INTERVAL_SECONDS",
    "SESSION_PURGE_RETAIN_DAYS",
    "OAUTH_KNOWN_REDIRECT_HOSTS",
    "MCP_CONCURRENCY_MODE",
    "MCP_CONCURRENCY_WAIT_SECONDS",
    "MCP_CONCURRENCY_REQUESTS",
    "MCP_CONCURRENCY_FINGERPRINT",
    "MCP_CONCURRENCY_AUTH",
    "MCP_CONCURRENCY_TOOLS",
    "MCP_CONCURRENCY_TENANT",
    "MCP_CONCURRENCY_PRINCIPAL",
    "MCP_CONCURRENCY_EMBEDDING",
    "MCP_CONCURRENCY_VECTOR",
    "MCP_CONCURRENCY_WRITE",
    "MCP_CONCURRENCY_OTHER",
    "MCP_CONCURRENCY_WAITERS",
    "MCP_CONCURRENCY_TENANT_WAITERS",
    "MCP_CONCURRENCY_PRINCIPAL_WAITERS",
    "MCP_CONCURRENCY_REGISTRY_SIZE",
    "MCP_CONCURRENCY_WRITERS",
    "MCP_CONCURRENCY_WRITER_WAITERS",
    "MCP_CONCURRENCY_WRITER_WAIT_SECONDS",
    "MCP_SANDBOX_MODE",
    # The /mcp rate controls (#188, #194). Every one is a `Settings` field, so
    # a developer with any of them exported would otherwise change what the
    # suite measures — which for a *limiter* means a green run on a machine
    # whose buckets are off.
    "MCP_AUTH_FAILURE_LIMIT",
    "MCP_AUTH_FAILURE_WINDOW_SECONDS",
    "MCP_AUTH_FAILURE_TABLE_SIZE",
    "MCP_RATE_LIMIT_PER_MINUTE",
    "MCP_RATE_LIMIT_BURST",
    "MCP_WRITE_RATE_LIMIT_PER_MINUTE",
    "MCP_WRITE_RATE_LIMIT_BURST",
    "MCP_LIMITER_MAX_TRACKED_PRINCIPALS",
    "MCP_REFUSAL_LOG_INTERVAL_SECONDS",
    "DEFAULT_DAILY_REQUEST_LIMIT",
    "LOG_LEVEL",
    "LOG_FORMAT",
)

# The values the singleton is built from. Everything else in SETTINGS_ENV_KEYS
# is *removed* for the duration of the import so the model defaults apply.
CONTROLLED_TEST_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
    "SECRET_KEY": "test",
    "VAULT_PATH": "/tmp/test-vault",
}

# Escape hatch for the import-isolated subprocess suites
# (`tests/_transport_body_limit_cases.py`), which deliberately choose
# import-time settings — MAX_FILE_WRITE_BYTES, MCP_SANDBOX_MODE, VAULT_PATH —
# and pass them in a *scrubbed* environment of their own construction. Only
# those spawners set it; a developer shell must not.
TRUST_ENV_VAR = "OMCP_TEST_TRUST_ENV"


# pydantic-settings matches environment variables **case-insensitively**, so
# `base_url=…` feeds the singleton exactly like `BASE_URL=…`. Scrubbing only the
# canonical upper-case spellings therefore left the hole open for any other
# casing, which is why the scrub below walks the real environment and compares
# folded names instead of popping a fixed list of keys.
SETTINGS_ENV_KEYS_FOLDED = frozenset(k.casefold() for k in SETTINGS_ENV_KEYS)


@contextmanager
def _hermetic_settings_env():
    """Replace every `Settings`-shaped variable with controlled values.

    `os.environ.setdefault` was not enough: a developer with `BASE_URL` or
    `MCP_HOSTNAME` exported (both are real deployment variables, and `.env` is
    routinely sourced) still fed them into the singleton, so what the app's
    CORS/TrustedHost middleware allowed — and therefore what the suite asserted
    — depended on the machine. Env vars outrank dotenv in pydantic-settings, so
    suppressing the `.env` file alone does not close this.

    Every entry whose *casefolded* name matches a `Settings` field is removed,
    whatever its spelling, and the exact original entries (original casing and
    value) are put back afterwards.
    """
    saved = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in SETTINGS_ENV_KEYS_FOLDED
    }
    try:
        for key in saved:
            os.environ.pop(key, None)
        os.environ.update(CONTROLLED_TEST_ENV)
        yield
    finally:
        # Drop the controlled values we introduced (a differently-cased
        # original is restored below under its own name), then put back exactly
        # what was there.
        for key in CONTROLLED_TEST_ENV:
            if key not in saved:
                os.environ.pop(key, None)
        os.environ.update(saved)


# Build the `src.config.settings` singleton *without* the repo-root `.env` and
# without the host's environment. A developer checkout has a real `.env` (it
# doubles as the compose env file), and its BASE_URL / MCP_HOSTNAME would
# otherwise decide what the app's CORS and TrustedHost middleware allow —
# making test results depend on the machine. Doing it here, before any test
# module is imported, means the first import of `src.config` is always the
# hermetic one; individual test modules no longer have to win the
# collection-order race to get a clean config.
import pydantic_settings  # noqa: E402

_orig_settings_init = pydantic_settings.BaseSettings.__init__


def _init_without_env_file(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_settings_init(self, *args, **kwargs)


if os.environ.get(TRUST_ENV_VAR) == "1":
    # The spawner owns this process's environment; leave it alone.
    for _key, _value in CONTROLLED_TEST_ENV.items():
        os.environ.setdefault(_key, _value)
    pydantic_settings.BaseSettings.__init__ = _init_without_env_file
    try:
        import src.config  # noqa: F401,E402
    finally:
        pydantic_settings.BaseSettings.__init__ = _orig_settings_init
else:
    with _hermetic_settings_env():
        pydantic_settings.BaseSettings.__init__ = _init_without_env_file
        try:
            import src.config  # noqa: F401,E402
        finally:
            pydantic_settings.BaseSettings.__init__ = _orig_settings_init


@pytest.fixture(autouse=True)
def published_vault_root_snapshot():
    """Publish an **empty** vault-root overlap snapshot around every test.

    `vault._vault_root` refuses every multi-user caller until a snapshot has
    been published in the process — that readiness state is what keeps the
    server closed rather than permissive between accepting connections and the
    first detection. Without this fixture it would also turn every existing
    multi-user test into a vault-unavailable failure, for a reason that has
    nothing to do with what those tests assert.

    Empty, not synthetic: the default is "everything was checked and nothing
    overlaps", which is the state the production deployment is expected to be
    in. Tests that need a quarantined caller publish their own entries with
    `vault_overlap.publish_synthetic_snapshot`, and the handful that exercise
    the *detector entry points* opt in to the never-published state with the
    `unpublished_vault_root_snapshot` fixture below rather than relying on
    import or collection order.
    """
    from src.services import vault_overlap

    vault_overlap.reset_snapshot_state()
    vault_overlap.publish_synthetic_snapshot()
    yield
    vault_overlap.reset_snapshot_state()


@pytest.fixture
def unpublished_vault_root_snapshot():
    """Opt in to the never-published state for one test.

    Requested by name, so a test that depends on the readiness refusal says so.
    It runs after the autouse fixture above and undoes it for the duration.
    """
    from src.services import vault_overlap

    vault_overlap.reset_snapshot_state()
    yield
    vault_overlap.reset_snapshot_state()


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    """Clear the cached provider singleton between tests so changes to
    `settings.embedding_provider` propagate."""
    from src.services import embeddings

    embeddings.get_provider.cache_clear()
    yield
    embeddings.get_provider.cache_clear()


@pytest.fixture(autouse=True)
def _reset_concurrency_controller():
    """Give each test its own explicit process-controller boundary (#261).

    Lifespan tests intentionally close admissions and writers. That shutdown
    must persist for the rest of that test, but must not turn unrelated later
    middleware calls into 429s or suppress their usage inserts. Production
    requests never reset the controller; tests and lifespan own that boundary.
    Captured leases still release their original controller after replacement.
    """
    from src.services import concurrency

    concurrency.reset_controller()
    yield
    concurrency.get_controller().shutdown(close_writers=True)


@pytest.fixture(autouse=True)
def _reset_rate_limiter_state():
    """Start every test with full buckets and an empty failure table (#194).

    The /mcp rate controls are deliberately in-process and deliberately not
    persisted, which makes them the one kind of state a test suite can leak
    through: one test that drives a credential past its burst would refuse the
    next test's first call, and one that produces authentication failures
    charges the same address slot every other middleware test uses — a request
    with no client address is charged to the *shared reserved* slot, on
    purpose, so "no address" cannot be a bypass. Across a few hundred
    middleware tests that slot would eventually cross the budget and turn an
    unrelated 401 assertion into a 429, in an order-dependent way.

    Resetting between tests is therefore hermeticity, not convenience.
    """
    from src.services import rate_limits

    rate_limits.reset_state_for_tests()
    yield
    rate_limits.reset_state_for_tests()
