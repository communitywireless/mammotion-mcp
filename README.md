# mammotion-mcp

MCP server exposing canonical Mammotion Luba2-AWD mower control to Guard Well Farm agents.

**Status:** v0 scaffold (2026-05-14) — pending Investigator-led pymammotion surface lock, then Driver-built v1 tools.
**Deployment tier (initial):** mycroft-sandbox on Thor2 (per `rules/sandbox-first.md`).
**Owner:** mower-recovery-pm (transient) → folds into mycroft-desktop on close.

## Purpose

Other agents (CM, sandbox, mycroft-desktop, future Violet) need a **forcing function** for the canonical mower control sequence. Today they reach for `ha-mcp.call_service` and compose `mammotion.start_mow` themselves — which is exactly the wrong path. They hit Error 1202, dock dirty, leave the mower in "task paused, not ready" state.

This MCP server is the structural fix: one tool, `mow_area(name)`, internally fires the verified 5-step canonical sequence (cancel → start_stop_blades → start_mow → poll → dock → post-dock cancel). Agents cannot compose it wrong; they don't have the primitives.

## v1 tool surface (planned — locked after Investigator returns)

### Tier 1 — Core mow (load-bearing)
- `mow_area(area_name, blade_height_mm=55, mow_duration_sec=None, return_to_dock=True)` — full canonical sequence
- `dock_and_clear()` — recall + post-dock cancel
- `cancel_job()` — standalone task-state clear

### Tier 2 — Status reads (cheap, high-value)
- `get_mower_status()` — state + charging + battery + activity_mode + last_error
- `list_areas()` — area_name → switch_entity → hash mapping
- `get_position()` — current lat/lon/bearing/speed (telemetry capture lever)

### Tier 3 — Pause / resume
- `pause_mow()` — leaves breakpoint
- `resume_mow()` — resumes from breakpoint

### Tier 4 (gated behind `ENABLE_DIAGNOSTIC_TOOLS=true`)
- `start_stop_blades(start_stop, blade_height_mm=55)` — low-level blade motor
- `reload_integration()` — polling-stall workaround
- `manual_drive(direction, duration_sec, speed)` — nudge primitives (if pymammotion exposes)
- `goto_coord(lat, lon)` — point-to-point (if pymammotion exposes)

### Built-in safety gates
- Quiet-hours: refuse mow between 21:00–08:00 HST unless `override_quiet_hours=True`
- Blade-height bounds: 15 ≤ blade_height_mm ≤ 100
- Pre-flight battery check: require `battery_pct ≥ 30` before start
- Concurrent-call file lock: prevent overlapping `mow_area` calls

## Architecture

```
agent (CM / sandbox / mycroft-desktop)
  │
  │ MCP stdio
  ▼
mammotion-mcp (Python, this repo)
  │
  │ HTTP REST
  ▼
Home Assistant @ thor1:8123 (mammotion.* + lawn_mower.* services)
  │
  │ pymammotion → MQTT
  ▼
Aliyun cloud
  │
  ▼
Luba2-AWD physical device
```

For tools that need behaviors NOT exposed by HA (manual nudge, goto_coord), this server may need to bypass HA and talk to pymammotion directly. That call gets made after the Investigator returns the actual pymammotion surface.

## Config (env vars per `rules/config-over-env.md`)

| Var | Default | Notes |
|---|---|---|
| `HA_URL` | `http://192.168.1.201:8123` | Thor1 prod HA |
| `HA_TOKEN` | (required) | from `gwf-creds` at boot OR `.env` |
| `MOWER_ENTITY_ID` | `lawn_mower.luba2_awd_1` | |
| `AREA_MAPPING_PATH` | `/app/data/area-mapping.json` | baked in image |
| `ENABLE_DIAGNOSTIC_TOOLS` | `false` | gate Tier 4 |
| `QUIET_HOURS_START_HST` | `21` | int 0-23 |
| `QUIET_HOURS_END_HST` | `8` | int 0-23 |
| `MIN_BATTERY_PCT` | `30` | int 0-100 |
| `LOG_LEVEL` | `INFO` | |

## Deployment (sandbox-first per `rules/sandbox-first.md`)

1. Build: `docker compose build` (Dockerfile in this repo)
2. Deploy to Thor2: `docker compose up -d` from `/opt/mammotion-mcp/`
3. Register in mycroft-sandbox `.mcp.json`
4. Smoke-test read-side tools (`get_mower_status`, `list_areas`, `get_position`) via mycroft-sandbox agent
5. Joshua eyes-on Tier-1 live `mow_area` test
6. After 24h clean: promote to mycroft-desktop `.mcp.json`, then CM/Mycroft-container

## Doctrine compliance

- `rules/sandbox-first.md` — install Thor2 first, observation window before promotion
- `rules/git-deployment-doctrine.md` — repo public-private at GitHub from inception; running production reproducible by `git clone + configure + run`
- `rules/branch-discipline-deploy-trees.md` — feature branches on worktrees, deploy tree on `main`
- `rules/config-over-env.md` — config via env vars in `.env`, not hard-coded
- `rules/hyperscale-ready-disciplines.md` — single-tenant v1, but `companion_id` parameter on every mutation in the API surface (defaults to `joshua-mont` v1)
- `rules/agent-portability.md` — service lives WITH the consumer agent (sandbox → desktop → CM); per-instance config

## Repository layout (v0 scaffold)

```
mammotion-mcp/
  ├── README.md           (this file)
  ├── pyproject.toml      (Python package metadata)
  ├── Dockerfile          (container build)
  ├── compose.yml         (deployment compose)
  ├── .env.example        (config template)
  ├── .gitignore
  ├── mammotion_mcp/
  │   ├── __init__.py
  │   ├── server.py       (MCP server entry point)
  │   ├── tools/
  │   │   ├── mow.py      (Tier 1 mow tools)
  │   │   ├── status.py   (Tier 2 status tools)
  │   │   ├── motion.py   (Tier 3 pause/resume + Tier 4 manual)
  │   │   └── diag.py     (Tier 4 diagnostics)
  │   ├── ha_client.py    (HA REST wrapper)
  │   ├── safety.py       (quiet-hours, bounds, battery, lock)
  │   └── area_resolver.py (area_name → switch_entity)
  ├── data/
  │   └── area-mapping.json (baked-in area mapping)
  └── tests/
      └── (tests TBD)
```
