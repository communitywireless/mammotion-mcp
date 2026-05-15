"""Tier-2 status tools: get_mower_status, list_areas, get_position.

Read-only — cheap to call, safe for any agent at any time.

v0 scaffold — Driver implements per Investigator-confirmed telemetry fields.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from mammotion_mcp.ha_client import HAClient

LOGGER = logging.getLogger("mammotion_mcp.tools.status")


def register(server: FastMCP, *, ha_client: HAClient) -> None:
    """Register Tier-2 status tools."""

    @server.tool()
    async def get_mower_status() -> dict[str, Any]:
        """Snapshot of mower telemetry.

        Returns:
            Dict with state, activity_mode, charging, battery_pct,
            last_error_code, last_error_time, blade_used_time_hr,
            last_changed timestamp.
        """
        # Driver implements via HAClient.get_mower_status()
        raise NotImplementedError("Driver: implement")

    @server.tool()
    async def list_areas() -> list[dict[str, str]]:
        """Available mowing areas by app-name.

        Returns:
            List of {app_name, hash, ha_switch_entity} dicts. Agents resolve
            app-name → switch_entity via this surface; they do NOT hard-code
            entity IDs.
        """
        mapping_path = os.environ.get(
            "AREA_MAPPING_PATH", "/app/data/area-mapping.json"
        )
        try:
            with open(mapping_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            LOGGER.error("Area mapping not found at %s", mapping_path)
            return []
        by_app = data.get("by_app_name", {})
        return [
            {
                "app_name": name,
                "hash": str(entry.get("hash", "")),
                "ha_switch_entity": entry.get("ha_switch_entity", ""),
            }
            for name, entry in by_app.items()
        ]

    @server.tool()
    async def get_position() -> dict[str, Any]:
        """Current mower GPS + local coords + heading.

        Returns:
            Dict with latitude, longitude, pos_x, pos_y, bearing, speed,
            activity_mode. None values for fields not currently reported.

        Useful for: waypoint capture, "where is it stuck", path replay,
        post-incident location.
        """
        # Driver: read from device.report_data.dev.* fields exposed via HA
        # state attributes OR direct pymammotion if necessary
        raise NotImplementedError("Driver: implement")
