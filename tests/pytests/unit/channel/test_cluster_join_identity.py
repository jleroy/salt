"""Cluster identity adoption must survive concurrent peer discovery."""

import asyncio
from types import SimpleNamespace

import pytest

import salt.channel.server as server
import salt.crypt
import salt.master
import salt.payload
import salt.utils.event
from tests.conftest import FIPS_TESTRUN
from tests.support.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def masters(tmp_path):
    channels = {}
    for name in ("founder", "second", "third"):
        root = tmp_path / name
        root.mkdir()
        (root / "peers").mkdir()
        (root / "files").mkdir()
        (root / "pillar").mkdir()
        salt.crypt.write_keys(str(root), "master", 2048)
        pem = (root / "master.pem").read_bytes()
        pub = (root / "master.pub").read_bytes()
        (root / "cluster.pem").write_bytes(pem)
        (root / "cluster.pub").write_bytes(pub)
        aes = salt.crypt.Crypticle.generate_key_string().encode()
        (root / ".aes").write_bytes(aes)
        channel = server.MasterPubServerChannel.__new__(server.MasterPubServerChannel)
        channel.opts = {
            "id": name,
            "interface": name,
            "cachedir": str(root),
            "cluster_pki_dir": str(root),
            "cluster_id": "test-cluster",
            "cluster_peers": [],
            "cluster_pool_port": 4520,
            "cluster_encryption_algorithm": (
                "OAEP-SHA224" if FIPS_TESTRUN else "OAEP-SHA1"
            ),
            "publish_signing_algorithm": (
                "PKCS1v15-SHA224" if FIPS_TESTRUN else "PKCS1v15-SHA1"
            ),
            "file_roots": {"base": [str(root / "files")]},
            "pillar_roots": {"base": [str(root / "pillar")]},
        }
        channel.master_key = SimpleNamespace(
            master_rsa_path=str(root / "master.pem"),
            master_pub_path=str(root / "master.pub"),
            cluster_rsa_path=str(root / "cluster.pem"),
            cluster_pub_path=str(root / "cluster.pub"),
            cache=MagicMock(),
        )
        channel._init_join_state()
        channel._discover_token = None
        channel._discover_event = None
        channel._discover_candidates = {}
        channel.cluster_peers = []
        channel._raft_dispatcher = None
        channel._raft_service = None
        channel._start_raft_as_learner = MagicMock()
        channel.send_aes_key_event = MagicMock()
        channel.discover_peers = MagicMock()
        channel.pushers = []
        channel.pusher = MagicMock(
            side_effect=lambda peer: MagicMock(
                pull_host=peer, pull_port=4520, publish=AsyncMock()
            )
        )
        channel.test_secrets = {
            "aes": {"secret": MagicMock(value=aes)},
            "cluster_aes": {"secret": MagicMock(value=aes)},
        }
        channels[name] = channel
    channels["founder"]._mark_joined_cluster()
    yield channels
    for channel in channels.values():
        channel._clear_pending_join()
        for session in getattr(channel, "_state_sync_sessions", {}).values():
            if session.watchdog_handle:
                session.watchdog_handle.cancel()


def signed_event(channel, tag, inner, cluster_key=False):
    payload = salt.payload.dumps(inner)
    key = channel.cluster_key() if cluster_key else channel.private_key()
    signature = salt.crypt.PrivateKeyString(key).sign(
        payload, algorithm=channel.opts["publish_signing_algorithm"]
    )
    return salt.utils.event.SaltEvent.pack(tag, {"payload": payload, "sig": signature})


async def deliver(channel, payload):
    with patch.dict(salt.master.SMaster.secrets, channel.test_secrets):
        await channel.handle_pool_publish(payload)


def discovery(channel):
    return signed_event(
        channel,
        "cluster/peer/discover",
        {
            "peer_id": channel.opts["id"],
            "pub": channel.public_key(),
            "token": "discover",
        },
    )


