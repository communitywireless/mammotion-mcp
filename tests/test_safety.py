"""Tests for safety gates: quiet hours, blade height, battery, file lock."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from mammotion_mcp.safety import HST, SafetyGate, SafetyViolation


@pytest.fixture
def gate(tmp_path) -> SafetyGate:
    """Standard gate with farm defaults + per-test lock file."""
    return SafetyGate(
        quiet_hours_start_hst=21,
        quiet_hours_end_hst=8,
        min_battery_pct=30,
        lock_file_path=str(tmp_path / "test.lock"),
    )


# --- Quiet hours ----------------------------------------------------------


def test_quiet_hours_block_at_22_hst(gate: SafetyGate) -> None:
    """22:00 HST is inside the 21-08 quiet window."""
    with mock.patch("mammotion_mcp.safety.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 14, 22, 0, tzinfo=HST)
        with pytest.raises(SafetyViolation, match="Quiet hours"):
            gate.check_quiet_hours()


def test_quiet_hours_block_at_03_hst(gate: SafetyGate) -> None:
    """03:00 HST is inside the 21-08 wrap-midnight window."""
    with mock.patch("mammotion_mcp.safety.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 14, 3, 0, tzinfo=HST)
        with pytest.raises(SafetyViolation, match="Quiet hours"):
            gate.check_quiet_hours()


def test_quiet_hours_pass_at_10_hst(gate: SafetyGate) -> None:
    """10:00 HST is outside the 21-08 window."""
    with mock.patch("mammotion_mcp.safety.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 14, 10, 0, tzinfo=HST)
        gate.check_quiet_hours()  # no raise


def test_quiet_hours_pass_at_20_hst(gate: SafetyGate) -> None:
    """20:00 HST is just before quiet hours."""
    with mock.patch("mammotion_mcp.safety.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 14, 20, 30, tzinfo=HST)
        gate.check_quiet_hours()  # no raise


def test_quiet_hours_override_bypasses(gate: SafetyGate) -> None:
    """override=True skips the check entirely."""
    with mock.patch("mammotion_mcp.safety.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 14, 22, 0, tzinfo=HST)
        gate.check_quiet_hours(override=True)  # no raise


def test_quiet_hours_non_wrap_window(tmp_path) -> None:
    """A non-wrap window (e.g. 09-17 'business hours') correctly evaluates."""
    g = SafetyGate(
        quiet_hours_start_hst=9,
        quiet_hours_end_hst=17,
        min_battery_pct=30,
        lock_file_path=str(tmp_path / "t.lock"),
    )
    with mock.patch("mammotion_mcp.safety.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 14, 12, 0, tzinfo=HST)
        with pytest.raises(SafetyViolation):
            g.check_quiet_hours()
    with mock.patch("mammotion_mcp.safety.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 14, 18, 0, tzinfo=HST)
        g.check_quiet_hours()


# --- Blade height ---------------------------------------------------------


def test_blade_height_in_bounds_passes(gate: SafetyGate) -> None:
    gate.check_blade_height(55)
    gate.check_blade_height(15)
    gate.check_blade_height(100)


def test_blade_height_too_low_raises(gate: SafetyGate) -> None:
    with pytest.raises(SafetyViolation, match=r"out of bounds"):
        gate.check_blade_height(10)


def test_blade_height_too_high_raises(gate: SafetyGate) -> None:
    with pytest.raises(SafetyViolation, match=r"out of bounds"):
        gate.check_blade_height(150)


# --- Battery --------------------------------------------------------------


def test_battery_below_min_raises(gate: SafetyGate) -> None:
    with pytest.raises(SafetyViolation, match=r"battery_pct=20"):
        gate.check_battery(20)


def test_battery_at_min_passes(gate: SafetyGate) -> None:
    gate.check_battery(30)


def test_battery_above_min_passes(gate: SafetyGate) -> None:
    gate.check_battery(80)


def test_battery_none_raises(gate: SafetyGate) -> None:
    with pytest.raises(SafetyViolation, match=r"unavailable"):
        gate.check_battery(None)


# --- File lock ------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_acquire_release(gate: SafetyGate) -> None:
    """Single async-with acquires + releases the lock."""
    async with gate:
        assert gate._lock is not None  # noqa: SLF001
        assert gate._lock.is_locked
    assert not gate._lock.is_locked  # noqa: SLF001


@pytest.mark.asyncio
async def test_lock_contention_raises(tmp_path) -> None:
    """Two gates pointing at the same lock file → second times out fast."""
    lock_path = str(tmp_path / "shared.lock")
    g1 = SafetyGate(
        quiet_hours_start_hst=21, quiet_hours_end_hst=8,
        min_battery_pct=30, lock_file_path=lock_path,
    )
    g2 = SafetyGate(
        quiet_hours_start_hst=21, quiet_hours_end_hst=8,
        min_battery_pct=30, lock_file_path=lock_path,
    )
    async with g1:
        # While g1 holds the lock, g2 should fail to acquire
        with pytest.raises(SafetyViolation, match="in flight"):
            async with g2:
                pass  # pragma: no cover — should not reach here
