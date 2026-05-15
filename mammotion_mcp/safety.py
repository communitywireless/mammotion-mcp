"""Safety gates for mower control.

Implements:
- Quiet hours (refuse mow between configured HST start/end)
- Blade-height bounds (15 <= mm <= 100)
- Pre-flight battery check (>= min_battery_pct)
- Concurrent-call file lock (prevent overlapping mow_area)

Driver fleshes out each method body.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("mammotion_mcp.safety")
HST = ZoneInfo("Pacific/Honolulu")


class SafetyViolation(Exception):
    """Raised when a tool call is rejected by a safety gate."""


@dataclass
class SafetyGate:
    quiet_hours_start_hst: int  # 0-23
    quiet_hours_end_hst: int    # 0-23
    min_battery_pct: int
    lock_file_path: str

    def check_quiet_hours(self, *, override: bool = False) -> None:
        """Raises SafetyViolation if currently in quiet hours and not overridden."""
        if override:
            return
        now_hst = datetime.now(HST)
        hour = now_hst.hour
        start, end = self.quiet_hours_start_hst, self.quiet_hours_end_hst
        if start < end:
            in_quiet = start <= hour < end
        else:
            in_quiet = hour >= start or hour < end
        if in_quiet:
            raise SafetyViolation(
                f"Quiet hours active ({start:02d}:00-{end:02d}:00 HST). "
                f"Current HST hour: {hour}. Pass override_quiet_hours=True to bypass."
            )

    def check_blade_height(self, blade_height_mm: int) -> None:
        """Raises SafetyViolation if outside [15, 100]."""
        if not (15 <= blade_height_mm <= 100):
            raise SafetyViolation(
                f"blade_height_mm={blade_height_mm} out of bounds [15, 100]"
            )

    def check_battery(self, battery_pct: int | None) -> None:
        """Raises SafetyViolation if battery_pct < min_battery_pct."""
        if battery_pct is None:
            raise SafetyViolation(
                "Pre-flight battery check failed: battery_pct unavailable. "
                "Refuse start to avoid mid-mow shutoff."
            )
        if battery_pct < self.min_battery_pct:
            raise SafetyViolation(
                f"battery_pct={battery_pct}% < min_battery_pct={self.min_battery_pct}%. "
                f"Dock + charge before mow."
            )

    async def __aenter__(self) -> "SafetyGate":
        """Acquire concurrent-call file lock.

        Driver: implement with portalocker or filelock package.
        Holding the lock for the duration of a mow_area call prevents two
        agents from firing overlapping cycles.
        """
        # Driver implements
        return self

    async def __aexit__(self, *exc_info) -> None:
        """Release concurrent-call file lock."""
        # Driver implements
        return