def join_reply(sender, receiver, token, **overrides):
    pub = salt.crypt.PublicKeyString(receiver.public_key())
    session_key = salt.crypt.Crypticle.generate_key_string()
    inner = {
        "peer_id": sender.opts["id"],
        "return_token": token,
        "cluster_aes": pub.encrypt(
            token.encode() + sender.test_secrets["cluster_aes"]["secret"].value,
            algorithm=sender.opts["cluster_encryption_algorithm"],
        ),
        "cluster_key_session": pub.encrypt(
            token.encode() + session_key.encode(),
            algorithm=sender.opts["cluster_encryption_algorithm"],
        ),
        "cluster_pem": salt.crypt.Crypticle(sender.opts, session_key).encrypt(
            sender.cluster_key().encode()
        ),
        "cluster_pub": sender.cluster_public_key(),
        "peers": {},
    }
    inner.update(overrides)
    return signed_event(sender, "cluster/peer/join-reply", inner)


async def test_unjoined_master_defers_discovery(masters):
    """
    Defer discovery replies until this master has an established cluster identity.
    """
    second, third = masters["second"], masters["third"]
    request = discovery(second)
    await deliver(third, request)
    third.pusher.assert_not_called()
    assert third._deferred_discovery == {"second": request}
    third._mark_joined_cluster()
    await asyncio.sleep(0)
    third.pusher.assert_called_once_with("second")
    assert not third._deferred_discovery


async def test_concurrent_discovery_uses_independent_negotiations(masters):
    """
    Keep a separate pending negotiation and token for each responding peer.
    """
    founder, second, third = (masters[k] for k in ("founder", "second", "third"))
    for sender in (founder, third):
        response = signed_event(
            sender,
            "cluster/peer/discover-reply",
            {
                "peer_id": sender.opts["id"],
                "pub": sender.public_key(),
                "cluster_pub": sender.cluster_public_key(),
                "token": "challenge",
            },
            cluster_key=True,
        )
        await deliver(second, response)
    assert set(second._pending_joins) == {"founder", "third"}
    assert second._pending_joins["founder"][0] != second._pending_joins["third"][0]
    assert second.pusher.call_count == 2


async def test_unjoined_master_does_not_distribute_keys(masters):
    """
    Do not distribute provisional keys before joining the cluster.
    """
    second, third = masters["second"], masters["third"]
    await deliver(third, signed_event(second, "cluster/peer/join", {}))
    third.pusher.assert_not_called()
    third.send_aes_key_event.assert_not_called()


async def test_duplicate_discovery_preserves_join_challenge(masters):
    """
    Reuse the challenge for duplicate discovery requests so pending joins remain valid.
    """
    founder, second = masters["founder"], masters["second"]
    request = discovery(second)
    await deliver(founder, request)
    candidate = founder._discover_candidates["second"]
    await deliver(founder, request)
    assert founder._discover_candidates["second"] == candidate


async def test_discovery_and_join_adopt_founder_identity(masters):
    """
    Complete discovery and join, adopt the founder's keys, and clear the negotiation.
    """
    founder, second = masters["founder"], masters["second"]
    founder_pusher = MagicMock(pull_host="second", publish=AsyncMock())
    second_pusher = MagicMock(pull_host="founder", publish=AsyncMock())
    founder.pusher.side_effect = lambda peer: founder_pusher
    second.pusher.side_effect = lambda peer: second_pusher
    await deliver(founder, discovery(second))
    await deliver(second, founder_pusher.publish.call_args.args[0])
    await deliver(founder, second_pusher.publish.call_args.args[0])
    await deliver(second, founder_pusher.publish.call_args.args[0])
    assert second._cluster_identity_ready
    assert not second._pending_joins
    assert not second._join_timeout_handles
    assert second.cluster_key() == founder.cluster_key()
    assert (
        second.test_secrets["cluster_aes"]["secret"].value
        == founder.test_secrets["cluster_aes"]["secret"].value
    )


