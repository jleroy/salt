"""Tests for state compilation on the Salt SSH controller."""

import pytest

from salt.client.ssh.state import SSHHighState


@pytest.mark.parametrize("separate_master_extensions", [False, True])
def test_master_tops_extension_path(tmp_path, minion_opts, separate_master_extensions):
    """Load tops locally while preserving the target's identity and extension path."""
    remote_extensions = tmp_path / "remote-extmods"
    master_extensions = tmp_path / "master-extmods"
    for root, label in ((remote_extensions, "remote"), (master_extensions, "master")):
        tops = root / "tops"
        tops.mkdir(parents=True)
        (tops / "ssh_test.py").write_text(
            "def top(opts, grains, **kwargs):\n"
            f"    return {{'base': ['{label}', opts['id'], grains['role']]}}\n"
        )
    opts = dict(minion_opts)
    opts.update(
        id="ssh-target",
        grains={"role": "web"},
        extension_modules=str(remote_extensions),
        master_tops={"ssh_test": True},
    )
    if separate_master_extensions:
        opts["__master_opts__"] = {
            "id": "controller",
            "extension_modules": str(master_extensions),
        }
    highstate = SSHHighState.__new__(SSHHighState)
    highstate.opts = opts

    expected_source = "master" if separate_master_extensions else "remote"
    assert highstate._master_tops() == {"base": [expected_source, "ssh-target", "web"]}
    assert opts["extension_modules"] == str(remote_extensions)
