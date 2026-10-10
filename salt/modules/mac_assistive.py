"""
This module allows you to manage assistive access on macOS minions with 11+

.. versionadded:: 2016.3.0

.. code-block:: bash

    salt '*' assistive.install /usr/bin/osascript
"""

import logging
import sqlite3
import time

import salt.utils.platform
import salt.utils.stringutils
from salt.exceptions import CommandExecutionError
from salt.utils.versions import Version

log = logging.getLogger(__name__)

__virtualname__ = "assistive"
__func_alias__ = {"enable_": "enable"}

TCC_DB_PATH = "/Library/Application Support/com.apple.TCC/TCC.db"


def __virtual__():
    """
    Only work on Mac OS
    """
    if not salt.utils.platform.is_darwin():
        return False, "Must be run on macOS"
    if Version(__grains__["osrelease"]) < Version("11"):
        return False, "Must be run on macOS 11 or newer"
    return __virtualname__


def install(app_id, enable=True, tries=3, wait=10):
    """
    Install a bundle ID or command as being allowed to use
    assistive access.

    app_id
        The bundle ID or command to install for assistive access.

    enabled
        Sets enabled or disabled status. Default is ``True``.

    tries
        How many times to try and write to a read-only tcc. Default is ``True``.

    wait
        Number of seconds to wait between tries. Default is ``10``.

    CLI Example:

    .. code-block:: bash

        salt '*' assistive.install /usr/bin/osascript
        salt '*' assistive.install com.smileonmymac.textexpander
    """
    num_tries = 1
    while True:
        with TccDB() as db:
            try:
                return db.install(app_id, enable=enable)
            except sqlite3.Error as exc:
                if "attempt to write a readonly database" not in str(exc):
                    raise CommandExecutionError(
                        f"Error installing app({app_id}): {exc}"
                    )
                elif num_tries < tries:
                    num_tries += 1
                else:
                    raise CommandExecutionError(
                        f"Error installing app({app_id}): {exc}"
                    )
        time.sleep(wait)


def installed(app_id):
    """
    Check if a bundle ID or command is listed in assistive access.
    This will not check to see if it's enabled.

    app_id
        The bundle ID or command to check installed status.

    CLI Example:

    .. code-block:: bash

        salt '*' assistive.installed /usr/bin/osascript
        salt '*' assistive.installed com.smileonmymac.textexpander
    """
    with TccDB() as db:
        try:
            return db.installed(app_id)
        except sqlite3.Error as exc:
            raise CommandExecutionError(
                f"Error checking if app({app_id}) is installed: {exc}"
            )


def enable_(app_id, enabled=True):
    """
    Enable or disable an existing assistive access application.

    app_id
        The bundle ID or command to set assistive access status.

    enabled
        Sets enabled or disabled status. Default is ``True``.

    CLI Example:

    .. code-block:: bash

        salt '*' assistive.enable /usr/bin/osascript
        salt '*' assistive.enable com.smileonmymac.textexpander enabled=False
    """
    with TccDB() as db:
        try:
            if enabled:
                return db.enable(app_id)
            else:
                return db.disable(app_id)
        except sqlite3.Error as exc:
            raise CommandExecutionError(
                f"Error setting enable to {enabled} on app({app_id}): {exc}"
            )


def enabled(app_id):
    """
    Check if a bundle ID or command is listed in assistive access and
    enabled.

    app_id
        The bundle ID or command to retrieve assistive access status.

    CLI Example:

    .. code-block:: bash

        salt '*' assistive.enabled /usr/bin/osascript
        salt '*' assistive.enabled com.smileonmymac.textexpander
    """
    with TccDB() as db:
        try:
            return db.enabled(app_id)
        except sqlite3.Error as exc:
            raise CommandExecutionError(
                f"Error checking if app({app_id}) is enabled: {exc}"
            )


