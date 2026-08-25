# Agent setup

Read this file first, before anything else, when starting work in this repo.

## Get running

Run this yourself at the start of any session in this repo -- don't make
the user type it. Narrate what you're doing as you go (e.g. "no `.venv`
found, creating one and installing the SDK..." or "`.venv` already exists,
reusing it") so the user can follow along.

```bash
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
sc2-sdk-setup
```

The venv check makes this safe to re-run: an existing `.venv` is reused,
not recreated, and `pip install`/`sc2-sdk-setup` are both cheap no-ops when
already up to date.

`sc2-sdk-setup` finds an existing Battle.net-managed client, or (on Linux)
downloads and extracts Blizzard's official headless package, then syncs the
fixed map pool this project runs on. If it can't find or install a client,
it exits with an actionable message -- follow that message rather than
guessing or reaching for a different install method.

If it printed an `export SC2PATH=...` line (only happens with a non-default
`--dest`), run that too, in this same shell session.

## Confirm you're connection-ready

Run the dedicated smoke test yourself right after "Get running" -- don't
leave the user wondering whether setup actually worked.

```bash
pytest -q tests/integration/test_connection_smoke.py
```

This launches one real local game, advances it for one in-game second, and
exits. It proves the client and primary map can actually connect without
making every user run the repository's full test matrix. Do not run the full
unit or integration suites as part of setup or before ordinary play; those
are development/CI checks and should only be run when the task calls for
them.

Report the outcome plainly:

- the smoke test ran (not skipped) and passed -- tell the user they have a
  working, connected client and are ready to play.
- it skipped -- tell the user no client was found, and point them at
  whatever actionable message `sc2-sdk-setup` printed above.
- it failed outright -- surface the failure; don't guess at a fix or re-run
  setup speculatively.

## Then start playing

See README.md for the play modes (raw connect-and-play, the verified
`bot.*`/`sdk.*` action API, interactive MCP `execute_code`, autonomous bot
scripts) and pick whichever matches the task at hand. Don't re-derive
install/setup steps from there -- this file is the canonical first step.

One mode is not covered by "Get running" above and needs its own ask:
`sc2-sdk-host`/`sc2-sdk-join` (cross-machine 1v1, ticket #12) will, if no
Tailscale IP is already present, open a browser to Tailscale's download
page and wait for the user to install and log in (`install/tailscale.py`).
That's real networking software landing on the user's machine, for a
project that otherwise never touches the network -- get explicit
confirmation before running host/join, don't just launch it because the
user mentioned playing against another machine. Every other play mode
(raw, verified-action, MCP, autonomous bot, self-play) is local-only and
doesn't need this.

This does **not** cover two LLMs playing each other on the same machine
(`sc2-sdk-mcp --multiplayer` + the `host_game`/`join_game` MCP tools,
ticket #17) -- that path defaults `host_ip` to loopback and never touches
Tailscale, so it needs no special confirmation, same as any other local-
only mode. The confirmation gate above returns, scoped specifically to
whatever opts into Tailscale, once cross-machine multiplayer over MCP is
built.

## Also read

- `CLAUDE.md` -- issue tracker, triage labels, and domain-doc conventions.
- `README.md` -- full API and play-mode reference.
