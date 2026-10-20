"""The aiokafka coordinator patches, and how they step aside for upstream fixes."""

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiokafka import errors as Errors
from aiokafka.consumer.group_coordinator import GroupCoordinator
from aiokafka.consumer.subscription_state import Assignment, SubscriptionState
from aiokafka.coordinator.protocol import ConsumerProtocol
from aiokafka.structs import TopicPartition

from kaspr.core import coordinator_patch

PATCHED = tuple(coordinator_patch.VERIFIED)

#: The coordinator methods each patch replaces, with Kaspr's copies.
COPIES = {
    "idle_rejoin": {
        "ensure_active_group": coordinator_patch._ensure_active_group,
        "_heartbeat_routine": coordinator_patch._heartbeat_routine,
    },
    "stale_assignment": {"_do_rejoin_group": coordinator_patch._do_rejoin_group},
}
#: Whether the installed aiokafka has the fix a patch backports.
FIXED = {
    "idle_rejoin": coordinator_patch._idle_rejoin_fixed,
    "stale_assignment": coordinator_patch._stale_assignment_fixed,
}


def expected_outcome(patch):
    """What ``apply()`` makes of a patch against the installed aiokafka."""
    if patch == "stale_assignment" and coordinator_patch.RequestStruct is None:
        return "unsupported"
    return "fixed upstream" if FIXED[patch]() else "applied"


@pytest.fixture
def upstream(monkeypatch):
    """aiokafka as released, for ``apply()`` to patch; restored afterwards."""
    for name in PATCHED:
        monkeypatch.setattr(GroupCoordinator, name, coordinator_patch.UPSTREAM[name])
    monkeypatch.setitem(Errors.kafka_errors, coordinator_patch.FENCED_INSTANCE_ID, Errors.UnknownError)
    monkeypatch.setattr(coordinator_patch, "_outcomes", None)


def test_apply_patches_what_aiokafka_has_not_fixed(upstream):
    expected = {patch: expected_outcome(patch) for patch in COPIES}

    outcomes = coordinator_patch.apply()

    for patch, copies in COPIES.items():
        assert outcomes[patch] == expected[patch]
        for name, copy in copies.items():
            applied = expected[patch] == "applied"
            assert getattr(GroupCoordinator, name) is (copy if applied else coordinator_patch.UPSTREAM[name])
    # The fixture unmaps error 82, as before aiokafka 0.15
    assert outcomes["fenced_instance_id"] == "applied"
    assert Errors.for_code(82) is coordinator_patch.FencedInstanceIdError


def test_apply_is_safe_to_call_again(upstream, monkeypatch):
    first = coordinator_patch.apply()
    assert coordinator_patch.apply() is first

    # Patches already in place count as applied, not as changed upstream code
    monkeypatch.setattr(coordinator_patch, "_outcomes", None)
    assert coordinator_patch.apply() == first


def test_a_patch_fixed_upstream_steps_aside_and_says_it_can_go(upstream, monkeypatch, caplog):
    monkeypatch.setattr(GroupCoordinator, "_holds_partitions", Mock(), raising=False)
    monkeypatch.setattr(SubscriptionState, "reset_fetcher_idle_time", Mock(), raising=False)
    monkeypatch.setattr(GroupCoordinator, "_accept_assignment", AsyncMock(), raising=False)
    fenced = type("FencedInstanceIdError", (Errors.BrokerResponseError,), {"errno": 82})
    monkeypatch.setitem(Errors.kafka_errors, 82, fenced)

    with caplog.at_level(logging.WARNING, logger=coordinator_patch.__name__):
        outcomes = coordinator_patch.apply()

    assert set(outcomes.values()) == {"fixed upstream"}
    for name in PATCHED:
        assert getattr(GroupCoordinator, name) is coordinator_patch.UPSTREAM[name]
    assert Errors.for_code(82) is fenced
    removable = [r.getMessage() for r in caplog.records if "can be removed" in r.getMessage()]
    assert len(removable) == 3
    assert any("aio-libs/aiokafka#1187" in message for message in removable)
    assert any("aio-libs/aiokafka#1186" in message for message in removable)
    assert any("aio-libs/aiokafka#1208" in message for message in removable)


