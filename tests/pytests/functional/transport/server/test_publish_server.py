import asyncio

import salt.transport


async def test_publsh_server(
    io_loop, minion_opts, master_opts, transport, process_manager
):
    minion_opts["transport"] = master_opts["transport"] = transport

    pub_server = salt.transport.publish_server(master_opts)
    pub_server.pre_fork(process_manager)

    pub_client = salt.transport.publish_client(
        minion_opts, io_loop, master_opts["interface"], master_opts["publish_port"]
    )
    ready = asyncio.Event()
    event = asyncio.Event()
    messages = []
    # TODO: Fix this inconsistancy.
    if transport == "zeromq":
        probe = b"publish-ready"
        msg = b"meh"
    else:
        probe = {b"probe": b"publish-ready"}
        msg = {b"foo": b"bar"}

    async def handle_msg(payload):
        if payload == probe:
            ready.set()
            return
        messages.append(payload)
        event.set()

    async def wait_until_ready():
        await pub_client.connect()
        pub_client.on_recv(handle_msg)
        # ZeroMQ connect() does not wait for the subscription to reach the
        # publisher. Probe until delivery works before sending the test message.
        while not ready.is_set():
            await pub_server.publish(probe)
            try:
                await asyncio.wait_for(ready.wait(), 0.1)
            except asyncio.TimeoutError:
                pass

    try:
        await asyncio.wait_for(wait_until_ready(), 10)
        await pub_server.publish(msg)
        await asyncio.wait_for(event.wait(), 1)
        assert [msg] == messages
    finally:
        pub_server.close()
        pub_client.close()

    # Yield to loop in order to allow background close methods to finish.
    await asyncio.sleep(0.3)
