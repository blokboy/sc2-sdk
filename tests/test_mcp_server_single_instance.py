"""Fast, non-integration tests for `sdk.mcp_server`'s single-instance guard
-- see that module's "Single-instance guard + explicit SC2-client PID
tracking" section for the full design this exercises.

Why real dummy subprocesses, not mocked process discovery
------------------------------------------------------------
`_terminate_stale_instance` doesn't do any process *discovery* of its own
(no `ps`/`psutil` scan for candidates) -- it only ever acts on PIDs an
already-parsed lockfile names explicitly, then verifies each one against
`psutil` before touching it (liveness + `cmdline()` identity). That means
the realistic way to exercise it end-to-end is to hand it a real lockfile
naming real PIDs and let it run its real `psutil`-based verification and
`SIGTERM`/`SIGKILL` escalation against them -- mocking `psutil` here would
mostly just be re-asserting this module's own mocked expectations back at
itself, rather than proving the actual liveness/identity/signal-escalation
logic works. So every test below spawns real, lightweight, short-lived
`python -c ...` subprocesses (never a real `sc2-sdk-mcp` or real SC2
client -- both would be slow/flaky/require a local install) with a
recognizable marker string embedded directly in the `-c` source text, which
lands verbatim in `psutil.Process(pid).cmdline()` and lets
`_is_sc2_sdk_mcp_process`/`_looks_like_sc2_client_process` match it exactly
the way they'd match a real `sc2-sdk-mcp`/SC2-client invocation, without
needing a real one. Every spawned process is explicitly terminated and
reaped in a `finally` block regardless of test outcome, so nothing leaks
even if an assertion fails or `_terminate_stale_instance` doesn't kill it
(the "must survive" test below expects exactly that).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from install.paths import BINARY_NAME, platform_name
from sdk.mcp_server import (
    DEFAULT_LOCKFILE_PATH,
    _is_sc2_sdk_mcp_process,
    _lockfile_path_for,
    _looks_like_sc2_client_process,
    _parse_args,
    _read_lockfile,
    _terminate_stale_instance,
    _write_lockfile,
)

#: Short bounded waits so this test module stays fast -- the dummy
#: processes below install no signal handlers of their own, so they die
#: on SIGTERM almost immediately; these just bound the worst case.
_TERMINATE_WAIT_SECONDS = 2.0
_KILL_WAIT_SECONDS = 1.0

#: A `cmdline()`-visible marker for "this dummy process should be matched by
#: _is_sc2_sdk_mcp_process", embedded as a trailing comment in the `-c`
#: source text so it appears verbatim in the process's argv.
_MCP_MARKER = "sc2-sdk-mcp"

#: Same idea for "this dummy process should be matched by
#: _looks_like_sc2_client_process" -- the current platform's actual SC2
#: binary marker, so this test stays correct across platforms without
#: hardcoding "SC2"/"SC2_x64"/etc.
_SC2_CLIENT_MARKER = Path(BINARY_NAME[platform_name()]).name


def _spawn_dummy(marker: str) -> subprocess.Popen:
    """A real, short-lived (5 minutes, generous relative to this test
    module's own timeouts) subprocess whose `cmdline()` contains `marker`
    verbatim, installs no custom signal handlers (so it dies immediately on
    SIGTERM, keeping this test module fast), and does nothing else."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep(300)  # {marker}"],
    )


def _reap(proc: subprocess.Popen) -> None:
    """Best-effort cleanup for a dummy spawned above: SIGKILL then reap, so
    a test failure (or a dummy `_terminate_stale_instance` deliberately
    left alive) never leaks a real process past this test."""
    if proc.poll() is None:
        with contextlib.suppress(Exception):
            proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


def test_identity_helpers_match_their_own_markers() -> None:
    """Sanity-check the two identity predicates directly, independent of
    `_terminate_stale_instance`, against real dummy processes."""
    mcp_dummy = _spawn_dummy(_MCP_MARKER)
    sc2_dummy = _spawn_dummy(_SC2_CLIENT_MARKER)
    try:
        assert _is_sc2_sdk_mcp_process(mcp_dummy.pid)
        assert not _looks_like_sc2_client_process(mcp_dummy.pid)
        assert _looks_like_sc2_client_process(sc2_dummy.pid)
        assert not _is_sc2_sdk_mcp_process(sc2_dummy.pid)
        # A pid that (almost certainly) isn't live at all matches neither.
        dead_pid = mcp_dummy.pid
        mcp_dummy.kill()
        mcp_dummy.wait(timeout=5)
        assert not psutil.pid_exists(dead_pid) or not _is_sc2_sdk_mcp_process(dead_pid)
    finally:
        _reap(mcp_dummy)
        _reap(sc2_dummy)


