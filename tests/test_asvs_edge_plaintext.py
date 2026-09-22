"""Plaintext HTTP is refused, not redirected, on machine-facing paths — #196.

ASVS V4.1.2. Traefik's `http-catchall` redirects every plaintext request to
HTTPS and the repo's routers bound `https` only, so nothing in this tree
refused plaintext. An MCP client misconfigured with `http://…/mcp` put
`Authorization: Bearer omcp_…` on the wire in cleartext, took a 307 (which
preserves method and body), and replayed successfully over TLS because httpx
deliberately keeps the `Authorization` header across a direct http→https hop.
Every request worked, so the leak was silent by construction.

These are **configuration** assertions over the tracked deployment files.
There is no way to unit-test another process's proxy, so the behavioural gate
is the `curl` matrix in the change's `tasks.md` (7.2), run after deploy. What
this module protects is the shape of the rule: that the refusal exists, that it
outranks the catch-all deterministically, that it covers every machine-facing
path *including* the Bearer-qualified root under every method, that it cannot
swallow the ACME challenge, and that no deployment-specific value reaches a
public repository.
"""

import ipaddress
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
CADDYFILE_PATH = REPO_ROOT / "Caddyfile.example"

PLAINTEXT_ROUTER = "obsidian-mcp-plaintext-rtr"
PLAINTEXT_MIDDLEWARE = "obsidian-mcp-no-plaintext"

# The seven machine-facing path matchers the refusal must carry, spelled as
# they appear in a Traefik v3 rule.
MACHINE_PATH_MATCHERS = (
    "PathPrefix(`/mcp`)",
    "PathPrefix(`/transfer`)",
    "Path(`/health`)",
    "PathPrefix(`/.well-known`)",
    "Path(`/register`)",
    "Path(`/token`)",
    "Path(`/revoke`)",
)

# The catch-all redirect router declares no explicit priority, so Traefik
# derives one from its rule's length: len("HostRegexp(`.+`)") == 16. Ours must
# beat that by a number we wrote down, not by a string length.
CATCHALL_DERIVED_PRIORITY = 16

# Addresses reserved for documentation. A refusal whose whole mechanism is an
# unroutable source range cannot be verified by a check that bans literals
# outright, so these are permitted by name.
DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "192.0.2.0/24",  # RFC 5737 TEST-NET-1
        "198.51.100.0/24",  # RFC 5737 TEST-NET-2
        "203.0.113.0/24",  # RFC 5737 TEST-NET-3
        "2001:db8::/32",  # RFC 3849
    )
)

# Literals the reference files carried before this change. The requirement
# bans values that identify a deployment, not example values already
# published; re-checking these would only invite someone to delete them.
PRE_EXISTING_EXAMPLE_ADDRESSES = frozenset({"1.2.3.4", "5.6.7.8/32"})

# Loopback and the unspecified address identify no deployment.
ALWAYS_ALLOWED_ADDRESSES = frozenset({"127.0.0.1", "::1", "0.0.0.0"})


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_labels() -> dict[str, str]:
    """The `obsidian-mcp` service's Traefik labels, as a key → value map."""
    doc = yaml.safe_load(_read(COMPOSE_PATH))
    labels = doc["services"]["obsidian-mcp"]["labels"]
    out: dict[str, str] = {}
    for entry in labels:
        key, _, value = str(entry).partition("=")
        out[key] = value
    return out


@pytest.fixture(scope="module")
def plaintext_rule(compose_labels: dict[str, str]) -> str:
    key = f"traefik.http.routers.{PLAINTEXT_ROUTER}.rule"
    assert key in compose_labels, (
        "the plaintext-refusal router is missing from docker-compose.yml; "
        "without it every plaintext request is redirected and a bearer "
        "credential sent to http:// is replayed over TLS silently (#196)"
    )
    return compose_labels[key]


@pytest.fixture(scope="module")
def caddyfile_text() -> str:
    return _read(CADDYFILE_PATH)


