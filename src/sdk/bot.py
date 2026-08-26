"""The verified `bot.*` action/observation layer, plus `sdk.*` raw passthrough.

This is the core deliverable of ticket #3
(https://github.com/blokboy/sc2-sdk/issues/3): a `Bot` wrapper around a live
`python-sc2` `BotAI` instance that turns "issue a command" into "issue a
command and confirm what actually happened," per the project's central
design decision (see #1's spec, "Implementation Decisions"):

    `bot.*` -- the primary, high-level tier. Semantic, verified actions and
    observations... Each action re-observes game state after issuing the
    underlying python-sc2 call(s) and reports whether the intended effect
    occurred, not just that a command was dispatched.

    `sdk.*` -- the raw tier. Direct passthrough to the underlying
    python-sc2 BotAI instance and its unit/action API.

Design decision: why verification needs to advance real game steps
--------------------------------------------------------------------
python-sc2's `BotAI.do()` (which every write-side call -- train, build,
research, move, attack -- ultimately goes through) only *queues* a command
into `self.actions`; the command is not sent to the SC2 process until
`main.py`'s outer loop calls `_after_step()` after `on_step` returns, and
the server doesn't act on it until the *following* simulation step. So
"did the command actually take effect" cannot be answered synchronously,
purely from Python-side bookkeeping, within the same call that issued it --
only the server's own subsequent state does. python-sc2 itself ships a
`@final` method for exactly this situation: `BotAI._advance_steps(n)`,
documented as "meant to be used as a debugging and testing tool" -- it
flushes queued actions, steps the simulation, fetches a fresh observation,
and refreshes `self.units`/`self.structures`/`self.state` accordingly. Each
verified action below issues its underlying command, then calls
`_advance_steps(1)` in a small retry loop (bounded by `max_wait_steps`) and
re-observes until the intended effect is confirmed or the window elapses --
at which point it reports `effect_confirmed=False` rather than silently
assuming success. See `outcomes.py` for the exact three-way ok/confirmed/
error shape this produces, and `runtime.py` for how a `BotAI` subclass
built around this class is actually driven through a live game.

`_advance_steps` drives its progress by sending a manual `RequestStep` to
the SC2 engine -- only a meaningful request when the engine is otherwise
paused, which non-realtime mode guarantees (nothing but `sc2.main`'s outer
loop ever steps it) but realtime mode does not: there, the engine already
free-runs on its own wall-clock timer and `sc2.main`'s own realtime branch
never calls `client.step()` itself. `Bot._advance` therefore branches on
`self._ai.realtime` and uses `Bot._advance_realtime` instead in that case --
same flush-then-reobserve shape, but it waits for the engine's own
free-running simulation to reach a target game loop rather than manually
stepping it. See `Bot._advance`'s docstring for the full reasoning.

Race-agnostic by design: train/build/research/move/chat are all generic
python-sc2 mechanisms (the same underlying `BotAI.train()`/`.build()`/
`.research()` work for any of the three races); nothing Terran-specific is
hardcoded here. Ticket #3 only *exercises* this against Terran integration
tests -- #4 (Protoss) and #5 (Zerg) are expected to reuse this class
directly rather than re-deriving the verification pattern.
"""

from __future__ import annotations

from s2clientprotocol import sc2api_pb2 as sc_pb
from sc2.bot_ai import BotAI
from sc2.data import Result
from sc2.game_state import GameState
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units

from sdk.observation import Observation, observe
from sdk.outcomes import BuildOutcome, ChatOutcome, MoveOutcome, ResearchOutcome, TrainOutcome

#: Number of raw simulation frames each verification poll advances by (see
#: Bot._advance). 4 matches python-sc2's own default per-iteration cadence
#: (Client.game_step), i.e. one poll below is worth roughly one normal bot
#: "tick" of game time (~0.18s). Non-realtime only -- see _REALTIME_POLL_FRAMES
#: for realtime mode's tighter equivalent.
_POLL_FRAMES = 4

#: Default number of verification polls (each _POLL_FRAMES long) a verified
#: action will retry, re-observing after each, before giving up on
#: confirming its effect. Kept small: these polls happen inside a single
#: on_step call (see the module docstring), so a large default would make
#: every action call expensive. Callers doing something that legitimately
#: takes longer (e.g. a structure whose builder has to walk further, or a
#: build with no fast-build cheat active) can pass a larger max_wait_steps
#: explicitly -- see e.g. build()'s own, larger default below. Non-realtime
#: only -- see _REALTIME_DEFAULT_MAX_WAIT_STEPS for realtime mode.
_DEFAULT_MAX_WAIT_STEPS = 5

