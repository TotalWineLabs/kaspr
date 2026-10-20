"""Runtime patches for :pypi:`aiokafka`'s consumer group coordinator.

Each patch backports a fix proposed upstream, so that it takes effect against the
aiokafka release that ships in the image. The patches are meant to go away: ``apply()``
checks the installed aiokafka before patching, and for each patch

- skips it if aiokafka already has the fix, and logs a warning that the patch can be
  removed;
- skips it if the code it replaces is not what it was verified against, and logs a
  warning to re-check it. Patches 1 and 3 replace whole methods with copies of the
  upstream code, so applying them over newer code could undo other upstream changes;
- applies it otherwise.

All three are about static group membership (KIP-345):

1. A consumer holding no partitions can never rejoin after ``max_poll_interval_ms``
   (aio-libs/aiokafka#1187, fix in PR #1189). A member with no partitions never
   fetches, so its idle time only grows: the heartbeat makes it leave the group and
   ``ensure_active_group()`` then refuses to rejoin it, and rejoining is the only way it
   would get partitions. That check also ran after the revoke callback and the
   heartbeat teardown, so a member that declined to rejoin was left silently dead. And
   a member that did get partitions would be taken for stuck by its first heartbeat,
   its idle time counting from its last fetch. Replaces ``ensure_active_group`` and
   ``_heartbeat_routine``.

2. Kafka error code 82 (``FENCED_INSTANCE_ID``) has no mapping
   (aio-libs/aiokafka#1186, fixed by PR #1188 for aiokafka 0.15.0), so it resolves to
   ``UnknownError`` and is re-raised as a bare ``KafkaError``, stalling the
   coordination routine on an error nobody can interpret. It is raised whenever a
   replacement pod reclaims a ``group.instance.id`` the broker still holds -- which is
   exactly what happens when a hung member is terminated mid-rebalance. Registers the
   error.

3. A static member restarted with fewer subscribed topics wedges (aio-libs/aiokafka,
   issue and PR to be filed). Rejoining a stable group within the session timeout, it
   gets back the assignment it held, which includes a topic it no longer subscribes
   to. ``SubscriptionState`` asserts on it ("Received an assignment for unsubscribed
   topic"), the coordination routine dies, and the member neither consumes nor
   heartbeats until it is restarted. Such a member now leaves the group under its
   ``group.instance.id`` (LeaveGroup v3, Kafka 2.4) and rejoins as a new member, so
   the group rebalances with every member's current subscription. A leave that fails
   with a retriable error is retried; any other error is raised. Replaces
   ``_do_rejoin_group``. Needs aiokafka 0.13 or later, for its versioned requests.

See https://github.com/aio-libs/aiokafka for the upstream code being patched.
"""

import asyncio
import hashlib
import inspect
import logging
import textwrap
import time

from aiokafka import __version__ as aiokafka_version
from aiokafka import errors as Errors
from aiokafka.consumer.group_coordinator import CoordinatorGroupRebalance, GroupCoordinator
from aiokafka.consumer.subscription_state import SubscriptionState
from aiokafka.coordinator.protocol import ConsumerProtocol

try:
    from aiokafka.protocol.api import Request, RequestStruct, Response
    from aiokafka.protocol.types import Array, Int16, Int32, Schema, String
except ImportError:  # aiokafka 0.12: requests are not versioned
    RequestStruct = None

__all__ = ["apply"]

logger = logging.getLogger(__name__)
#: The copied coordinator code logs where upstream's does.
log = logging.getLogger("aiokafka.consumer.group_coordinator")

IDLE_REJOIN = "aio-libs/aiokafka#1187 (PR #1189)"
FENCED_INSTANCE = "aio-libs/aiokafka#1186 (PR #1188)"
STALE_ASSIGNMENT = "the stale static assignment (no aio-libs/aiokafka issue yet)"

#: Fingerprints of the upstream methods the patches replace, as verified. Anything else
#: is not patched. ``ensure_active_group`` is the same from aiokafka 0.12.0 to 0.15.0rc1;
#: the other two differ in 0.12.0 only in how it names the unknown member id and splits
#: a debug message, which the copies below do not depend on.
VERIFIED = {
    "ensure_active_group": frozenset({"c9c8b85daa6ecc8e"}),
    "_heartbeat_routine": frozenset({"643329035c210586", "b38c6f43e82e11f4"}),
    "_do_rejoin_group": frozenset({"415d72426a0809aa", "350eade5c97c6ad7"}),
}

