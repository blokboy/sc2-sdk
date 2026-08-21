# Agent setup

Read this file first, before anything else, when starting work in this repo.

## Get running

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
sc2-sdk-setup
```

`sc2-sdk-setup` finds an existing Battle.net-managed client, or (on Linux)
downloads and extracts Blizzard's official headless package, then syncs the
fixed map pool this project runs on. If it can't find or install a client,
it exits with an actionable message -- follow that message rather than
guessing or reaching for a different install method.

If it printed an `export SC2PATH=...` line (only happens with a non-default
`--dest`), run that too, in this same shell session.

## Confirm you're connection-ready

```bash
pytest -m "not integration"   # fast unit/tooling tests -- should pass regardless of client install
pytest -m integration         # real-game tests -- skip cleanly with no client, run for real otherwise
```

If `pytest -m integration` runs instead of skipping, and passes, you have a
working, connected client and are ready to play.

## Then start playing

See README.md for the play modes (raw connect-and-play, the verified
`bot.*`/`sdk.*` action API, interactive MCP `execute_code`, autonomous bot
scripts) and pick whichever matches the task at hand. Don't re-derive
install/setup steps from there -- this file is the canonical first step.

## Also read

- `CLAUDE.md` -- issue tracker, triage labels, and domain-doc conventions.
- `README.md` -- full API and play-mode reference.