#: move()/attack_move()'s own (non-realtime) default, promoted to a named
#: constant so it sits next to its realtime counterpart below -- the two
#: literal `3`s the module docstring/ticket #20 refer to as "move/attack_move
#: override to 3" (unchanged from before this ticket).
_MOVE_DEFAULT_MAX_WAIT_STEPS = 3

#: build()'s own (non-realtime) default -- unchanged from before this
#: ticket, just moved from a class attribute to a module-level constant
#: (alongside its realtime counterpart below) so `_resolve_max_wait_steps`
#: can reference it as a plain name from inside a method body, where a
#: same-named class attribute wouldn't be in scope. Building requires the
#: assigned worker to walk to the placement point before construction can
#: start, unlike train/research which dispatch from a structure that's
#: already in place -- so build()'s default verification window is longer
#: (see also: confirmation requires the structure entity to actually
#: appear, not just already_pending() ticking up while the worker is still
#: walking there -- see build()'s own confirmation loop).
_BUILD_DEFAULT_MAX_WAIT_STEPS = 40

# -- realtime-only overrides (ticket #20) -------------------------------------
#
# In realtime mode the SC2 engine free-runs on its own wall-clock timer (see
# Bot._advance_realtime's docstring), so every poll above is real, additive
# wall-clock latency sitting on top of an LLM caller's own turn -- unlike
# non-realtime/stepped mode, where polling more frames costs nothing in wall
# time. These constants are used ONLY on Bot._advance's `self._ai.realtime`
# branch (see there) and by each verified action's own realtime branch below;
# non-realtime behavior and the constants above are completely unchanged.
#
# Derived from real measurement (not guessed -- see
# scripts/benchmark_realtime_verification.py, kept committed so these numbers
# can be re-derived later), against a real local realtime match, built-in
# Easy AI opponent, three separate runs across three different maps
# (AutomatonLE/KairosJunctionLE/CyberForestLE). Combined per-action-type
# wall-clock latency from command dispatch to effect_confirmed=True (26
# samples each for train/move/attack_move/build, 12 for research, all with
# an artificially huge max_wait_steps so no sample was truncated by the
# ceiling being measured):
#
#   action        min     p50     p95     max    (seconds)
#   train        0.139   0.183   0.243   0.528
#   move         0.147   0.191   0.238   0.249
#   attack_move  0.150   0.178   0.248   0.254
#   build        0.939   2.767   7.500   7.537
#   research     0.156   0.183   0.266   0.267
#
# train/move/attack_move/research all confirm within a couple hundred ms in
# the overwhelming majority of samples (typically the very first poll or
# two); build is the outlier by design (see _BUILD_DEFAULT_MAX_WAIT_STEPS's
# own docstring) -- a worker genuinely has to walk to the site, and that walk
# alone measured as high as 7.5s here, essentially matching non-realtime's
# existing 7.2s worst-case budget (_BUILD_DEFAULT_MAX_WAIT_STEPS=40 *
# _POLL_FRAMES=4 / 22.4 loops-per-second).

#: Realtime polling granularity: finer than _POLL_FRAMES's 4 (~0.18s/poll) at
#: 2 frames (~0.09s/poll, 22.4 game loops/sec at normal speed -- see
#: sc2.main's own game_time_limit conversion). Finer polling means less
#: quantization error on top of an action's true confirmation latency --
#: capped below at a "poll cost floor" (2 frames), not 1, so this doesn't
#: chase diminishing returns against per-poll IPC/observation-request
#: overhead that finer-still polling wouldn't meaningfully reduce.
_REALTIME_POLL_FRAMES = 2

#: Realtime train/research/move-attack-default ceiling: 9 polls * 2 frames /
#: 22.4 loops-per-sec ~= 0.80s worst-case wait -- below the
#: non-realtime-shared ~0.89s equivalent (5 * 4 / 22.4), while staying
#: comfortably above the measured distribution for both action types
#: (train: p95=0.243s, max=0.528s across the main benchmark run, with a
#: repeat max of 0.534s seen in a separate spot-check against the tuned
#: defaults themselves -- see scripts/benchmark_realtime_verification.py's
#: module docstring; research: p95=0.266s, max=0.267s) -- a ~1.5x margin
#: over the rarer train max, and a much wider margin over the typical (p95)
#: case for both.
_REALTIME_DEFAULT_MAX_WAIT_STEPS = 9

#: Realtime move/attack_move ceiling: these confirm even faster and more
#: consistently than train/research (measured p95=0.238s/0.248s,
#: max=0.249s/0.254s -- no server-side production/research bookkeeping to
#: wait on, just an order being picked up) -- 5 polls * 2 frames / 22.4 ~=
#: 0.45s worst-case wait, well below the non-realtime-shared 3-step/~0.54s
#: equivalent, with a ~1.75x margin over the measured max.
_REALTIME_MOVE_MAX_WAIT_STEPS = 5

