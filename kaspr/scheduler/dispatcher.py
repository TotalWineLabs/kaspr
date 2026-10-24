import asyncio
import gc
from mode import Service
from mode.utils.objects import cached_property
from mode.utils.futures import notify
from typing import Any, Mapping, MutableSet, Optional, Set
from faust.types import ChannelT, StreamT, TopicT, FutureMessage, RecordMetadata
from kaspr.types import KasprAppT, CheckpointT, TTLocation, TTMessage, PT
from kaspr.sensors.kaspr import KasprMonitor
from mode.utils.locks import Event
from .utils import create_message_key, current_timekey, SchedulerPart

SECONDS_PER_DAY = 86400


class Dispatcher(Service):
    """Finds messages due for delivery.

    We scan a Timetable partition from the last persisted checkpoint up to the current
    wallclock. "Scans" are key lookups into the Timetable.

    We perform two scans: first by TimeKey (unix timestamp) and then by MessageKey
    (timestamp + sequence number). A scan progresses towards the wallclock by increments
    of 1 second.

    The value at TimeKey is an integer that tells us how many messages exist for that
    timestamp. Using that count, we further scan from sequence 0 to max count to retrieve
    all messages for that timestamp:

    Timetable
    ------------------------
    Key           | Value
    ------------------------
    1707171828    | 3         <--- TimeKey
    1707171828-0  | {...}     <--- MessageKey
    1707171828-1  | {...}     <--- MessageKey
    1707171828-2  | {...}     <--- MessageKey
    1707171901    | 1         <--- TimeKey
    1707171901-0  | {...}     <--- MessageKey
    ...

    """

    #: Records all statistics about dispatching
    monitor: KasprMonitor

    #: The Timetable partition dispatcher is working on
    partition: int

    # Buffer of pending message deliveries
    pending_deliveries: ChannelT

    #: Ensures dispatcher is paused during rebalance
    can_resume: Event
    flow_active: bool

    #: last Timetable location evaluated
    _last_location: int = None

    #: The dispatcher.wait_empty() method will set this to be notified
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
        self.pending_deliveries = app.channel(maxsize=1024, value_type=TTMessage)
        self.can_resume = Event()
        self.flow_active = False
        self._waiting_for_ack = None
        self._unacked_deliveries = set()
        self._pending_delivery_count = 0

    def on_init_dependencies(self):
        return []

    async def on_start(self) -> None:
        pass

    async def on_started(self) -> None:
        pass

    async def on_stop(self) -> None:
        pass

    def pause(self):
        """Pause saving."""
        self.can_resume.clear()
        self.flow_active = False
        self.monitor.on_dispatcher_paused(self)

    def resume(self):
        """Resume saving."""
        self.can_resume.set()
        self.flow_active = True
        self.monitor.on_dispatcher_resumed(self)

    def track_delivery(self, location: TTLocation) -> None:
        """Track delivery and mark it as pending ack."""
        # add to set of pending deliveries that must be acked for graceful
        # shutdown.
        self._unacked_deliveries.add(location)

    @property
    def default_checkpoint(self) -> TTLocation:
        """Checkpoint to use when no other exists.

        This is used when a dispatcher starts the very first time.
        """
        # default checkpoint
        default_timekey = (
            current_timekey()
            - self.app.conf.scheduler_dispatcher_default_checkpoint_lookback_days
            * SECONDS_PER_DAY
        )
        return TTLocation(self.partition, default_timekey)

    @property
    def highwater(self) -> TTLocation:
        """Timetable location dispatcher is working to get to."""
        # we offset wallclock by 1s to avoid issues with scheduler
        # writing to current TimeKey
        return TTLocation(self.partition, current_timekey() - 1)

    @property
    def last_location(self) -> Optional[TTLocation]:
        """Last timekey evaluated on Timetable."""
        return self._last_location

    @last_location.setter
    def last_location(self, loc: TTLocation):
        self._last_location = loc
        self.monitor.on_dispatcher_location_updated(self, loc, self.highwater)

    @cached_property
    def pt(self):
        """Tuple of (SchedulerPart.dispatcher and partition number) for this Dispatcher service."""
        return PT(SchedulerPart.dispatcher, self.partition)

    async def _maybe_wait(self):
        """Check if dispatching can continue. If not, wait until it can."""
        if not self.flow_active:
            self.log.dev("Waiting...")
            await self.wait(self.can_resume)

    async def wait_empty(self) -> None:
        """Wait for all deliveries that went out to complete."""
        wait_count = 0
        while not self.should_stop and self._unacked_deliveries:
            wait_count += 1
            if not wait_count % 10:
                remaining = [(d) for d in self._unacked_deliveries]
                self.log.warning("wait_empty: Waiting for deliveries %r", remaining)
            self.log.dev("STILL WAITING FOR DISPATCHER TO FINISH")
            self.log.dev("WAITING FOR %r DELIVERIES", len(self._unacked_deliveries))
            gc.collect()
            if not self._unacked_deliveries:
                break
            await self._wait_for_ack(timeout=1)

    async def _wait_for_ack(self, timeout: float) -> None:
        # arm future so that `ack()` can wake us up
        self._waiting_for_ack = asyncio.Future(loop=self.loop)
        try:
            # wait for `ack()` to wake us up
            await asyncio.wait_for(self._waiting_for_ack, loop=self.loop, timeout=1)
        except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
            pass
        finally:
            self._waiting_for_ack = None

    @Service.task
    async def _dispatch(self):
        """Find and dispatch due messages."""

        partition = self.partition
        checkpoints = self.checkpoints
        timetable = self.app.scheduler.timetable
        pending_deliveries = self.pending_deliveries

        await self.wait(self.app.scheduler.topics_created)
        await self._maybe_wait()

        if self.should_stop:
            return

        cp = checkpoints.get(self.pt, default=self.default_checkpoint)
        # starting timekey and sequence number
        time_key, seq = (
            cp.time_key,
            0 if cp.sequence < 0 else cp.sequence + 1,
        )

        while not self.should_stop:
            await self._maybe_wait()
            highwater = self.highwater
            time_key = (
                self.last_location.time_key + 1
                if self.last_location is not None
                else time_key
            )

            while time_key <= highwater.time_key:
                if self.last_location and time_key == self.last_location.time_key:
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
                # NOTE: allocated_slots is the number of *allocated* sequence slots, not
                # the number of live messages. A CANCEL removes a message entry but does
                # not decrement this value, so canceled slots appear as gaps (None) when
                # looked up below. This is a known tradeoff: we accept the cost of empty
                # lookups for canceled slots to keep CANCEL simple and append-only
                # sequence assignment intact. Use timetable key `timekey:live` for the
                # actual count of still-scheduled messages at this second.
                while seq < allocated_slots:
                    location = TTLocation(partition, time_key, seq)
                    await self._maybe_wait()
                    if self.should_stop:
                        break
                    message_key = create_message_key(location)
                    message = timetable.get_for_partition(
                        message_key, partition=partition
                    )
                    if message:
                        self._pending_delivery_count += 1
                        await pending_deliveries.put(TTMessage(message, location))
                    self.last_location = location
                    seq += 1
                    await asyncio.sleep(0)

                # reset sequence
                seq = 0
                time_key += 1
                self.last_location = location
                # give back control so loop can handle other tasks
                await asyncio.sleep(0)
            await self.sleep(0.25)

    def on_message_sent(self, delivery: TTMessage) -> None:
        """Called after a scheduled message is sent to a destination topic."""

        def _did_send(fut: FutureMessage):
            res: RecordMetadata = fut.result()

            # update checkpoint
            if res.offset is not None:
                self.monitor.on_message_delivered(self)
                prev, new = (
                    self.checkpoints.get(self.pt),
                    delivery.location,
                )
                if prev is None or new > prev:
                    self.checkpoints.update(self.pt, new)
                    self.log.dev(f"Delivered {new}!")
            notify(self._waiting_for_ack)
            self._unacked_deliveries.discard(delivery.location)
            self._pending_delivery_count -= 1

        return _did_send

    @Service.task
    async def deliver_messages(self):
        """Stream processor sending messages to destination topic(s)"""

        stream: StreamT[TTMessage] = self.app.stream(
            self.pending_deliveries, beacon=self.beacon
        )
        topics: Mapping[str, TopicT] = {}

        await self.app.tables.wait_until_recovery_completed()

        async for delivery in stream:
            await self._maybe_wait()
            # TODO: Confirm stream is "paused" during rebalance and recovery
            message = delivery.message
            tpname = message["__kms"]["d"]
            if not topics.get(tpname):
                topics[tpname] = self.app.topic(tpname)
            self.track_delivery(delivery.location)

            await topics.get(tpname).send(
                key=message["k"],
                value=message["v"],
                headers={
                    k: (v if isinstance(v, bytes) else v.encode())
                    for k, v in message["h"].items()
                }
                if message["h"] is not None
                else None,
                callback=self.on_message_sent(delivery),
            )

    @Service.task
    async def _periodic_checkpoint(self):
        """Periodically save dispatcher checkpoint.

        Only advances the checkpoint when no deliveries are in flight
        (pending in channel buffer or awaiting producer ack). This prevents
        the checkpoint from leaping ahead of unconfirmed deliveries, which
        would cause message loss on rebalance.
        """

        interval = self.app.conf.scheduler_dispatcher_checkpoint_interval
        await self._maybe_wait()
        while not self.should_stop:
            await self._maybe_wait()
            if (
                self.last_location
                and not self._unacked_deliveries
                and self._pending_delivery_count == 0
            ):
                self.checkpoints.update(self.pt, self.last_location)
            await self.sleep(interval)

    @property
    def unacked(self) -> Set[TTLocation]:
        """Return the set of currently unacknowledged deliveries."""
        return self._unacked_deliveries

    @cached_property
    def checkpoints(self) -> CheckpointT:
        return self.app.scheduler.checkpoints

    @cached_property
    def type(self):
        return "Dispatcher"

    @cached_property
    def name(self):
        return f"dispatcher-{self.partition}"

    @property
    def label(self) -> str:
        """Return human-readable description of dispatcher."""
        return self._dispatcher_label()

    def _dispatcher_label(self, name_suffix: str = "") -> str:
        s = f"{type(self).__name__}{name_suffix} (P{self.partition})"
        return s

    @property
    def shortlabel(self) -> str:
        """Return short description of dispatcher."""
        return self._dispatcher_label()
