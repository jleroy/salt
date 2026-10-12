"""Regression tests for TCP IPC fixture worker-port allocation."""

import socket

import pytest

from tests.support.mock import Mock, patch
from tests.support.worker_ports import WorkerPortAllocator

pytestmark = [pytest.mark.core_test]


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"worker_threads": 5},
        {"worker_pools_enabled": False},
        {"worker_pools": {"fast": {}, "general": {}, "custom": {}}},
    ],
)
def test_worker_ports_are_valid_and_not_reallocated(options):
    """High dynamic ports must not overflow or allow later fixture collisions."""
    from salt.transport.zeromq import RequestServer

    source = Mock(return_value=65530)
    allocator = WorkerPortAllocator(source)
    assert allocator.unused_port() == 65530
    config = allocator.configure_master(dict(options, ipc_mode="tcp"))
    assert config["tcp_master_workers"] <= 48000
    from salt.config.worker_pools import get_worker_pools_config

    pools = get_worker_pools_config(config) or {"": {}}
    endpoints = {
        int(RequestServer(config).get_worker_uri(name).rsplit(":", 1)[1])
        for name in pools
    }
    assert len(endpoints) == len(pools)
    assert all(1 <= port <= 65535 for port in endpoints)
    assert endpoints <= allocator.used
    source.side_effect = [*endpoints, 65530, 65531]
    assert allocator.unused_port() == 65531


def test_worker_ports_retry_occupied_endpoint():
    """A free base is insufficient when a derived pool port is already bound."""
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        allocator = WorkerPortAllocator(Mock())
        # The upstream offset for the default pool is 926.
        with patch(
            "tests.support.worker_ports.random.randint", side_effect=[port - 926, 12000]
        ):
            config = allocator.configure_master(
                {"worker_threads": 1, "ipc_mode": "tcp"}
            )
        assert config["tcp_master_workers"] == 12000
        assert port not in allocator.used


@pytest.mark.parametrize("default_mode", ["tcp", "ipc"])
@pytest.mark.parametrize("explicit_mode", [None, "tcp", "ipc"])
def test_worker_ports_follow_effective_ipc_mode(default_mode, explicit_mode):
    """An explicit IPC mode overrides the platform default on every platform."""
    config = {"tcp_master_workers": 65000}
    if explicit_mode is not None:
        config["ipc_mode"] = explicit_mode
    allocator = WorkerPortAllocator(Mock())
    with patch(
        "tests.support.worker_ports.DEFAULT_MASTER_OPTS", {"ipc_mode": default_mode}
    ):
        allocator.configure_master(config)
    if (explicit_mode or default_mode) == "tcp":
        assert 10000 <= config["tcp_master_workers"] <= 48000
        assert allocator.used
    else:
        assert config["tcp_master_workers"] == 65000
        assert not allocator.used