def test_a_patch_over_changed_upstream_code_is_not_applied(upstream, monkeypatch, caplog):
    async def heartbeat_routine(self):
        """Not the code the patch was verified against."""

    monkeypatch.setattr(GroupCoordinator, "_heartbeat_routine", heartbeat_routine)
    # Half of the upstream fix is not the fix
    monkeypatch.setattr(GroupCoordinator, "_holds_partitions", Mock(), raising=False)
    monkeypatch.delattr(SubscriptionState, "reset_fetcher_idle_time", raising=False)
    stale_assignment = expected_outcome("stale_assignment")

    with caplog.at_level(logging.WARNING, logger=coordinator_patch.__name__):
        outcomes = coordinator_patch.apply()

    assert outcomes["idle_rejoin"] == "changed upstream"
    # All of a patch or none of it
    assert GroupCoordinator._heartbeat_routine is heartbeat_routine
    assert GroupCoordinator.ensure_active_group is coordinator_patch.UPSTREAM["ensure_active_group"]
    # The others are left to themselves
    assert outcomes["stale_assignment"] == stale_assignment
    assert outcomes["fenced_instance_id"] == "applied"
    (warning,) = [r.getMessage() for r in caplog.records if "Re-check" in r.getMessage()]
    assert "GroupCoordinator._heartbeat_routine" in warning


def test_a_fix_upstream_is_told_by_what_it_adds(monkeypatch):
    for name in ("_holds_partitions", "_accept_assignment"):
        monkeypatch.delattr(GroupCoordinator, name, raising=False)
    monkeypatch.delattr(SubscriptionState, "reset_fetcher_idle_time", raising=False)
    assert not coordinator_patch._idle_rejoin_fixed()
    assert not coordinator_patch._stale_assignment_fixed()

    # Half of the fix for #1187 is not the fix
    monkeypatch.setattr(GroupCoordinator, "_holds_partitions", Mock(), raising=False)
    assert not coordinator_patch._idle_rejoin_fixed()

    monkeypatch.setattr(SubscriptionState, "reset_fetcher_idle_time", Mock(), raising=False)
    monkeypatch.setattr(GroupCoordinator, "_accept_assignment", AsyncMock(), raising=False)
    assert coordinator_patch._idle_rejoin_fixed()
    assert coordinator_patch._stale_assignment_fixed()


@pytest.mark.parametrize("patch", sorted(COPIES))
def test_the_copies_replace_what_they_were_verified_against(patch):
    if FIXED[patch]():
        pytest.skip(
            f"aiokafka {coordinator_patch.aiokafka_version} has the fix: the {patch} patch can be removed"
        )
    for name in COPIES[patch]:
        assert coordinator_patch.fingerprint(coordinator_patch.UPSTREAM[name]) in coordinator_patch.VERIFIED[name]


# -- 1. A consumer holding no partitions can rejoin ---------------------------------------


def subscribed(tps=(), idle=10_000):
    """A ``SubscriptionState`` subscribed to ``orders``, holding ``tps``, that last
    fetched ``idle`` seconds ago. Needs a running event loop."""
    state = SubscriptionState()
    state.subscribe(topics={"orders"})
    state.assign_from_subscribed(set(tps))
    state._last_fetch_ended = time.monotonic() - idle
    return state


def joining_member(state):
    """The parts of ``GroupCoordinator`` that ``ensure_active_group`` uses."""
    return SimpleNamespace(
        _subscription=state,
        _client=SimpleNamespace(force_metadata_update=AsyncMock()),
        _max_poll_interval=300,
        _retry_backoff_ms=1,
        _performed_join_prepare=False,
        _on_join_prepare=AsyncMock(),
        _stop_heartbeat_task=AsyncMock(),
        _do_rejoin_group=AsyncMock(return_value=True),
        _start_heartbeat_task=Mock(),
    )


def ensure_active_group(held, idle=10_000):
    """Run ``ensure_active_group`` for a member that held ``held``: its result, and the member."""

    async def run():
        member = joining_member(subscribed(held, idle))
        subscription = member._subscription.subscription
        result = await coordinator_patch._ensure_active_group(member, subscription, Assignment(held))
        return result, member

    return asyncio.run(run())


