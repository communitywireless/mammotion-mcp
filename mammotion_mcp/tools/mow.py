"""Tier-1 mow tools: mow_area, dock_and_clear, cancel_job.

Each tool fires the verified canonical sequence INTERNALLY. Agents cannot
compose the steps wrong because they don't have the primitives — they call
the high-level tool only.

The 5-step canonical sequence (verified 2026-05-14 18:10 HST eyes-on):

1. ``mammotion.cancel_job`` — clear stale breakpoint
2. ``mammotion.start_stop_blades(start_stop=True, blade_height=55)`` — engage blade motor
3. ``mammotion.start_mow(areas=[switch], blade_height=55)`` — plan + start nav
4. ``lawn_mower.dock`` — recall (when mow_duration_sec elapses or area completes)
5. ``mammotion.cancel_job`` — POST-DOCK clear (after charging=on confirmed)

Step 5 is the difference between "charging" and "100% clean + ready for
next task." Without it, the Mammotion app shows "task paused, not ready."

See ``~/projects/mower-recovery-pm/docs/2026-05-14-mower-usage-guide-for-agents.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from mammotion_mcp import area_resolver
from mammotion_mcp.ha_client import HAClient
from mammotion_mcp.safety import SafetyGate

LOGGER = logging.getLogger("mammotion_mcp.tools.mow")

# Canonical default — Joshua's operational blade height. NOT the HA schema
# default of 25mm (which triggers Error 1202 "cutting disk is jammed").
DEFAULT_BLADE_HEIGHT_MM = 55

# Step delays — empirically tuned (2026-05-14 iter-3)
_DELAY_AFTER_CANCEL_SEC = 10
_DELAY_AFTER_BLADE_TOGGLE_SEC = 3
_DELAY_AFTER_POST_DOCK_CANCEL_SEC = 5

# Poll deadlines
_POLL_MOWING_TIMEOUT_SEC = 60
_POLL_CHARGING_TIMEOUT_SEC = 480
_POLL_INTERVAL_SEC = 3.0


async def _poll_mowing_state(
    ha_client: HAClient,
    timeout_s: int = _POLL_MOWING_TIMEOUT_SEC,
) -> bool:
    """Poll ``lawn_mower.luba2_awd_1.state`` until it reads "mowing".

    Returns True on success, False on timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            state = await ha_client.get_state()
            if state.get("state") == "mowing":
                LOGGER.info("Mower transitioned to state=mowing")
                return True
        except Exception as exc:  # noqa: BLE001 — keep polling on transient err
            LOGGER.warning("Polling state read failed: %s", exc)
        await asyncio.sleep(_POLL_INTERVAL_SEC)
    LOGGER.warning("Timeout waiting for state=mowing (waited %ds)", timeout_s)
    return False


async def _poll_charging(
    ha_client: HAClient,
    timeout_s: int = _POLL_CHARGING_TIMEOUT_SEC,
) -> bool:
    """Poll ``binary_sensor.luba2_awd_1_charging`` until it reads "on".

    The charging sensor is the load-bearing "I'm home + happy" signal in
    Mammotion-HA 0.5.44 (the lawn_mower entity rests at "paused" at dock,
    not "docked"). Returns True on success, False on timeout.
    """
    base = ha_client.mower_entity_id.split(".", 1)[-1]
    charging_eid = f"binary_sensor.{base}_charging"
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            state = await ha_client.get_state(charging_eid)
            if state.get("state") == "on":
                LOGGER.info("Mower transitioned to charging=on")
                return True
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Polling charging read failed: %s", exc)
        await asyncio.sleep(_POLL_INTERVAL_SEC)
    LOGGER.warning("Timeout waiting for charging=on (waited %ds)", timeout_s)
    return False


