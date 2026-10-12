import os
import shlex
import subprocess
import sys
import time

import pytest

import salt.payload
import salt.utils.files
import salt.utils.state
import salt.utils.yaml


@pytest.fixture(scope="module")
def long_running_sls(base_env_state_tree_root_dir, tmp_path_factory):
    sls_name = "long_running"
    sls_dir = base_env_state_tree_root_dir
    sls_file = sls_dir / f"{sls_name}.sls"

    sls_content = """
    long_running_sleep:
      cmd.run:
        - name: sleep 20
    """
    with salt.utils.files.fopen(sls_file, "w", encoding="utf-8") as f:
        f.write(sls_content)

    yield sls_name


@pytest.fixture(scope="module")
def quick_sls(base_env_state_tree_root_dir, tmp_path_factory):
    sls_name = "quick"
    sls_dir = base_env_state_tree_root_dir
    sls_file = sls_dir / f"{sls_name}.sls"
    target_path = tmp_path_factory.mktemp("state_queue") / "quick_ran.txt"

    sls_content = f"""
    quick_run:
      file.touch:
        - name: {target_path}
    """
    with salt.utils.files.fopen(sls_file, "w", encoding="utf-8") as f:
        f.write(sls_content)

    yield sls_name, target_path


def test_state_queue_basic(salt_cli, salt_minion, quick_sls):
    """
    Test that state.apply with queue=True works correctly.
    This demonstrates the basic queuing functionality works.
    """

    quick_sls_name, quick_target_path = quick_sls

    # Ensure target doesn't exist
    if quick_target_path.exists():
        quick_target_path.unlink()

    # Step 1: Run a state job with queue=True
    # Since no conflicts exist, it should execute immediately
    ret = salt_cli.run(
        "state.apply",
        quick_sls_name,
        "queue=True",
        minion_tgt=salt_minion.id,
    )

    # Should execute immediately (no conflicts)
    assert ret.returncode == 0, f"Job failed: {ret}"
    assert quick_target_path.exists(), "Job should have executed immediately"

    # Step 2: Verify no files were left in state_queue
    state_queue_dir = salt.utils.state.state_queue_dir(salt_minion.config)
    if os.path.exists(state_queue_dir):
        files = os.listdir(state_queue_dir)
        queued_files = [
            f for f in files if f.startswith("queued_") and f.endswith(".p")
        ]
        assert len(queued_files) == 0, f"Unexpected queued files found: {queued_files}"


@pytest.fixture
def blocking_sls(base_env_state_tree_root_dir, tmp_path):
    """
    Provide a state that signals startup and waits for a release file.

    Yield the SLS name and the startup and release marker paths so the test
    can keep a state running while inspecting a second job's queued payload.
    Bound the wait and release the state during teardown to avoid leaving
    a blocked process behind if the test fails.
    """
    started = tmp_path / "started"
    release = tmp_path / "release"
    script = tmp_path / "wait_for_release.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).touch()\n"
        "deadline = time.monotonic() + 120\n"
        "while not pathlib.Path(sys.argv[2]).exists():\n"
        "    if time.monotonic() >= deadline:\n"
        "        raise TimeoutError('State was not released')\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    command = [sys.executable, str(script), str(started), str(release)]
    command = (
        subprocess.list2cmdline(command)
        if sys.platform == "win32"
        else shlex.join(command)
    )
    sls_file = base_env_state_tree_root_dir / "queue_blocking.sls"
    sls_file.write_text(
        salt.utils.yaml.safe_dump({"queue_blocking": {"cmd.run": [{"name": command}]}}),
        encoding="utf-8",
    )
    try:
        yield "queue_blocking", started, release
    finally:
        release.touch()
        sls_file.unlink()


def _read_queued_payload(state_queue_dir):
    """
    Return the (filename, payload_dict) tuple for the single queued file
    in ``state_queue_dir``, or (None, None) if none is present.
    """
    if not os.path.exists(state_queue_dir):
        return None, None
    for fn in sorted(os.listdir(state_queue_dir)):
        if fn.startswith("queued_") and fn.endswith(".p"):
            path = os.path.join(state_queue_dir, fn)
            with salt.utils.files.fopen(path, "rb") as fp_:
                return fn, salt.payload.load(fp_)
    return None, None