def test_lockfile_round_trip(tmp_path: Path) -> None:
    lockfile_path = tmp_path / "sc2-sdk-mcp.lock"
    _write_lockfile(lockfile_path, mcp_pid=4321, sc2_pids={111, 222})
    data = _read_lockfile(lockfile_path)
    assert data == {"mcp_pid": 4321, "sc2_pids": [111, 222]}


def test_no_lockfile_is_a_noop(tmp_path: Path) -> None:
    lockfile_path = tmp_path / "sc2-sdk-mcp.lock"
    assert not lockfile_path.exists()

    report = _terminate_stale_instance(lockfile_path, own_pid=99999)

    assert report == {
        "stale_mcp_pid": None,
        "stale_mcp_terminated": False,
        "sc2_pids_terminated": [],
        "sc2_pids_skipped": [],
    }


def test_stale_mcp_instance_is_terminated(tmp_path: Path) -> None:
    """(b) from the ticket: a genuinely stale instance gets terminated."""
    lockfile_path = tmp_path / "sc2-sdk-mcp.lock"
    stale_mcp = _spawn_dummy(_MCP_MARKER)
    try:
        _write_lockfile(lockfile_path, mcp_pid=stale_mcp.pid, sc2_pids=set())

        report = _terminate_stale_instance(
            lockfile_path,
            own_pid=99999,
            terminate_wait_seconds=_TERMINATE_WAIT_SECONDS,
            kill_wait_seconds=_KILL_WAIT_SECONDS,
        )

        assert report["stale_mcp_pid"] == stale_mcp.pid
        assert report["stale_mcp_terminated"] is True
        # Confirmed via a real wait(), not just the report: the process is
        # actually gone, not merely believed gone.
        assert stale_mcp.wait(timeout=5) is not None
    finally:
        _reap(stale_mcp)


def test_stale_sc2_client_pid_is_terminated_when_explicitly_tracked(tmp_path: Path) -> None:
    """The refinement's core scenario: a stale mcp instance's OWN recorded
    SC2 client PID (tracked explicitly in the lockfile, exactly like
    `_track_sc2_pid` would have written it) is cleaned up too -- this is
    the fallback for the confirmed root-cause finding that SIGTERM to the
    stale mcp process does NOT trigger SC2Process's SIGINT-only cleanup."""
    lockfile_path = tmp_path / "sc2-sdk-mcp.lock"
    stale_mcp = _spawn_dummy(_MCP_MARKER)
    stale_sc2_client = _spawn_dummy(_SC2_CLIENT_MARKER)
    try:
        _write_lockfile(lockfile_path, mcp_pid=stale_mcp.pid, sc2_pids={stale_sc2_client.pid})

        report = _terminate_stale_instance(
            lockfile_path,
            own_pid=99999,
            terminate_wait_seconds=_TERMINATE_WAIT_SECONDS,
            kill_wait_seconds=_KILL_WAIT_SECONDS,
        )

        assert report["stale_mcp_terminated"] is True
        assert report["sc2_pids_terminated"] == [stale_sc2_client.pid]
        assert report["sc2_pids_skipped"] == []
        assert stale_mcp.wait(timeout=5) is not None
        assert stale_sc2_client.wait(timeout=5) is not None
    finally:
        _reap(stale_mcp)
        _reap(stale_sc2_client)


def test_untracked_sc2_look_alike_process_is_never_touched(tmp_path: Path) -> None:
    """(c) from the ticket, made explicit per the refinement: a process
    that looks exactly like an SC2 client (matches
    `_looks_like_sc2_client_process`) but was NEVER recorded in any
    lockfile's `sc2_pids` -- e.g. a user's own, manually-started SC2 client
    running at the same time as an unrelated stale sc2-sdk-mcp instance --
    must survive completely untouched. `_terminate_stale_instance` only
    ever acts on PIDs its lockfile explicitly names; it must never scan for
    or infer "processes that look like an SC2 client" on its own."""
    lockfile_path = tmp_path / "sc2-sdk-mcp.lock"
    stale_mcp = _spawn_dummy(_MCP_MARKER)
    untracked_sc2_look_alike = _spawn_dummy(_SC2_CLIENT_MARKER)
    try:
        # Note: untracked_sc2_look_alike.pid is deliberately NOT listed here.
        _write_lockfile(lockfile_path, mcp_pid=stale_mcp.pid, sc2_pids=set())

        report = _terminate_stale_instance(
            lockfile_path,
            own_pid=99999,
            terminate_wait_seconds=_TERMINATE_WAIT_SECONDS,
            kill_wait_seconds=_KILL_WAIT_SECONDS,
        )

        assert report["stale_mcp_terminated"] is True
        assert report["sc2_pids_terminated"] == []
        # Still alive: never named in the lockfile, so never a candidate.
        assert untracked_sc2_look_alike.poll() is None
        assert psutil.pid_exists(untracked_sc2_look_alike.pid)
    finally:
        _reap(stale_mcp)
        _reap(untracked_sc2_look_alike)


