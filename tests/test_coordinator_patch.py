"""The aiokafka coordinator patch for a static member handed a stale assignment."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiokafka import errors as Errors
from aiokafka.consumer.group_coordinator import GroupCoordinator
from aiokafka.coordinator.protocol import ConsumerProtocol

from kaspr.core import coordinator_patch


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
        _retry_backoff_ms=1,
        _subscription=SimpleNamespace(topics={"orders"}),
        _send_req=AsyncMock(side_effect=send_req),
        _on_join_complete=AsyncMock(),
        reset_generation=Mock(),
    )


def rejoin(member, topics=("orders",)):
    subscription = SimpleNamespace(active=True, topics=set(topics))
    return asyncio.run(coordinator_patch._do_rejoin_group(member, subscription))


def test_a_static_member_handed_unsubscribed_topics_leaves_and_rejoins_as_new(join):
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


def test_an_assignment_within_the_subscription_completes_the_join(join):
    join(orders=[0, 1])
    member = coordinator()
    member._kaspr_stale_assignment_leaves = 2

    assert rejoin(member) is True

    member._on_join_complete.assert_awaited_once_with(7, "member-1", "faust", assignment_bytes(orders=[0, 1]))
    member._send_req.assert_not_awaited()
    assert member._kaspr_stale_assignment_leaves == 0


def test_a_dynamic_member_is_left_to_upstream(join):
    join(orders=[0], returns=[0])
    member = coordinator(instance_id=None)

    assert rejoin(member) is True

    member._send_req.assert_not_awaited()
    member._on_join_complete.assert_awaited_once()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"send_error": Errors.KafkaConnectionError()},
        {"send_error": NotImplementedError("LeaveGroup v3 not supported")},
        {"leave_errors": (0, coordinator_patch.FENCED_INSTANCE_ID)},
    ],
)
def test_a_member_that_cannot_leave_takes_the_assignment_as_upstream_does(join, kwargs):
    join(orders=[0], returns=[0])
    member = coordinator(**kwargs)

    assert rejoin(member) is True

    member.reset_generation.assert_not_called()
    member._on_join_complete.assert_awaited_once()


def test_a_member_the_broker_no_longer_knows_has_left(join):
    join(orders=[0], returns=[0])
    member = coordinator(leave_errors=(0, Errors.UnknownMemberIdError.errno))

    assert rejoin(member) is False
    member.reset_generation.assert_called_once_with()


def test_a_member_handed_stale_assignments_again_and_again_stops_leaving(join):
    join(orders=[0], returns=[0])
    member = coordinator()

    results = [rejoin(member) for _ in range(coordinator_patch.MAX_STALE_ASSIGNMENT_LEAVES + 1)]

    assert results == [False] * coordinator_patch.MAX_STALE_ASSIGNMENT_LEAVES + [True]
    assert member._send_req.await_count == coordinator_patch.MAX_STALE_ASSIGNMENT_LEAVES
    member._on_join_complete.assert_awaited_once()


def test_a_join_that_fails_or_whose_subscription_changed_is_retried(join):
    member = coordinator()
    FakeRebalance.result = None
    assert rejoin(member) is False

    join(orders=[0])
    inactive = SimpleNamespace(active=False, topics={"orders"})
    assert asyncio.run(coordinator_patch._do_rejoin_group(member, inactive)) is False
    member._on_join_complete.assert_not_awaited()


def test_apply_patches_the_rejoin_once(monkeypatch):
    original = GroupCoordinator._do_rejoin_group
    monkeypatch.setattr(coordinator_patch, "_applied", False)
    try:
        coordinator_patch.apply()
        coordinator_patch.apply()
        assert GroupCoordinator._do_rejoin_group is coordinator_patch._do_rejoin_group
        assert GroupCoordinator.ensure_active_group is coordinator_patch._ensure_active_group
    finally:
        if original is not coordinator_patch._do_rejoin_group:
            monkeypatch.setattr(GroupCoordinator, "_do_rejoin_group", original)
