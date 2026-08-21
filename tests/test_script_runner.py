"""Ticket #7: fast, non-integration tests for the standalone bot-script
discovery/loading mechanics in `sdk.script_runner` -- no SC2 client
involved, unlike `tests/integration/test_bot_script_runtime.py`'s live-game
wiring test. These cover the convention's edge cases (missing script, zero
BotAI subclasses, multiple BotAI subclasses) that would be slow and awkward
to exercise by actually booting a game for each one.
"""

from __future__ import annotations

import pytest
from sc2.bot_ai import BotAI

from sdk.bot import VerifiedBotAI
from sdk.script_runner import BOTS_DIR, load_bot_class, resolve_script_path


def test_bots_dir_is_repo_root_bots_directory() -> None:
    assert BOTS_DIR.is_dir()
    assert BOTS_DIR.name == "bots"
    assert (BOTS_DIR / "idle_example.py").is_file()


def test_resolve_script_path_by_name_hits_documented_location() -> None:
    resolved = resolve_script_path("idle_example")
    assert resolved == (BOTS_DIR / "idle_example.py").resolve()


def test_resolve_script_path_unknown_name_raises() -> None:
    with pytest.raises(FileNotFoundError, match="No bot script found"):
        resolve_script_path("does_not_exist_as_a_bot")


def test_resolve_script_path_accepts_literal_py_path(tmp_path) -> None:
    script = tmp_path / "adhoc.py"
    script.write_text("from sdk.bot import VerifiedBotAI\n\nclass Adhoc(VerifiedBotAI):\n    pass\n")
    assert resolve_script_path(str(script)) == script.resolve()


def test_load_bot_class_finds_the_one_defined_class() -> None:
    bot_class = load_bot_class(BOTS_DIR / "idle_example.py")
    assert bot_class.__name__ == "IdleExampleBot"
    assert issubclass(bot_class, VerifiedBotAI)
    assert issubclass(bot_class, BotAI)


def test_load_bot_class_ignores_merely_imported_classes(tmp_path) -> None:
    # Importing VerifiedBotAI/BotAI into the script's namespace must not
    # itself count as "a BotAI subclass defined in this script" -- only a
    # class this script actually declares should be a discovery candidate.
    script = tmp_path / "just_imports.py"
    script.write_text(
        "from sdk.bot import VerifiedBotAI\n"
        "from sc2.bot_ai import BotAI\n"
        "\n"
        "class MyBot(VerifiedBotAI):\n"
        "    pass\n"
    )
    bot_class = load_bot_class(script)
    assert bot_class.__name__ == "MyBot"
    assert bot_class.__module__ != "sdk.bot"


def test_load_bot_class_zero_candidates_raises(tmp_path) -> None:
    script = tmp_path / "no_bot.py"
    script.write_text("x = 1\n")
    with pytest.raises(ValueError, match="does not define a BotAI subclass"):
        load_bot_class(script)


def test_load_bot_class_multiple_candidates_raises(tmp_path) -> None:
    script = tmp_path / "two_bots.py"
    script.write_text(
        "from sdk.bot import VerifiedBotAI\n"
        "\n"
        "class FirstBot(VerifiedBotAI):\n"
        "    pass\n"
        "\n"
        "class SecondBot(VerifiedBotAI):\n"
        "    pass\n"
    )
    with pytest.raises(ValueError, match="defines multiple BotAI subclasses"):
        load_bot_class(script)