def test_a_member_without_partitions_rejoins_however_long_it_was_idle():
    result, member = ensure_active_group(held=[])

    assert result is member._subscription.subscription.assignment
    member._do_rejoin_group.assert_awaited_once()
    member._start_heartbeat_task.assert_called_once_with()
    # It gets max_poll_interval to fetch whatever it was just given
    assert member._subscription.fetcher_idle_time < 60


def test_an_idle_member_with_partitions_revokes_them_and_does_not_rejoin():
    tp = TopicPartition("orders", 0)
    result, member = ensure_active_group(held=[tp])

    assert result is None
    member._do_rejoin_group.assert_not_awaited()
    # Its partitions go to other members: revoking them begins their reassignment, so
    # the fetcher hands out none of their records until it rejoins
    (prev_assignment,), _ = member._on_join_prepare.await_args
    assert prev_assignment.tps == {tp}
    member._stop_heartbeat_task.assert_awaited_once_with()


def test_a_member_with_partitions_keeps_counting_its_idle_time():
    result, member = ensure_active_group(held=[TopicPartition("orders", 0)], idle=100)

    assert result is member._subscription.subscription.assignment
    assert member._subscription.fetcher_idle_time >= 100


def heartbeat(held, beats=3):
    """Run the heartbeat of an idle member holding ``held`` until it stopped after
    ``beats``, or left the group. Returns the member."""

    async def run():
        member = SimpleNamespace(
            member_id="member-1",
            _subscription=subscribed(held),
            _heartbeat_interval_ms=1,
            _session_timeout_ms=10_000,
            _retry_backoff_ms=1,
            _max_poll_interval=300,
            ensure_coordinator_known=AsyncMock(),
            coordinator_dead=Mock(),
        )

        async def do_heartbeat():
            if member._do_heartbeat.await_count >= beats:
                member.member_id = coordinator_patch.UNKNOWN_MEMBER_ID
            return True

        async def maybe_leave_group():
            member.member_id = coordinator_patch.UNKNOWN_MEMBER_ID

        member._do_heartbeat = AsyncMock(side_effect=do_heartbeat)
        member._maybe_leave_group = AsyncMock(side_effect=maybe_leave_group)
        await coordinator_patch._heartbeat_routine(member)
        return member

    return asyncio.run(run())


def test_the_heartbeat_keeps_an_idle_member_without_partitions_in_the_group():
    member = heartbeat(held=[])

    assert member._do_heartbeat.await_count == 3
    member._maybe_leave_group.assert_not_awaited()


def test_the_heartbeat_takes_an_idle_member_with_partitions_out_of_the_group():
    member = heartbeat(held=[TopicPartition("orders", 0)])

    member._maybe_leave_group.assert_awaited_once_with()
    assert member._do_heartbeat.await_count == 1


# -- 3. A static member handed back topics it no longer subscribes to --------------------


def assignment_bytes(**partitions):
    """An encoded consumer assignment, partitions by topic."""
    topics = [(topic.replace("_", "-"), list(parts)) for topic, parts in partitions.items()]
    return ConsumerProtocol.ASSIGNMENT(0, topics, b"").encode()


class FakeRebalance:
    """Stands in for ``CoordinatorGroupRebalance``: the join answers with ``result``."""

    result = None

    def __init__(self, coordinator, *args):
        self.coordinator = coordinator

    async def perform_group_join(self):
        return self.result


@pytest.fixture
def join(monkeypatch):
    monkeypatch.setattr(coordinator_patch, "CoordinatorGroupRebalance", FakeRebalance)

    def answer(**partitions):
        FakeRebalance.result = ("faust", assignment_bytes(**partitions))

    return answer


@pytest.fixture
def backoff(monkeypatch):
    """Records the retry backoffs instead of sleeping them."""
    sleep = AsyncMock()
    monkeypatch.setattr(coordinator_patch.asyncio, "sleep", sleep)
    return sleep


def coordinator(instance_id="app-0", leave_errors=(0, 0), send_error=None):
    """The parts of ``GroupCoordinator`` the rejoin uses."""

    async def send_req(request):
        if send_error is not None:
            raise send_error
        top, member = leave_errors
        return SimpleNamespace(error_code=top, members=[("member-1", instance_id, member)])

    return SimpleNamespace(
        group_id="app",
        coordinator_id=1,
        member_id="member-1",
        generation=7,
        _group_instance_id=instance_id,
        _assignors=[],
        _session_timeout_ms=120000,
        _retry_backoff_ms=100,
        _subscription=SimpleNamespace(topics={"orders"}),
        _send_req=AsyncMock(side_effect=send_req),
        _on_join_complete=AsyncMock(),
        reset_generation=Mock(),
        request_rejoin=Mock(),
    )


