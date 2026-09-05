"""Entry point for stdio MCP transport.

Registry sandboxes (Glama et al.) test MCP servers via stdio, but
this server speaks Streamable HTTP in production. This module
re-exposes the same FastMCP instance over stdio so the test harness
can enumerate tools without spinning up FastAPI.

Tools register but cannot run — there's no DB connection, no indexer,
no vault mount. Registries only call `list_tools`, which is satisfied
by the decorator-time registration in `src.mcp_server.server`.

Production deployments use `src.main:app` over HTTP; this entry
exists solely so stdio-only harnesses have something to talk to.
"""
import atexit
import logging

from src.mcp_server.server import mcp

from src.logging_setup import configure_logging
from src.services import security_events

# After the `mcp` import, for the reason `src/main.py` says: the SDK installs a
# `RichHandler` on the root logger when `FastMCP` is constructed, and
# `basicConfig` — which is what used to stand here — cannot displace it.
#
# **The handler writes to stderr**, which matters more here than anywhere else:
# stdout is the MCP protocol channel, and one log line on it is a protocol
# error. WARNING rather than the configured level, because a registry sandbox
# wants the tool list, not the server's narration.
configure_logging(level=logging.WARNING)

# No lifespan here, so the suppressor's shutdown flush hangs off `atexit`
# instead. Without it a window holding a withheld count loses it at exit.
atexit.register(security_events.flush_suppression_summaries)


if __name__ == "__main__":
    mcp.run("stdio")
