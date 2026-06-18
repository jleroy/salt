"""
A convenience system to manage reactors

Beginning in the 2017.7 release, the reactor runner requires that the reactor
system is running.  This is accomplished one of two ways, either
by having reactors configured or by including ``reactor`` in the
engine configuration for the Salt master.

    .. code-block:: yaml

    engines:
        - reactor

"""

import logging

import salt.config
import salt.syspaths
import salt.utils.event
import salt.utils.master
import salt.utils.process
import salt.utils.reactor
from salt.exceptions import CommandExecutionError

log = logging.getLogger(__name__)

__func_alias__ = {
    "list_": "list",
}


def _reactor_system_available():
    """
    Helper to see if the reactor system is available
    """
    if __opts__.get("engines", {}):
        if any([True for engine in __opts__["engines"] if "reactor" in engine]):
            return True
    elif __opts__.get("reactor", {}) and __opts__["reactor"]:
        return True
    return False


def list_(saltenv="base", test=None):
    """
    List currently configured reactors

    CLI Example:

    .. code-block:: bash

        salt-run reactor.list
    """
    if not _reactor_system_available():
        raise CommandExecutionError("Reactor system is not running.")

    with salt.utils.event.get_event(
        "master",
        __opts__["sock_dir"],
        opts=__opts__,
        listen=True,
    ) as sevent:

        master_key = salt.utils.master.get_master_key("root", __opts__)

        __jid_event__.fire_event({"key": master_key}, "salt/reactors/manage/list")

        results = sevent.get_event(wait=30, tag="salt/reactors/manage/list-results")
        reactors = results.get("reactors")
        return reactors


def add(event, reactors, saltenv="base", test=None):
    """
    Add a new reactor

    CLI Example:

    .. code-block:: bash

        salt-run reactor.add 'salt/cloud/*/destroyed' reactors='/srv/reactor/destroy/*.sls'
    """
    if not _reactor_system_available():
        raise CommandExecutionError("Reactor system is not running.")

    if isinstance(reactors, str):
        reactors = [reactors]

    with salt.utils.event.get_event(
        "master",
        __opts__["sock_dir"],
        opts=__opts__,
        listen=True,
    ) as sevent:

        master_key = salt.utils.master.get_master_key("root", __opts__)

        __jid_event__.fire_event(
            {"event": event, "reactors": reactors, "key": master_key},
            "salt/reactors/manage/add",
        )

        res = sevent.get_event(wait=30, tag="salt/reactors/manage/add-complete")
        return res.get("result")


def delete(event, saltenv="base", test=None):
    """
    Delete a reactor

    CLI Example:

    .. code-block:: bash

        salt-run reactor.delete 'salt/cloud/*/destroyed'
    """
    if not _reactor_system_available():
        raise CommandExecutionError("Reactor system is not running.")

    with salt.utils.event.get_event(
        "master",
        __opts__["sock_dir"],
        opts=__opts__,
        listen=True,
    ) as sevent:

        master_key = salt.utils.master.get_master_key("root", __opts__)

        __jid_event__.fire_event(
            {"event": event, "key": master_key}, "salt/reactors/manage/delete"
        )

        res = sevent.get_event(wait=30, tag="salt/reactors/manage/delete-complete")
        return res.get("result")


def _request_leader_value(payload, request_tag, attempts=3, wait=8):
    """
    Send a reactor management request and return the leader value reported back
    by the reactor engine.

    The first request can be lost to a slow-joiner race: the event subscriber
    may not be fully connected by the time the reactor engine fires its reply.
    To stay robust, the request is re-sent until a response is received (re-
    firing is harmless: ``is_leader`` is read-only and ``set_leader`` is
    idempotent). A clear error is raised if no response arrives, instead of
    crashing on a ``None`` result.
    """
    with salt.utils.event.get_event(
        "master",
        __opts__["sock_dir"],
        opts=__opts__,
        listen=True,
    ) as sevent:

        payload = dict(payload)
        payload["key"] = salt.utils.master.get_master_key("root", __opts__)

        for _ in range(attempts):
            __jid_event__.fire_event(payload, request_tag)
            res = sevent.get_event(wait=wait, tag="salt/reactors/manage/leader/value")
            if res is not None:
                return res["result"]

        raise CommandExecutionError(
            "Timed out waiting for the reactor system to report the leader value."
        )


def is_leader():
    """
    Return whether the running reactor is acting as a leader (responding to events).

    CLI Example:

    .. code-block:: bash

        salt-run reactor.is_leader
    """
    if not _reactor_system_available():
        raise CommandExecutionError("Reactor system is not running.")

    return _request_leader_value({}, "salt/reactors/manage/is_leader")


def set_leader(value=True):
    """
    Set the current reactor to act as a leader (responding to events). Defaults to True

    CLI Example:

    .. code-block:: bash

        salt-run reactor.set_leader True
    """
    if not _reactor_system_available():
        raise CommandExecutionError("Reactor system is not running.")

    return _request_leader_value(
        {"id": __opts__["id"], "value": value},
        "salt/reactors/manage/set_leader",
    )
