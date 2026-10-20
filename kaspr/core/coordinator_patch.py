"""Runtime patches for :pypi:`aiokafka`'s consumer group coordinator.

The first two are backports of fixes made in our aiokafka fork. They are applied
here so they take effect against the released aiokafka that ships in the image,
and can be deleted once a fork release carrying them is pinned.

Three defects are addressed, all of which surface with static group membership
(KIP-345), the first two when a Kaspr app is scaled out:

1. ``ensure_active_group()`` refuses to rejoin when the consumer has been idle
   for ``max_poll_interval``. A member holding *no* partitions never fetches, so
   its idle time only grows and it can never rejoin to get partitions. The check
   also runs after the revoke callback and heartbeat teardown, so a member that
   trips it is left silently dead rather than simply waiting.

2. Kafka error code 82 (``FENCED_INSTANCE_ID``) has no mapping, so it resolves to
   ``UnknownError`` and is re-raised as a bare ``KafkaError``. That stalls the
   coordination routine on an error nobody can interpret. It is raised whenever a
   replacement pod reclaims a ``group.instance.id`` the broker still holds --
   which is exactly what happens when a hung member is terminated mid-rebalance.

3. A static member restarted with fewer subscribed topics wedges. A static member
   rejoining a stable group within the session timeout gets back the assignment
   it held, so when its app stopped consuming a topic, that assignment includes
   the topic. ``SubscriptionState`` asserts on it ("Received an assignment for
   unsubscribed topic"), the coordination routine dies, and the member neither
   consumes nor heartbeats until it is restarted after the broker dropped it.
   Such a member now leaves the group by its ``group.instance.id`` (LeaveGroup
   v3) and rejoins as a new member, which makes the broker rebalance the group
   with every member's current subscription. Needs aiokafka 0.13 or later, for
   its versioned requests.

See https://github.com/aio-libs/aiokafka for the upstream code being patched.
"""

import asyncio
import logging

from aiokafka import __version__ as aiokafka_version
from aiokafka import errors as Errors
from aiokafka.consumer.group_coordinator import CoordinatorGroupRebalance, GroupCoordinator
from aiokafka.coordinator.protocol import ConsumerProtocol

try:
    from aiokafka.protocol.api import Request, RequestStruct, Response
    from aiokafka.protocol.types import Array, Int16, Int32, Schema, String
except ImportError:  # aiokafka 0.12: requests are not versioned
    RequestStruct = None

__all__ = ["apply"]

logger = logging.getLogger(__name__)

#: aiokafka releases whose coordinator source these patches were verified
#: against. ``ensure_active_group`` and ``_do_rejoin_group`` below are copies of
#: upstream's bodies (the fixes are mid-function, so they cannot be wrapped).
#: ``ensure_active_group`` is byte-identical across all three, and
#: ``_do_rejoin_group`` differs only in how 0.12 splits a debug message. Anything
#: else still gets patched, but says so loudly.
VERIFIED_VERSIONS = frozenset({"0.12.0", "0.13.0", "0.14.0"})

FENCED_INSTANCE_ID = 82
LEAVE_GROUP = 13

#: Stale assignments in a row a static member leaves its group over. Past them it
#: stops, and the assignment is refused as aiokafka refuses it, so a member that
#: keeps being handed one fails visibly instead of rejoining forever.
MAX_STALE_ASSIGNMENT_LEAVES = 3

_applied = False


class FencedInstanceIdError(Errors.BrokerResponseError):
    """Kafka error 82, missing from aiokafka's error table."""

    errno = FENCED_INSTANCE_ID
    message = "FENCED_INSTANCE_ID"
    description = (
        "The broker rejected this static consumer since another consumer with the"
        " same group.instance.id has registered with a different member.id."
    )


