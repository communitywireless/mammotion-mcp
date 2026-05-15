"""Tier-4 diagnostic tools — gated behind ENABLE_DIAGNOSTIC_TOOLS=true.

Includes:
- start_stop_blades — low-level blade motor toggle
- reload_integration — polling-stall workaround (HA integration restart)
- request_telemetry_snapshot — fire async_request_report_snapshot

v0 scaffold.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from mammotion_mcp.ha_client import HAClient

LOGGER = logging.getLogger("mammotion_mcp.tools.diag")


def register(server: FastMCP, *, ha_client: HAClient) -> None:
    """Register Tier-4 diagnostic tools."""

    @server.tool()
    async def start_stop_blades(
        start_stop: bool,
        blade_height_mm: int = 55,
    ) -> dict[str, Any]:
        """Low-level blade motor toggle (DrvMowCtrlByHand).

        Useful for: clearing Error 1202 via stop→start cycle; testing blade
        motor independent of nav planner; height-only adjustment via the
        DrvKnife protobuf field.
        """
        # Driver: ha_client.call_service("mammotion", "start_stop_blades", ...)
        raise NotImplementedError("Driver: implement")

    @server.tool()
    async def reload_integration() -> dict[str, Any]:
        """Reload the Mammotion HA integration.

        Workaround for polling-stall. POST /api/config/config_entries/entry/<id>/reload.
        Should become rarely-needed after iter-4 polling-stall fix deploys.
        """
        # Driver: discover the config_entry id at startup; cache; fire reload
        raise NotImplementedError("Driver: implement")

    @server.tool()
    async def request_telemetry_snapshot() -> dict[str, Any]:
        """Fire async_request_report_snapshot — force a telemetry refresh.

        Debug-only. Useful when the agent suspects pymammotion's last-seen
        state is stale.
        """
        # Driver: pymammotion-direct OR HA service if exposed
        raise NotImplementedError("Driver: implement")
