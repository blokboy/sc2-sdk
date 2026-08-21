"""This project's single integration-test harness.

Every later ticket's tests are expected to build on the `sc2_game_harness`
fixture defined here, per the spec's testing decision: "One primary seam:
integration tests exercise the SDK's public API against a real, locally-
running headless SC2 instance ... asserting on actual subsequent game state
... rather than mocking python-sc2."

Design notes:
  - `sc2_install` probes for a local SC2 client using install.paths (our own
    module, not sc2.paths.Paths) specifically because sc2.paths.Paths calls
    sys.exit(1) when nothing is found -- fine for a bot script, but it would
    kill the entire pytest process rather than letting a test skip cleanly.
  - If no local install is found, integration tests SKIP (not fail, not
    error) with a message telling a human what to run. This lets the suite
    stay green in environments (like a plain CI runner or this sandbox)
    that can't run a real SC2 client, while still being a real, non-mocked
    test wherever a working install is available.
"""

from __future__ import annotations

from typing import Callable

import pytest

from install.paths import Sc2Installation, find_installed_base
from sc2.data import Result


@pytest.fixture(scope="session")
def sc2_install() -> Sc2Installation:
    installation = find_installed_base()
    if installation is None:
        pytest.skip(
            "No local SC2 installation found (checked $SC2PATH, Battle.net's "
            "ExecuteInfo.txt, and this platform's default install directory). "
            "Run `python -m install.cli` (see README.md) to install one, then "
            "re-run the integration tests."
        )
    return installation


@pytest.fixture(scope="session")
def sc2_game_harness(sc2_install: Sc2Installation) -> Callable[..., Result]:
    """Returns a callable with the same signature as
    sdk.play.play_vs_builtin_ai, bound to a confirmed-present local install.

    Kept as a thin wrapper (rather than re-exporting play_vs_builtin_ai
    directly) so later tickets have one fixture name to depend on regardless
    of how the underlying play function's module/location evolves.
    """
    from sdk.play import play_vs_builtin_ai

    return play_vs_builtin_ai


@pytest.fixture(scope="session")
def sc2_verified_bot_harness(sc2_install: Sc2Installation) -> Callable[..., Result]:
    """Returns a callable with the same signature as
    sdk.runtime.run_bot_vs_builtin_ai, bound to a confirmed-present local
    install -- the harness for ticket #3's (and #4-#8's) verified `bot.*`/
    `sdk.*` API, as opposed to `sc2_game_harness`'s trivial do-nothing
    `_NullBot` walking skeleton.

    Deliberately a *second* fixture rather than a change to
    `sc2_game_harness`: that fixture and the walking-skeleton test it backs
    are the already-proven #2/#9 path and ticket #3's brief asks not to
    touch them. Any `BotAI` instance (typically a `sdk.bot.VerifiedBotAI`
    subclass) can be handed to the returned callable.
    """
    from sdk.runtime import run_bot_vs_builtin_ai

    return run_bot_vs_builtin_ai


@pytest.fixture(scope="session")
def sc2_bot_script_harness(sc2_install: Sc2Installation) -> Callable[..., Result]:
    """Returns a callable with the same signature as
    sdk.script_runner.run_bot_script, bound to a confirmed-present local
    install -- the harness for ticket #7's standalone bot-script runtime:
    discover a `BotAI` subclass at the documented `bots/<name>.py`
    location, load it, and run it to completion via
    `run_bot_vs_builtin_ai`/`sc2.main.run_game`, exactly like a real
    unattended script run would.
    """
    from sdk.script_runner import run_bot_script

    return run_bot_script


@pytest.fixture(scope="session")
def sc2_selfplay_harness(sc2_install: Sc2Installation) -> Callable[..., list[Result]]:
    """Returns a callable with the same signature as
    sdk.runtime.run_bot_vs_bot, bound to a confirmed-present local install --
    the harness for ticket #10's self-play mode: two `BotAI` instances
    (potentially two instances of the same class) playing each other
    directly, rather than either side facing the built-in AI.

    Deliberately a *new* fixture, not a change to `sc2_verified_bot_harness`:
    `run_bot_vs_bot` returns a two-element list of `Result` (one per side),
    not the single `Result` every other harness here returns -- a different
    enough shape that folding it into an existing fixture would be
    surprising for callers of those fixtures.
    """
    from sdk.runtime import run_bot_vs_bot

    return run_bot_vs_bot
