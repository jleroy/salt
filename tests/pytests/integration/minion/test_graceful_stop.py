"""
Integration coverage for the minion graceful-stop fixup (issue #70050 audit).

Three separate gaps are exercised here at process level via
pytest-salt-factories:

  Gap 1 -- ``MinionManager.stop_async`` now signals job-execution children
           in ``Minion.subprocess_list`` (not just ``process_manager``).
  Gap 2 -- ``SignalHandlingProcess._handle_signals`` invokes registered
           finalize methods before ``os._exit``; the minion registers
           ``salt.minion._remove_proc_file`` so proc files DO get removed
           on a clean shutdown.
  Gap 3 -- ``notify_systemd_stopping`` is called on entry to
           ``stop_async``. (No systemd here -- covered by unit tests and
           the pkg-level test.)

The observable end-to-end assertion for the whole set: after a graceful
SIGTERM to a minion running a long ``test.sleep`` job, the minion's
``<cachedir>/proc/`` is empty. Before the fix, that proc file survived.
"""

import os
import pathlib
import re
import signal
import time

import pytest
from saltfactories.utils import random_string

from tests.conftest import FIPS_TESTRUN

pytestmark = [
    pytest.mark.slow_test,
    pytest.mark.skip_on_windows(
        reason=(
            "graceful-stop signal delivery is a POSIX-signal path; the "
            "Windows service story is exercised by the pkg-tier test."
        )
    ),
]


def _wait_for(predicate, timeout=30, interval=0.1, msg="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {msg}")


@pytest.fixture
def running_minion(salt_master):
    """
    Fresh minion per test so the proc-dir assertion cannot be polluted by
    other test jobs.
    """
    # The session-scoped salt_minion_factory shares its cache across tests and
    # may contain stale proc files. Use a dedicated minion to isolate this check.
    factory = salt_master.salt_minion_daemon(
        random_string("graceful-stop-minion-"),
        overrides={
            "transport": salt_master.config["transport"],
            "fips_mode": FIPS_TESTRUN,
            "encryption_algorithm": "OAEP-SHA224" if FIPS_TESTRUN else "OAEP-SHA1",
            "signing_algorithm": "PKCS1v15-SHA224" if FIPS_TESTRUN else "PKCS1v15-SHA1",
        },
    )
    with factory.started(start_timeout=60):
        yield factory


def _minion_proc_dir(minion):
    """
    Return the proc directory from the minion's factory configuration.
    """
    cachedir = pathlib.Path(minion.config["cachedir"])
    return cachedir / "proc"


def test_graceful_stop_removes_proc_files_for_inflight_jobs(
    salt_cli, salt_master, running_minion
):
    """
    Publish a long-running ``test.sleep`` job, wait until the minion has
    written the proc file, SIGTERM the minion, then assert the proc dir
    is empty after the minion has exited.

    Before the fixup:
      * ``stop_async`` never signaled the job child (Gap 1) so the
        proc file persisted until systemd cgroup escalation.
      * Even when the child DID receive SIGTERM, ``_handle_signals``
        called ``os._exit`` and skipped ``_thread_return``'s
        ``finally: os.remove(fn_)`` (Gap 2).

    Post-fix, both paths converge on a clean proc dir.
    """
    proc_dir = _minion_proc_dir(running_minion)
    # Publish a long sleep via ``--async`` so the CLI returns immediately
    # with a jid. The point is to have the job child running on the
    # minion when we deliver SIGTERM.
    dispatch = salt_cli.run(
        "test.sleep",
        "30",
        "--async",
        minion_tgt=running_minion.id,
    )
    assert dispatch.returncode == 0, f"async dispatch failed: {dispatch}"

    jid_match = re.search(r"Executed command with job ID: (\d+)", dispatch.stdout)
    assert jid_match, f"async dispatch did not return a jid: {dispatch}"
    proc_file = proc_dir / jid_match.group(1)

    # Wait for this job, not a proc file left behind by an earlier job.
    _wait_for(
        proc_file.is_file,
        timeout=20,
        msg="proc file to appear",
    )
    proc_files_before = {p.name for p in proc_dir.iterdir()}
    assert proc_files_before, "precondition: expected at least one in-flight proc file"

    # Signal only the parent so factory cleanup cannot hide orphaned jobs.
    os.kill(running_minion.pid, signal.SIGTERM)
    _wait_for(
        lambda: not running_minion.is_running(),
        timeout=20,
        msg="minion to exit after SIGTERM",
    )

    # The proc dir must be empty after the minion has exited cleanly.
    assert not running_minion.is_running(), "minion did not exit after SIGTERM"
    remaining = [p.name for p in proc_dir.iterdir()] if proc_dir.exists() else []
    assert not remaining, (
        f"proc files survived graceful stop: {remaining!r}; "
        f"before-stop set was {proc_files_before!r}"
    )
