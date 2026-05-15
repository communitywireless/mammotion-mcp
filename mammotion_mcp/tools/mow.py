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

v1.1 (2026-05-15) adds in-tool POST-DISPATCH verification — see
``_verify_mowing`` below — that proves the mower actually mowed (W-003 fix).
v1.0 returned success after HA-service-call ACK (plumbing); v1.1 returns
success only after blade engagement is confirmed via ``blade_used_time``
delta (faucet). See README "What 'success' means" for the W-003 context.

See ``~/projects/mower-recovery-pm/docs/2026-05-14-mower-usage-guide-for-agents.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
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

# Poll deadlines — v1.0 HA-ACK semantics
_POLL_MOWING_TIMEOUT_SEC = 60
_POLL_CHARGING_TIMEOUT_SEC = 480
_POLL_INTERVAL_SEC = 3.0

# -------------------------------------------------------------------------
# v1.1 verification thresholds (per Investigator report 2026-05-15 §5)
#
# These are EMPIRICAL thresholds, not theoretical limits. Do not adjust
# without re-running the Investigator's surface characterization.
# -------------------------------------------------------------------------

# Phase 1 — sustained-mowing-state confirmation
_PHASE1_POLL_INTERVAL_SEC = 5
_PHASE1_NUM_POLLS = 18  # 18 x 5s = 90s
_PHASE1_SUSTAINED_MOWING_SEC = 30  # 24s = failed cycle (preflight abort)

# Phase 2 — area-arrival confirmation via work_area sensor
_PHASE2_POLL_INTERVAL_SEC = 10
_PHASE2_NUM_POLLS = 60  # 60 x 10s = 600s (10 min)

# Phase 3 — blade engagement via blade_used_time delta
_PHASE3_POLL_INTERVAL_SEC = 30
_PHASE3_NUM_POLLS = 40  # 40 x 30s = 1200s (20 min)
_PHASE3_BLADE_DELTA_THRESHOLD_HR = 0.001  # ~3.6 seconds of blade time
_PHASE3_DOCK_MIN_ELAPSED_SEC = 120  # min elapsed before treating docked as terminal


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


async def _safe_state_string(ha_client: HAClient, entity_id: str) -> str | None:
    """Read entity state as a string, or None if unavailable.

    Internal helper for ``_verify_mowing``. Distinct from
    :meth:`HAClient.safe_float_state` because verification needs string
    state values (``"mowing"``, ``"docked"``, area-hash strings).

    Network/HTTP errors propagate; callers decide whether to treat them as
    environmental failures.
    """
    state_obj = await ha_client.get_state(entity_id)
    raw = state_obj.get("state")
    if raw is None or raw == "unknown" or raw == "unavailable":
        return None
    return str(raw)


