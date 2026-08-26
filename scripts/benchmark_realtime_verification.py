#!/usr/bin/env python3
"""Measure real, observed wall-clock latency (command dispatch ->
`effect_confirmed=True`) for each verified `bot.*` action type, in realtime
mode, against a real local match -- ticket #20
(https://github.com/blokboy/sc2-sdk/issues/20).

Why this exists
-----------------
`Bot._advance` (`src/sdk/bot.py`) branches on `self._ai.realtime`, and the
verification-poll ceilings it uses today (`_POLL_FRAMES`,
`_DEFAULT_MAX_WAIT_STEPS`, `_BUILD_DEFAULT_MAX_WAIT_STEPS`) were sized for
non-realtime (stepped) games, where polling more frames costs nothing in
wall-clock time. In realtime mode those same ceilings sit on the critical
path of every verified action as genuine additive wall-clock latency on top
of an LLM's own inference time. Ticket #20 asks for a realtime-only, tighter
override -- but the numbers must come from real measurement against a real
match, not be guessed (see the ticket's "design decisions already made").
This script is that measurement tool, kept committed (not a throwaway) so
the numbers can be re-derived later if SC2 client behavior, map choice, or
hardware changes.

What it measures
-------------------
Runs one realtime match against the built-in AI (Terran vs. Easy) and, for
each verified action type (train/build/move/attack_move/research), issues
several samples back-to-back, timing each with a plain wall-clock
`time.monotonic()` around the `await bot.<action>(...)` call -- i.e. exactly
"observed wall-clock time from command dispatch to effect_confirmed=True".

Each sample is dispatched with a deliberately huge `max_wait_steps`
(`_BENCHMARK_MAX_WAIT_STEPS`) so a slow sample is never silently truncated
by whatever ceiling `bot.py` ships with today (or after this ticket) --
the whole point is to observe the *true* latency distribution, uncensored.

`debug_all_resources` and `debug_fast_build` are granted up front (same
cheats `tests/integration/test_verified_bot_actions.py` uses) so repeated
trains/builds/research aren't bottlenecked on real economy or production
timers -- these do NOT affect the one latency that matters most here:
build()'s worker-walk time to the placement site is untouched by either
cheat (fast_build only speeds up construction *after* the worker starts
building, not the walk to get there).

Build samples are spread across increasing distance from the townhall (up
to build()'s own default max_distance=20) specifically to capture that
worker-walk-time distribution, not just a fixed near-base case.

Research needs its own idle, completed, capable structure per sample (an
already-busy structure can't be told to research something else), so this
script builds one EngineeringBay per research sample -- fast_build makes
each ready almost immediately after its worker arrives, without touching
that worker's walk time.

Usage
------
    python scripts/benchmark_realtime_verification.py
    python scripts/benchmark_realtime_verification.py --samples 12 --json out.json

Requires a working local SC2 install (`sc2-sdk-setup`, see AGENTS.md) --
this launches a real game, same as any other integration test/play mode.
Not run in CI (real-time wall-clock match, ~a few minutes) -- re-run by
hand whenever these numbers need to be re-derived.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field

from sc2.data import Difficulty, Race
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2

from sdk.bot import VerifiedBotAI
from sdk.runtime import DEFAULT_MAP, run_bot_vs_builtin_ai

#: Ceiling used ONLY for this benchmark's own bot.* calls -- deliberately far
#: above anything bot.py ships with today, so a genuinely slow sample is
#: observed in full rather than being cut off at whatever max_wait_steps
#: happens to be shipped. At the default _POLL_FRAMES=4 (~0.18s/poll, see
#: bot.py), 300 polls is a ~54s ceiling per sample -- comfortably above any
#: realistic single-action wait, including a worst-case cross-map worker walk.
_BENCHMARK_MAX_WAIT_STEPS = 300

#: EngineeringBay-researchable upgrades with no upgrade prerequisite of their
#: own (unlike e.g. TERRANINFANTRYWEAPONSLEVEL2, which needs LEVEL1 already
#: *researched*, not just started) -- each one gives one clean, independent
#: research() sample without needing to wait for a prior level to complete.
_RESEARCH_UPGRADES = (
    UpgradeId.HISECAUTOTRACKING,
    UpgradeId.TERRANBUILDINGARMOR,
    UpgradeId.TERRANINFANTRYWEAPONSLEVEL1,
    UpgradeId.TERRANINFANTRYARMORSLEVEL1,
)

#: Safety cap (in-game seconds, ~= wall-clock seconds in realtime mode) --
#: the match is scored a Tie if this benchmark somehow never calls
#: client.leave() to end it early (see _BenchmarkBot._run's last step).
_SAFETY_TIME_LIMIT = 900


@dataclass
class Sample:
    action: str
    seconds: float
    confirmed: bool
    detail: str


@dataclass
class ActionStats:
    action: str
    n: int
    unconfirmed: int
    min_s: float
    max_s: float
    mean_s: float
    p50_s: float
    p95_s: float
    samples: list[float] = field(default_factory=list)

    @classmethod
    def from_samples(cls, action: str, samples: list[Sample]) -> "ActionStats":
        secs = sorted(s.seconds for s in samples)
        unconfirmed = sum(1 for s in samples if not s.confirmed)
        return cls(
            action=action,
            n=len(secs),
            unconfirmed=unconfirmed,
            min_s=min(secs),
            max_s=max(secs),
            mean_s=statistics.mean(secs),
            p50_s=statistics.median(secs),
            p95_s=_percentile(secs, 0.95),
            samples=secs,
        )


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted list -- no numpy
    dependency needed for a handful of samples."""
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = min(len(sorted_values) - 1, max(0, round(pct * (len(sorted_values) - 1))))
    return sorted_values[idx]


