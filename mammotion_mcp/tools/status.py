"""Tier-2 status tools: get_mower_status, list_areas, get_position.

Read-only — cheap to call, safe for any agent at any time.

Verified surface (2026-05-14 against Thor1 HA):
- ``device_tracker.luba2_awd_1_luba_vpmnn5kt`` carries lat/lon/direction in
  ``attributes`` (degrees; HA converts from pymammotion radians natively).
  Note: this is RTK-corrected GPS — do NOT use ``sensor.*_latitude`` /
  ``sensor.*_longitude``, which appear to mirror the raw device feed and
  diverge from the RTK-corrected value by ~30cm (per the device_tracker's
  gwf_position_warning attribute).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from mammotion_mcp import area_resolver
from mammotion_mcp.ha_client import HAClient

LOGGER = logging.getLogger("mammotion_mcp.tools.status")


def register(server: FastMCP, *, ha_client: HAClient) -> None:
    """Register Tier-2 status tools."""

    mapping_path = os.environ.get("AREA_MAPPING_PATH")  # None → package default

    @server.tool()
    async def get_mower_status() -> dict[str, Any]:
        """Snapshot of mower telemetry.

        Reads (in parallel):
        - ``lawn_mower.luba2_awd_1`` for overall state + last_changed
        - ``sensor.luba2_awd_1_activity_mode``
        - ``binary_sensor.luba2_awd_1_charging``
        - ``sensor.luba2_awd_1_battery``
        - ``sensor.luba2_awd_1_last_error_code``
        - ``sensor.luba2_awd_1_last_error_time``
        - ``sensor.luba2_awd_1_blade_used_time``

        Returns:
            Dict with state, activity_mode, charging, battery_pct,
            last_error_code, last_error_time, blade_used_time_hr,
            last_changed. Any individual sensor that fails to read becomes
            None — best-effort, not all-or-nothing.
        """
        status = await ha_client.get_mower_status()
        return status.to_dict()

    @server.tool()
    async def list_areas() -> list[dict[str, str]]:
        """Available mowing areas by app-name.

        Returns:
            List of {app_name, hash, ha_switch_entity} dicts. Agents resolve
            app-name → switch_entity via this surface; they do NOT hard-code
            entity IDs (HA's auto-generated friendly_names DO NOT match
            Joshua's app names).
        """
        return area_resolver.list_areas(mapping_path)

    @server.tool()
    async def get_position() -> dict[str, Any]:
        """Current mower GPS + heading.

        Reads from ``device_tracker.luba2_awd_1_luba_vpmnn5kt`` (RTK-
        corrected GPS in degrees).

        Returns:
            Dict with latitude, longitude, heading_deg, gps_accuracy,
            battery_level, activity_mode. None values for fields not
            currently reported.
        """
        base = ha_client.mower_entity_id.split(".", 1)[-1]  # "luba2_awd_1"
        tracker_eid = f"device_tracker.{base}_luba_vpmnn5kt"

        # Read device_tracker for GPS + sensor.activity_mode for state.
        position: dict[str, Any] = {
            "latitude": None,
            "longitude": None,
            "heading_deg": None,
            "gps_accuracy": None,
            "battery_level": None,
            "activity_mode": None,
            "protocol_version": 1,
        }

        try:
            tracker = await ha_client.get_state(tracker_eid)
            attrs = tracker.get("attributes", {}) or {}
            position["latitude"] = attrs.get("latitude")
            position["longitude"] = attrs.get("longitude")
            position["heading_deg"] = attrs.get("direction")
            position["gps_accuracy"] = attrs.get("gps_accuracy")
            position["battery_level"] = attrs.get("battery_level")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to read device_tracker %s: %s", tracker_eid, exc)

        try:
            activity = await ha_client.get_state(f"sensor.{base}_activity_mode")
            position["activity_mode"] = activity.get("state")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to read activity_mode sensor: %s", exc)

        return position