#: Realtime build ceiling: DELIBERATELY NOT shrunk the way the ceilings
#: above are -- see this ticket's explicit warning against letting build()
#: regress into effect_confirmed=False being the common case. Measured
#: worker-walk-time maxed out at 7.537s wall-clock (~169 simulation loops at
#: 22.4 loops/sec) across 26 samples spanning three maps -- notably, that
#: already exceeds non-realtime's own nominal budget (_BUILD_DEFAULT_MAX_WAIT_STEPS=40
#: * _POLL_FRAMES=4 = 160 loops), meaning the pre-existing non-realtime
#: ceiling was already only marginally sufficient for a real worst-case walk,
#: with no margin at all. 170 polls * 2 frames / 22.4 ~= 15.2s worst-case
#: wait -- roughly double the measured max/p95 (both ~7.5s), and comfortably
#: above the ~169-loop worst case observed, which is the generous-margin
#: behavior this ticket explicitly asks for on build specifically. Finer
#: 2-frame polling here mainly buys faster typical-case detection once a
#: worker actually arrives, not a smaller ceiling -- the ceiling itself has
#: to stay sized to real, physical worker-travel time, which realtime
#: polling cadence cannot shrink.
_REALTIME_BUILD_MAX_WAIT_STEPS = 170


async def refresh_realtime_state(ai: BotAI, frames: int = 0) -> None:
    """Flush queued actions and refresh `ai`'s state (`ai.state`/`ai.units`/
    etc.) to the server's own free-running game loop, `frames` loops ahead
    of whatever `ai.state.game_loop` currently is -- the realtime-mode
    equivalent of manually stepping the simulation (which, unlike
    non-realtime mode, isn't legal here; see `Bot._advance_realtime`'s
    docstring below, which this function was factored out of).

    `frames=0` (the default) means "refresh to whatever the server's
    current loop already is" rather than "wait for N more loops to
    elapse": since realtime mode's engine free-runs on its own wall-clock
    timer independently of Python, `ai.state.game_loop` going into this
    call is already whatever loop was last observed -- typically some real
    time in the past by the time this coroutine actually runs -- so the
    server's own current loop is normally already at or past
    `target_loop`, and `client.observation(target_loop)` returns as soon as
    its round trip completes, with no additional waiting. (Only if this is
    called back-to-back with essentially no elapsed wall-clock time could
    the server's loop not yet have reached `target_loop`, in which case
    this waits for one more tick -- still on the order of the server's own
    tick rate, not an open-ended block.)

    Shared by `Bot._advance_realtime` (verification polling, which passes
    a positive `frames` to wait for actual progress) and
    `ExecuteCodeBotAI.on_step` (`sdk/mcp_server.py`, ticket #21: refreshing
    state once per dequeued request, with `frames=0`, to close the "state
    is stale by however long the caller took composing its next call" gap)
    -- both need the identical flush-observe-rebuild-reprepare sequence,
    just for different reasons and different target loops, so it lives
    here once rather than being reimplemented at the second call site.
    """
    await ai._after_step()  # noqa: SLF001 -- see this class's _advance docstring
    target_loop = ai.state.game_loop + frames
    state = await ai.client.observation(target_loop)
    gs = GameState(state.observation)
    proto_game_info = await ai.client._execute(game_info=sc_pb.RequestGameInfo())  # noqa: SLF001
    ai._prepare_step(gs, proto_game_info)  # noqa: SLF001
    await ai.issue_events()


