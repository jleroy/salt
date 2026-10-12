"""
Unit tests for the salt CLI (salt.cli.salt.SaltCMD).
"""

import pytest

from salt.cli.salt import SaltCMD
from tests.support.mock import MagicMock, patch


def _fake_saltcmd():
    """
    A stand-in SaltCMD self with just the attributes _run_batch's non-static
    branch touches, so the method can be exercised without full CLI parsing.
    """
    fake = MagicMock()
    fake.config = {}
    fake.options.eauth = ""
    fake.options.static = False
    fake.options.batch = "100%"
    return fake


def _run_batch_exit_code(fake_batch):
    fake = _fake_saltcmd()
    with patch("salt.cli.batch.Batch", return_value=fake_batch):
        with pytest.raises(SystemExit) as exc:
            SaltCMD._run_batch(fake)
    return exc.value.code


def test_run_batch_no_minions_exits_nonzero():
    """
    Regression test for #57357.

    When a batch run matches zero minions, ``batch.run()`` yields nothing. The
    CLI must exit non-zero -- matching the non-batch path, which prints
    "No return received" and exits 2 -- instead of silently exiting 0. Pins the
    bug: before the fix the empty loop leaves ``retcode=0`` and the CLI exits 0.
    """
    fake_batch = MagicMock()
    fake_batch.run.return_value = iter([])
    fake_batch.minions = []
    assert _run_batch_exit_code(fake_batch) == 2


def test_run_batch_matched_minions_uses_highest_job_retcode():
    """
    Inverse of #57357: when minions match, the exit code is the highest job
    retcode seen, and the no-return path is not taken.
    """
    fake_batch = MagicMock()
    fake_batch.run.return_value = iter([({"m1": {}}, 0), ({"m2": {}}, 3)])
    fake_batch.minions = ["m1", "m2"]
    assert _run_batch_exit_code(fake_batch) == 3


def test_run_batch_matched_minions_success_exits_zero():
    """
    A successful batch that matched minions still exits 0 -- the fix must not
    regress the normal path.
    """
    fake_batch = MagicMock()
    fake_batch.run.return_value = iter([({"m1": {}}, 0)])
    fake_batch.minions = ["m1"]
    assert _run_batch_exit_code(fake_batch) == 0


def test_async_batch_handoff_leaves_publication_to_master():
    """The CLI submits all targets through auth without publishing a first lot."""
    cli = _fake_saltcmd()
    cli.config = {"fun": "test.ping", "arg": []}
    cli.options.batch = "1"
    cli.local_client.run_job.return_value = {"jid": "123", "minions": ["a", "b"]}
    helper = MagicMock()
    helper.gather_minions.return_value = (["a", "b"], None, [])
    with patch("salt.cli.batch.Batch", return_value=helper), patch(
        "salt.utils.jid.gen_jid", return_value="123"
    ), patch("salt.utils.stringutils.print_cli"), patch(
        "salt.utils.batch_state.write_batch_state"
    ) as write:
        SaltCMD._run_batch_async(cli, {"token": "secret"})
    cli.local_client.run_job.assert_called_once()
    kwargs = cli.local_client.run_job.call_args.kwargs
    assert kwargs["tgt"] == ["a", "b"]
    assert kwargs["token"] == "secret"
    assert kwargs["batch_handoff"]["batch"] == "1"
    write.assert_not_called()