# `{$MCP_HOSTNAME}`, `{uri}` and friends are Caddy placeholders, not block
# delimiters. Counting braces without masking them closes the site block early.
_CADDY_PLACEHOLDER = re.compile(r"\{\$?[A-Za-z_][\w.]*\}")


@pytest.fixture(scope="module")
def caddy_plaintext_block(caddyfile_text: str) -> str:
    """The `http://{$MCP_HOSTNAME}` site block, delimiters included."""
    masked = _CADDY_PLACEHOLDER.sub(lambda m: " " * len(m.group()), caddyfile_text)
    start = caddyfile_text.index("http://{$MCP_HOSTNAME}")
    depth = 0
    for index in range(start, len(masked)):
        char = masked[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return caddyfile_text[start : index + 1]
    raise AssertionError("the http:// site block in Caddyfile.example is unbalanced")


# ---------------------------------------------------------------------------
# Traefik: the router exists, on the plaintext entrypoint, and outranks the
# catch-all by a number rather than by an accident of string length.
# ---------------------------------------------------------------------------


def test_refusal_router_binds_the_plaintext_entrypoint(compose_labels):
    entrypoints = compose_labels[f"traefik.http.routers.{PLAINTEXT_ROUTER}.entrypoints"]
    assert entrypoints == "http"


def test_refusal_router_declares_an_explicit_priority_above_the_catchall(compose_labels):
    raw = compose_labels.get(f"traefik.http.routers.{PLAINTEXT_ROUTER}.priority")
    assert raw is not None, (
        "the refusal router must declare its priority explicitly; relying on "
        "Traefik's length-derived default makes the control depend on how long "
        "a rule string happens to be"
    )
    assert raw.isdigit()
    assert int(raw) > CATCHALL_DERIVED_PRIORITY


def test_refusal_router_carries_the_refusing_middleware_and_a_service(compose_labels):
    middlewares = compose_labels[f"traefik.http.routers.{PLAINTEXT_ROUTER}.middlewares"]
    assert PLAINTEXT_MIDDLEWARE in middlewares
    # Traefik requires a service even though the middleware short-circuits
    # before it is reached.
    assert compose_labels[f"traefik.http.routers.{PLAINTEXT_ROUTER}.service"]


def test_middleware_source_range_is_a_documentation_reserved_cidr(compose_labels):
    key = f"traefik.http.middlewares.{PLAINTEXT_MIDDLEWARE}.ipallowlist.sourcerange"
    source_range = compose_labels[key]
    network = ipaddress.ip_network(source_range)
    assert any(network.subnet_of(doc) for doc in DOCUMENTATION_NETWORKS if doc.version == network.version), (
        f"{source_range} must stay inside a documentation-reserved range so the "
        "allow-list can never match a real client; a routable value here would "
        "reopen the leak this rule exists to close"
    )


def test_middleware_does_not_override_the_reject_status(compose_labels):
    # 403 is `ipAllowList`'s native default and was chosen, not merely
    # inherited. `rejectstatuscode` exists; leaving it unset is what keeps the
    # status from drifting.
    key = f"traefik.http.middlewares.{PLAINTEXT_MIDDLEWARE}.ipallowlist.rejectstatuscode"
    assert key not in compose_labels


# ---------------------------------------------------------------------------
# Traefik: what the rule matches.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("matcher", MACHINE_PATH_MATCHERS)
def test_rule_covers_every_machine_facing_path(plaintext_rule, matcher):
    assert matcher in plaintext_rule


def test_rule_refuses_the_bearer_qualified_root(plaintext_rule):
    # Both reference deployments route `/` with a bearer header into the MCP
    # surface, so a refusal that omitted it would leave the original leak open
    # on a supported entry point.
    assert re.search(r"Path\(`/`\)\s*&&\s*HeaderRegexp\(", plaintext_rule), (
        "the root clause must conjoin Path(`/`) with a HeaderRegexp on "
        "Authorization"
    )
    header_match = re.search(
        r"HeaderRegexp\(`(?P<header>[^`]+)`,\s*`(?P<pattern>[^`]+)`\)", plaintext_rule
    )
    assert header_match is not None
    assert header_match.group("header").lower() == "authorization"
    assert "Bearer" in header_match.group("pattern")


def test_the_root_clause_is_never_a_bare_path_match(plaintext_rule):
    # A bare Path(`/`) would refuse ordinary browser traffic to the root, which
    # the browser-facing requirement forbids.
    bare_root = re.findall(r"(?<![A-Za-z])Path\(`/`\)", plaintext_rule)
    qualified_root = re.findall(r"(?<![A-Za-z])Path\(`/`\)\s*&&\s*HeaderRegexp\(", plaintext_rule)
    assert len(bare_root) == len(qualified_root) == 1


def test_the_refusal_is_method_independent(plaintext_rule):
    # A redirect that preserves the method is the case the finding turns on, so
    # the refusal may not be narrowed to a method set.
    assert "Method(" not in plaintext_rule


def test_rule_exempts_the_acme_challenge_prefix(plaintext_rule):
    assert "!PathPrefix(`/.well-known/acme-challenge/`)" in plaintext_rule


def test_the_acme_exemption_precedes_the_path_group(plaintext_rule):
    # Placed first so it cannot be lost by someone editing the path group.
    assert plaintext_rule.index("acme-challenge") < plaintext_rule.index("PathPrefix(`/mcp`)")


@pytest.mark.parametrize("browser_path", ["/admin", "/authorize", "/api"])
def test_rule_leaves_browser_facing_paths_to_the_redirect(plaintext_rule, browser_path):
    assert browser_path not in plaintext_rule


def test_rule_names_the_host_only_through_the_variable(plaintext_rule):
    host_match = re.search(r"Host\(`(?P<host>[^`]+)`\)", plaintext_rule)
    assert host_match is not None
    assert host_match.group("host").startswith("${MCP_HOSTNAME")


def test_the_https_routers_are_left_alone(compose_labels):
    # The refusal adds a router; it does not re-scope the existing ones. In
    # particular the https root router keeps its own matcher and priority.
    assert (
        compose_labels["traefik.http.routers.obsidian-mcp-root-rtr.rule"]
        == "Host(`${MCP_HOSTNAME:-obsidian-mcp.localhost}`) && Path(`/`) "
        "&& HeaderRegexp(`Authorization`, `^Bearer `)"
    )
    for router in (
        "obsidian-mcp-panel-rtr",
        "obsidian-mcp-api-rtr",
        "obsidian-mcp-apirest-rtr",
        "obsidian-mcp-root-rtr",
    ):
        assert compose_labels[f"traefik.http.routers.{router}.entrypoints"] == "https"


# ---------------------------------------------------------------------------
# Caddy: the published reference must not be weaker than what we run.
# ---------------------------------------------------------------------------


def test_caddy_defines_a_plaintext_site_block(caddy_plaintext_block):
    assert caddy_plaintext_block.startswith("http://{$MCP_HOSTNAME}")


@pytest.mark.parametrize(
    "path_matcher",
    ["/mcp*", "/transfer*", "/health", "/.well-known*", "/register", "/token", "/revoke"],
)
def test_caddy_refuses_every_machine_facing_path(caddy_plaintext_block, path_matcher):
    assert path_matcher in caddy_plaintext_block


def test_caddy_responds_403_rather_than_redirecting(caddy_plaintext_block):
    assert re.search(r"respond\s+\"[^\"]*\"\s+403", caddy_plaintext_block)


def test_caddy_exempts_the_acme_challenge_prefix(caddy_plaintext_block):
    assert "/.well-known/acme-challenge/*" in caddy_plaintext_block
    # The carve-out must come before the matcher that would otherwise refuse
    # the whole /.well-known prefix.
    assert caddy_plaintext_block.index("/.well-known/acme-challenge/*") < caddy_plaintext_block.index(
        "/.well-known*"
    )


def test_caddy_still_redirects_everything_else(caddy_plaintext_block):
    assert re.search(r"redir\s+https://\{\$MCP_HOSTNAME\}", caddy_plaintext_block)


def test_caddy_plaintext_root_matcher_is_its_own_and_method_independent(caddy_plaintext_block):
    """The plaintext root matcher must not be the HTTPS block's `@rootMcp`.

    `@rootMcp` carries `method POST GET DELETE`, a routing decision that is
    correct there and fatal here: a `HEAD /` with a bearer header would fall
    past the refusal into the redirect and replay the credential over TLS.
    """
    blocks = re.findall(r"@(\w+)\s*\{(.*?)\}", caddy_plaintext_block, flags=re.DOTALL)
    root_matchers = [
        (name, body)
        for name, body in blocks
        if re.search(r"^\s*path\s+/\s*$", body, flags=re.MULTILINE)
        and re.search(r"header\s+Authorization\s+Bearer", body)
    ]
    assert len(root_matchers) == 1, "expected exactly one bearer-qualified root matcher"
    name, body = root_matchers[0]
    assert name != "rootMcp", "the plaintext block must define its own root matcher"
    assert "method" not in body, (
        "the plaintext root matcher may not restrict methods; a refusal has to "
        "cover every method, HEAD included"
    )
    assert f"handle @{name}" in caddy_plaintext_block


def test_caddy_https_root_matcher_is_unchanged(caddyfile_text, caddy_plaintext_block):
    https_block = caddyfile_text.replace(caddy_plaintext_block, "")
    assert "method POST GET DELETE" in https_block


# ---------------------------------------------------------------------------
# The repository is public: no value in these files may identify a deployment.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [COMPOSE_PATH, CADDYFILE_PATH], ids=lambda p: p.name)
def test_no_deployment_specific_address_is_committed(path):
    text = _read(path)
    offenders = []
    for token in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b", text):
        if token in PRE_EXISTING_EXAMPLE_ADDRESSES:
            continue
        bare = token.split("/")[0]
        if bare in ALWAYS_ALLOWED_ADDRESSES:
            continue
        try:
            network = ipaddress.ip_network(token, strict=False)
        except ValueError:
            continue
        if any(network.subnet_of(doc) for doc in DOCUMENTATION_NETWORKS if doc.version == network.version):
            continue
        offenders.append(token)
    assert not offenders, (
        f"{path.name} carries address literals that are neither "
        f"documentation-reserved nor pre-existing examples: {offenders}"
    )


def test_compose_names_the_hostname_only_through_the_variable():
    text = _read(COMPOSE_PATH)
    for host in re.findall(r"Host\(`([^`]+)`\)", text):
        assert host.startswith("${MCP_HOSTNAME"), (
            f"{host!r} hard-codes a hostname into a public file; the host may "
            "only enter through MCP_HOSTNAME"
        )


def test_caddy_site_addresses_come_from_the_environment(caddyfile_text):
    addresses = re.findall(r"^\s*((?:https?://)?\{\$MCP_HOSTNAME\}[^\s{]*)\s*\{", caddyfile_text, re.MULTILINE)
    assert len(addresses) >= 2, "expected an https site block and a plaintext one"


def test_no_host_filesystem_path_is_bind_mounted_literally():
    """Every host-side bind source is a variable, a relative default, or tz data.

    Checked structurally against the parsed volumes rather than by scanning the
    text, so the file's own example comments are untouched — the requirement
    bans values that identify a deployment, not published examples.
    """
    doc = yaml.safe_load(_read(COMPOSE_PATH))
    allowed_literals = {"/etc/timezone", "/etc/localtime"}
    for volume in doc["services"]["obsidian-mcp"]["volumes"]:
        source = str(volume).split(":")[0]
        if source.startswith("${") or source.startswith("./") or source in allowed_literals:
            continue
        raise AssertionError(
            f"{source!r} is a literal host path in a public file; it belongs in "
            ".env behind a variable"
        )