#: The upstream methods, as imported, before any patch replaced them.
UPSTREAM = {name: getattr(GroupCoordinator, name) for name in VERIFIED}

#: ``JoinGroupRequest.UNKNOWN_MEMBER_ID``, ``JoinGroupRequest[0].UNKNOWN_MEMBER_ID`` on 0.12.
UNKNOWN_MEMBER_ID = ""
FENCED_INSTANCE_ID = 82
LEAVE_GROUP = 13

APPLIED = "applied"
FIXED_UPSTREAM = "fixed upstream"
CHANGED_UPSTREAM = "changed upstream"
UNSUPPORTED = "unsupported"

_outcomes = None


def fingerprint(function) -> str:
    """A short hash of a function's source, insensitive to indentation and trailing spaces."""
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        return ""
    normalized = "\n".join(line.rstrip() for line in textwrap.dedent(source).splitlines())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _replace(methods: dict, upstream: str, fixed: bool) -> str:
    """Replace ``GroupCoordinator`` methods with Kaspr's copies: all of them, or none."""
    if fixed:
        logger.warning(
            "aiokafka %s includes the fix for %s: Kaspr's patch for it is not applied,"
            " and can be removed from kaspr/core/coordinator_patch.py.",
            aiokafka_version,
            upstream,
        )
        return FIXED_UPSTREAM
    current = {name: getattr(GroupCoordinator, name) for name in methods}
    if all(getattr(method, "_kaspr_patched", False) for method in current.values()):
        return APPLIED
    changed = sorted(
        name for name, method in current.items() if fingerprint(method) not in VERIFIED[name]
    )
    if changed:
        logger.warning(
            "aiokafka %s changed GroupCoordinator.%s since Kaspr's patch for %s was"
            " verified against it (aiokafka 0.12.0 to 0.15.0rc1), and does not look"
            " fixed: the patch is not applied. Re-check it against the upstream code.",
            aiokafka_version,
            ", GroupCoordinator.".join(changed),
            upstream,
        )
        return CHANGED_UPSTREAM
    for name, method in methods.items():
        method._kaspr_patched = True
        setattr(GroupCoordinator, name, method)
    return APPLIED


# -- 1. A consumer holding no partitions can never rejoin (#1187) ----------------------


def _holds_partitions(coordinator) -> bool:
    """Whether this member currently has any partitions assigned."""
    subscription = coordinator._subscription.subscription
    if subscription is None:
        return False
    assignment = subscription.assignment
    return assignment is not None and bool(assignment.tps)


def _reset_fetcher_idle_time(subscription_state) -> None:
    """Count the fetcher's idle time from now on."""
    reset = getattr(subscription_state, "reset_fetcher_idle_time", None)
    if reset is not None:
        reset()
    else:
        subscription_state._last_fetch_ended = time.monotonic()


async def _ensure_active_group(self, subscription, prev_assignment):
    """Copy of upstream ``GroupCoordinator.ensure_active_group`` with the fix of PR
    #1189: the idle check runs before the revoke callback and the heartbeat teardown,
    skips a member that holds no partitions, and such a member's idle time counts from
    the moment it joins.
    """
    if self._subscription.subscribed_pattern:
        await self._client.force_metadata_update()
        if not subscription.active:
            return None

    # We will not attempt rejoin if there is no activity on consumer. Checked before
    # revoking and stopping the heartbeat, so a member that declines keeps both. A
    # member holding no partitions fetches nothing, so its idle time only grows:
    # rejoining is how it gets partitions, so it may always rejoin.
    if prev_assignment is not None and prev_assignment.tps:
        if self._subscription.fetcher_idle_time >= self._max_poll_interval:
            await asyncio.sleep(self._retry_backoff_ms / 1000)
            return None

    if not self._performed_join_prepare:
        # NOTE: We pass the previously used assignment here.
        await self._on_join_prepare(prev_assignment)
        self._performed_join_prepare = True

    # NOTE: we did not stop heartbeat task before to keep the member alive during the
    # callback, as it can commit offsets.
    await self._stop_heartbeat_task()

    success = await self._do_rejoin_group(subscription)
    if success:
        self._performed_join_prepare = False
        if prev_assignment is None or not prev_assignment.tps:
            # It had nothing to fetch, so its idle time only tells how long it went
            # without partitions. Counted from its last fetch, the first heartbeat
            # would take a member that was just given partitions for stuck.
            _reset_fetcher_idle_time(self._subscription)
        self._start_heartbeat_task()
        return subscription.assignment
    return None


