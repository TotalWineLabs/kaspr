"""Runtime patches for :pypi:`aiokafka`'s consumer group coordinator.

These are backports of fixes made in our aiokafka fork. They are applied here so
they take effect against the released aiokafka that ships in the image, and can
be deleted once a fork release carrying them is pinned.

Two defects are addressed, both of which surface when a Kaspr app is scaled out
while using static group membership (KIP-345):

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

See https://github.com/aio-libs/aiokafka for the upstream code being patched.
"""

import asyncio
import logging

from aiokafka import __version__ as aiokafka_version
from aiokafka import errors as Errors
from aiokafka.consumer.group_coordinator import GroupCoordinator

__all__ = ["apply"]

logger = logging.getLogger(__name__)

#: aiokafka releases whose coordinator source these patches were verified
#: against. ``ensure_active_group`` below is a copy of upstream's body (the fix
#: is mid-function, so it cannot be wrapped), and is byte-identical across all
#: three. Anything else still gets patched, but says so loudly.
VERIFIED_VERSIONS = frozenset({"0.12.0", "0.13.0", "0.14.0"})

FENCED_INSTANCE_ID = 82

_applied = False


class FencedInstanceIdError(Errors.BrokerResponseError):
    """Kafka error 82, missing from aiokafka's error table."""

    errno = FENCED_INSTANCE_ID
    message = "FENCED_INSTANCE_ID"
    description = (
        "The broker rejected this static consumer since another consumer with the"
        " same group.instance.id has registered with a different member.id."
    )


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

    logger.info(
        "Applied aiokafka coordinator patches (aiokafka %s, "
        "FENCED_INSTANCE_ID registered: %s)",
        aiokafka_version,
        registered,
    )