@pytest.mark.slow_test
def test_queued_state_preserves_master_jid_69386(
    salt_cli, salt_minion, salt_master, event_listener, blocking_sls, quick_sls
):
    """
    Regression test for #69386.

    When a state run is queued because another state run is already in
    flight, the minion must shelve and re-execute the queued payload
    under the JID the master published, not under a freshly-minted JID.
    Job-tracking infrastructure on the master (returners, the jobs
    runner, syndic forwarding) keys on the published JID; if the minion
    swaps it out, returns come back under a JID the master never
    published and the run is invisible to ``jobs.lookup_jid``.

    The bug was at the write site
    (``salt.modules.state._check_queue``), which called
    ``salt.utils.jid.gen_jid()`` and stamped the new value into both the
    serialized payload and the queue filename. We assert directly on the
    on-disk artefact -- that's where the bug lives, so that's the most
    deterministic place to test it.

    Steps:

    1. Fire a slow state job ``--async`` to occupy the state slot, and
       capture its master-assigned JID.
    2. Fire a second state job with ``queue=True`` (also ``--async`` so
       the publisher doesn't block on the queued run). Capture its JID.
    3. While the slow job is still running, read the file the minion
       wrote into the state_queue directory. Assert that both the JID
       embedded in the filename and the ``jid`` field inside the
       msgpack-serialized payload equal the second job's master-
       assigned JID. Under the bug both would be a freshly-minted JID
       generated by the minion.
    """
    blocking_sls_name, started, release = blocking_sls
    start_time = time.time()
    quick_sls_name, quick_target_path = quick_sls

    # Clean slate -- the module-scoped fixtures may have left files
    # around from a prior test in the file.
    if quick_target_path.exists():
        quick_target_path.unlink()
    state_queue_dir = salt.utils.state.state_queue_dir(salt_minion.config)
    if os.path.exists(state_queue_dir):
        for fn in os.listdir(state_queue_dir):
            try:
                os.remove(os.path.join(state_queue_dir, fn))
            except OSError:
                pass

    # 1) Fire the slow state job async, capture its master JID.
    slow_ret = salt_cli.run(
        "state.apply",
        blocking_sls_name,
        "--async",
        minion_tgt=salt_minion.id,
    )
    assert slow_ret.returncode == 0, f"async dispatch failed: {slow_ret}"
    # ``--async`` stdout is e.g. "Executed command with job ID: <jid>"
    slow_jid = slow_ret.stdout.strip().split()[-1]
    assert slow_jid.isdigit(), f"unexpected async stdout: {slow_ret.stdout!r}"

    queued_jid = None
    try:
        # macOS process startup can outlast a fixed sleep. Wait for the state
        # to start, then hold it until the second job is demonstrably queued.
        deadline = time.monotonic() + 60
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert started.exists(), "The blocking state did not start"

        # 2) Fire the second state job with queue=True, also async so the
        #    publish doesn't block. Capture its master JID.
        queued_ret = salt_cli.run(
            "state.apply",
            quick_sls_name,
            "queue=True",
            "--async",
            minion_tgt=salt_minion.id,
        )
        assert queued_ret.returncode == 0, f"queue=True dispatch failed: {queued_ret}"
        queued_jid = queued_ret.stdout.strip().split()[-1]
        assert queued_jid.isdigit(), f"unexpected async stdout: {queued_ret.stdout!r}"
        assert queued_jid != slow_jid

        # Keep the first state blocked until the queued payload has been inspected.
        queued_fn = None
        queued_payload = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            queued_fn, queued_payload = _read_queued_payload(state_queue_dir)
            if queued_payload is not None:
                break
            time.sleep(0.1)

        assert queued_payload is not None, (
            f"no queued file appeared in {state_queue_dir}; the second state "
            "run did not hit the state-queue conflict path -- test scaffolding "
            "problem, not #69386"
        )

        # The whole point of #69386: the on-disk payload's jid must equal
        # the master-published jid, NOT a minion-minted one.
        assert queued_payload["jid"] == queued_jid, (
            f"queued payload jid {queued_payload['jid']!r} does not match the "
            f"master-published jid {queued_jid!r} -- minion re-stamped the JID "
            "(#69386 regression)"
        )
        # And the filename must agree, because the drain side parses the JID
        # back out of the filename for ordering.
        assert queued_fn.endswith(f"_{queued_jid}.p"), (
            f"queued filename {queued_fn!r} does not embed the master-published "
            f"jid {queued_jid!r} (#69386 regression)"
        )

    finally:
        release.touch()
        jids = [slow_jid]
        if queued_jid is not None:
            jids.append(queued_jid)
        returns = event_listener.wait_for_events(
            [(salt_master.id, f"salt/job/{jid}/ret/{salt_minion.id}") for jid in jids],
            after_time=start_time,
            timeout=60,
        )
        assert returns.found_all_events, "State jobs did not finish after release"

    assert quick_target_path.exists(), "The queued state did not execute"


def test_state_queue_true(salt_cli, salt_minion, quick_sls):
    """
    Test that state.apply with queue=True works correctly.
    Since creating real conflicts is complex and timing-dependent,
    this test verifies that queuing doesn't break normal execution.
    """

    quick_sls_name, quick_target_path = quick_sls

    # Ensure target doesn't exist
    if quick_target_path.exists():
        quick_target_path.unlink()

    # Run a state job with queue=True when no conflicts exist
    # It should execute immediately
    ret = salt_cli.run(
        "state.apply",
        quick_sls_name,
        "queue=True",
        minion_tgt=salt_minion.id,
    )

    # Should execute successfully (no conflicts to queue against)
    assert ret.returncode == 0, f"Job failed: {ret}"
    assert quick_target_path.exists(), "Job should have executed"

    # Verify no files were left in state_queue (since it executed immediately)
    state_queue_dir = salt.utils.state.state_queue_dir(salt_minion.config)
    if os.path.exists(state_queue_dir):
        files = os.listdir(state_queue_dir)
        queued_files = [
            f for f in files if f.startswith("queued_") and f.endswith(".p")
        ]
        assert len(queued_files) == 0, f"Unexpected queued files found: {queued_files}"