async def _heartbeat_routine(self):
    """Copy of upstream ``GroupCoordinator._heartbeat_routine`` with the fix of PR
    #1189: a member holding no partitions is not made to leave for being idle.
    """
    last_ok_heartbeat = time.monotonic()
    hb_interval = self._heartbeat_interval_ms / 1000
    session_timeout = self._session_timeout_ms / 1000
    retry_backoff = self._retry_backoff_ms / 1000
    sleep_time = hb_interval

    # There is no point to heartbeat after Broker stopped recognizing
    # this consumer, so we stop after resetting generation.
    while self.member_id != UNKNOWN_MEMBER_ID:
        try:
            await asyncio.sleep(sleep_time)
            await self.ensure_coordinator_known()

            t0 = time.monotonic()
            success = await self._do_heartbeat()
        except asyncio.CancelledError:
            break

        # NOTE: We let all other errors propagate up to coordination
        # routine

        if success:
            last_ok_heartbeat = time.monotonic()
            sleep_time = max((0, hb_interval - last_ok_heartbeat + t0))
        else:
            sleep_time = retry_backoff

        session_time = time.monotonic() - last_ok_heartbeat
        if session_time > session_timeout:
            # the session timeout has expired without seeing a successful
            # heartbeat, so we should probably make sure the coordinator
            # is still healthy.
            log.error("Heartbeat session expired - marking coordinator dead")
            self.coordinator_dead()

        # If consumer is idle (no records consumed) for too long we need
        # to leave the group
        idle_time = self._subscription.fetcher_idle_time
        if idle_time < self._max_poll_interval:
            sleep_time = min(sleep_time, self._max_poll_interval - idle_time)
        elif not _holds_partitions(self):
            # A member holding no partitions has nothing to consume, so its idle time
            # says nothing about whether the application makes progress. Leaving
            # would trigger a rebalance it cannot benefit from, and
            # ``ensure_active_group()`` rejoins such a member straight away, so it
            # would leave and rejoin on every heartbeat. Keep heartbeating instead.
            pass
        else:
            await self._maybe_leave_group()

    log.debug("Stopping heartbeat task")


def _apply_idle_rejoin() -> str:
    fixed = hasattr(GroupCoordinator, "_holds_partitions") and hasattr(
        SubscriptionState, "reset_fetcher_idle_time"
    )
    return _replace(
        {"ensure_active_group": _ensure_active_group, "_heartbeat_routine": _heartbeat_routine},
        IDLE_REJOIN,
        fixed,
    )


# -- 2. FENCED_INSTANCE_ID has no mapping (#1186) --------------------------------------


class FencedInstanceIdError(Errors.BrokerResponseError):
    """Kafka error 82, missing from aiokafka's error table."""

    errno = FENCED_INSTANCE_ID
    message = "FENCED_INSTANCE_ID"
    description = (
        "The broker rejected this static consumer since another consumer with the"
        " same group.instance.id has registered with a different member.id."
    )


def _apply_fenced_instance_id() -> str:
    """Make ``Errors.for_code(82)`` resolve to a typed, meaningful error.

    ``kafka_errors`` is built once at import time by walking subclasses, so declaring
    the class is not enough -- it has to be inserted by hand.
    """
    mapped = Errors.for_code(FENCED_INSTANCE_ID)
    if mapped is FencedInstanceIdError:
        return APPLIED
    if mapped is not Errors.UnknownError:
        logger.warning(
            "aiokafka %s maps Kafka error %d itself, the fix for %s: Kaspr's patch for"
            " it is not applied, and can be removed from kaspr/core/coordinator_patch.py.",
            aiokafka_version,
            FENCED_INSTANCE_ID,
            FENCED_INSTANCE,
        )
        return FIXED_UPSTREAM
    Errors.kafka_errors[FENCED_INSTANCE_ID] = FencedInstanceIdError
    if not hasattr(Errors, "FencedInstanceIdError"):
        Errors.FencedInstanceIdError = FencedInstanceIdError
    return APPLIED


# -- 3. A static member handed back topics it no longer subscribes to ------------------


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