class _BenchmarkBot(VerifiedBotAI):
    """Runs the scripted sample-collection sequence once (first on_step),
    then leaves the game -- see module docstring for the full plan."""

    def __init__(self, samples_per_action: int) -> None:
        super().__init__()
        self.samples_per_action = samples_per_action
        self.samples: list[Sample] = []
        self._started = False

    async def on_step(self, iteration: int) -> None:
        if self._started:
            return
        self._started = True
        await self._run()

    async def _timed(self, action: str, outcome_coro) -> None:
        start = time.monotonic()
        outcome = await outcome_coro
        elapsed = time.monotonic() - start
        self.samples.append(Sample(action, elapsed, outcome.effect_confirmed, outcome.detail))

    async def _wait_ready(self, structure_type: UnitTypeId, max_polls: int = 60) -> bool:
        """Wait (via the same realtime-safe Bot._advance this ticket is
        tuning) for at least one `structure_type` to become ready. Uses
        Bot._advance rather than BotAI._advance_steps directly -- the latter
        is documented (bot.py's module docstring) to stall for tens of real
        seconds if called directly in realtime mode; Bot._advance already
        branches correctly on self.realtime."""
        for _ in range(max_polls):
            await self.bot._advance()  # noqa: SLF001 -- see docstring above
            if self.structures(structure_type).ready:
                return True
        return False

    async def _run(self) -> None:
        bot = self.bot
        ai = self
        n = self.samples_per_action

        await ai.client.debug_all_resources()
        await ai.client.debug_fast_build()
        await self.bot._advance()  # noqa: SLF001 -- let the cheats land, see _wait_ready

        # -- train: queue behind whatever's already in progress each time,
        # so samples don't depend on waiting for a prior train to finish. --
        for _ in range(n):
            await self._timed(
                "train",
                bot.train(
                    UnitTypeId.SCV,
                    train_only_idle_buildings=False,
                    max_wait_steps=_BENCHMARK_MAX_WAIT_STEPS,
                ),
            )

        # -- move / attack_move: retarget workers one at a time. --
        workers = list(ai.workers)
        for i in range(n):
            worker = workers[i % len(workers)]
            target = ai.game_info.map_center.towards(ai.start_location, 2 + (i % 6))
            await self._timed(
                "move",
                bot.move(units=worker, target=target, max_wait_steps=_BENCHMARK_MAX_WAIT_STEPS),
            )
        attack_target = ai.enemy_start_locations[0] if ai.enemy_start_locations else ai.game_info.map_center
        for i in range(n):
            worker = workers[i % len(workers)]
            await self._timed(
                "attack_move",
                bot.attack_move(units=worker, target=attack_target, max_wait_steps=_BENCHMARK_MAX_WAIT_STEPS),
            )

        # -- build: spread across increasing distance from home, up to
        # build()'s own default max_distance=20, to capture the real
        # worker-walk-time distribution rather than just a fixed near case. --
        townhall_pos = ai.townhalls.first.position
        for i in range(n):
            radius = 4 + (i * 16 // max(1, n - 1)) if n > 1 else 4  # spread 4 .. 20
            point = townhall_pos.towards(ai.game_info.map_center, radius).offset(Point2((i % 3, (2 * i) % 3)))
            await self._timed(
                "build",
                bot.build(UnitTypeId.SUPPLYDEPOT, near=point, max_wait_steps=_BENCHMARK_MAX_WAIT_STEPS),
            )

        # -- research: one dedicated EngineeringBay per sample (an
        # already-busy structure can't take a second research order). --
        upgrades = list(_RESEARCH_UPGRADES)[:n] if n <= len(_RESEARCH_UPGRADES) else list(_RESEARCH_UPGRADES)
        for i, upgrade in enumerate(upgrades):
            point = townhall_pos.towards(ai.game_info.map_center, -6 - i * 3).offset(Point2((i, -i)))
            build_outcome = await bot.build(
                UnitTypeId.ENGINEERINGBAY, near=point, max_wait_steps=_BENCHMARK_MAX_WAIT_STEPS
            )
            if not build_outcome.effect_confirmed:
                print(f"[benchmark] skipping research sample {i}: EngineeringBay build not confirmed", file=sys.stderr)
                continue
            if not await self._wait_ready(UnitTypeId.ENGINEERINGBAY):
                print(f"[benchmark] skipping research sample {i}: EngineeringBay never became ready", file=sys.stderr)
                continue
            await self._timed(
                "research",
                bot.research(upgrade, max_wait_steps=_BENCHMARK_MAX_WAIT_STEPS),
            )

        print("[benchmark] all samples collected, leaving the game early.", file=sys.stderr)
        await ai.client.leave()


def _run_benchmark(samples_per_action: int, map_name: str) -> list[Sample]:
    bot_ai = _BenchmarkBot(samples_per_action)
    result = run_bot_vs_builtin_ai(
        bot_ai,
        map_name=map_name,
        my_race=Race.Terran,
        opponent_race=Race.Zerg,
        difficulty=Difficulty.Easy,
        realtime=True,
        game_time_limit=_SAFETY_TIME_LIMIT,
    )
    print(f"[benchmark] match ended with result={result!r} (Defeat is expected -- see client.leave()).")
    return bot_ai.samples


def _report(samples: list[Sample]) -> dict[str, ActionStats]:
    by_action: dict[str, list[Sample]] = {}
    for s in samples:
        by_action.setdefault(s.action, []).append(s)

    stats: dict[str, ActionStats] = {}
    print()
    print(f"{'action':<14}{'n':>4}{'unconfirmed':>13}{'min':>9}{'p50':>9}{'p95':>9}{'max':>9}{'mean':>9}   (seconds)")
    for action in ("train", "build", "move", "attack_move", "research"):
        action_samples = by_action.get(action, [])
        if not action_samples:
            print(f"{action:<14}  (no samples collected)")
            continue
        st = ActionStats.from_samples(action, action_samples)
        stats[action] = st
        print(
            f"{st.action:<14}{st.n:>4}{st.unconfirmed:>13}{st.min_s:>9.3f}{st.p50_s:>9.3f}"
            f"{st.p95_s:>9.3f}{st.max_s:>9.3f}{st.mean_s:>9.3f}"
        )
    print()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--samples", type=int, default=8, help="Samples per action type (default: 8; research is capped at 4 -- see module docstring)."
    )
    parser.add_argument("--map", default=DEFAULT_MAP, help=f"Map to play on (default: {DEFAULT_MAP}).")
    parser.add_argument("--json", type=str, default=None, help="Optional path to also write raw samples + stats as JSON.")
    args = parser.parse_args(argv)

    samples = _run_benchmark(args.samples, args.map)
    stats = _report(samples)

    if args.json:
        payload = {
            "samples": [s.__dict__ for s in samples],
            "stats": {k: v.__dict__ for k, v in stats.items()},
        }
        with open(args.json, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"[benchmark] wrote raw data to {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