if RequestStruct is not None:

    class LeaveGroupResponse_v3(Response):
        API_KEY = LEAVE_GROUP
        API_VERSION = 3
        SCHEMA = Schema(
            ("throttle_time_ms", Int32),
            ("error_code", Int16),
            (
                "members",
                Array(
                    ("member_id", String("utf-8")),
                    ("group_instance_id", String("utf-8")),
                    ("error_code", Int16),
                ),
            ),
        )

    class LeaveGroupRequest_v3(RequestStruct):
        """LeaveGroup naming its members by ``group.instance.id`` (KIP-345)."""

        API_KEY = LEAVE_GROUP
        API_VERSION = 3
        RESPONSE_TYPE = LeaveGroupResponse_v3
        SCHEMA = Schema(
            ("group", String("utf-8")),
            (
                "members",
                Array(
                    ("member_id", String("utf-8")),
                    ("group_instance_id", String("utf-8")),
                ),
            ),
        )

    class StaticMemberLeaveGroupRequest(Request[LeaveGroupRequest_v3]):
        """Leave a group as a static member. aiokafka sends only LeaveGroup v0-1,
        which cannot name one, and never leaves as a static member."""

        API_KEY = LEAVE_GROUP

        def __init__(self, group: str, member_id: str, group_instance_id: str):
            self._group = group
            self._member_id = member_id
            self._group_instance_id = group_instance_id

        def build(self, request_struct_class):
            return request_struct_class(
                self._group, [(self._member_id, self._group_instance_id)]
            )


def _unsubscribed_topics(subscription, member_assignment_bytes):
    """Topics an assignment hands this member that it does not subscribe to."""
    assignment = ConsumerProtocol.ASSIGNMENT.decode(member_assignment_bytes)
    return {tp.topic for tp in assignment.partitions()} - set(subscription.topics)


async def _leave_as_static_member(self) -> bool:
    """Leave the group by ``group.instance.id``, so the broker forgets this member
    and the assignment it held. Returns whether it left."""
    request = StaticMemberLeaveGroupRequest(
        self.group_id, self.member_id, self._group_instance_id
    )
    try:
        response = await self._send_req(request)
    except (Errors.KafkaError, NotImplementedError) as exc:
        logger.warning(
            "Static member %s could not leave group %s: %r",
            self._group_instance_id,
            self.group_id,
            exc,
        )
        return False
    errors = [Errors.for_code(response.error_code)]
    errors += [Errors.for_code(code) for _, _, code in response.members]
    # A member the broker no longer knows has nothing left to forget.
    if all(error in (Errors.NoError, Errors.UnknownMemberIdError) for error in errors):
        return True
    logger.warning(
        "Static member %s could not leave group %s: %s",
        self._group_instance_id,
        self.group_id,
        ", ".join(error.__name__ for error in errors),
    )
    return False


async def _do_rejoin_group(self, subscription):
    """Copy of upstream ``GroupCoordinator._do_rejoin_group``. A static member
    handed back an assignment of topics it no longer subscribes to leaves the
    group and rejoins, instead of failing on it (defect 3 above).
    """
    rebalance = CoordinatorGroupRebalance(
        self,
        self.group_id,
        self.coordinator_id,
        subscription,
        self._assignors,
        self._session_timeout_ms,
        self._retry_backoff_ms,
    )
    assignment = await rebalance.perform_group_join()

    if not subscription.active:
        logger.debug(
            "Subscription changed during rebalance from %s to %s. Rejoining group.",
            subscription.topics,
            self._subscription.topics,
        )
        return False
    if assignment is None:
        # wait backoff and try again
        await asyncio.sleep(self._retry_backoff_ms / 1000)
        return False

    protocol, member_assignment_bytes = assignment
    if self._group_instance_id is not None and RequestStruct is not None:
        stale = _unsubscribed_topics(subscription, member_assignment_bytes)
        leaves = getattr(self, "_kaspr_stale_assignment_leaves", 0)
        if stale and leaves < MAX_STALE_ASSIGNMENT_LEAVES:
            logger.warning(
                "Static member %s of group %s was handed back an assignment of"
                " topics it no longer subscribes to (%s), as a static member"
                " rejoining a stable group is; leaving the group so that it"
                " rebalances with the current subscriptions",
                self._group_instance_id,
                self.group_id,
                ", ".join(sorted(stale)),
            )
            self._kaspr_stale_assignment_leaves = leaves + 1
            if await _leave_as_static_member(self):
                # Rejoins without a member id: a new member, which rebalances.
                self.reset_generation()
                return False
        elif not stale:
            self._kaspr_stale_assignment_leaves = 0

    await self._on_join_complete(
        self.generation, self.member_id, protocol, member_assignment_bytes
    )
    return True