def remove(app_id):
    """
    Remove a bundle ID or command as being allowed to use assistive access.

    app_id
        The bundle ID or command to remove from assistive access list.

    CLI Example:

    .. code-block:: bash

        salt '*' assistive.remove /usr/bin/osascript
        salt '*' assistive.remove com.smileonmymac.textexpander
    """
    with TccDB() as db:
        try:
            return db.remove(app_id)
        except sqlite3.Error as exc:
            raise CommandExecutionError(f"Error removing app({app_id}): {exc}")


class TccDB:
    def __init__(self, path=None):
        if path is None:
            path = TCC_DB_PATH
        self.path = path
        self.connection = None
        self.schema_version = None

    def _check_schema_version(self):
        try:
            row = self.connection.execute(
                "SELECT value FROM admin WHERE key = 'version'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise CommandExecutionError(
                "Unable to read TCC database schema version"
            ) from exc
        version = row["value"] if row is not None else None
        # TCC versions describe the whole database. The supported access schemas
        # start at 19 (Big Sur), 29 (Sonoma), and 36 (Golden Gate).
        # Gaps correspond to versions not found in the examined macOS releases,
        # presumably used only in internal Apple builds.
        # See https://github.com/jacobsalmela/tccutil/issues/82
        if not isinstance(version, int) or not (
            19 <= version <= 27 or 29 <= version <= 32 or version >= 36
        ):
            raise CommandExecutionError(
                f"Unsupported TCC database schema version: {version}"
            )
        self.schema_version = version

    def _get_client_type(self, app_id):
        if app_id[0] == "/":
            # This is a command line utility
            return 1
        # This is a bundle ID
        return 0

    def installed(self, app_id):
        cursor = self.connection.execute(
            "SELECT * from access WHERE client=? and service='kTCCServiceAccessibility'",
            (app_id,),
        )
        for row in cursor.fetchall():
            if row:
                return True
        return False

    def install(self, app_id, enable=True):
        client_type = self._get_client_type(app_id)
        auth_value = 1 if enable else 0
        columns = [
            "service",
            "client",
            "client_type",
            "auth_value",
            "auth_reason",
            "auth_version",
            "csreq",
            "policy_id",
            "indirect_object_identifier_type",
            "indirect_object_identifier",
            "indirect_object_code_identity",
            "flags",
            "last_modified",
        ]
        values = [
            "kTCCServiceAccessibility",
            app_id,
            client_type,
            auth_value,
            4,
            1,
            None,
            None,
            0 if self.schema_version <= 27 else None,
            "UNUSED",
            None,
            0,
            0,
        ]
        if self.schema_version >= 29:
            columns.extend(["pid", "pid_version", "boot_uuid", "last_reminded"])
            values.extend([0, 0, "UNUSED", time.time()])
        if self.schema_version >= 36:
            columns.extend(["one_time_reprompt_eligible", "reminder_count"])
            values.extend([None, 0])
        placeholders = ", ".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT OR REPLACE INTO access ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        self.connection.commit()
        return True

    def enabled(self, app_id):
        cursor = self.connection.execute(
            "SELECT * from access WHERE client=? and service='kTCCServiceAccessibility'",
            (app_id,),
        )
        for row in cursor.fetchall():
            if row["auth_value"]:
                return True
        return False

    def enable(self, app_id):
        if not self.installed(app_id):
            return False
        self.connection.execute(
            "UPDATE access SET auth_value = ? WHERE client=? AND service IS 'kTCCServiceAccessibility'",
            (1, app_id),
        )
        self.connection.commit()
        return True

    def disable(self, app_id):
        if not self.installed(app_id):
            return False
        self.connection.execute(
            "UPDATE access SET auth_value = ? WHERE client=? AND service IS 'kTCCServiceAccessibility'",
            (0, app_id),
        )
        self.connection.commit()
        return True

    def remove(self, app_id):
        if not self.installed(app_id):
            return False
        self.connection.execute(
            "DELETE from access where client IS ? AND service IS 'kTCCServiceAccessibility'",
            (app_id,),
        )
        self.connection.commit()
        return True

    def __enter__(self):
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        try:
            self._check_schema_version()
        except Exception:
            self.connection.close()
            raise
        return self

    def __exit__(self, *_):
        self.connection.close()