async def _leave_group_as_static_member(self) -> bool:
    """Leave the group under our ``group_instance_id``, so the broker forgets this
    member and the assignment it held. Needs LeaveGroup v3 (Kafka 2.4).

    Returns whether the broker has no such member any more, or ``False`` on a
    retriable error. Other errors are raised: rejoining as the same member would only
    be handed the same assignment again.
    """
    request = StaticMemberLeaveGroupRequest(
        self.group_id, self.member_id, self._group_instance_id
    )
    try:
        response = await self._send_req(request)
    except NotImplementedError as err:  # the broker has no LeaveGroup v3
        raise Errors.IncompatibleBrokerVersion(
            "Leaving a group as a static member needs LeaveGroup v3 (Kafka 2.4)"
        ) from err
    except Errors.KafkaError as err:
        if not err.retriable:
            raise
        log.error("LeaveGroup request for static member failed: %s", err)
        return False

    codes = [response.error_code] + [member[-1] for member in response.members]
    # A member the broker doesn't know any more has nothing left to forget
    errors = [
        error_type
        for error_type in map(Errors.for_code, codes)
        if error_type not in (Errors.NoError, Errors.UnknownMemberIdError)
    ]
    if not errors:
        log.info("LeaveGroup request for static member succeeded")
        return True
    for error_type in errors:
        if not error_type.retriable:
            raise error_type()
    log.error(
        "LeaveGroup request for static member failed: %s",
        ", ".join(error_type.__name__ for error_type in errors),
    )
    return False


async def _accept_assignment(self, subscription, member_assignment_bytes) -> bool:
    """Check an assignment against what this member subscribes to.

    A static member that rejoins a stable group within its session timeout is handed
    back the assignment it held before. If the member now subscribes to fewer topics,
    that assignment still has partitions of topics it dropped, and
    ``SubscriptionState`` refuses it. Rejoining as the same member would be handed the
    same assignment again, so a static member leaves the group under its
    ``group_instance_id`` first, then rejoins as a new member, and the group rebalances
    with everyone's current subscriptions.

    Returns whether the assignment can be taken.
    """
    assignment = ConsumerProtocol.ASSIGNMENT.decode(member_assignment_bytes)
    unsubscribed = {tp.topic for tp in assignment.partitions()} - set(subscription.topics)
    if not unsubscribed:
        return True

    if self._group_instance_id is None:
        # Like the Java client, rejoin rather than fail. A dynamic member's assignment
        # is made from the subscription its join carried, so only an assignor that
        # ignores subscriptions gets here.
        log.info(
            "Assignment for group %s includes topics %s this consumer is not"
            " subscribed to, rejoining",
            self.group_id,
            sorted(unsubscribed),
        )
    else:
        log.warning(
            "Static member %s was handed back its previous assignment in group %s,"
            " which includes topics it is no longer subscribed to: %s. Leaving the"
            " group so it rebalances with the current subscription",
            self._group_instance_id,
            self.group_id,
            sorted(unsubscribed),
        )
        if await _leave_group_as_static_member(self):
            # Rejoins without a member id: a new member, which rebalances the group.
            self.reset_generation()
            return False
    await asyncio.sleep(self._retry_backoff_ms / 1000)
    self.request_rejoin()
    return False


async def _do_rejoin_group(self, subscription):
    """Copy of upstream ``GroupCoordinator._do_rejoin_group``, which checks the
    assignment against the subscription before taking it (``_accept_assignment``).
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
        log.debug(
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
    if not await _accept_assignment(self, subscription, member_assignment_bytes):
        return False
    await self._on_join_complete(
        self.generation, self.member_id, protocol, member_assignment_bytes
    )
    return True


def _apply_stale_assignment() -> str:
    if RequestStruct is None:
        logger.warning(
            "aiokafka %s has no versioned requests: Kaspr's patch for %s is not applied.",
            aiokafka_version,
            STALE_ASSIGNMENT,
        )
        return UNSUPPORTED
    return _replace(
        {"_do_rejoin_group": _do_rejoin_group},
        STALE_ASSIGNMENT,
        hasattr(GroupCoordinator, "_accept_assignment"),
    )


def apply() -> dict:
    """Apply the coordinator patches. Safe to call more than once.

    Returns what became of each patch: applied, fixed upstream, changed upstream, or
    unsupported.
    """
    global _outcomes
    if _outcomes is None:
        _outcomes = {
            "idle_rejoin": _apply_idle_rejoin(),
            "fenced_instance_id": _apply_fenced_instance_id(),
            "stale_assignment": _apply_stale_assignment(),
        }
        logger.info(
            "aiokafka %s coordinator patches: %s",
            aiokafka_version,
            ", ".join(f"{name} {outcome}" for name, outcome in _outcomes.items()),
        )
    return _outcomes
