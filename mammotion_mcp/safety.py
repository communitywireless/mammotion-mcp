"""Safety gates for mower control.

Implements:
- Quiet hours (refuse mow between configured HST start/end)
- Blade-height bounds (15 <= mm <= 100)
- Pre-flight battery check (>= min_battery_pct)
- Concurrent-call file lock (prevent overlapping mow_area)

The lock is per-host file-based (filelock package) — survives crashes
(stale lock files cleaned on next acquire) and prevents two MCP processes
from launching overlapping ``mow_area`` cycles. NOT cross-host; per
``rules/agent-portability.md`` the MCP runs as a per-agent service so
single-host scope is correct.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout

LOGGER = logging.getLogger("mammotion_mcp.safety")
HST = ZoneInfo("Pacific/Honolulu")

# How long to wait for the lock before declaring contention. Short — if
# another mow_area is in flight, the caller should retry or cancel_job.
LOCK_ACQUIRE_TIMEOUT_SEC = 5.0


class SafetyViolation(Exception):
    """Raised when a tool call is rejected by a safety gate."""


@dataclass
class SafetyGate:
    """Aggregates quiet-hours / blade-height / battery / concurrency gates.

    Constructed once at MCP server startup (see ``server.py``); shared by
    all tool registrations. Each safety check is independent and can be
    invoked directly. The lock is acquired via the async context manager
    (``async with safety: ...``) around the canonical sequence.
    """

    quiet_hours_start_hst: int  # 0-23
    quiet_hours_end_hst: int    # 0-23
    min_battery_pct: int
    lock_file_path: str
    _lock: Optional[FileLock] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Ensure parent dir exists so FileLock can create the lock file
        lock_path = Path(self.lock_file_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(lock_path))

    def check_quiet_hours(self, *, override: bool = False) -> None:
        """Raise SafetyViolation if currently in quiet hours and not overridden.

        Quiet hours wrap midnight: start_hour=21, end_hour=8 means
        21:00-23:59 + 00:00-07:59 are blocked.

        Args:
            override: If True, skip the check entirely. Use only when the
                      caller has explicit human approval to mow at night.
        """
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
        """Raise SafetyViolation if outside [15, 100] mm.

        The HA service schema bounds are 15-100 with step 5. We accept any
        integer in range; HA will reject non-step-5 values at service-call
        time with HTTP 400 (caught + surfaced via HAClient).
        """
        if not (15 <= blade_height_mm <= 100):
            raise SafetyViolation(
                f"blade_height_mm={blade_height_mm} out of bounds [15, 100]"
            )

    def check_battery(self, battery_pct: int | None) -> None:
        """Raise SafetyViolation if battery_pct is None or below min_battery_pct.

        ``None`` is treated as a hard fail (refuse start) rather than a
        soft warning — starting a mow without knowing battery state is
        worse than the friction of one extra status fetch.
        """
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
        """Acquire the per-host file lock; raise SafetyViolation on contention.

        Returns the gate so it can be used with ``async with`` syntax.
        """
        try:
            assert self._lock is not None  # populated in __post_init__
            self._lock.acquire(timeout=LOCK_ACQUIRE_TIMEOUT_SEC)
        except Timeout as exc:
            raise SafetyViolation(
                "Another mow_area call is in flight (lock held). "
                "Wait or call cancel_job to interrupt."
            ) from exc
        LOGGER.debug("SafetyGate lock acquired (%s)", self.lock_file_path)
        return self

    async def __aexit__(self, *exc_info) -> None:
        """Release the per-host file lock."""
        assert self._lock is not None
        if self._lock.is_locked:
            self._lock.release()
            LOGGER.debug("SafetyGate lock released (%s)", self.lock_file_path)