async def _ensure_active_group(self, subscription, prev_assignment):
    """Copy of upstream ``GroupCoordinator.ensure_active_group``, with the idle
    check moved ahead of the revoke callback and heartbeat teardown, and made to
    ignore members that hold no partitions.
    """
    if self._subscription.subscribed_pattern:
        await self._client.force_metadata_update()
        if not subscription.active:
            return None

    # An assignment with no partitions can never make progress on its own:
    # nothing is fetched, so idle time only grows and the member would be
    # stranded here forever. Such a member must always be allowed to rejoin,
    # which is how it gets partitions in the first place.
    if prev_assignment is not None and prev_assignment.tps:
        if self._subscription.fetcher_idle_time >= self._max_poll_interval:
            await asyncio.sleep(self._retry_backoff_ms / 1000)
            return None

    if not self._performed_join_prepare:
        await self._on_join_prepare(prev_assignment)
        self._performed_join_prepare = True

    # NOTE: we did not stop heartbeat task before to keep the member alive
    # during the callback, as it can commit offsets.
    await self._stop_heartbeat_task()

    success = await self._do_rejoin_group(subscription)
    if success:
        self._performed_join_prepare = False
        self._start_heartbeat_task()
        return subscription.assignment
    return None


def _register_fenced_instance_id() -> bool:
    """Make ``Errors.for_code(82)`` resolve to a typed, meaningful error.

    ``kafka_errors`` is built once at import time by walking subclasses, so
    declaring the class is not enough -- it has to be inserted by hand.
    """
    if Errors.for_code(FENCED_INSTANCE_ID) is not Errors.UnknownError:
        return False
    Errors.kafka_errors[FENCED_INSTANCE_ID] = FencedInstanceIdError
    if not hasattr(Errors, "FencedInstanceIdError"):
        Errors.FencedInstanceIdError = FencedInstanceIdError
    return True


def apply() -> None:
    """Apply the coordinator patches. Safe to call more than once."""
    global _applied
    if _applied:
        return
    _applied = True

    if aiokafka_version not in VERIFIED_VERSIONS:
        logger.warning(
            "Applying aiokafka coordinator patches against unverified version "
            "%s (verified: %s). Re-check kaspr/core/coordinator_patch.py against "
            "the installed group_coordinator.py.",
            aiokafka_version,
            ", ".join(sorted(VERIFIED_VERSIONS)),
        )

    registered = _register_fenced_instance_id()

    # Only patch what still looks like the unfixed upstream code, so pinning a
    # fixed aiokafka later makes this a no-op instead of a regression.
    if not getattr(GroupCoordinator.ensure_active_group, "_kaspr_patched", False):
        _ensure_active_group._kaspr_patched = True
        GroupCoordinator.ensure_active_group = _ensure_active_group

    if RequestStruct is None:
        logger.warning(
            "aiokafka %s has no versioned requests: a static member handed an"
            " assignment of topics it no longer subscribes to is not made to leave"
            " its group",
            aiokafka_version,
        )
    if not getattr(GroupCoordinator._do_rejoin_group, "_kaspr_patched", False):
        _do_rejoin_group._kaspr_patched = True
        GroupCoordinator._do_rejoin_group = _do_rejoin_group

    logger.info(
        "Applied aiokafka coordinator patches (aiokafka %s, "
        "FENCED_INSTANCE_ID registered: %s)",
        aiokafka_version,
        registered,
    )
