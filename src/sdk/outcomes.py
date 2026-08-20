"""Return types for `bot.py`'s verified `Bot.*` actions.

Every verified action returns one of these instead of the underlying
python-sc2 call's raw bool/int, so that a caller (an agent, or a test) can
distinguish three states instead of two:

  1. the command was rejected outright (`ok=False`) -- with `error` set to
     a clear, actionable message (insufficient resources, illegal
     placement, unknown unit tag, tech requirement not met, ...);
  2. the command was accepted and a *subsequent* observation confirms the
     intended effect actually happened (`ok=True`, `effect_confirmed=True`);
  3. the command was accepted but a subsequent observation could *not*
     confirm the effect within the verification window (`ok=True`,
     `effect_confirmed=False`) -- this is not necessarily a bug (e.g. a
     structure that legitimately takes longer to complete than the
     window this action polled for) but it is reported honestly rather
     than assumed.

`detail` is always a short human-readable sentence explaining what was
observed, useful for logging/debugging regardless of which state above
applies.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrainOutcome:
    ok: bool
    effect_confirmed: bool
    error: str | None
    detail: str
    unit_type: str
    requested_amount: int
    dispatched_amount: int
    new_unit_tags: tuple[int, ...]


@dataclass(frozen=True)
class BuildOutcome:
    ok: bool
    effect_confirmed: bool
    error: str | None
    detail: str
    structure_type: str
    position: tuple[float, float] | None
    structure_tag: int | None


@dataclass(frozen=True)
class ResearchOutcome:
    ok: bool
    effect_confirmed: bool
    error: str | None
    detail: str
    upgrade_type: str


@dataclass(frozen=True)
class MoveOutcome:
    ok: bool
    effect_confirmed: bool
    error: str | None
    detail: str
    mode: str  # "move" | "attack_move"
    requested_tags: tuple[int, ...]
    confirmed_tags: tuple[int, ...]


@dataclass(frozen=True)
class ChatOutcome:
    ok: bool
    effect_confirmed: bool
    error: str | None
    detail: str
    message: str
