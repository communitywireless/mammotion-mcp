"""Tier-3 (pause/resume) + Tier-4 (manual nudge, goto_coord) motion tools.

Tier-3 always available. Tier-4 only registered when
`ENABLE_DIAGNOSTIC_TOOLS=true` in env (gated by server.py).

v0 scaffold — Driver implements per Investigator-confirmed surface.
manual_drive and goto_coord depend on pymammotion exposing the primitives;
Investigator will confirm CONFIRMED-AVAILABLE / NEEDS-WRAPPER /
NOT-IN-PYMAMMOTION per behavior.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from mammotion_mcp.ha_client import HAClient
from mammotion_mcp.safety import SafetyGate

LOGGER = logging.getLogger("mammotion_mcp.tools.motion")


def register(server: FastMCP, *, ha_client: HAClient, safety: SafetyGate) -> None:
    """Register motion tools. Tier-4 manual_drive + goto_coord only registered
    when ENABLE_DIAGNOSTIC_TOOLS=true (caller checked)."""

    # --- Tier 3 — pause / resume (always-on if motion module loaded) ---

    @server.tool()
    async def pause_mow() -> dict[str, Any]:
        """Pause mowing in-place. Breakpoint preserved (resumable)."""
        # Driver: lawn_mower.pause
        raise NotImplementedError("Driver: implement")

    @server.tool()
    async def resume_mow() -> dict[str, Any]:
        """Resume mowing from previously-paused breakpoint."""
        # Driver: lawn_mower.start_mowing while paused
        raise NotImplementedError("Driver: implement")

    # --- Tier 4 — manual nudge (diag-gated) ---
    # Driver: confirm via Investigator report whether pymammotion exposes a
    # joystick / manual-drive primitive. If yes, implement; if not, document
    # in NOT-IN-PYMAMMOTION section + raise NotImplementedError with a useful
    # message.

    @server.tool()
    async def manual_drive(
        direction: Literal["forward", "backward", "left", "right"],
        duration_sec: float = 1.0,
        speed: float = 0.5,
    ) -> dict[str, Any]:
        """Joystick-equivalent nudge — for "when it gets stuck" recovery.

        Args:
            direction: forward / backward / left (rotate) / right (rotate)
            duration_sec: How long to apply the nudge. 0.5-5.0 typical.
            speed: Normalized speed 0.0-1.0. Default 0.5 (slow + safe).

        Safety: only available when ENABLE_DIAGNOSTIC_TOOLS=true.
        """
        # Driver: pymammotion device.drive(...) or equivalent if exposed
        raise NotImplementedError("Driver: implement after surface confirmed")

    # --- Tier 4 — goto arbitrary GPS coord (diag-gated) ---

    @server.tool()
    async def goto_coord(
        latitude: float,
        longitude: float,
    ) -> dict[str, Any]:
        """Send mower to an arbitrary GPS coordinate (no mowing).

        Args:
            latitude: Target latitude in decimal degrees.
            longitude: Target longitude in decimal degrees.

        Returns:
            Dict with status + estimated arrival.

        NOT-IN-PYMAMMOTION may be the verdict; if so, this tool returns
        an error and points the agent at `manual_drive` for nudge-based
        positioning.
        """
        # Driver: confirm pymammotion exposure; implement if YES, otherwise
        # raise with a clear "not exposed by library" message
        raise NotImplementedError("Driver: implement after surface confirmed")