def test_pid_reused_by_unrelated_process_is_ignored(tmp_path: Path) -> None:
    """PID-reuse guard: a lockfile can outlive the process it describes. If
    the recorded `mcp_pid` is alive but its `cmdline()` no longer looks
    like an sc2-sdk-mcp process (the OS handed that PID to something
    unrelated in the meantime), it must not be terminated."""
    lockfile_path = tmp_path / "sc2-sdk-mcp.lock"
    unrelated = _spawn_dummy("totally-unrelated-python-process")
    try:
        _write_lockfile(lockfile_path, mcp_pid=unrelated.pid, sc2_pids=set())

        report = _terminate_stale_instance(
            lockfile_path,
            own_pid=99999,
            terminate_wait_seconds=_TERMINATE_WAIT_SECONDS,
            kill_wait_seconds=_KILL_WAIT_SECONDS,
        )

        assert report["stale_mcp_pid"] is None
        assert report["stale_mcp_terminated"] is False
        assert unrelated.poll() is None
    finally:
        _reap(unrelated)


def test_own_pid_is_never_treated_as_stale(tmp_path: Path) -> None:
    """A lockfile that happens to name this very process's own pid (e.g. a
    prior run of this same process, before it re-claimed the lockfile)
    must never cause self-termination."""
    lockfile_path = tmp_path / "sc2-sdk-mcp.lock"
    _write_lockfile(lockfile_path, mcp_pid=os.getpid(), sc2_pids=set())

    report = _terminate_stale_instance(lockfile_path, own_pid=os.getpid())

    assert report["stale_mcp_pid"] is None
    assert report["stale_mcp_terminated"] is False


@pytest.mark.parametrize("stale_sc2_pid_alive", [True, False])
def test_dead_recorded_sc2_pid_is_skipped_not_terminated(tmp_path: Path, stale_sc2_pid_alive: bool) -> None:
    """A recorded `sc2_pids` entry that's already dead by the time the
    guard runs (the common, non-buggy case: the game ended and
    SC2Process's own cleanup already tore it down) is reported as
    "skipped", not "terminated" -- there's nothing to do, and
    `_terminate_pid` should never be invoked against a pid that doesn't
    exist."""
    lockfile_path = tmp_path / "sc2-sdk-mcp.lock"
    stale_mcp = _spawn_dummy(_MCP_MARKER)
    sc2_dummy = _spawn_dummy(_SC2_CLIENT_MARKER) if stale_sc2_pid_alive else None
    try:
        if stale_sc2_pid_alive:
            dead_pid = sc2_dummy.pid
        else:
            # A pid that's already gone before we even start.
            throwaway = _spawn_dummy(_SC2_CLIENT_MARKER)
            throwaway.kill()
            throwaway.wait(timeout=5)
            dead_pid = throwaway.pid

        _write_lockfile(lockfile_path, mcp_pid=stale_mcp.pid, sc2_pids={dead_pid})

        if stale_sc2_pid_alive:
            # Kill it for real right before running the guard, simulating
            # "already torn down by the time the guard gets to it".
            sc2_dummy.kill()
            sc2_dummy.wait(timeout=5)

        report = _terminate_stale_instance(
            lockfile_path,
            own_pid=99999,
            terminate_wait_seconds=_TERMINATE_WAIT_SECONDS,
            kill_wait_seconds=_KILL_WAIT_SECONDS,
        )

        assert report["sc2_pids_terminated"] == []
        assert report["sc2_pids_skipped"] == [dead_pid]
    finally:
        _reap(stale_mcp)
        if sc2_dummy is not None:
            _reap(sc2_dummy)