class Bot:
    """Wraps a live `BotAI` instance with the verified `bot.*` API.

    Constructed once per game (see `VerifiedBotAI` below, which every
    integration test's bot subclasses). `bot.sdk` is the raw passthrough
    tier -- literally the wrapped `BotAI` instance itself, since that
    instance *is* "the underlying python-sc2 BotAI instance and its
    unit/action API" the spec asks `sdk.*` to expose.
    """

    def __init__(self, ai: BotAI) -> None:
        self._ai = ai
        #: Set by VerifiedBotAI.on_end once the match concludes; None while
        #: the game is still in progress. Exposed via Observation.match_result.
        self.match_result: Result | None = None

    @property
    def sdk(self) -> BotAI:
        """Raw passthrough to the underlying python-sc2 BotAI instance."""
        return self._ai

    def observe(self) -> Observation:
        """Report own units/structures/resources/supply, visible enemy
        units/structures, minimap/vision, and match outcome (once known).

        Pure and side-effect-free -- safe to call at any time, including
        after the match has ended (it then reports the last-known state
        plus the now-final `match_result`)."""
        return observe(self._ai, self.match_result)

    # -- internal helpers ---------------------------------------------------

    def _require_live(self) -> None:
        if not self._ai.client.in_game:
            raise RuntimeError(
                "Cannot issue bot.* actions after the match has ended "
                f"(match_result={self.match_result!r}). Call bot.observe() to "
                "inspect the final state instead."
            )

    async def _advance(self, frames: int | None = None) -> None:
        """Flush queued actions, advance the simulation by `frames` raw
        simulation frames, and refresh self._ai's state -- see the module
        docstring for why this is necessary for verification.

        `frames=None` (the default every call site above uses) resolves to
        `_POLL_FRAMES` (non-realtime) or `_REALTIME_POLL_FRAMES` (realtime) --
        see the "realtime-only overrides" section up top for why realtime
        gets its own, finer default here: this is the one place that
        distinction is applied, so every verified action automatically picks
        up the right granularity for the mode it's actually running in,
        without any of them branching on `self._ai.realtime` themselves.
        Passing `frames` explicitly (no current caller does) overrides this
        resolution entirely, same as before this ticket.

        Branches on `self._ai.realtime` (set by python-sc2's own
        `_prepare_start`): non-realtime games use `BotAI._advance_steps`
        directly, a thin wrapper around python-sc2's own `@final`,
        explicitly-documented-for-this-purpose method. Realtime games use
        `_advance_realtime` instead: `_advance_steps` sends a manual
        `RequestStep` to the SC2 engine, which is only a
        meaningful request when the engine is otherwise paused (true in
        non-realtime mode, where `sc2.main`'s own outer loop is the only
        thing that ever steps it). In realtime mode the engine already
        free-runs on its own wall-clock timer -- `sc2.main._play_game_ai`'s
        realtime branch never calls `client.step()` at all -- so issuing
        `RequestStep` concurrently with that free-run is exactly the
        untested "debugging and testing tool only" territory
        `_advance_steps`'s own upstream docstring warns about, and
        empirically stalled for tens of real seconds per call."""
        if self._ai.realtime:
            await self._advance_realtime(frames if frames is not None else _REALTIME_POLL_FRAMES)
        else:
            await self._ai._advance_steps(frames if frames is not None else _POLL_FRAMES)  # noqa: SLF001 -- see module docstring

    async def _advance_realtime(self, frames: int) -> None:
        """Realtime-mode equivalent of `BotAI._advance_steps` that waits
        for `frames` more game loops to elapse on the server's own
        free-running wall-clock simulation, instead of manually stepping
        it. Mirrors the request shape `sc2.main._play_game_ai`'s own
        realtime branch uses: a `client.observation(game_loop=...)` call
        pinned to a specific future loop blocks until the server's
        already-ticking simulation actually reaches it, which is the
        realtime-legal way to "wait a bit and re-observe" -- unlike
        `_advance_steps`, this never sends a `RequestStep`. The rest
        mirrors `_advance_steps` exactly (same private hooks, same
        reasoning for reaching into them -- see this class's `_advance`
        and the module docstring). Delegates to the module-level
        `refresh_realtime_state`, which this method's implementation was
        factored out into (ticket #21) so `ExecuteCodeBotAI.on_step` could
        reuse the identical sequence for its own per-dequeue staleness
        refresh without duplicating it."""
        await refresh_realtime_state(self._ai, frames)

    def _resolve_units(self, units: Unit | Units | int | list) -> tuple[list[Unit], list[int]]:
        """Normalize a caller-supplied unit selector (a Unit, a tag, or an
        iterable of either) into (found live Units, tags that don't
        currently resolve to any unit we control)."""
        if isinstance(units, Unit):
            requested = [units.tag]
        elif isinstance(units, int):
            requested = [units]
        else:
            requested = [u.tag if isinstance(u, Unit) else int(u) for u in units]
        found: list[Unit] = []
        missing: list[int] = []
        for tag in requested:
            unit = self._ai.all_own_units.find_by_tag(tag)
            if unit is None:
                missing.append(tag)
            else:
                found.append(unit)
        return found, missing

    def _resolve_max_wait_steps(
        self, max_wait_steps: int | None, realtime_default: int, non_realtime_default: int
    ) -> int:
        """Resolve a caller-supplied `max_wait_steps` (`None` meaning "use
        whichever default fits the mode this game is actually running in")
        to a concrete step count -- the max_wait_steps half of ticket #20's
        realtime-only override, mirroring what `_advance`'s own `frames`
        resolution already does for polling granularity.

        This can't be expressed as an ordinary parameter default (e.g.
        `max_wait_steps: int = _DEFAULT_MAX_WAIT_STEPS`) because a parameter
        default is evaluated once, at class-definition time -- there's no
        `self` available then to check `self._ai.realtime` against. Each
        verified action below instead defaults its own `max_wait_steps` to
        `None` and calls this at the top of its body, where `self._ai` is
        live. Non-realtime callers get exactly the pre-#20 constants either
        way -- only the realtime branch's values are new.
        """
        if max_wait_steps is not None:
            return max_wait_steps
        return realtime_default if self._ai.realtime else non_realtime_default

    # -- train ---------------------------------------------------------------

    async def train(
        self,
        unit_type: UnitTypeId,
        amount: int = 1,
        closest_to: Point2 | None = None,
        train_only_idle_buildings: bool = True,
        max_wait_steps: int | None = None,
    ) -> TrainOutcome:
        """Train `amount` of `unit_type` from any eligible, idle, completed
        production structure, and confirm production actually started.

        `train_only_idle_buildings` (passed straight through to python-sc2's
        `BotAI.train`) controls whether a structure that already has
        production queued is skipped (default) or queued behind -- pass
        False to add to an existing queue (e.g. a second unit at your only
        production structure while the first is still building).

        "Effect confirmed" here means the server-side pending-production
        count for `unit_type` increased (or, if it completes within the
        wait window, that the unit(s) themselves now exist) -- not merely
        that `python-sc2`'s local, optimistic bookkeeping thought the
        command would succeed.

        `max_wait_steps` defaults to `None`, which resolves to
        `_DEFAULT_MAX_WAIT_STEPS` (non-realtime) or
        `_REALTIME_DEFAULT_MAX_WAIT_STEPS` (realtime) -- see
        `_resolve_max_wait_steps` and the "realtime-only overrides" section
        near the top of this module. Pass an explicit value to override
        either default.
        """
        ai = self._ai
        self._require_live()
        max_wait_steps = self._resolve_max_wait_steps(
            max_wait_steps, _REALTIME_DEFAULT_MAX_WAIT_STEPS, _DEFAULT_MAX_WAIT_STEPS
        )

        if ai.tech_requirement_progress(unit_type) < 1:
            return TrainOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Tech requirement not met for {unit_type.name}: the building(s) "
                    "required to train it aren't ready yet."
                ),
                detail="Command not dispatched: tech requirement check failed.",
                unit_type=unit_type.name,
                requested_amount=amount,
                dispatched_amount=0,
                new_unit_tags=(),
            )
        if not ai.can_afford(unit_type):
            cost = ai.calculate_cost(unit_type)
            return TrainOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Insufficient resources to train {unit_type.name}: need "
                    f"{cost.minerals} minerals / {cost.vespene} vespene, have "
                    f"{ai.minerals} / {ai.vespene}."
                ),
                detail="Command not dispatched: affordability check failed.",
                unit_type=unit_type.name,
                requested_amount=amount,
                dispatched_amount=0,
                new_unit_tags=(),
            )

        before_tags = {u.tag for u in ai.units(unit_type)}
        pending_before = ai.already_pending(unit_type)
        dispatched = ai.train(
            unit_type,
            amount=amount,
            closest_to=closest_to,
            train_only_idle_buildings=train_only_idle_buildings,
        )

        if dispatched == 0:
            idle_clause = "idle, " if train_only_idle_buildings else ""
            return TrainOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Could not train {unit_type.name}: no {idle_clause}completed structure "
                    f"capable of training it was available (checked {len(ai.structures)} "
                    "of our structures)."
                ),
                detail="Command not dispatched: no eligible production structure found.",
                unit_type=unit_type.name,
                requested_amount=amount,
                dispatched_amount=0,
                new_unit_tags=(),
            )

        new_tags: set[int] = set()
        confirmed = False
        for _ in range(max_wait_steps):
            await self._advance()
            pending_now = ai.already_pending(unit_type)
            new_tags = {u.tag for u in ai.units(unit_type)} - before_tags
            if pending_now >= pending_before + dispatched or len(new_tags) >= dispatched:
                confirmed = True
                break

        if confirmed:
            detail = (
                f"Dispatched {dispatched}/{amount} {unit_type.name}; confirmed via "
                f"{'newly observed unit(s)' if new_tags else 'increased pending-production count'}."
            )
        else:
            detail = (
                f"Dispatched {dispatched}/{amount} {unit_type.name}; not confirmed via "
                f"re-observation within {max_wait_steps} step(s)."
            )
        return TrainOutcome(
            ok=True,
            effect_confirmed=confirmed,
            error=None,
            detail=detail,
            unit_type=unit_type.name,
            requested_amount=amount,
            dispatched_amount=dispatched,
            new_unit_tags=tuple(sorted(new_tags)),
        )

    # -- build -----------------------------------------------------------------

    async def build(
        self,
        structure_type: UnitTypeId,
        near: Unit | Point2,
        max_distance: int = 20,
        build_worker: Unit | None = None,
        max_wait_steps: int | None = None,
    ) -> BuildOutcome:
        """Build `structure_type` near `near`, and confirm construction
        actually started -- meaning a new structure entity of that type is
        now observable (not merely that already_pending() ticked up, which
        happens as soon as the assigned worker is *dispatched*, before it
        has even arrived at the site -- see the module docstring on why a
        real subsequent observation, not optimistic bookkeeping, is what
        "confirmed" means here).

        Checks `tech_requirement_progress` first, same as `train()`: unlike
        `train()`'s underlying `BotAI.train`, `BotAI.build` doesn't validate
        this itself -- it happily finds a placement, assigns a worker, and
        issues the command even when the prerequisite (e.g. a Barracks
        before any Supply Depot has *finished*, not just started) isn't
        met. The SC2 server then silently drops the command server-side:
        no error, no order ever appears on the worker, nothing to
        re-observe -- so without this check here, `dispatched` above would
        be `True` while nothing happened, and confirmation would fail for
        an entirely different reason (never having been dispatched at all)
        than what `effect_confirmed=False` normally means (dispatched, but
        not yet observably true).

        `max_wait_steps` defaults to `None`, which resolves to
        `_BUILD_DEFAULT_MAX_WAIT_STEPS` (non-realtime) or
        `_REALTIME_BUILD_MAX_WAIT_STEPS` (realtime) -- see
        `_resolve_max_wait_steps` and the "realtime-only overrides" section
        near the top of this module. Both budgets stay generous: a worker
        genuinely has to walk to the site, which measured up to ~7.5s of
        real wall-clock time even in realtime mode (see
        `scripts/benchmark_realtime_verification.py`) -- shrinking this the
        way train/research/move's ceilings were shrunk would make
        `effect_confirmed=False` the common case for the most build-heavy
        parts of play, which this ticket explicitly rules out.
        """
        ai = self._ai
        self._require_live()
        max_wait_steps = self._resolve_max_wait_steps(
            max_wait_steps, _REALTIME_BUILD_MAX_WAIT_STEPS, _BUILD_DEFAULT_MAX_WAIT_STEPS
        )

        near_point = near.position if isinstance(near, Unit) else near
        position_report = (near_point.x, near_point.y) if isinstance(near_point, Point2) else None

        if ai.tech_requirement_progress(structure_type) < 1:
            return BuildOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Tech requirement not met for {structure_type.name}: the building(s) "
                    "required to build it aren't ready yet."
                ),
                detail="Command not dispatched: tech requirement check failed.",
                structure_type=structure_type.name,
                position=position_report,
                structure_tag=None,
            )
        if not ai.can_afford(structure_type):
            cost = ai.calculate_cost(structure_type)
            return BuildOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Insufficient resources to build {structure_type.name}: need "
                    f"{cost.minerals} minerals / {cost.vespene} vespene, have "
                    f"{ai.minerals} / {ai.vespene}."
                ),
                detail="Command not dispatched: affordability check failed.",
                structure_type=structure_type.name,
                position=position_report,
                structure_tag=None,
            )

        before_tags = {u.tag for u in ai.structures(structure_type)}
        dispatched = await ai.build(
            structure_type, near=near, max_distance=max_distance, build_worker=build_worker
        )

        if not dispatched:
            return BuildOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Could not place {structure_type.name} near {position_report}: no "
                    f"valid buildable position was found within max_distance={max_distance}, "
                    "or no worker was available to build it."
                ),
                detail="Command not dispatched: placement or builder-availability check failed.",
                structure_type=structure_type.name,
                position=position_report,
                structure_tag=None,
            )

        # Confirmation requires the structure entity to actually appear --
        # NOT just already_pending(structure_type) ticking up, which
        # happens as soon as the worker is dispatched, well before it has
        # arrived and actually started construction (see docstring above).
        new_tag: int | None = None
        confirmed = False
        for _ in range(max_wait_steps):
            await self._advance()
            new_tags = {u.tag for u in ai.structures(structure_type)} - before_tags
            if new_tags:
                new_tag = next(iter(new_tags))
                confirmed = True
                break

        detail = (
            f"Dispatched build of {structure_type.name}; "
            + (
                f"confirmed (structure_tag={new_tag})."
                if confirmed
                else f"not confirmed via re-observation within {max_wait_steps} step(s)."
            )
        )
        return BuildOutcome(
            ok=True,
            effect_confirmed=confirmed,
            error=None,
            detail=detail,
            structure_type=structure_type.name,
            position=position_report,
            structure_tag=new_tag,
        )

    # -- research ----------------------------------------------------------------

    async def research(
        self,
        upgrade_type: UpgradeId,
        max_wait_steps: int | None = None,
    ) -> ResearchOutcome:
        """Research `upgrade_type` from any idle, completed structure that
        can research it, and confirm research actually started.

        `max_wait_steps` defaults to `None`, which resolves to
        `_DEFAULT_MAX_WAIT_STEPS` (non-realtime) or
        `_REALTIME_DEFAULT_MAX_WAIT_STEPS` (realtime) -- see
        `_resolve_max_wait_steps` and the "realtime-only overrides" section
        near the top of this module.
        """
        ai = self._ai
        self._require_live()
        max_wait_steps = self._resolve_max_wait_steps(
            max_wait_steps, _REALTIME_DEFAULT_MAX_WAIT_STEPS, _DEFAULT_MAX_WAIT_STEPS
        )

        if upgrade_type in ai.state.upgrades:
            return ResearchOutcome(
                ok=False,
                effect_confirmed=False,
                error=f"{upgrade_type.name} has already been researched.",
                detail="Command not dispatched: upgrade already completed.",
                upgrade_type=upgrade_type.name,
            )
        if not ai.can_afford(upgrade_type):
            cost = ai.calculate_cost(upgrade_type)
            return ResearchOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Insufficient resources to research {upgrade_type.name}: need "
                    f"{cost.minerals} minerals / {cost.vespene} vespene, have "
                    f"{ai.minerals} / {ai.vespene}."
                ),
                detail="Command not dispatched: affordability check failed.",
                upgrade_type=upgrade_type.name,
            )

        pending_before = ai.already_pending_upgrade(upgrade_type)
        try:
            dispatched = ai.research(upgrade_type)
        except AssertionError:
            return ResearchOutcome(
                ok=False,
                effect_confirmed=False,
                error=f"{upgrade_type.name} is not a known researchable upgrade.",
                detail="Command not dispatched: unrecognized upgrade type.",
                upgrade_type=upgrade_type.name,
            )

        if not dispatched:
            return ResearchOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Could not research {upgrade_type.name}: no idle, completed, "
                    "appropriately-tech'd structure was available to research it from "
                    "(or its prerequisite building/upgrade is missing)."
                ),
                detail="Command not dispatched: no eligible research structure found.",
                upgrade_type=upgrade_type.name,
            )

        confirmed = False
        for _ in range(max_wait_steps):
            await self._advance()
            if ai.already_pending_upgrade(upgrade_type) > pending_before or upgrade_type in ai.state.upgrades:
                confirmed = True
                break

        detail = (
            f"Dispatched research of {upgrade_type.name}; "
            + ("confirmed." if confirmed else f"not confirmed via re-observation within {max_wait_steps} step(s).")
        )
        return ResearchOutcome(
            ok=True,
            effect_confirmed=confirmed,
            error=None,
            detail=detail,
            upgrade_type=upgrade_type.name,
        )

    # -- move / attack-move --------------------------------------------------

    async def move(
        self,
        units: Unit | Units | int | list,
        target: Point2 | Unit,
        queue: bool = False,
        max_wait_steps: int | None = None,
    ) -> MoveOutcome:
        """Move a unit or unit group to `target`, and confirm each unit
        actually picked up a move order.

        `max_wait_steps` defaults to `None`, which resolves to
        `_MOVE_DEFAULT_MAX_WAIT_STEPS` (non-realtime, 3) or
        `_REALTIME_MOVE_MAX_WAIT_STEPS` (realtime) -- see
        `_resolve_max_wait_steps` and the "realtime-only overrides" section
        near the top of this module.
        """
        return await self._move_or_attack("move", units, target, queue, max_wait_steps)

    async def attack_move(
        self,
        units: Unit | Units | int | list,
        target: Point2 | Unit,
        queue: bool = False,
        max_wait_steps: int | None = None,
    ) -> MoveOutcome:
        """Attack-move a unit or unit group toward `target`, and confirm
        each unit actually picked up an attack order. See `move`'s docstring
        for `max_wait_steps`'s default-resolution behavior."""
        return await self._move_or_attack("attack_move", units, target, queue, max_wait_steps)

    async def _move_or_attack(
        self,
        mode: str,
        units: Unit | Units | int | list,
        target: Point2 | Unit,
        queue: bool,
        max_wait_steps: int | None,
    ) -> MoveOutcome:
        ai = self._ai
        self._require_live()
        max_wait_steps = self._resolve_max_wait_steps(
            max_wait_steps, _REALTIME_MOVE_MAX_WAIT_STEPS, _MOVE_DEFAULT_MAX_WAIT_STEPS
        )

        found, missing = self._resolve_units(units)
        requested_tags = tuple(u.tag for u in found) + tuple(missing)

        if missing:
            return MoveOutcome(
                ok=False,
                effect_confirmed=False,
                error=(
                    f"Unknown unit tag(s): {missing}. These tags don't correspond to any "
                    "unit currently under our control."
                ),
                detail="Command not dispatched: one or more unit tags could not be resolved.",
                mode=mode,
                requested_tags=requested_tags,
                confirmed_tags=(),
            )
        if not found:
            return MoveOutcome(
                ok=False,
                effect_confirmed=False,
                error="No units given to move/attack-move.",
                detail="Command not dispatched: empty unit selection.",
                mode=mode,
                requested_tags=(),
                confirmed_tags=(),
            )

        for unit in found:
            command = unit.attack(target, queue=queue) if mode == "attack_move" else unit.move(target, queue=queue)
            ai.do(command, ignore_warning=True)

        confirmed_tags: set[int] = set()
        for _ in range(max_wait_steps):
            await self._advance()
            confirmed_tags = set()
            for tag in requested_tags:
                unit = ai.all_own_units.find_by_tag(tag)
                if unit is None:
                    continue  # died mid-verification; can't confirm, don't crash
                is_doing_it = unit.is_attacking if mode == "attack_move" else unit.is_moving
                if is_doing_it:
                    confirmed_tags.add(tag)
            if len(confirmed_tags) == len(found):
                break

        confirmed = len(confirmed_tags) == len(found)
        detail = (
            f"Dispatched {mode} for {len(found)} unit(s); confirmed for "
            f"{len(confirmed_tags)}/{len(found)} within {max_wait_steps} step(s)."
        )
        return MoveOutcome(
            ok=True,
            effect_confirmed=confirmed,
            error=None,
            detail=detail,
            mode=mode,
            requested_tags=requested_tags,
            confirmed_tags=tuple(sorted(confirmed_tags)),
        )

    # -- chat ------------------------------------------------------------------

    async def chat(self, message: str, team_only: bool = False) -> ChatOutcome:
        """Send a chat message.

        Known limitation, documented rather than papered over: python-sc2 /
        the SC2 game API give no way to read a chat message back to confirm
        the game client actually displayed it, so `effect_confirmed` here
        can only ever reflect "the chat_send call completed without the
        client rejecting it" -- not a true independent re-observation like
        every other action above. This is called out explicitly rather than
        silently claiming the same verification strength as train/build/
        research/move.
        """
        ai = self._ai
        self._require_live()
        if not isinstance(message, str) or not message:
            return ChatOutcome(
                ok=False,
                effect_confirmed=False,
                error="Chat message must be a non-empty string.",
                detail="Command not dispatched: invalid message.",
                message=message,
            )
        try:
            await ai.chat_send(message, team_only=team_only)
        except Exception as exc:  # noqa: BLE001 -- report any client-side rejection as a clear error
            return ChatOutcome(
                ok=False,
                effect_confirmed=False,
                error=f"chat_send failed: {exc}",
                detail="Command not dispatched: the client rejected the chat message.",
                message=message,
            )
        return ChatOutcome(
            ok=True,
            effect_confirmed=True,
            error=None,
            detail=(
                "chat_send completed without error. Note: this is not an independent "
                "re-observation (the SC2 API doesn't expose received chat back to the "
                "sender) -- see this method's docstring."
            ),
            message=message,
        )


