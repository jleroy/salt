import threading

import pytest

from salt.client.mixins import AsyncClientMixin
from tests.support.mock import MagicMock, patch


@pytest.mark.parametrize("local", [True, False])
def test_asynchronous_returns_before_spawned_job_finishes(local):
    """Return the JID while the spawned job runs, then reap and close its process."""
    client = AsyncClientMixin({})
    publication = {"jid": "test-jid", "tag": "salt/run/test-jid"}
    release_job = threading.Event()
    join_started = threading.Event()
    reaped = threading.Event()
    process = MagicMock()
    process.name = "test-runner"

    def join():
        join_started.set()
        assert release_job.wait(10), "Caller waited for job completion before returning"

    process.join.side_effect = join
    process.close.side_effect = reaped.set
    try:
        with patch("salt.utils.platform.spawning_platform", return_value=True), patch(
            "salt.utils.process.SignalHandlingProcess", return_value=process
        ):
            result = client.asynchronous("test.sleep", {}, pub=publication, local=local)
        assert result == publication
        assert join_started.wait(5)
        process.start.assert_called_once()
        process.close.assert_not_called()
    finally:
        release_job.set()
        assert reaped.wait(5)
    process.join.assert_called_once()
    process.close.assert_called_once()


def test_asynchronous_joins_daemonizing_child():
    """Keep reaping the intermediate child inline on platforms that daemonize."""
    client = AsyncClientMixin({})
    publication = {"jid": "test-jid", "tag": "salt/run/test-jid"}
    with patch("salt.utils.platform.spawning_platform", return_value=False), patch(
        "salt.utils.process.SignalHandlingProcess"
    ) as process_class, patch("salt.client.mixins.threading.Thread") as thread_class:
        assert client.asynchronous("test.sleep", {}, pub=publication) == publication
    process_class.return_value.start.assert_called_once()
    process_class.return_value.join.assert_called_once()
    thread_class.assert_not_called()
