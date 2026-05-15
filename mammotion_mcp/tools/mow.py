"""Tier-1 mow tools: mow_area, dock_and_clear, cancel_job.

Each tool fires the verified canonical sequence INTERNALLY. Agents cannot
compose the steps wrong because they don't have the primitives — they call
the high-level tool only.

v0 scaffold — Driver implements full bodies per the locked Investigator
surface.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from mammotion_mcp.ha_client import HAClient
from mammotion_mcp.safety import SafetyGate

LOGGER = logging.getLogger("mammotion_mcp.tools.mow")

# Canonical default — Joshua's operational blade height. NOT the HA schema
# default of 25mm (which triggers Error 1202).
DEFAULT_BLADE_HEIGHT_MM = 55


def register(server: FastMCP, *, ha_client: HAClient, safety: SafetyGate) -> None:
    """Register Tier-1 mow tools on the FastMCP server."""

    @server.tool()
    async def mow_area(
        area_name: str,
        blade_height_mm: int = DEFAULT_BLADE_HEIGHT_MM,
        mow_duration_sec: int | None = None,
        return_to_dock: bool = True,
        override_quiet_hours: bool = False,
    ) -> dict[str, Any]:
        """Mow the named area using the verified 5-step canonical sequence.

        Args:
            area_name: App-side name (e.g. "Area 6"). Resolved via area-mapping.json
                       to the HA switch entity. See `list_areas` for available names.
            blade_height_mm: Cutting height in mm. Must be 15-100. Default 55
                             (Joshua's operational value; do NOT use 25 — triggers
                             Error 1202).
            mow_duration_sec: If set, mow for this many seconds then auto-dock.
                              If None, mower runs until area completes or paused.
            return_to_dock: If True, recall to dock after mow_duration_sec elapses
                            AND fire post-dock cancel_job (clean task state).
            override_quiet_hours: Bypass quiet-hours gate. Use only when explicitly
                                  needed (e.g. user-requested at night).

        Returns:
            Dict with status, area_resolved, duration, and final mower state.

        Raises:
            SafetyViolation: if quiet hours active without override, blade_height
                             out of bounds, or battery < min.
            ValueError: if area_name not resolvable.
        """
        # Driver implements:
        # 1. safety.check_quiet_hours(override=override_quiet_hours)
        # 2. safety.check_blade_height(blade_height_mm)
        # 3. resolve area_name -> switch_entity via area_resolver
        # 4. async with safety: (acquire concurrent-call lock)
        # 5.   status = await ha_client.get_mower_status()
        # 6.   safety.check_battery(status.battery_pct)
        # 7.   await ha_client.call_service("mammotion", "cancel_job"); sleep 10
        # 8.   await ha_client.call_service("mammotion", "start_stop_blades",
        #                                    start_stop=True, blade_height=blade_height_mm); sleep 3
        # 9.   await ha_client.call_service("mammotion", "start_mow",
        #                                    areas=[switch_entity], blade_height=blade_height_mm)
        # 10.  poll for state=mowing (max 60s)
        # 11.  if mow_duration_sec: sleep, then dock
        # 12.  if return_to_dock: dock + poll charging=on + post-dock cancel_job
        # 13.  return final status snapshot
        raise NotImplementedError("Driver: implement canonical sequence")

    @server.tool()
    async def dock_and_clear() -> dict[str, Any]:
        """Recall mower to dock + wait for charging=on + post-dock cancel_job.

        Use this to send the mower home in a fully-clean state. Without
        the post-dock cancel_job, the Mammotion app shows "task paused,
        not ready" even though the mower is physically docked + charging.
        """
        # Driver: lawn_mower.dock + poll charging=on + mammotion.cancel_job
        raise NotImplementedError("Driver: implement")

    @server.tool()
    async def cancel_job() -> dict[str, Any]:
        """Standalone mammotion.cancel_job — clear task state without recall.

        Useful for cleanup after manual app-side ops, or to clear a lingering
        breakpoint without sending the mower home.
        """
        # Driver: mammotion.cancel_job + return snapshot
        raise NotImplementedError("Driver: implement")
