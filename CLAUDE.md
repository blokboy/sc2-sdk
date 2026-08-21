## Getting set up

See `AGENTS.md` first -- it's the canonical get-running/confirm-connected
sequence for a fresh clone. Don't re-derive it from here or from README.md.

## Agent skills

### Issue tracker

Issues live in GitHub Issues (`blokboy/sc2-sdk`, via `gh`). External PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary — label strings match role names exactly (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Multi-context layout — `CONTEXT-MAP.md` at the root points to per-context `CONTEXT.md` files (e.g. under `src/sdk/`, `src/install/`, and root-level `bots/` as those areas get built). `bots/` was deliberately placed outside `src/` (ticket #7): it's where an agent adds its own standalone scripts, and keeping it out of `src/` means editing a bot script never invalidates the Dockerfile's expensive client-install layer, the same reasoning that already keeps `tests/` outside `src/`. See `docs/agents/domain.md`.
