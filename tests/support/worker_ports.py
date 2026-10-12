"""Allocate TCP IPC test ports without changing Salt's worker pool protocol."""

import contextlib
import random
import socket
import zlib

from salt.config import DEFAULT_MASTER_OPTS
from salt.config.worker_pools import get_worker_pools_config


class WorkerPortAllocator:
    """Keep pool endpoints out of subsequent fixture port allocations."""

    def __init__(self, get_unused_port):
        self.get_unused_port = get_unused_port
        self.used = set()

    def unused_port(self, use_cache=False):
        for _ in range(1000):
            port = self.get_unused_port()
            if port not in self.used:
                self.used.add(port)
                return port
        raise RuntimeError("Could not allocate an unused test port")

    def configure_master(self, config):
        if config.get("ipc_mode", DEFAULT_MASTER_OPTS["ipc_mode"]) != "tcp":
            return config

        pools = get_worker_pools_config(config) or {}
        offsets = [zlib.adler32(name.encode()) % 1000 for name in pools]
        if len(offsets) != len(set(offsets)):
            raise ValueError("Test worker pool names have colliding TCP port offsets")
        for _ in range(1000):
            # Stay below Windows' default ephemeral range (49152-65535).
            base = random.randint(10000, 48000)
            endpoints = {base, *(base + offset for offset in offsets)}
            if endpoints & self.used:
                continue
            try:
                # Hold all probes together so availability is checked as a group.
                with contextlib.ExitStack() as stack:
                    for port in endpoints:
                        sock = stack.enter_context(socket.socket())
                        sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            self.used.update(endpoints)
            config["tcp_master_workers"] = base
            return config
        raise RuntimeError("Could not allocate TCP ports for test worker pools")
