import psutil
import shutil
import os
from mode import Service
from collections import defaultdict
from typing import Mapping, MutableMapping, Optional, Dict
from kaspr.types import (
    PT,
    TTLocation,
    DispatcherT,
    JanitorT,
    KasprAppT,
    KasprTableT,
)
from faust.sensors.monitor import Monitor
from mode.utils.objects import KeywordReduce


from kaspr.scheduler.utils import SchedulerPart

DISPATCHER = SchedulerPart.dispatcher
JANITOR = SchedulerPart.janitor


class DispatcherState(KeywordReduce):
    """Represents the current state of a dispatcher."""

    #: The dispatcher this object records statistics for
    dispatcher: DispatcherT = None

    #: Is dispatcher paused
    paused: bool = None

    #: Number of times a key has been retrieved from this table.
    messages_delivered: int = 0

    #: Last saved checkpoint
    last_checkpoint: Optional[TTLocation] = None

    #: Last Timetable location evaluated
    last_location: Optional[TTLocation] = None

    #: Last evaluated Timetable highwater mark
    last_highwater: Optional[TTLocation] = None

    @property
    def lag(self) -> int:
        """Difference between last location and highwater in seconds"""
        if self.last_location and self.last_highwater:
            return self.last_highwater.time_key - self.last_location.time_key

    def __init__(
        self,
        dispatcher: DispatcherT,
        *,
        paused: bool = None,
        messages_delivered: int = 0,
        last_checkpoint: TTLocation = None,
        last_location: TTLocation = None,
        last_highwater: TTLocation = None,
    ) -> None:
        self.paused = paused
        self.dispatcher = dispatcher
        self.messages_delivered = messages_delivered
        self.last_checkpoint = last_checkpoint
        self.last_location = last_location
        self.last_highwater = last_highwater

    def asdict(self) -> Mapping:
        """Return table state as dictionary."""
        return {
            "messages_delivered": self.messages_delivered,
            "last_checkpoint": self.last_checkpoint._asdict(),
        }

    def __reduce_keywords__(self) -> Mapping:
        return {**self.asdict(), "dispatcher": self.dispatcher}


class JanitorState(KeywordReduce):
    """Represents the current state of a dispatcher."""

    #: The janitor this object records statistics for.
    janitor: JanitorT = None

    #: Is janitor paused
    paused: bool = None

    #: Number of times a key has been deleted from the Timetable.
    messages_removed: int = 0

    #: Last saved checkpoint
    last_checkpoint: Optional[TTLocation] = None

    #: Last Timetable location evaluated
    last_location: Optional[TTLocation] = None

    #: Last evaluated Timetable highwater mark
    last_highwater: Optional[TTLocation] = None

    @property
    def lag(self) -> int:
        """Difference between last location and highwater in seconds"""
        if self.last_location and self.last_highwater:
            return self.last_highwater.time_key - self.last_location.time_key

    def __init__(
        self,
        janitor: JanitorT,
        *,
        paused: bool = None,
        messages_removed: int = 0,
        last_checkpoint: TTLocation = None,
        last_location: TTLocation = None,
        last_highwater: TTLocation = None,
    ) -> None:
        self.janitor = janitor
        self.paused = paused
        self.messages_removed = messages_removed
        self.last_checkpoint = last_checkpoint
        self.last_location = last_location
        self.last_highwater = last_highwater

    def asdict(self) -> Mapping:
        """Return table state as dictionary."""
        return {
            "messages_removed": self.messages_removed,
            "last_checkpoint": self.last_checkpoint._asdict(),
        }

    def __reduce_keywords__(self) -> Mapping:
        return {**self.asdict(), "janitor": self.janitor}


