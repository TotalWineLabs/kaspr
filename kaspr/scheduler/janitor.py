import gc
import asyncio
from mode import Service
from mode.utils.objects import cached_property
from mode.utils.futures import notify
from typing import Any, Optional, MutableSet
from faust.types import ChannelT, StreamT, FutureMessage, RecordMetadata
from kaspr.types import KasprAppT, CheckpointT, TTLocation, TTMessage, PT
from kaspr.sensors.kaspr import KasprMonitor
from mode.utils.locks import Event

from .utils import create_message_key, current_timekey, SchedulerPart, TK_LIVE_SUFFIX

SECONDS_PER_DAY = 86400


class Janitor(Service):
    """Removes old (already delivered) messages from Timetable."""

    #: Records all statistics about clean up
    monitor: KasprMonitor

    #: The Timetable partition dispatcher is working on
    partition: int

    #: Buffer of Timetable locations to-be-removed
    pending_removals: ChannelT

    #: Ensures dispatcher is paused during rebalance
    can_resume: Event
    flow_active: bool

    #: last Timetable location evaluated
    _last_location: int = None

    #: The wait_empty() method will set this to be notified
    #: when something acks a delivery.
    _waiting_for_ack: Optional[asyncio.Future] = None

    #: Set of unacked messages: that is messages that we started processing
    #: and that we MUST attempt to complete processing of, before
    #: shutting down or resuming a rebalance.
    _unacked_deliveries: MutableSet[TTLocation]

    def __init__(
        self, app: KasprAppT, partition: int, monitor: KasprMonitor, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.app = app
        self.monitor = monitor
        self.partition = partition
        self.pending_removals = app.channel(maxsize=1024, value_type=TTMessage)
        self.can_resume = Event()
        self.flow_active = False
        self._waiting_for_ack = None
        self._unacked_deliveries = set()
        self._pending_removal_count = 0

    async def on_start(self) -> None:
        pass

    async def on_started(self) -> None:
        pass

    async def on_stop(self) -> None:
        pass

    def pause(self):
        """Pause removal of messages in Timetable."""
        self.can_resume.clear()
        self.flow_active = False
        self.monitor.on_janitor_paused(self)

    def resume(self):
        """Resume cleaning."""
        self.can_resume.set()
        self.flow_active = True
        self.monitor.on_janitor_resumed(self)

    def track_removal(self, location: TTLocation) -> None:
        """Track deletion changelog and mark it as pending ack."""
        # add to set of pending deliveries that must be acked for graceful
        # shutdown.
        self._unacked_deliveries.add(location)

    @property
    def default_checkpoint(self) -> TTLocation:
        """Checkpoint to use when no other exists.

        This is used when a janitor starts the very first time.
        """
        # default checkpoint
        timekey = (
            current_timekey()
            - self.app.conf.scheduler_dispatcher_default_checkpoint_lookback_days
            * SECONDS_PER_DAY
        )
        return TTLocation(self.partition, timekey)

    @property
    def default_highwater(self) -> TTLocation:
        """Janitor uses a default highwater location when a dispatcher checkpoint is not found.

        This is generally only needed during the initial moments when an app starts
        for the first time and a dispatcher checkpoint is not yet set.
        """
        timekey = (
            current_timekey()
            - self.app.conf.scheduler_dispatcher_default_checkpoint_lookback_days
            * SECONDS_PER_DAY
        )
        return TTLocation(self.partition, timekey)

    @property
    def highwater(self) -> Optional[TTLocation]:
        """Timetable location janitor is working to get to.

        The janitor's highwater is (last dispatcher checkpoint - fixed offset)
        Dispatcher checkpoint may not be set, which in that case highwater will be None.
        """
        dispatcher_cp = self.checkpoints.get(self.dpt)
        if dispatcher_cp:
            timekey = int((int(dispatcher_cp.time_key) - self.highwater_offset) - 1)
            return TTLocation(self.partition, timekey)
        else:
            return None

    @cached_property
    def pt(self) -> PT:
        """Tuple of (SchedulerPart.janitor and partition number) for this Janitor service."""
        return PT(SchedulerPart.janitor, self.partition)

    @cached_property
    def dpt(self) -> PT:
        """Tuple of (SchedulerPart.dispatcher and partition number) for the dispatcher service for the same partition."""
        return PT(SchedulerPart.dispatcher, self.partition)

    @cached_property
    def highwater_offset(self) -> int:
        """Static highwater offset"""
        return self.app.conf.scheduler_janitor_highwater_offset_seconds

    @property
    def last_location(self) -> Optional[TTLocation]:
        """Last timekey evaluated on Timetable."""
        return self._last_location

    @last_location.setter
    def last_location(self, loc: TTLocation):
        self._last_location = loc
        self.monitor.on_janitor_location_updated(self, loc, self.highwater)

    async def _maybe_wait(self):
        """Check if cleaning can continue; it not, wait until it can."""
        if not self.flow_active:
            self.log.dev("Waiting to resume...")
            await self.wait(self.can_resume)

    async def wait_empty(self) -> None:
        """Wait for all deliveries that started processing to be acked."""
        wait_count = 0
        while not self.should_stop and self._unacked_deliveries:
            wait_count += 1
            if not wait_count % 10:
                remaining = [(d) for d in self._unacked_deliveries]
                self.log.warning("wait_empty: Waiting for changelogs %r", remaining)
            self.log.dev("STILL WAITING FOR CHANGELOG SENDS TO FINISH")
            self.log.dev(
                "WAITING FOR %r CHANGELOG CALLBACKS", len(self._unacked_deliveries)
            )
            gc.collect()
            if not self._unacked_deliveries:
                break
            await self._wait_for_ack(timeout=1)

    async def _wait_for_ack(self, timeout: float) -> None:
        # arm future so that `on_changelog_sent()` can wake us up
        self._waiting_for_ack = asyncio.Future(loop=self.loop)
        try:
            # wait for `ack()` to wake us up
            await asyncio.wait_for(self._waiting_for_ack, loop=self.loop, timeout=1)
        except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
            pass
        finally:
            self._waiting_for_ack = None

    @Service.task
    async def _clean(self):
        """Find and removes delievered messages from Timetable

        TODO:
            + Consider throttling the janitor when other processors
            are running.
        """

        partition = self.partition
        checkpoints = self.checkpoints
        timetable = self.app.scheduler.timetable
        pending_removals = self.pending_removals

        # Ensure topics are created
        await self.wait(self.app.scheduler.topics_created)

        # Ensure dispatcher has made a checkpoint
        # We depend on it for calculating janitor's highwater
        await self.wait(self.checkpoints.dispatcher_checkpointed)

        # Coast clear?
        await self._maybe_wait()

        if self.should_stop:
            return

        cp = checkpoints.get(self.pt, default=self.default_checkpoint)

        # starting sequence number for the restored TimeKey
        time_key, seq = (
            cp.time_key,
            0 if cp.sequence < 0 else cp.sequence + 1,
        )
        interval = self.app.conf.scheduler_janitor_clean_interval_seconds

        while not self.should_stop:
            await self._maybe_wait()
            time_key = (
                self.last_location.time_key + 1
                if self.last_location is not None
                else time_key
            )
            highwater = self.highwater
            while highwater is not None and time_key <= highwater.time_key:
                if self.last_location and time_key == self.last_location:
                    time_key += 1
                    continue

                location = TTLocation(partition, time_key, seq)
                await self._maybe_wait()
                if self.should_stop:
                    break
                allocated_slots = (
                    timetable.get_for_partition(str(time_key), partition=partition) or 0
                )
                if seq < allocated_slots:
                    self.log.info(
                        f"eval: {time_key} @ P{partition} has {allocated_slots} allocated slots."
                    )
                # remove in reverse order (SEQ - 1, SEQ - 2, SEQ - 3 ... 0)
                if allocated_slots:
                    seq = allocated_slots - 1
                    while seq >= 0:
                        location = TTLocation(partition, time_key, seq)
                        await self._maybe_wait()
                        if self.should_stop:
                            break
                        message_key = create_message_key(location)
                        message = timetable.get_for_partition(
                            message_key, partition=partition
                        )
                        if message:
                            self._pending_removal_count += 1
                            await pending_removals.put(location)
                        self.last_location = location
                        seq -= 1
                        await asyncio.sleep(0)

                    # remove the TimeKey itself
                    location = TTLocation(partition, time_key)
                    self.last_location = location
                    self._pending_removal_count += 1
                    await pending_removals.put(location)

                # reset sequence
                seq = 0
                time_key += 1
                self.last_location = location
                # give back control so loop can handle other tasks
                await asyncio.sleep(0)
            await self.sleep(interval)

    def on_changelog_sent(self, location: TTLocation) -> None:
        def _did_send(fut: FutureMessage):
            res: RecordMetadata = fut.result()

            # update checkpoint
            if res.offset is not None:
                self.monitor.on_message_removed(self, location)
                prev, new = (
                    self.checkpoints.get(self.pt),
                    location,
                )
                # Timetable entries are removed in timekey asc, sequence desc
                # we enforce checkpoints are updated in that order.
                if (
                    prev is None
                    or new.time_key > prev.time_key
                    or (new.time_key == prev.time_key and new.sequence < prev.sequence)
                ):
                    self.checkpoints.update(self.pt, new)
                    self.log.dev(f"Removed {location}")
            notify(self._waiting_for_ack)
            self._unacked_deliveries.discard(location)
            self._pending_removal_count -= 1

        return _did_send

    @Service.task
    async def remove_messages(self):
        """Process Timetable removal requests."""

        await self.wait(self.app.scheduler.topics_created)
        stream: StreamT[TTMessage] = self.app.stream(
            self.pending_removals, beacon=self.beacon
        )
        await self.app.tables.wait_until_recovery_completed()

        async for location in stream:
            await self._maybe_wait()
            # TODO: Confirm stream is "paused" during rebalance and recovery
            timetable = self.app.scheduler.timetable
            schedule_index = self.app.scheduler.schedule_index
            partition, timekey, sequence = (
                location.partition,
                location.time_key,
                location.sequence,
            )

            if sequence >= 0:
                message_key = create_message_key(location)
                message = timetable.get_for_partition(message_key, partition=partition)
                self.track_removal(location)
                timetable.del_for_partition(
                    message_key,
                    partition=partition,
                    callback=self.on_changelog_sent(location),
                )
                # Remove reverse index entry if this scheduled row was keyed by request_id.
                rid = ((message or {}).get("__kms") or {}).get("rid")
                if rid:
                    index_entry = schedule_index.get_for_partition(
                        rid, partition=partition
                    )
                    if index_entry:
                        idx_timekey = index_entry.get("tk")
                        idx_sequence = index_entry.get("seq")
                        if (
                            idx_timekey is not None
                            and idx_sequence is not None
                            and int(idx_timekey) == int(timekey)
                            and int(idx_sequence) == int(sequence)
                        ):
                            schedule_index.del_for_partition(rid, partition=partition)

            elif sequence < 0:
                self.track_removal(location)
                timetable.del_for_partition(
                    str(timekey),
                    partition=partition,
                    callback=self.on_changelog_sent(location),
                )
                # Remove the companion live-count key for this timekey, if present
                live_key = f"{timekey}{TK_LIVE_SUFFIX}"
                if timetable.get_for_partition(live_key, partition=partition) is not None:
                    timetable.del_for_partition(live_key, partition=partition)

    @Service.task
    async def _periodic_checkpoint(self):
        """Periodically save janitor checkpoint.

        Only advances the checkpoint when no removals are in flight
        (pending in channel buffer or awaiting changelog ack). This prevents
        the checkpoint from leaping ahead of unconfirmed removals, which
        would cause duplicate cleanup work on rebalance.
        """

        interval = self.app.conf.scheduler_janitor_checkpoint_interval
        await self._maybe_wait()
        while not self.should_stop:
            await self._maybe_wait()
            if (
                self.last_location
                and not self._unacked_deliveries
                and self._pending_removal_count == 0
            ):
                self.checkpoints.update(self.pt, self.last_location)
            await self.sleep(interval)

    @cached_property
    def checkpoints(self) -> CheckpointT:
        return self.app.scheduler.checkpoints

    @cached_property
    def type(self):
        return "Janitor"

    @cached_property
    def name(self):
        return f"janitor-{self.partition}"

    @property
    def label(self) -> str:
        """Return human-readable description of dispatcher."""
        return self._janitor_label()

    def _janitor_label(self, name_suffix: str = "") -> str:
        s = f"{type(self).__name__}{name_suffix} (P{self.partition})"
        return s

    @property
    def shortlabel(self) -> str:
        """Return short description of dispatcher."""
        return self._janitor_label()