async def _verify_mowing(
    ha_client: HAClient,
    target_area_hash: int,
    target_area_name: str,
) -> dict[str, Any]:
    """Three-phase verification per Investigator report 2026-05-15 §5.

    Runs AFTER ``mammotion.start_mow`` ACK and BEFORE returning success.
    Proves the mower physically mowed (vs merely accepted the command).

    +-------+-------------+----------------------------------+-------------------------------+
    | Phase | Window      | Success criterion                | Failure interpretation        |
    +=======+=============+==================================+===============================+
    | 1     | 0-90s       | state=mowing + activity=         | Preflight failure or dock     |
    |       |             | MODE_WORKING sustained >=30s     | return                        |
    +-------+-------------+----------------------------------+-------------------------------+
    | 2     | 90-600s     | work_area sensor contains        | Mower never reached target    |
    |       |             | "area <target_hash>"             | within 10 min                 |
    +-------+-------------+----------------------------------+-------------------------------+
    | 3     | 600-1800s   | blade_used_time delta >=         | Blades never physically       |
    |       |             | 0.001 hr (~3.6s blade time)      | engaged (SysReport lag        |
    |       |             |                                  | tolerated up to 20 min)       |
    +-------+-------------+----------------------------------+-------------------------------+

    Phase 3 is THE proof — ``blade_used_time`` is the only signal that
    conclusively proves blades physically spun. ``current_cutter_rpm``
    does NOT exist in HA's surface (Investigator §1).

    Edge cases handled:

    - **Stale-SysReport rollback** (Investigator §2.2): if
      ``current_value < baseline``, do NOT update baseline; continue
      polling. Recovers within ~3 min.
    - **Mid-mow obstacle pause**: in Phase 1, ``paused`` after sustained
      mowing started is ignored if ``progress > 0`` (obstacle avoidance);
      only aborts on ``progress == 0`` (preflight abort).
    - **Mower dock-return mid-Phase-3**: if state=docked and elapsed
      > 120s, perform a final blade-time check — partial-success on any
      positive delta, failure otherwise.

    Args:
        ha_client: HA REST client.
        target_area_hash: Integer area hash (from
            :func:`area_resolver.resolve_with_hash`).
        target_area_name: Joshua's app-side name (for error messages).

    Returns:
        Dict::

            {
                "verified": bool,
                "phase_reached": 1 | 2 | 3,
                "detail": str,
                "blade_delta_hr": float | None,
                "final_work_area": str | None,
                "final_state": str | None,
            }

    Raises:
        RuntimeError: if HA connectivity is lost for the entire verification
            window (distinguishes environmental failure from verification
            failure). Verification failures DO NOT raise — they return the
            structured dict with ``verified=False``.
    """
    base = ha_client.mower_entity_id.split(".", 1)[-1]
    state_eid = ha_client.mower_entity_id
    activity_eid = f"sensor.{base}_activity_mode"
    work_area_eid = f"sensor.{base}_work_area"
    blade_eid = f"sensor.{base}_blade_used_time"
    progress_eid = f"sensor.{base}_progress"

    target_area_string = f"area {target_area_hash}"
    LOGGER.info(
        "Verification starting for %r (hash=%s, looking for %r in work_area)",
        target_area_name, target_area_hash, target_area_string,
    )

    # ---- Phase 1 ----------------------------------------------------------
    # Confirm mower undocked and reached sustained state=mowing.
    # 24-second mowing then paused = failed cycle (preflight abort).
    # ----------------------------------------------------------------------
    mowing_start_time: float | None = None
    state_mowing_confirmed = False
    final_state: str | None = None

    for _ in range(_PHASE1_NUM_POLLS):
        await asyncio.sleep(_PHASE1_POLL_INTERVAL_SEC)
        try:
            mower_state = await _safe_state_string(ha_client, state_eid)
            activity = await _safe_state_string(ha_client, activity_eid)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Phase 1 HA read failed (continuing): %s", exc)
            continue

        final_state = mower_state

        if mower_state == "mowing" and activity == "MODE_WORKING":
            if mowing_start_time is None:
                mowing_start_time = time.monotonic()
                LOGGER.info("Phase 1: state=mowing observed; awaiting sustainment")
            elapsed = time.monotonic() - mowing_start_time
            if elapsed >= _PHASE1_SUSTAINED_MOWING_SEC:
                state_mowing_confirmed = True
                LOGGER.info("Phase 1 PASS: sustained mowing %.1fs", elapsed)
                break
        elif mower_state in ("paused", "docked") and mowing_start_time is not None:
            # Transitioned back before we hit sustained threshold.
            # If progress=0, this was a preflight abort. If progress>0,
            # it's obstacle avoidance — keep waiting.
            try:
                progress_raw = await _safe_state_string(ha_client, progress_eid)
                progress = int(float(progress_raw)) if progress_raw else 0
            except (TypeError, ValueError):
                progress = 0

            if progress == 0:
                duration = int(time.monotonic() - mowing_start_time)
                return {
                    "verified": False,
                    "phase_reached": 1,
                    "detail": (
                        f"Mower aborted after only {duration}s of mowing state with "
                        f"0% progress — preflight failure or dock return. "
                        f"activity_mode={activity}, mower_state={mower_state}"
                    ),
                    "blade_delta_hr": None,
                    "final_work_area": None,
                    "final_state": mower_state,
                }
            # Obstacle avoidance — continue polling, do NOT reset start time

    if not state_mowing_confirmed:
        return {
            "verified": False,
            "phase_reached": 1,
            "detail": (
                f"Never reached sustained mowing state within "
                f"{_PHASE1_NUM_POLLS * _PHASE1_POLL_INTERVAL_SEC}s. "
                f"Current state: {final_state!r}"
            ),
            "blade_delta_hr": None,
            "final_work_area": None,
            "final_state": final_state,
        }

    # ---- Phase 2 ----------------------------------------------------------
    # Confirm mower physically arrived in target area (work_area contains
    # "area <hash>"). Mower needs time to navigate dock -> target area;
    # successful cycles: ~7-8 min observed. Generous 10-min timeout.
    # ----------------------------------------------------------------------
    area_arrived = False
    final_work_area: str | None = None

    for _ in range(_PHASE2_NUM_POLLS):
        await asyncio.sleep(_PHASE2_POLL_INTERVAL_SEC)
        try:
            work_area = await _safe_state_string(ha_client, work_area_eid)
            mower_state = await _safe_state_string(ha_client, state_eid)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Phase 2 HA read failed (continuing): %s", exc)
            continue

        final_work_area = work_area
        final_state = mower_state

        if work_area and target_area_string in work_area:
            area_arrived = True
            LOGGER.info("Phase 2 PASS: work_area=%r matches target", work_area)
            break

        # Abort conditions — mower returned without reaching area
        if mower_state == "docked":
            try:
                progress_raw = await _safe_state_string(ha_client, progress_eid)
                progress = int(float(progress_raw)) if progress_raw else 0
            except (TypeError, ValueError):
                progress = 0
            return {
                "verified": False,
                "phase_reached": 2,
                "detail": (
                    f"Mower returned to dock before reaching {target_area_name}. "
                    f"work_area at abort: {work_area!r}, progress={progress}%"
                ),
                "blade_delta_hr": None,
                "final_work_area": work_area,
                "final_state": mower_state,
            }

    if not area_arrived:
        return {
            "verified": False,
            "phase_reached": 2,
            "detail": (
                f"Mower never reached {target_area_name} "
                f"(hash={target_area_hash}) within "
                f"{_PHASE2_NUM_POLLS * _PHASE2_POLL_INTERVAL_SEC}s of navigation. "
                f"work_area={final_work_area!r}, state={final_state!r}"
            ),
            "blade_delta_hr": None,
            "final_work_area": final_work_area,
            "final_state": final_state,
        }

    # ---- Phase 3 ----------------------------------------------------------
    # Confirm blades physically engaged via blade_used_time delta.
    # blade_used_time is async SysReport — updates every 5-17 min observed.
    # Tolerate stale-SysReport rollback anomaly (current < baseline).
    # ----------------------------------------------------------------------
    try:
        baseline_blade_hr = await ha_client.safe_float_state(blade_eid)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"HA connectivity lost capturing blade_used_time baseline: {exc}"
        ) from exc

    if baseline_blade_hr is None:
        baseline_blade_hr = 0.0
        LOGGER.warning("Phase 3 baseline unavailable; defaulting to 0.0")

    baseline_time = time.monotonic()
    LOGGER.info(
        "Phase 3 baseline: blade_used_time=%.6f hr @ T+0", baseline_blade_hr
    )

    last_current_blade_hr: float | None = baseline_blade_hr

    for _ in range(_PHASE3_NUM_POLLS):
        await asyncio.sleep(_PHASE3_POLL_INTERVAL_SEC)
        try:
            current_blade_hr = await ha_client.safe_float_state(blade_eid)
            mower_state = await _safe_state_string(ha_client, state_eid)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Phase 3 HA read failed (continuing): %s", exc)
            continue

        if current_blade_hr is None:
            continue
        last_current_blade_hr = current_blade_hr
        final_state = mower_state

        blade_delta = current_blade_hr - baseline_blade_hr
        elapsed = time.monotonic() - baseline_time

        # Stale-SysReport rollback guard (Investigator §2.2): current < baseline
        # means a cached/stale report arrived. Do NOT update baseline; keep polling.
        if blade_delta < 0:
            LOGGER.warning(
                "Phase 3 stale SysReport rollback detected "
                "(current=%.6f < baseline=%.6f); continuing",
                current_blade_hr, baseline_blade_hr,
            )
            continue

        if blade_delta >= _PHASE3_BLADE_DELTA_THRESHOLD_HR:
            detail = (
                f"Mowing verified. Blade time increased by {blade_delta:.4f} hr "
                f"({blade_delta * 3600:.0f}s) after {elapsed:.0f}s in {target_area_name}."
            )
            LOGGER.info("Phase 3 PASS: %s", detail)
            return {
                "verified": True,
                "phase_reached": 3,
                "detail": detail,
                "blade_delta_hr": blade_delta,
                "final_work_area": final_work_area,
                "final_state": mower_state,
            }

        # Mower dock-return mid-Phase-3: final blade-time check
        if mower_state == "docked" and elapsed > _PHASE3_DOCK_MIN_ELAPSED_SEC:
            try:
                final_blade_hr = await ha_client.safe_float_state(blade_eid)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"HA connectivity lost on Phase 3 dock-return final check: {exc}"
                ) from exc
            if final_blade_hr is None:
                final_blade_hr = current_blade_hr
            final_delta = final_blade_hr - baseline_blade_hr
            if final_delta >= _PHASE3_BLADE_DELTA_THRESHOLD_HR:
                detail = (
                    f"Mowing completed. Blade time increased by {final_delta:.4f} hr "
                    f"after job (mower returned to dock)."
                )
                LOGGER.info("Phase 3 PASS (dock-return): %s", detail)
                return {
                    "verified": True,
                    "phase_reached": 3,
                    "detail": detail,
                    "blade_delta_hr": final_delta,
                    "final_work_area": final_work_area,
                    "final_state": mower_state,
                }
            return {
                "verified": False,
                "phase_reached": 3,
                "detail": (
                    f"Mower returned to dock but blade_used_time unchanged "
                    f"(baseline={baseline_blade_hr:.6f}, final={final_blade_hr:.6f}). "
                    f"Blades may not have engaged in {target_area_name}."
                ),
                "blade_delta_hr": final_delta,
                "final_work_area": final_work_area,
                "final_state": mower_state,
            }

    # Phase 3 timeout
    final_current = (
        last_current_blade_hr if last_current_blade_hr is not None else baseline_blade_hr
    )
    return {
        "verified": False,
        "phase_reached": 3,
        "detail": (
            f"Phase 3 timeout: blade_used_time unchanged after "
            f"{_PHASE3_NUM_POLLS * _PHASE3_POLL_INTERVAL_SEC // 60} min in "
            f"{target_area_name}. baseline={baseline_blade_hr:.6f} hr, "
            f"final={final_current:.6f} hr. SysReport may not have arrived; "
            f"consider manual inspection."
        ),
        "blade_delta_hr": final_current - baseline_blade_hr,
        "final_work_area": final_work_area,
        "final_state": final_state,
    }


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
        verify: bool = True,
    ) -> dict[str, Any]:
        """Mow the named area using the verified 5-step canonical sequence.

        "Success" semantics (v1.1):
          When ``verify=True`` (default), this tool returns success ONLY when
          the mower has been observed PHYSICALLY MOWING via ``blade_used_time``
          delta. Verification phases (per Investigator 2026-05-15):
            Phase 1 (0-90s): ``state=mowing`` sustained >=30s
            Phase 2 (90-600s): mower entered target area (``work_area`` sensor)
            Phase 3 (600-1800s): ``blade_used_time`` incremented >= 0.001 hr

          **Long-running:** ``verify=True`` can take up to ~30 min for Phase 3
          timeout on a failed cycle. Callers should set MCP timeouts >= 1800s.

          When ``verify=False`` (opt-out), the tool returns immediately after
          step 3 with HA-ACK semantics (v1.0-compatible behavior). Use ONLY
          when the caller will do their own verification.

        Composition with ``mow_duration_sec`` (v1.1):
          When ``verify=True`` AND ``mow_duration_sec`` is set, verification
          runs first; if Phase 3 succeeds BEFORE mow_duration_sec elapses,
          the tool continues mowing for the rest of the duration. If verification
          fails (any phase), the tool still attempts dock recovery (when
          ``return_to_dock=True``) so the mower doesn't end up in a partial
          state — but returns ``mow_failed_verification`` regardless.

        Args:
            area_name: App-side name (e.g. "Area 6"). Resolved via
                       area-mapping.json. See ``list_areas`` for available names.
            blade_height_mm: Cutting height in mm. Must be 15-100, step 5.
                             Default 55 (Joshua's operational value; do NOT
                             use 25 — triggers Error 1202).
            mow_duration_sec: If set, mow for this many seconds then auto-dock.
                              If None and return_to_dock=True, return the
                              snapshot after verification (or after state=mowing
                              if verify=False); if None and return_to_dock=False,
                              return immediately after verification.
            return_to_dock: If True, recall to dock after mow_duration_sec
                            elapses AND fire post-dock cancel_job. Also fires
                            dock recovery on verification failure.
            override_quiet_hours: Bypass quiet-hours gate.
            verify: If True (default), run 3-phase post-dispatch verification.
                    If False, return immediately after start_mow ACK (v1.0
                    semantics — for callers who handle verification themselves).

        Returns:
            Dict with:
              result: One of:
                ``"mow_complete"`` (verify=True succeeded)
                ``"mow_dispatched_unverified"`` (verify=False)
                ``"mow_failed_verification"`` (verify=True, phase 1/2/3 fail)
                ``"mowing_started"`` (verify=False AND return_to_dock=False)
              area_resolved: switch entity for the named area
              blade_height_mm: blade height used
              verification: dict from _verify_mowing (only when verify=True)
              mower_status: final status snapshot
              protocol_version: 2 (v1.1 bumped 1 -> 2)

        Raises:
            SafetyViolation: quiet hours / blade height / battery preflight
            ValueError: unknown area
            RuntimeError: actual HA-API failures (network down, service
                          unreachable). Note: physical-mowing-failure does
                          NOT raise — it returns ``mow_failed_verification``
                          with a structured ``verification`` dict so callers
                          can react programmatically.
        """
        LOGGER.info(
            "mow_area called: area=%r blade_height=%d duration=%s return_to_dock=%s "
            "override=%s verify=%s",
            area_name, blade_height_mm, mow_duration_sec, return_to_dock,
            override_quiet_hours, verify,
        )

        # Safety gates (raise SafetyViolation if violated)
        safety.check_quiet_hours(override=override_quiet_hours)
        safety.check_blade_height(blade_height_mm)

        # Resolve area name → (switch entity, hash). The hash drives Phase 2.
        # (raises ValueError on unknown)
        switch_entity, area_hash = area_resolver.resolve_with_hash(
            area_name, mapping_path
        )
        LOGGER.info(
            "Area %r resolved → entity=%s hash=%d",
            area_name, switch_entity, area_hash,
        )

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

            # v1.0 fast path: opt-out of verification
            if not verify:
                # Poll for state=mowing (v1.0 semantics)
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

                if not return_to_dock:
                    final_status = await ha_client.get_mower_status()
                    return {
                        "result": "mowing_started",
                        "area_resolved": switch_entity,
                        "blade_height_mm": blade_height_mm,
                        "mower_status": final_status.to_dict(),
                        "protocol_version": 2,
                    }

                # Dock + post-dock cleanup for verify=False with return_to_dock=True
                LOGGER.info("Step 4/5: lawn_mower.dock")
                await ha_client.call_service("lawn_mower", "dock")
                charging_reached = await _poll_charging(
                    ha_client, timeout_s=_POLL_CHARGING_TIMEOUT_SEC
                )
                if not charging_reached:
                    raise RuntimeError(
                        f"Mower did not reach charging=on within "
                        f"{_POLL_CHARGING_TIMEOUT_SEC}s after dock"
                    )
                LOGGER.info("Step 5/5: mammotion.cancel_job (post-dock cleanup)")
                await ha_client.call_service("mammotion", "cancel_job")
                await asyncio.sleep(_DELAY_AFTER_POST_DOCK_CANCEL_SEC)
                final_status = await ha_client.get_mower_status()
                return {
                    "result": "mow_dispatched_unverified",
                    "area_resolved": switch_entity,
                    "blade_height_mm": blade_height_mm,
                    "mower_status": final_status.to_dict(),
                    "protocol_version": 2,
                }

            # ---- v1.1 verification path -----------------------------------
            verification = await _verify_mowing(
                ha_client,
                target_area_hash=area_hash,
                target_area_name=area_name,
            )

            if not verification["verified"]:
                LOGGER.warning(
                    "Verification FAILED at phase %d: %s",
                    verification["phase_reached"], verification["detail"],
                )
                # Attempt dock recovery so mower doesn't sit in a partial state
                if return_to_dock:
                    LOGGER.info(
                        "Verification failed; attempting dock recovery"
                    )
                    try:
                        await ha_client.call_service("lawn_mower", "dock")
                        charging_reached = await _poll_charging(
                            ha_client, timeout_s=_POLL_CHARGING_TIMEOUT_SEC
                        )
                        if charging_reached:
                            await ha_client.call_service("mammotion", "cancel_job")
                            await asyncio.sleep(_DELAY_AFTER_POST_DOCK_CANCEL_SEC)
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.error(
                            "Dock recovery on verification failure errored: %s", exc
                        )

                final_status = await ha_client.get_mower_status()
                return {
                    "result": "mow_failed_verification",
                    "area_resolved": switch_entity,
                    "blade_height_mm": blade_height_mm,
                    "verification": verification,
                    "mower_status": final_status.to_dict(),
                    "protocol_version": 2,
                }

            # Verification succeeded — Phase 3 PASS.
            # If mow_duration_sec was provided, continue mowing for the
            # remainder of the requested duration before docking.
            if mow_duration_sec:
                LOGGER.info(
                    "Verification passed; continuing mow for remainder of "
                    "mow_duration_sec=%d", mow_duration_sec,
                )
                await asyncio.sleep(mow_duration_sec)

            if not return_to_dock:
                final_status = await ha_client.get_mower_status()
                return {
                    "result": "mow_complete",
                    "area_resolved": switch_entity,
                    "blade_height_mm": blade_height_mm,
                    "verification": verification,
                    "mower_status": final_status.to_dict(),
                    "protocol_version": 2,
                }

            # Step 4: lawn_mower.dock
            LOGGER.info("Step 4/5: lawn_mower.dock")
            await ha_client.call_service("lawn_mower", "dock")

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
                "verification": verification,
                "mower_status": final_status.to_dict(),
                "protocol_version": 2,
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
