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

Only run this on initial setup -- i.e. when "Get running" just created
`.venv` rather than reusing an existing one -- or when the user explicitly
asks to confirm/verify the connection. Don't run it on every session against
an already-set-up repo; it launches a real SC2 game each time, which is
excessive overhead for a routine session start.

```bash
pytest -q tests/integration/test_connection_smoke.py
```

This launches one real local game, advances it for one in-game second, and
exits. It proves the client and primary map can actually connect without
making every user run the repository's full test matrix. Do not run the full
unit or integration suites as part of setup or before ordinary play; those
are development/CI checks and should only be run when the task calls for
them.

When it does run, report the outcome plainly:

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

## Writing `execute_code` snippets

One action per call, and never wait inside a snippet. This matters most
during a live realtime match, and it's easy to get wrong because the
anti-pattern reads so reasonably:

```python
# DON'T -- this stalls the entire game
for _ in range(30):
    if sdk.minerals >= 310:
        break
    await bot._advance(8)
loc = await sdk.get_next_expansion()
await bot.build(UnitTypeId.COMMANDCENTER, near=loc)
```

Three things compound to make that far more expensive than it looks:

- **A snippet holds the game's only queue.** `ExecuteCodeBotAI.on_step`
  services exactly one request at a time, and `start_task` turns drain from
  that *same* queue -- so for as long as a snippet runs, every background
  task gets zero turns and supply/worker/unit production all stop. A snippet
  that waits for minerals is starving the tasks that would spend them.
- **Waiting costs real wall-clock time in realtime mode.** `bot._advance(8)`
  waits for the server's free-running clock to advance 8 game loops
  (~0.36s), so a 30-iteration loop is ~11s. Each `bot.build()` adds its own
  verification budget on top -- up to ~15s in realtime, because a worker
  physically walks to the site (see `_REALTIME_BUILD_MAX_WAIT_STEPS`).
- **A timeout re-runs the whole snippet.** On timeout the server retries
  once, from the top (see `mcp_server.py`'s "Per-call timeout and single
  automatic retry"), so an over-long snippet stalls the game for roughly
  twice its timeout and can still end up having built nothing.

Instead:

- Put "keep doing X until Y" in `start_task`. That's what it's for: one
  bounded turn per `on_step`, interleaved fairly with everything else,
  rather than one snippet monopolising the queue.
- Keep `execute_code` to a single decision and its order, passing a small
  `max_wait_steps` (e.g. 4) when you only need the command dispatched and
  don't need the effect verified.
- Read state in one short call, decide, act in the next. Between two calls
  the realtime engine advances on its own -- that *is* your wait.

Measured cost of getting this wrong, once: two 75s timeouts back to back
(~150s of game time) with every background task frozen, straight through an
enemy attack that went uncontested as a result.

## Also read

- `CLAUDE.md` -- issue tracker, triage labels, and domain-doc conventions.
- `README.md` -- full API and play-mode reference.
