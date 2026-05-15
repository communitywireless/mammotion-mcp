"""Tier-4 diagnostic tools — gated behind ENABLE_DIAGNOSTIC_TOOLS=true.

Verified surface (2026-05-14 against Thor1 HA):

PRESENT as HA mammotion.* services:
- ``mammotion.start_stop_blades(start_stop, blade_height)`` — low-level blade
- ``mammotion.cancel_job`` — already exposed via Tier 1
- ``mammotion.reset_blade_time`` — maintenance counter reset
- ``mammotion.set_blade_warning_time`` — alert threshold
- ``mammotion.set_non_work_hours(start_time, end_time)`` — DND window

NOT present as HA services (excluded from v1.0):
- ``mammotion.set_blade_height`` — controlled via ``number.luba2_awd_1_blade_height``
  entity (out of scope for v1)
- ``mammotion.set_cutter_mode`` — controlled via ``select.luba2_awd_1_cutter_speed``
- ``mammotion.set_speed`` — controlled via ``number.luba2_awd_1_working_speed``
- ``mammotion.set_headlight`` / ``set_sidelight`` — controlled via
  ``switch.luba2_awd_1_manual_light_on_off`` / ``_side_led_on_off``
- ``mammotion.remote_restart`` — controlled via ``button.luba2_awd_1_restart_mower``
- ``mammotion.leave_dock`` — controlled via ``button.luba2_awd_1_undock``

The button/select/number entities ARE callable via the standard HA service
calls (``button.press``, ``select.select_option``, etc.), but are deferred
to v1.1 — the Driver spec explicitly says "if the mammotion.* service
doesn't exist, exclude that tool from v1.0 and document."

PRESENT but mostly UNAVAILABLE:
- ``button.luba2_awd_1_emergency_nudge_{forward,backward,left,right}`` — these
  exist but report state=unavailable on docked mower; the Tier-4
  ``manual_drive`` tool uses the mammotion.move_* services instead which are
  always available.

The config_entry id for ``reload_integration`` is discovered at startup
(via ``GET /api/config/config_entries``) and cached on first use.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from mammotion_mcp.ha_client import HAClient

LOGGER = logging.getLogger("mammotion_mcp.tools.diag")

# Default config entry id — discovered against Thor1 HA 2026-05-14.
# Overridable via env in case the integration is re-added with a new id.
_DEFAULT_MAMMOTION_ENTRY_ID = "01KKDQWAR44ZH9JEMZ3R7GJP6T"


async def _discover_mammotion_entry_id(ha_client: HAClient) -> str | None:
    """Discover the mammotion integration's config_entry_id via HA REST.

    Returns None if discovery fails — caller falls back to the env default.
    """
    try:
        resp = await ha_client._request_with_retry(  # noqa: SLF001 — intentional
            "GET", "/api/config/config_entries/entry"
        )
        entries = resp.json()
        for entry in entries:
            if entry.get("domain") == "mammotion":
                eid = entry.get("entry_id")
                LOGGER.info("Discovered mammotion config_entry_id=%s", eid)
                return eid
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to discover mammotion config entry: %s", exc)
    return None


def register(server: FastMCP, *, ha_client: HAClient) -> None:
    """Register Tier-4 diagnostic tools."""

    # Lazy entry-id cache populated on first reload call
    _entry_id_cache: dict[str, str | None] = {"id": None}
    env_default_entry_id = os.environ.get(
        "MAMMOTION_ENTRY_ID", _DEFAULT_MAMMOTION_ENTRY_ID
    )

    @server.tool()
    async def start_stop_blades(
        start_stop: bool,
        blade_height_mm: int = 55,
    ) -> dict[str, Any]:
        """Low-level blade motor toggle (``DrvMowCtrlByHand``).

        Args:
            start_stop: True engages blades, False stops them.
            blade_height_mm: Blade height in mm (15-100, step 5). Default 55
                             (Joshua's operational value).

        Useful for: clearing Error 1202 via stop→start cycle; testing blade
        motor independent of nav planner; height-only adjustment via the
        DrvKnife protobuf field.

        Returns:
            Dict with result + final mower status.
        """
        if not (15 <= blade_height_mm <= 100):
            raise ValueError(f"blade_height_mm={blade_height_mm} out of [15, 100]")
        LOGGER.info(
            "start_stop_blades(start_stop=%s, blade_height=%d)",
            start_stop, blade_height_mm,
        )
        await ha_client.call_service(
            "mammotion",
            "start_stop_blades",
            start_stop=start_stop,
            blade_height=blade_height_mm,
        )
        status = await ha_client.get_mower_status()
        return {
            "result": "blades_toggled",
            "start_stop": start_stop,
            "blade_height_mm": blade_height_mm,
            "mower_status": status.to_dict(),
            "protocol_version": 1,
        }

    @server.tool()
    async def reload_integration() -> dict[str, Any]:
        """Reload the Mammotion HA integration.

        Workaround for the polling-stall issue (state=unavailable for 5-15+
        min after dock). Discovers the config_entry_id at first use and
        caches it; falls back to the env-default if discovery fails.

        Returns:
            Dict with reload result + the entry_id used.
        """
        # Resolve entry_id (cached after first discovery)
        if _entry_id_cache["id"] is None:
            discovered = await _discover_mammotion_entry_id(ha_client)
            _entry_id_cache["id"] = discovered or env_default_entry_id
        entry_id = _entry_id_cache["id"]
        LOGGER.info("reload_integration using entry_id=%s", entry_id)
        await ha_client.reload_config_entry(entry_id)
        return {
            "result": "integration_reloaded",
            "entry_id": entry_id,
            "protocol_version": 1,
        }

    @server.tool()
    async def get_error_code() -> dict[str, Any]:
        """Read ``sensor.luba2_awd_1_last_error_code`` + last_error_time.

        Read-only — no service call. Useful for diagnosing why a mow
        sequence failed without firing a full status snapshot.

        Returns:
            Dict with last_error_code (int) + last_error_time (ISO string).
        """
        base = ha_client.mower_entity_id.split(".", 1)[-1]
        try:
            code_state = await ha_client.get_state(
                f"sensor.{base}_last_error_code"
            )
            time_state = await ha_client.get_state(
                f"sensor.{base}_last_error_time"
            )
            return {
                "last_error_code": code_state.get("state"),
                "last_error_time": time_state.get("state"),
                "protocol_version": 1,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "last_error_code": None,
                "last_error_time": None,
                "error": str(exc),
                "protocol_version": 1,
            }

    @server.tool()
    async def reset_blade_time() -> dict[str, Any]:
        """Reset the blade usage-time counter to zero.

        Maps to ``mammotion.reset_blade_time``. Use after physical blade
        replacement.
        """
        LOGGER.info("reset_blade_time called")
        await ha_client.call_service("mammotion", "reset_blade_time")
        return {"result": "blade_time_reset", "protocol_version": 1}

    @server.tool()
    async def request_telemetry_snapshot() -> dict[str, Any]:
        """Force a telemetry refresh via integration reload.

        v1.0 implementation: triggers a reload of the mammotion integration
        which forces pymammotion to repoll the device. Same call as
        ``reload_integration`` but framed for the "I think state is stale"
        use case.

        Returns:
            Dict with reload result.
        """
        LOGGER.info("request_telemetry_snapshot (via integration reload)")
        if _entry_id_cache["id"] is None:
            discovered = await _discover_mammotion_entry_id(ha_client)
            _entry_id_cache["id"] = discovered or env_default_entry_id
        entry_id = _entry_id_cache["id"]
        await ha_client.reload_config_entry(entry_id)
        return {
            "result": "snapshot_requested_via_reload",
            "entry_id": entry_id,
            "protocol_version": 1,
        }
