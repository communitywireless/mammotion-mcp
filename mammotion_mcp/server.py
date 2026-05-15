"""MCP server entry point — Mammotion Luba2-AWD canonical control.

Run via `python -m mammotion_mcp.server` (stdio transport).

This module wires the FastMCP instance, registers tools from the `tools/`
subpackage, and starts the stdio event loop. Driver work fleshes out the
tool surface against the Investigator-locked pymammotion surface.

v0 scaffold — placeholders for Driver to implement.
"""

from __future__ import annotations

import logging
import os
import sys

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "FATAL: `mcp` package not installed. Run `pip install -e .` from the project root.\n"
    )
    raise

from mammotion_mcp.ha_client import HAClient
from mammotion_mcp.safety import SafetyGate

LOGGER = logging.getLogger("mammotion_mcp")


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
        stream=sys.stderr,  # MCP stdio uses stdout for protocol — log to stderr
    )


def build_server() -> FastMCP:
    """Construct the FastMCP server with all tools registered.

    Wired by Driver during the build pipeline. v0 scaffold returns an empty
    server.
    """
    _configure_logging()

    server = FastMCP("mammotion-mcp")

    # Shared resources for tools (Driver wires)
    ha_client = HAClient(
        url=os.environ.get("HA_URL", "http://192.168.1.201:8123"),
        token=os.environ.get("HA_TOKEN", ""),
        mower_entity_id=os.environ.get("MOWER_ENTITY_ID", "lawn_mower.luba2_awd_1"),
    )
    safety = SafetyGate(
        quiet_hours_start_hst=int(os.environ.get("QUIET_HOURS_START_HST", "21")),
        quiet_hours_end_hst=int(os.environ.get("QUIET_HOURS_END_HST", "8")),
        min_battery_pct=int(os.environ.get("MIN_BATTERY_PCT", "30")),
        lock_file_path=os.environ.get("LOCK_FILE_PATH", "/tmp/mammotion-mcp.lock"),
    )

    # Tool registration (Driver implements each tools/*.py module)
    from mammotion_mcp.tools import motion, mow, status

    mow.register(server, ha_client=ha_client, safety=safety)
    status.register(server, ha_client=ha_client)

    enable_diag = os.environ.get("ENABLE_DIAGNOSTIC_TOOLS", "false").lower() == "true"
    motion.register(
        server,
        ha_client=ha_client,
        safety=safety,
        enable_diagnostic_tools=enable_diag,
    )

    if enable_diag:
        from mammotion_mcp.tools import diag
        diag.register(server, ha_client=ha_client)

    LOGGER.info("mammotion-mcp server constructed (HA=%s)", ha_client.url)
    return server


def main() -> None:
    """Stdio entry point."""
    server = build_server()
    server.run()  # blocks on stdio


if __name__ == "__main__":
    main()