class InfraState(KeywordReduce):
    """Represents infrastructure metrics (CPU, memory, etc.)"""

    #: CPU usage (0.0 - 1.0)
    cpu_utilization: float

    #: RSS memory usage
    process_rss_bytes: int

    #: virtual memory utilization
    vm_utilization: float

    #: total virtual memory available
    vm_total: int

    #: total virtual memory used
    vm_used: int

    #: swap memory available
    sm_total: int

    #: swap memory used
    sm_used: int

    #: swap memory utilization
    sm_utilization: float

    #: Total disk space available in bytes
    disk_space_total_bytes: int

    #: Total disk space free in bytes
    disk_space_free_bytes: int

    #: Total disk space used in bytes
    disk_space_used_bytes: int


class KasprMonitor(Monitor):
    """Monitor records statistics about message scheduling, dispatching, clean up, etc."""

    app: KasprAppT

    process = psutil.Process

    #: Number of messages added to timetable by partition
    #: since startup
    scheduled_total: Mapping[int, int]

    #: Number of messages immediately sent out
    #: by scheduler because the message was
    #: already past due (since startup by partition)
    instant_send_total: Mapping[int, int]

    #: Number of REPLACE actions that replaced an existing schedule
    #: since startup by partition
    replaced_total: Mapping[int, int]

    #: Number of REPLACE actions that were no-ops due to identical schedule
    #: since startup by partition
    replace_noop_total: Mapping[int, int]

    #: Number of CANCEL actions that canceled an existing schedule
    #: since startup by partition
    canceled_total: Mapping[int, int]

    #: Number of cron schedules registered (since startup by partition)
    cron_registered_total: Mapping[int, int]

    #: Number of cron schedules canceled (since startup by partition)
    cron_canceled_total: Mapping[int, int]

    #: Number of cron schedules paused (since startup by partition)
    cron_paused_total: Mapping[int, int]

    #: Number of cron schedules resumed (since startup by partition)
    cron_resumed_total: Mapping[int, int]

    #: Number of cron fires materialized into timetable (since startup by partition)
    cron_fires_materialized_total: Mapping[int, int]

    #: Number of missed cron fires recovered during catchup (since startup by partition)
    cron_fires_missed_total: Mapping[int, int]

    #: Number of cron fires skipped due to skip policy (since startup by partition)
    cron_fires_skipped_total: Mapping[int, int]

    #: Infrastructure information
    infra: InfraState = None

    #: Mapping of dispatchers
    dispatchers: MutableMapping[str, DispatcherState] = None

    #: Mapping of partition to dispatcher
    dispatchers_by_partition: MutableMapping[int, DispatcherState] = None

    #: Mapping of janitors
    janitors: MutableMapping[str, JanitorState] = None

    #: Mapping of partition to janitor
    janitors_by_partition: MutableMapping[int, JanitorState] = None

    #: Mapping of SchedulerPart/partition to a timetable location
    checkpoints: Mapping[PT, TTLocation] = None

    #: #: Count of keys in Timetable
    count_timetable_keys: int

    #: Number of keys in table
    count_table_keys: Mapping[KasprTableT, int] = None

    def __init__(
        self,
        app: KasprAppT,
        *args,
        infra: InfraState = None,
        dispatchers: MutableMapping[str, DispatcherState] = None,
        janitors: MutableMapping[str, JanitorState] = None,
        checkpoints: MutableMapping[PT, TTLocation] = None,
        count_timetable_keys: int = None,
        **kwargs,
    ) -> None:
        self.app = app
        self.process = psutil.Process(os.getpid())
        self.scheduled_total = defaultdict(int)
        self.instant_send_total = defaultdict(int)
        self.replaced_total = defaultdict(int)
        self.replace_noop_total = defaultdict(int)
        self.canceled_total = defaultdict(int)
        self.cron_registered_total = defaultdict(int)
        self.cron_canceled_total = defaultdict(int)
        self.cron_paused_total = defaultdict(int)
        self.cron_resumed_total = defaultdict(int)
        self.cron_fires_materialized_total = defaultdict(int)
        self.cron_fires_missed_total = defaultdict(int)
        self.cron_fires_skipped_total = defaultdict(int)
        self.infra = InfraState() if infra is None else infra
        self.dispatchers = {} if dispatchers is None else dispatchers
        self.dispatchers_by_partition = (
            {}
            if dispatchers is None
            else {d.dispatcher.partition: d for d in dispatchers.values()}
        )
        self.janitors = {} if janitors is None else janitors
        self.janitors_by_partition = (
            {}
            if janitors is None
            else {j.janitor.partition: j for j in janitors.values()}
        )
        self.checkpoints = {} if checkpoints is None else checkpoints
        self.count_timetable_keys = count_timetable_keys or 0
        self.count_table_keys = defaultdict(int)

        super().__init__(*args, **kwargs)

    def on_app_started(self, app: KasprAppT):
        """Call when app has fully initilized, recovered and started."""
        ...

    def on_checkpoint_updated(self, pt: PT, location: TTLocation):
        """Call when the checkpoint for dispatcher or janitor is updated."""
        self.checkpoints[pt] = location
        if pt.part == DISPATCHER:
            if pt.partition in self.dispatchers_by_partition:
                self.dispatchers_by_partition[pt.partition].last_checkpoint = location
                self.on_dispatcher_checkpoint_updated(
                    self.dispatchers_by_partition[pt.partition].dispatcher, location
                )
        elif pt.part == JANITOR:
            if pt.partition in self.janitors_by_partition:
                self.janitors_by_partition[pt.partition].last_checkpoint = location
                self.on_janitor_checkpoint_updated(
                    self.janitors_by_partition[pt.partition].janitor, location
                )

    def on_dispatcher_location_updated(
        self, dispatcher: DispatcherT, location: TTLocation, highwater: TTLocation
    ):
        """Call when dispatcher completes evaluation of a Timetable location."""
        state = self._dispatcher_or_create(dispatcher)
        state.last_location = location
        state.last_highwater = highwater
        self.on_dispatcher_state_updated(dispatcher)

    def on_janitor_location_updated(
        self, janitor: JanitorT, location: TTLocation, highwater: TTLocation
    ):
        """Call when janitor completes evaluation of a Timetable location."""
        state = self._janitor_or_create(janitor)
        state.last_location = location
        state.last_highwater = highwater
        self.on_janitor_state_updated(janitor)

    def on_dispatcher_checkpoint_updated(
        self, dispatcher: DispatcherState, checkpoint: TTLocation
    ):
        ...

    def on_janitor_checkpoint_updated(
        self, janitor: JanitorState, checkpoint: TTLocation
    ):
        ...

    def on_dispatcher_state_updated(self, dispatcher: DispatcherT):
        ...

    def on_janitor_state_updated(self, janitor: JanitorT):
        ...

    def on_message_scheduled(self, location: TTLocation):
        """Call when a message is added to the Timetable."""
        self.scheduled_total[location.partition] += 1

    def on_message_delivered(
        self, dispatchor: Optional[DispatcherT] = None, partition: int = None
    ):
        """Call when a message is delivered to destination topic.

        Args:
            dispatcher:
                The dispatcher that delivered the message.
                Note, if the dispatcher is none, that means the message was
                delivered immediately at the time of scheduling
                (i.e the message was already past due.)
            partition:
                The scheduler partition number that delievered the message.
                This is only provided when dispatcher is None.
        """
        if dispatchor is None and partition is not None:
            self.instant_send_total[partition] += 1
        else:
            self._dispatcher_or_create(dispatchor).messages_delivered += 1

    def on_message_removed(self, janitor: JanitorT, location: TTLocation):
        """Call when a message is removed from Timetable."""
        self._janitor_or_create(janitor).messages_removed += 1

    def on_message_replaced(self, partition: int):
        """Call when REPLACE action updates an existing schedule entry."""
        self.replaced_total[partition] += 1

    def on_message_canceled(self, partition: int):
        """Call when CANCEL action removes an existing schedule entry."""
        self.canceled_total[partition] += 1

    def on_message_replace_noop(self, partition: int):
        """Call when REPLACE short-circuits because schedule is identical."""
        self.replace_noop_total[partition] += 1

    def on_cron_registered(self, partition: int):
        """Call when a cron schedule is registered."""
        self.cron_registered_total[partition] += 1

    def on_cron_canceled(self, partition: int):
        """Call when a cron schedule is canceled."""
        self.cron_canceled_total[partition] += 1

    def on_cron_paused(self, partition: int):
        """Call when a cron schedule is paused."""
        self.cron_paused_total[partition] += 1

    def on_cron_resumed(self, partition: int):
        """Call when a cron schedule is resumed."""
        self.cron_resumed_total[partition] += 1

    def on_cron_fire_materialized(self, partition: int):
        """Call when a cron fire is materialized into the timetable."""
        self.cron_fires_materialized_total[partition] += 1

    def on_cron_fires_missed(self, partition: int, count: int = 1):
        """Call when missed cron fires are recovered during catchup."""
        self.cron_fires_missed_total[partition] += count

    def on_cron_fires_skipped(self, partition: int, count: int = 1):
        """Call when cron fires are skipped due to skip policy."""
        self.cron_fires_skipped_total[partition] += count

    def on_cron_ticker_tick(self, partition: int):
        """Call when cron ticker completes a tick cycle."""
        ...

    def on_dispatcher_paused(self, dispatcher: DispatcherT):
        """Call when dispatcher paused processing."""
        self._dispatcher_or_create(dispatcher).paused = True

    def on_janitor_paused(self, janitor: JanitorT):
        """Call when janitor paused processing."""
        self._janitor_or_create(janitor).paused = True

    def on_dispatcher_resumed(self, dispatcher: DispatcherT):
        """Call when dispatcher resumes processing."""
        self._dispatcher_or_create(dispatcher).paused = False

    def on_janitor_resumed(self, janitor: JanitorT):
        """Call when janitor resumes processing."""
        self._janitor_or_create(janitor).paused = False

    def on_dispatcher_assigned(self, dispatcher: DispatcherT):
        """Call when dispatcher is assigned to worker."""
        self._dispatcher_or_create(dispatcher)

    def on_janitor_assigned(self, janitor: JanitorT):
        """Call when janitor is assigned to worker."""
        self._janitor_or_create(janitor)

    def on_dispatcher_revoked(self, dispatcher: DispatcherT):
        """Call when dispatcher is revoked from worker."""
        self._remove_dispatcher(dispatcher)

    def on_janitor_revoked(self, janitor: JanitorT):
        """Call when janitor is revoked from worker."""
        self._remove_janitor(janitor)

    def on_memory_stats_refreshed(self):
        """Call when memory stats are updated."""
        ...

    def on_cpu_stats_refreshed(self):
        """Call when CPU stats are updated."""
        ...

    def on_disk_stats_refreshed(self):
        """Call when disk usage stats are updated."""
        ...

    def janitor_state(self, janitor: JanitorT) -> JanitorState:
        return self._janitor_or_create(janitor)

    def dispatcher_state(self, dispatcher: DispatcherT) -> DispatcherState:
        return self._dispatcher_or_create(dispatcher)

    def _dispatcher_or_create(self, dispatcher: DispatcherT) -> DispatcherState:
        try:
            return self.dispatchers[dispatcher.name]
        except KeyError:
            state = self.dispatchers[dispatcher.name] = DispatcherState(dispatcher)
            self.dispatchers_by_partition[dispatcher.partition] = state
            return state

    def _janitor_or_create(self, janitor: JanitorT) -> JanitorState:
        try:
            return self.janitors[janitor.name]
        except KeyError:
            state = self.janitors[janitor.name] = JanitorState(janitor)
            self.janitors_by_partition[janitor.partition] = state
            return state

    def _remove_dispatcher(self, dispatcher: DispatcherT) -> None:
        if dispatcher.name in self.dispatchers:
            del self.dispatchers[dispatcher.name]
            del self.dispatchers_by_partition[dispatcher.partition]

    def _remove_janitor(self, janitor: JanitorT) -> None:
        if janitor.name in self.janitors:
            del self.janitors[janitor.name]
            del self.janitors_by_partition[janitor.partition]

    @Service.task
    async def _scheduler_sampler(self) -> None:
        async for sleep_time in self.itertimer(5.0, name="KasprMonitor.sampler"):
            self._sample_tables()
            self._sample_cpu()
            self._sample_memory()
            self._sample_disk_space()
            if self.app.conf.scheduler_enabled and self.app.conf.scheduler_cron_enabled:
                self._sample_cron_inventory()

    def _sample_tables(self):
        tt_changelog_name = None
        if self.app.conf.scheduler_enabled:
            tt_changelog_name = self.app.scheduler.timetable.changelog_topic.get_topic_name()
        for _, table in self.app.tables.items():
            table: KasprTableT = table
            if table.name == tt_changelog_name:
                self.count_timetable_keys = len(table.keys())
                self.on_timetable_size_refreshed(table)
            else:
                self.count_table_keys[table] = len(table.keys())
                self.on_table_key_count_refreshed(table)

    def _sample_memory(self):
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        self.infra.process_rss_bytes = self.process.memory_info().rss
        self.infra.vm_utilization = vm.percent
        self.infra.vm_total = vm.total
        self.infra.vm_used = vm.used
        self.infra.sm_total = sm.total
        self.infra.sm_used = sm.used
        self.infra.sm_utilization = sm.percent
        self.on_memory_stats_refreshed()

    def _sample_cpu(self):
        self.infra.cpu_utilization = self.process.cpu_percent()
        self.on_cpu_stats_refreshed()

    def _sample_disk_space(self):
        total, used, free = shutil.disk_usage(self.app.conf.tabledir)
        self.infra.disk_space_free_bytes = free
        self.infra.disk_space_total_bytes = total
        self.infra.disk_space_used_bytes = used
        self.on_disk_stats_refreshed()

    def _sample_cron_inventory(self):
        """Sample cron registry size."""
        cron_registry = self.app.scheduler.cron_registry
        self.on_cron_inventory_refreshed(len(cron_registry))

    def on_cron_inventory_refreshed(self, total_count: int):
        """Call when cron registry size is refreshed."""
        ...

    def on_timetable_size_refreshed(self, table: KasprTableT):
        """Count of keys in Timetable is refreshed."""
        ...

    def on_table_key_count_refreshed(self, table: KasprTableT):
        """Number of keys in table is refreshed."""
        ...

    def on_rebalance_start(self, *args, **kwargs) -> Dict:
        """Cluster rebalance in progress."""
        res = super().on_rebalance_start(*args, **kwargs)
        # reset the count of table keys
        self.count_table_keys = defaultdict(int)
        self.count_timetable_keys = 0
        return res

    def on_rebalance_return(self, *args, **kwargs) -> None:
        """Consumer replied assignment is done to broker."""
        super().on_rebalance_return(*args, **kwargs)

    def on_rebalance_end(self, *args, **kwargs) -> None:
        """Cluster rebalance fully completed (including recovery)."""
        super().on_rebalance_end(*args, **kwargs)

    def asdict(self) -> Mapping:
        """Return monitor state as dictionary."""
        base = super().asdict()
        base.update(
            {
                "dispatchers": {
                    name: dispatcher.asdict()
                    for name, dispatcher in self.dispatchers.items()
                },
                "janitors": {
                    name: janitor.asdict() for name, janitor in self.janitors.items()
                },
            }
        )
        return base