# ---------------------------------------------------------------------------
# --multiplayer opt-in: per-instance lockfile scoping (ticket: "Multiplayer
# opt-in flag for the single-instance guard"). See _lockfile_path_for's
# docstring for why this is keyed by a caller-supplied id, not by PID.
# ---------------------------------------------------------------------------


def test_lockfile_path_for_default_is_unchanged() -> None:
    assert _lockfile_path_for(None) == DEFAULT_LOCKFILE_PATH


def test_lockfile_path_for_different_ids_are_distinct_paths() -> None:
    host_path = _lockfile_path_for("host")
    join_path = _lockfile_path_for("join")
    assert host_path != join_path
    assert host_path != DEFAULT_LOCKFILE_PATH
    assert join_path != DEFAULT_LOCKFILE_PATH


def test_lockfile_path_for_same_id_is_stable_across_calls() -> None:
    """Same id -> same path every time -- required for a relaunch under the
    same id (e.g. a harness reconnect) to find its own predecessor's
    lockfile, exactly like the default guard does."""
    assert _lockfile_path_for("host") == _lockfile_path_for("host")


def test_lockfile_path_for_strips_directory_components() -> None:
    """A multiplayer id containing path separators must not let the caller
    point the lockfile outside the usual lockfile directory."""
    path = _lockfile_path_for("../../etc/host")
    assert path.parent == DEFAULT_LOCKFILE_PATH.parent
    assert ".." not in path.name


def test_parse_args_multiplayer_defaults_to_none() -> None:
    args = _parse_args([])
    assert args.multiplayer is None


def test_parse_args_multiplayer_accepts_instance_id() -> None:
    args = _parse_args(["--multiplayer", "host"])
    assert args.multiplayer == "host"


def test_multiplayer_instances_with_different_ids_do_not_cross_terminate(tmp_path: Path) -> None:
    """(a)/(c) from the ticket: a process recorded stale under one
    --multiplayer id's lockfile must not be touched by a guard run scoped
    to a DIFFERENT id's lockfile -- proving two concurrently-launched
    `sc2-sdk-mcp --multiplayer` instances (e.g. one hosting, one joining a
    two-LLM match) can never terminate each other, because each only ever
    reads its own lockfile path."""
    host_lock = tmp_path / _lockfile_path_for("host").name
    join_lock = tmp_path / _lockfile_path_for("join").name
    host_stale = _spawn_dummy(_MCP_MARKER)
    try:
        _write_lockfile(host_lock, mcp_pid=host_stale.pid, sc2_pids=set())

        # A "join" instance starting up only ever consults its OWN lockfile
        # path -- it must never discover, let alone terminate, whatever is
        # recorded under "host"'s.
        report = _terminate_stale_instance(join_lock, own_pid=99999)

        assert report["stale_mcp_pid"] is None
        assert report["stale_mcp_terminated"] is False
        assert host_stale.poll() is None  # still alive and untouched
    finally:
        _reap(host_stale)


def test_default_mode_and_multiplayer_instance_do_not_cross_terminate(tmp_path: Path) -> None:
    """A mix (one default-mode instance, one --multiplayer instance) must
    not cross-terminate either, since they use different lockfile paths."""
    default_lock = tmp_path / DEFAULT_LOCKFILE_PATH.name
    multiplayer_lock = tmp_path / _lockfile_path_for("host").name
    default_stale = _spawn_dummy(_MCP_MARKER)
    try:
        _write_lockfile(default_lock, mcp_pid=default_stale.pid, sc2_pids=set())

        report = _terminate_stale_instance(multiplayer_lock, own_pid=99999)

        assert report["stale_mcp_terminated"] is False
        assert default_stale.poll() is None
    finally:
        _reap(default_stale)


def test_reconnect_under_same_multiplayer_id_still_cleans_up_stale_instance(tmp_path: Path) -> None:
    """A --multiplayer instance relaunched under the SAME id (e.g. the
    harness restarting the host side after a reconnect) still gets the
    ordinary stale-cleanup guarantee -- only DIFFERENT ids are mutually
    invisible to each other, not same-id relaunches."""
    host_lock = tmp_path / _lockfile_path_for("host").name
    stale_host = _spawn_dummy(_MCP_MARKER)
    try:
        _write_lockfile(host_lock, mcp_pid=stale_host.pid, sc2_pids=set())

        report = _terminate_stale_instance(host_lock, own_pid=99999)

        assert report["stale_mcp_terminated"] is True
        assert stale_host.wait(timeout=5) is not None
    finally:
        _reap(stale_host)