class VerifiedBotAI(BotAI):
    """Base class for a live-game `BotAI` that wires up `self.bot` (the
    verified layer above) and `self.sdk` (raw passthrough), and records the
    final match result.

    This is the "concrete shape" for driving `bot.*`/`sdk.*` against a real
    game (see the module docstring and `runtime.py`): subclass this,
    override `on_step` to call `self.bot.*`/`self.sdk.*` and record
    whatever you want to assert on later onto `self` (a list, a dict, plain
    attributes -- whatever's convenient), and hand an *instance* of your
    subclass to `runtime.run_bot_vs_builtin_ai`. Because `run_game()`
    doesn't discard the bot instance, the same live Python object is still
    there to inspect afterward -- the test function then asserts on what
    got recorded, exactly per the pattern used by python-sc2's own example/
    test bots.

    #4-#8 (Protoss/Zerg macro, MCP execute_code, the autonomous script
    runtime) are all expected to subclass this rather than re-deriving the
    `self.bot`/`self.sdk` wiring.
    """

    async def on_start(self) -> None:
        self.bot = Bot(self)
        self.sdk = self

    async def on_end(self, game_result: Result) -> None:
        # self.bot only exists once on_start has run; guard for the (rare)
        # case where the game ends before that (e.g. an immediate forfeit).
        if hasattr(self, "bot"):
            self.bot.match_result = game_result