async def test_failed_identity_install_does_not_complete_join(masters):
    """
    Leave the join incomplete and the in-memory AES key unchanged if persistence fails.
    """
    founder, second = masters["founder"], masters["second"]
    original_aes = second.test_secrets["cluster_aes"]["secret"].value
    second._pending_joins["founder"] = ("join-token", founder.public_key())
    with patch("salt.utils.atomicfile.atomic_open", side_effect=OSError("Disk full")):
        await deliver(second, join_reply(founder, second, "join-token"))
    assert not second._cluster_identity_ready
    assert not second._has_joined_cluster()
    assert second._pending_joins
    assert second.test_secrets["cluster_aes"]["secret"].value == original_aes
    second._start_raft_as_learner.assert_not_called()


async def test_late_join_reply_cannot_replace_cluster_identity(masters, tmp_path):
    """
    Ignore an unsolicited stale reply and retain working encrypted file transfers.
    """
    founder, second, third = (masters[k] for k in ("founder", "second", "third"))
    stale_reply = join_reply(third, second, "join-token")
    for receiver in (second, third):
        receiver._pending_joins["founder"] = ("join-token", founder.public_key())
        await deliver(receiver, join_reply(founder, receiver, "join-token"))
        assert receiver._cluster_identity_ready
    await deliver(second, stale_reply)
    for receiver in (second, third):
        assert receiver.cluster_key() == founder.cluster_key()
        assert (
            receiver.test_secrets["cluster_aes"]["secret"].value
            == founder.test_secrets["cluster_aes"]["secret"].value
        )
        receiver.master_key.cache.store.assert_any_call(
            "master_keys", "cluster.pem", founder.cluster_key().encode()
        )

    # Exercise real encrypted root transfer after the rejected late reply.
    (tmp_path / "founder" / "files" / "test.sls").write_text("test marker")
    for receiver in (second, third):
        pusher = MagicMock(pull_host=receiver.opts["id"], publish=AsyncMock())
        founder.pushers = [pusher]
        with patch.dict(salt.master.SMaster.secrets, founder.test_secrets):
            await founder._run_root_sync_to_peers(["file_roots"])
        for call in pusher.publish.call_args_list:
            await deliver(receiver, call.args[0])
        assert (
            tmp_path / receiver.opts["id"] / "files" / "test.sls"
        ).read_text() == "test marker"


@pytest.mark.parametrize(
    "invalid", ["peer", "token", "signature", "key_pair", "wrapped_token"]
)
async def test_invalid_join_reply_does_not_change_identity(masters, invalid):
    """
    Reject invalid peer IDs, tokens, signatures, or key material before adopting identity.
    """
    founder, second = masters["founder"], masters["second"]
    original_pem = second.cluster_key()
    original_aes = second.test_secrets["cluster_aes"]["secret"].value
    second._pending_joins["founder"] = ("join-token", founder.public_key())
    overrides = {}
    if invalid == "peer":
        overrides["peer_id"] = "other"
    elif invalid == "token":
        overrides["return_token"] = "wrong"
    elif invalid == "key_pair":
        overrides["cluster_pub"] = second.public_key()
    elif invalid == "wrapped_token":
        overrides["cluster_aes"] = salt.crypt.PublicKeyString(
            second.public_key()
        ).encrypt(
            b"wrong-token" + original_aes,
            algorithm=founder.opts["cluster_encryption_algorithm"],
        )
    payload = join_reply(founder, second, "join-token", **overrides)
    if invalid == "signature":
        tag, data = salt.utils.event.SaltEvent.unpack(payload)
        data["sig"] = b"invalid"
        payload = salt.utils.event.SaltEvent.pack(tag, data)
    await deliver(second, payload)
    assert not second._cluster_identity_ready
    assert second.cluster_key() == original_pem
    assert second.test_secrets["cluster_aes"]["secret"].value == original_aes
    second.master_key.cache.store.assert_not_called()


async def test_join_timeout_without_alternative_restarts_discovery(masters):
    """
    Clear the expired negotiation and restart peer discovery.
    """
    second = masters["second"]
    second._pending_joins["unavailable"] = ("token", "pub")
    second._retry_cluster_join("unavailable")
    assert not second._pending_joins
    second.discover_peers.assert_called_once_with()


