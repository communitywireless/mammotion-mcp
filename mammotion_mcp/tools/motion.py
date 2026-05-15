"""Tier-3 pause/resume + Tier-4 manual nudge.

Tier-3 (pause_mow / resume_mow) is always-on when the motion module loads.
Tier-4 (manual_drive) is only registered when ``ENABLE_DIAGNOSTIC_TOOLS=true``
(gated in server.py).

Verified surface (2026-05-14 against Thor1 HA):
- ``lawn_mower.pause`` exists (no parameters)
- ``lawn_mower.start_mowing`` exists — resumes from breakpoint when paused
- ``mammotion.move_forward/backward/left/right`` — no parameters; single-shot
  nudge primitives. HA fires them as one-tick movements; the underlying
  pymammotion call uses speed 0.4 by default (per HA's button.py).

NOT-IN-HA-SURFACE (verified absent in Thor1 HA service inventory 2026-05-14):
- ``goto_coord`` / point-to-point GPS navigation — pymammotion does not expose
  this as a high-level command. The protobuf has telemetry-side lat/lon
  fields but no app→device target-coord saga. See Investigator report §2.2.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from mammotion_mcp.ha_client import HAClient
from mammotion_mcp.safety import SafetyGate

LOGGER = logging.getLogger("mammotion_mcp.tools.motion")


def register(server: FastMCP, *, ha_client: HAClient, safety: SafetyGate) -> None:
    """Register motion tools.

    Tier-4 manual_drive only registered when ENABLE_DIAGNOSTIC_TOOLS=true
    (caller-side gate; this function is only invoked if so).
    """

    # --- Tier 3 — pause / resume (always-on if motion module loaded) ---

    @server.tool()
    async def pause_mow() -> dict[str, Any]:
        """Pause mowing in-place. Breakpoint preserved (resumable).

        Maps to ``lawn_mower.pause``.

        Returns:
            Final mower status snapshot.
        """
        LOGGER.info("pause_mow called")
        await ha_client.call_service("lawn_mower", "pause")
        status = await ha_client.get_mower_status()
        return {
            "result": "paused",
            "mower_status": status.to_dict(),
            "protocol_version": 1,
        }

    @server.tool()
    async def resume_mow() -> dict[str, Any]:
        """Resume mowing from previously-paused breakpoint.

        Maps to ``lawn_mower.start_mowing`` (which resumes from breakpoint
        when the mower is in paused state).

        Returns:
            Final mower status snapshot.
        """
        LOGGER.info("resume_mow called")
        await ha_client.call_service("lawn_mower", "start_mowing")
        status = await ha_client.get_mower_status()
        return {
            "result": "resumed",
            "mower_status": status.to_dict(),
            "protocol_version": 1,
        }

    # --- Tier 4 — manual nudge (diag-gated, only registered when enabled) ---

    @server.tool()
    async def manual_drive(
        direction: Literal["forward", "backward", "left", "right"],
        duration_sec: float = 1.0,
        speed: float = 0.4,
    ) -> dict[str, Any]:
        """Joystick-equivalent nudge — for "when it gets stuck" recovery.

        Args:
            direction: forward / backward / left (rotate) / right (rotate)
            duration_sec: How long to apply the nudge. 0.5-5.0 typical.
                          Note: HA's mammotion.move_* services are single-shot
                          and do NOT accept duration — this parameter is
                          retained for future tuning but currently fires
                          one move command (whose internal duration is
                          determined by the device).
            speed: Normalized speed 0.0-1.0. Default 0.4 (slow + safe).
                   Note: HA's move_* services do NOT accept a speed parameter;
                   the device uses its configured default. This parameter is
                   accepted for future API compatibility but currently no-op.

        Returns:
            Dict with direction sent + final mower status.

        Safety: only available when ENABLE_DIAGNOSTIC_TOOLS=true.
        """
        # Bounds checks
        if not (0.0 <= speed <= 1.0):
            raise ValueError(f"speed={speed} out of bounds [0.0, 1.0]")
        if not (0.0 < duration_sec <= 10.0):
            raise ValueError(f"duration_sec={duration_sec} out of bounds (0, 10]")

        service_map = {
            "forward": "move_forward",
            "backward": "move_backward",
            "left": "move_left",
            "right": "move_right",
        }
        service = service_map[direction]
        LOGGER.info("manual_drive: direction=%s service=mammotion.%s", direction, service)
        await ha_client.call_service("mammotion", service)
        status = await ha_client.get_mower_status()
        return {
            "result": "nudged",
            "direction": direction,
            "duration_sec_requested": duration_sec,
            "speed_requested": speed,
            "mower_status": status.to_dict(),
            "protocol_version": 1,
        }