def register(server: FastMCP, *, ha_client: HAClient, safety: SafetyGate) -> None:
    """Register Tier-1 mow tools on the FastMCP server."""

    mapping_path = os.environ.get("AREA_MAPPING_PATH")  # None → package default

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
            area_name: App-side name (e.g. "Area 6"). Resolved via
                       area-mapping.json. See ``list_areas`` for available names.
            blade_height_mm: Cutting height in mm. Must be 15-100, step 5.
                             Default 55 (Joshua's operational value; do NOT
                             use 25 — triggers Error 1202).
            mow_duration_sec: If set, mow for this many seconds then auto-dock.
                              If None and return_to_dock=True, return the
                              snapshot after state=mowing is observed (caller
                              responsible for later dock); if None and
                              return_to_dock=False, return immediately after
                              mowing is observed.
            return_to_dock: If True, recall to dock after mow_duration_sec
                            elapses AND fire post-dock cancel_job.
            override_quiet_hours: Bypass quiet-hours gate.

        Returns:
            Dict with status, area_resolved, duration, and final mower state.

        Raises:
            SafetyViolation: if quiet hours active without override, blade_height
                             out of bounds, or battery < min.
            ValueError: if area_name not resolvable.
            RuntimeError: if mower fails to transition to state=mowing within
                          60s, or to charging=on within 480s after dock.
        """
        LOGGER.info(
            "mow_area called: area=%r blade_height=%d duration=%s return_to_dock=%s override=%s",
            area_name, blade_height_mm, mow_duration_sec, return_to_dock, override_quiet_hours,
        )

        # Safety gates (raise SafetyViolation if violated)
        safety.check_quiet_hours(override=override_quiet_hours)
        safety.check_blade_height(blade_height_mm)

        # Resolve area name → switch entity (raises ValueError on unknown)
        switch_entity = area_resolver.resolve(area_name, mapping_path)
        LOGGER.info("Area %r resolved → %s", area_name, switch_entity)

        async with safety:  # acquire single-flight lock
            # Preflight battery check
            status = await ha_client.get_mower_status()
            safety.check_battery(status.battery_pct)

            # Step 1: cancel_job (clear stale breakpoint)
            LOGGER.info("Step 1/5: mammotion.cancel_job")
            await ha_client.call_service("mammotion", "cancel_job")
            await asyncio.sleep(_DELAY_AFTER_CANCEL_SEC)

            # Step 2: start_stop_blades(true, blade_height) → fires DrvMowCtrlByHand
            LOGGER.info(
                "Step 2/5: mammotion.start_stop_blades(start_stop=True, blade_height=%d)",
                blade_height_mm,
            )
            await ha_client.call_service(
                "mammotion",
                "start_stop_blades",
                start_stop=True,
                blade_height=blade_height_mm,
            )
            await asyncio.sleep(_DELAY_AFTER_BLADE_TOGGLE_SEC)

            # Step 3: start_mow with explicit blade_height
            LOGGER.info(
                "Step 3/5: mammotion.start_mow(areas=[%s], blade_height=%d)",
                switch_entity, blade_height_mm,
            )
            await ha_client.call_service(
                "mammotion",
                "start_mow",
                areas=[switch_entity],
                blade_height=blade_height_mm,
            )

            # Poll for state=mowing (max 60s)
            mowing_reached = await _poll_mowing_state(
                ha_client, timeout_s=_POLL_MOWING_TIMEOUT_SEC
            )
            if not mowing_reached:
                raise RuntimeError(
                    f"Mower did not transition to state=mowing within "
                    f"{_POLL_MOWING_TIMEOUT_SEC}s"
                )

            # If mow_duration_sec specified, mow for that long
            if mow_duration_sec:
                LOGGER.info("Mowing for %d seconds then auto-dock", mow_duration_sec)
                await asyncio.sleep(mow_duration_sec)

            # If NOT returning to dock, return snapshot now
            if not return_to_dock:
                final_status = await ha_client.get_mower_status()
                return {
                    "result": "mowing_started",
                    "area_resolved": switch_entity,
                    "blade_height_mm": blade_height_mm,
                    "mower_status": final_status.to_dict(),
                    "protocol_version": 1,
                }

            # Step 4: lawn_mower.dock
            LOGGER.info("Step 4/5: lawn_mower.dock")
            await ha_client.call_service("lawn_mower", "dock")

            # Poll for charging=on (480s timeout)
            charging_reached = await _poll_charging(
                ha_client, timeout_s=_POLL_CHARGING_TIMEOUT_SEC
            )
            if not charging_reached:
                raise RuntimeError(
                    f"Mower did not reach charging=on within "
                    f"{_POLL_CHARGING_TIMEOUT_SEC}s after dock"
                )

            # Step 5: post-dock cancel_job (clean task state)
            LOGGER.info("Step 5/5: mammotion.cancel_job (post-dock cleanup)")
            await ha_client.call_service("mammotion", "cancel_job")
            await asyncio.sleep(_DELAY_AFTER_POST_DOCK_CANCEL_SEC)

            final_status = await ha_client.get_mower_status()
            return {
                "result": "mow_complete",
                "area_resolved": switch_entity,
                "blade_height_mm": blade_height_mm,
                "mower_status": final_status.to_dict(),
                "protocol_version": 1,
            }

    @server.tool()
    async def dock_and_clear() -> dict[str, Any]:
        """Recall mower to dock + wait for charging=on + post-dock cancel_job.

        Use this to send the mower home in a fully-clean state. Without
        the post-dock cancel_job, the Mammotion app shows "task paused,
        not ready" even though the mower is physically docked + charging.

        Returns:
            Final mower status snapshot.

        Raises:
            RuntimeError: if charging=on not reached within 480s.
        """
        LOGGER.info("dock_and_clear called")
        async with safety:
            await ha_client.call_service("lawn_mower", "dock")
            charging_reached = await _poll_charging(
                ha_client, timeout_s=_POLL_CHARGING_TIMEOUT_SEC
            )
            if not charging_reached:
                raise RuntimeError(
                    f"Mower did not reach charging=on within "
                    f"{_POLL_CHARGING_TIMEOUT_SEC}s after dock"
                )
            await ha_client.call_service("mammotion", "cancel_job")
            await asyncio.sleep(_DELAY_AFTER_POST_DOCK_CANCEL_SEC)
            final_status = await ha_client.get_mower_status()
            return {
                "result": "docked_and_cleared",
                "mower_status": final_status.to_dict(),
                "protocol_version": 1,
            }

    @server.tool()
    async def cancel_job() -> dict[str, Any]:
        """Standalone ``mammotion.cancel_job`` — clear task state without recall.

        Useful for cleanup after manual app-side ops, or to clear a lingering
        breakpoint without sending the mower home. Does NOT acquire the
        single-flight lock — safe to fire while another mow_area is in
        progress (it will interrupt that cycle's planning).

        Returns:
            Final mower status snapshot.
        """
        LOGGER.info("cancel_job called")
        await ha_client.call_service("mammotion", "cancel_job")
        await asyncio.sleep(_DELAY_AFTER_POST_DOCK_CANCEL_SEC)
        final_status = await ha_client.get_mower_status()
        return {
            "result": "job_cancelled",
            "mower_status": final_status.to_dict(),
            "protocol_version": 1,
        }
