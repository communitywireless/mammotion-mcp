# mammotion-mcp — Project-local CLAUDE.md

You are an agent working on `mammotion-mcp`, an MCP server that exposes canonical Mammotion Luba2-AWD mower control to other Guard Well Farm agents.

## Identity

- Repo: `communitywireless/mammotion-mcp` (private)
- Initial deployment tier: mycroft-sandbox on Thor2 (per `~/.claude/rules/sandbox-first.md`)
- Customer of record: mower-recovery-pm (transient PM) → folds into mycroft-desktop on close per `~/.claude/rules/layered-memory-model.md`

## Why this exists

Other agents on the farm have `ha-mcp` and reach for `mammotion.start_mow` directly. That fires only `NavReqCoverPath` (the navigation planner protobuf) and NOT `DrvMowCtrlByHand` (the blade motor driver protobuf). Result: navigation happens, blades stay disengaged or hit Error 1202, post-dock state is dirty.

The verified canonical sequence requires 5 steps in order:
1. `mammotion.cancel_job` — clear stale breakpoint
2. `mammotion.start_stop_blades(start_stop=true, blade_height=55)` — fire DrvMowCtrlByHand
3. `mammotion.start_mow(areas=[switch], blade_height=55)` — fire NavReqCoverPath
4. `lawn_mower.dock` — recall + poll for charging=on
5. `mammotion.cancel_job` (post-dock) — clear task state for clean "ready for next" state

This MCP server is the forcing function: agents call `mow_area(name)` and the server fires the full sequence internally. Agents cannot compose it wrong because they don't have the primitives.

## Doctrine references

- `~/.claude/rules/sandbox-first.md` — Thor2 deployment first, observation window before promotion
- `~/.claude/rules/git-deployment-doctrine.md` — GitHub remote at inception, `main` branch, reproducible from `git clone + configure + run`
- `~/.claude/rules/branch-discipline-deploy-trees.md` — deploy tree on master, feature work on worktrees
- `~/.claude/rules/config-over-env.md` — config via `.env` not hard-coded
- `~/.claude/rules/hyperscale-ready-disciplines.md` — versioned protocol + `companion_id` partition key + stateless business logic
- `~/.claude/rules/no-forward-todos.md` — fix in scope or register a project; no banked debt

## First-action gate

1. Read this CLAUDE.md
2. Read README.md
3. Check `docs/2026-05-14-pymammotion-public-surface-investigator-report.md` (in `~/.claude_temp/reports/`) — this is the LOCKED tool surface for v1
4. If you're a Driver: read your dispatch spec at `~/projects/mower-recovery-pm/docs/2026-05-14-mammotion-mcp-driver-spec.md`

## NOT

- NOT Mycroft. NOT a Driver unless explicitly dispatched as one. NOT mower-recovery-pm.
- NOT authorized to deploy to production HA host without Joshua's greenlight.
- NOT authorized to bypass safety gates (quiet hours, blade-height bounds, battery preflight).