@pytest.mark.parametrize(
    "invalid", [None, "token", "signature", "identity", "rsa_identity"]
)
async def test_additional_peer_sync_preserves_identity(masters, tmp_path, invalid):
    """
    Recover files and pillar from an additional peer without reinstalling cluster keys.

    Reject invalid replies or incompatible AES/RSA identities without opening a
    sync session. A valid reply permits encrypted transfers, ignores duplicates,
    and does not start Raft a second time.
    """
    founder, second, third = (masters[k] for k in ("founder", "second", "third"))
    # Join an empty peer first, then recover files held only by another peer.
    for receiver in (second, third):
        receiver._pending_joins["founder"] = ("initial", founder.public_key())
        await deliver(receiver, join_reply(founder, receiver, "initial"))
    third.opts["cluster_isolated_filesystem"] = True
    second.opts["cluster_isolated_filesystem"] = True
    (tmp_path / "second" / "files" / "test.sls").write_text("file marker")
    (tmp_path / "second" / "pillar" / "top.sls").write_text("pillar marker")
    # Exercise discovery after identity adoption, with its own signed challenge.
    response = signed_event(
        second,
        "cluster/peer/discover-reply",
        {
            "peer_id": "second",
            "pub": second.public_key(),
            "cluster_pub": second.cluster_public_key(),
            "token": "challenge",
        },
        cluster_key=True,
    )
    await deliver(third, response)
    token = third._pending_joins["second"][0]
    if invalid == "identity":
        second.test_secrets["cluster_aes"][
            "secret"
        ].value = salt.crypt.Crypticle.generate_key_string().encode()
    if invalid == "rsa_identity":
        root = tmp_path / "second"
        (root / "cluster.pem").write_bytes((root / "master.pem").read_bytes())
        (root / "cluster.pub").write_bytes((root / "master.pub").read_bytes())
    reply = join_reply(
        second,
        third,
        "wrong" if invalid == "token" else token,
        state_sync_session="additional",
    )
    if invalid == "signature":
        tag, data = salt.utils.event.SaltEvent.unpack(reply)
        data["sig"] = b"invalid"
        reply = salt.utils.event.SaltEvent.pack(tag, data)
    third.master_key.cache.store.reset_mock()
    with patch("salt.utils.atomicfile.atomic_open") as atomic_open:
        await deliver(third, reply)
        atomic_open.assert_not_called()
    third.master_key.cache.store.assert_not_called()
    assert third.cluster_key() == founder.cluster_key()
    assert (
        third.test_secrets["cluster_aes"]["secret"].value
        == founder.test_secrets["cluster_aes"]["secret"].value
    )
    third._start_raft_as_learner.assert_called_once()
    if invalid:
        assert "additional" not in getattr(third, "_state_sync_sessions", {})
        return
    session = third._state_sync_sessions["additional"]
    await deliver(third, reply)
    assert third._state_sync_sessions["additional"] is session
    pusher = MagicMock(publish=AsyncMock())
    second.pusher.side_effect = lambda peer: pusher
    with patch.dict(salt.master.SMaster.secrets, second.test_secrets), patch(
        "salt.cluster.state_sync.iter_keys_chunks", return_value=[]
    ):
        await second._send_state_sync_chunks("additional", "third")
    for call in pusher.publish.call_args_list:
        await deliver(third, call.args[0])
    assert (tmp_path / "third" / "files" / "test.sls").read_text() == "file marker"
    assert (tmp_path / "third" / "pillar" / "top.sls").read_text() == "pillar marker"
    assert "additional" not in third._state_sync_sessions
    third._start_raft_as_learner.assert_called_once()


async def test_peer_timeout_preserves_other_negotiations(masters):
    """
    Retry an expired peer after identity adoption without clearing other negotiations.
    """
    second = masters["second"]
    second._cluster_identity_ready = True
    second._pending_joins = {"founder": ("one", "pub"), "third": ("two", "pub")}
    handle = asyncio.get_running_loop().call_later(60, lambda: None)
    second._join_timeout_handles["founder"] = handle
    second._retry_cluster_join("founder")
    assert handle.cancelled()
    assert second._pending_joins == {"third": ("two", "pub")}
    second.discover_peers.assert_called_once_with()
