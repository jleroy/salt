import sqlite3
from pathlib import Path

import pytest

import salt.modules.mac_assistive as assistive
from salt.exceptions import CommandExecutionError
from tests.support.mock import patch
from tests.support.runtests import RUNTIME_VARS

# Schemas extracted from tccd, including the admin version used for dispatch.
SCHEMAS = sorted(
    (Path(RUNTIME_VARS.TESTS_DIR) / "unit" / "files" / "tcc").glob("*.sql")
)


@pytest.fixture(autouse=True, params=SCHEMAS, ids=lambda path: path.stem)
def tcc_db_path(tmp_path, request):
    db = tmp_path / "tcc.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(request.param.read_text())
    finally:
        conn.close()
    return str(db)


@pytest.fixture
def configure_loader_modules(tcc_db_path):
    return {assistive: {"TCC_DB_PATH": tcc_db_path}}


def test_install_assistive_bundle():
    """
    Test installing a bundle ID as being allowed to run with assistive access
    """
    assert assistive.install("foo")


def test_install_assistive_error():
    """
    Test installing a bundle ID as being allowed to run with assistive access
    """
    with patch.object(assistive.TccDB, "install", side_effect=sqlite3.Error("Foo")):
        pytest.raises(CommandExecutionError, assistive.install, "foo")


def test_installed_bundle():
    """
    Test checking to see if a bundle id is installed as being able to use assistive access
    """
    assistive.install("foo")
    assert assistive.installed("foo")


def test_installed_bundle_not():
    """
    Test checking to see if a bundle id is installed as being able to use assistive access
    """
    assert not assistive.installed("foo")


def test_enable_assistive():
    """
    Test enabling a bundle ID as being allowed to run with assistive access
    """
    assistive.install("foo", enable=False)
    assert assistive.enable_("foo", True)


def test_enable_error():
    """
    Test enabled a bundle ID that throws a command error
    """
    with patch.object(assistive.TccDB, "enable", side_effect=sqlite3.Error("Foo")):
        pytest.raises(CommandExecutionError, assistive.enable_, "foo")


def test_enable_false():
    """
    Test return of enable function when app isn't found.
    """
    assert not assistive.enable_("foo")


def test_enabled_assistive():
    """
    Test enabling a bundle ID as being allowed to run with assistive access
    """
    assistive.install("foo")
    assert assistive.enabled("foo")


def test_enabled_assistive_false():
    """
    Test if a bundle ID is disabled for assistive access
    """
    assistive.install("foo", enable=False)
    assert not assistive.enabled("foo")


def test_remove_assistive():
    """
    Test removing an assitive bundle.
    """
    assistive.install("foo")
    assert assistive.remove("foo")


def test_remove_assistive_error():
    """
    Test removing an assitive bundle.
    """
    with patch.object(assistive.TccDB, "remove", side_effect=sqlite3.Error("Foo")):
        pytest.raises(CommandExecutionError, assistive.remove, "foo")


@pytest.mark.parametrize("version", [18, 28, 33, 34, 35, None])
def test_unsupported_schema_version(tcc_db_path, version):
    with sqlite3.connect(tcc_db_path) as conn:
        if version is None:
            conn.execute("DELETE FROM admin WHERE key = 'version'")
        else:
            conn.execute("UPDATE admin SET value = ? WHERE key = 'version'", (version,))
    with pytest.raises(
        CommandExecutionError, match="Unsupported TCC database schema version"
    ):
        assistive.install("foo")


def test_assistive_lifecycle(tcc_db_path):
    """Exercise writes and reads against each extracted TCC schema."""
    app_id = "/usr/bin/osascript"
    assert assistive.install(app_id, enable=False)
    assert assistive.installed(app_id)
    assert not assistive.enabled(app_id)
    assert assistive.enable_(app_id)
    assert assistive.enabled(app_id)
    assert assistive.enable_(app_id, False)
    assert not assistive.enabled(app_id)
    assert assistive.remove(app_id)
    assert not assistive.installed(app_id)


@pytest.mark.parametrize(
    "osrelease, supported", [("10.15.7", False), ("11.0.1", True), ("27.0", True)]
)
def test_virtual_macos_version(osrelease, supported):
    with patch.object(
        assistive.salt.utils.platform, "is_darwin", return_value=True
    ), patch.dict(assistive.__grains__, {"osrelease": osrelease}):
        result = assistive.__virtual__()
    assert (result == "assistive") is supported