def rejoin(member, topics=("orders",)):
    subscription = SimpleNamespace(active=True, topics=set(topics))
    return asyncio.run(coordinator_patch._do_rejoin_group(member, subscription))


def test_a_static_member_handed_unsubscribed_topics_leaves_and_rejoins_as_new(join, backoff):
    join(orders=[0, 1], returns=[0])
    member = coordinator()

    assert rejoin(member) is False

    (request,), _ = member._send_req.await_args
    leave = request.prepare({coordinator_patch.LEAVE_GROUP: (0, 5)})
    assert type(leave).__name__ == "LeaveGroupRequest_v3"
    assert leave.to_object() == {
        "group": "app",
        "members": [{"member_id": "member-1", "group_instance_id": "app-0"}],
    }
    # Rejoining without a member id makes it a new member, which rebalances the group.
    member.reset_generation.assert_called_once_with()
    member._on_join_complete.assert_not_awaited()
    backoff.assert_not_awaited()


def test_an_assignment_within_the_subscription_completes_the_join(join):
    join(orders=[0, 1])
    member = coordinator()

    assert rejoin(member) is True

    member._on_join_complete.assert_awaited_once_with(7, "member-1", "faust", assignment_bytes(orders=[0, 1]))
    member._send_req.assert_not_awaited()


def test_a_dynamic_member_rejoins_after_the_backoff(join, backoff):
    join(orders=[0], returns=[0])
    member = coordinator(instance_id=None)

    assert rejoin(member) is False

    member._send_req.assert_not_awaited()
    member._on_join_complete.assert_not_awaited()
    backoff.assert_awaited_once_with(0.1)
    member.request_rejoin.assert_called_once_with()


def test_a_member_the_broker_no_longer_knows_has_left(join, backoff):
    join(orders=[0], returns=[0])
    member = coordinator(leave_errors=(0, Errors.UnknownMemberIdError.errno))

    assert rejoin(member) is False
    member.reset_generation.assert_called_once_with()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"send_error": Errors.KafkaConnectionError()},
        {"send_error": Errors.RequestTimedOutError()},
        {"leave_errors": (Errors.GroupCoordinatorNotAvailableError.errno, 0)},
    ],
)
def test_a_member_that_cannot_leave_for_now_tries_again(join, backoff, kwargs):
    join(orders=[0], returns=[0])
    member = coordinator(**kwargs)

    assert rejoin(member) is False

    member.reset_generation.assert_not_called()
    member._on_join_complete.assert_not_awaited()
    backoff.assert_awaited_once_with(0.1)
    member.request_rejoin.assert_called_once_with()


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"leave_errors": (0, coordinator_patch.FENCED_INSTANCE_ID)}, Errors.BrokerResponseError),
        ({"send_error": Errors.GroupAuthorizationFailedError()}, Errors.GroupAuthorizationFailedError),
    ],
)
def test_a_member_refused_leaving_for_good_raises(join, backoff, kwargs, error):
    join(orders=[0], returns=[0])
    member = coordinator(**kwargs)

    with pytest.raises(error):
        rejoin(member)
    member._on_join_complete.assert_not_awaited()


def test_a_member_of_a_broker_without_leave_group_v3_raises(join, backoff):
    join(orders=[0], returns=[0])
    member = coordinator()
    # A broker before Kafka 2.4 stops at LeaveGroup v2
    member._send_req.side_effect = lambda request: request.prepare({coordinator_patch.LEAVE_GROUP: (0, 2)})

    with pytest.raises(Errors.IncompatibleBrokerVersion):
        rejoin(member)
    member._on_join_complete.assert_not_awaited()


def test_a_join_that_fails_or_whose_subscription_changed_is_retried(join, backoff):
    member = coordinator()
    FakeRebalance.result = None
    assert rejoin(member) is False

    join(orders=[0])
    inactive = SimpleNamespace(active=False, topics={"orders"})
    assert asyncio.run(coordinator_patch._do_rejoin_group(member, inactive)) is False
    member._on_join_complete.assert_not_awaited()
