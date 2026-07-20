"""Monitor using Promethus."""
import typing
import kaspr

import re
from typing import Pattern, Mapping, List, Optional

from mode.utils.objects import cached_property
from faust.exceptions import ImproperlyConfigured
from faust import web

from aiohttp.web import Response

from faust.types.assignor import PartitionAssignorT
from faust.types.transports import ConsumerT, ProducerT
from faust.types import (
    CollectionT,
    EventT,
    Message,
    PendingMessage,
    RecordMetadata,
    StreamT,
    TP,
)

from .kaspr import KasprMonitor
from faust.sensors.monitor import TPOffsetMapping

from kaspr.utils.functional import utc_now
from kaspr.types import KasprAppT, KasprTableT, DispatcherT, JanitorT, TTLocation


try:
    import prometheus_client
    from prometheus_client import Counter, Gauge, Histogram, generate_latest, REGISTRY
except ImportError:  # pragma: no cover
    prometheus_client = None


__all__ = ["PrometheusMonitor"]

RE_NORMALIZE = re.compile(r"[\<\>:\s]+")
RE_NORMALIZE_SUBSTITUTION = "_"

now = utc_now


class PrometheusMonitor(KasprMonitor):
    """
    Prometheus Faust Sensor.
    This sensor, records statistics using prometheus_client and expose
    them using the aiohttp server running under /metrics by default
    Usage:
        import faust
        from faust.sensors.prometheus import PrometheusMonitor
        app = faust.App('example', broker='kafka://')
        app.monitor = PrometheusMonitor(app, pattern='/metrics')
    """

    ERROR = "error"
    COMPLETED = "completed"
    KEYS_RETRIEVED = "keys_retrieved"
    KEYS_UPDATED = "keys_updated"
    KEYS_DELETED = "keys_deleted"

    # App states
    INITIALIZING = -1
    OK = 0
    REBALANCING = 1
    RECOVERING = 2

    DISPATCHER = "D"
    JANITOR = "J"

    PAUSED = "PAUSED"
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"

    DEFAULT_LATENCY_WIDE_BUCKET = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.075,
        0.1,
        0.25,
        0.5,
        0.75,
        1.0,
        2.5,
        5.0,
        7.5,
        10.0,
        20.0,
        40.0,
        80.0,
        100.00,
        1000.0,
        5000.0,
        10000.0,
    )

    #: HTTP path where metrics are served
    pattern: str = None

    #: Prefix applied to all metrics
    metric_prefix = None

    common_labels: Mapping[str, str] = None

    def __init__(
        self,
        app: KasprAppT,
        pattern: str = "/metrics",
        metric_prefix="kaspr_",
        **kwargs,
    ) -> None:
        self.app = app
        self.pattern = pattern
        self.last_event_received_at = None
        self.metric_prefix = metric_prefix
        self.common_labels = {
            "app_name": self.app.conf.name,
            "worker_name": self.app.conf.worker_name,
        }

        if prometheus_client is None:
            raise ImproperlyConfigured(
                "prometheus_client requires `pip install prometheus_client`."
            )

        # Initialize base monitor state first so metric attributes set below
        # are not overwritten by KasprMonitor.__init__ defaults.
        super().__init__(app=app, **kwargs)
        self._initialize_metrics()
        self.expose_metrics()
        self._on_init()

    def _on_init(self):
        """App is preparing to initialize."""
        self.app_info.labels(
            group_id=self.app.conf.name,
            kaspr_version=kaspr.__version__,
            kms_enabled="1" if self.app.conf.scheduler_enabled else "0",
            **self.common_labels,
        ).set_to_current_time()
        self.health.labels(**self.common_labels).set(self.INITIALIZING)

    def _initialize_metrics(self) -> None:
        """
        Initialize Prometheus metrics
        """

        prefix = self.metric_prefix
        common_label_keys = self.common_label_keys

        # On message received
        self.messages_received = Counter(
            f"{prefix}messages_received",
            "Total messages received",
            [*self.common_label_keys],
        )
        self.active_messages = Gauge(
            f"{prefix}active_messages", "Total active messages"
        )
        self.messages_received_per_topics = Counter(
            f"{prefix}messages_received_per_topic",
            "Messages received per topic",
            ["topic", *common_label_keys],
        )
        self.messages_received_per_topics_partition = Gauge(
            f"{prefix}messages_received_per_topics_partition",
            "Messages received per topic/partition",
            ["topic", "partition", *common_label_keys],
        )
        self.events_runtime_latency = Histogram(
            f"{prefix}events_runtime_ms",
            "Events runtime in ms",
            buckets=self.DEFAULT_LATENCY_WIDE_BUCKET,
        )

        # On Event Stream in
        self.total_events = Counter(f"{prefix}total_events", "Total events received")
        self.total_active_events = Gauge(
            f"{prefix}total_active_events", "Total active events"
        )
        self.total_events_per_stream = Counter(
            f"{prefix}total_events_per_stream", "Events received per Stream", ["stream"]
        )

        # Tables
        # On table changes get/set/del keys
        self.table_operations = Counter(
            f"{prefix}table_operations",
            "Total table operations",
            ["table", "operation", *self.common_label_keys],
        )
        self.table_key_count = Gauge(
            f"{prefix}table_key_count",
            "Total keys in table",
            labelnames=[
                "table", *common_label_keys,
            ],
        )

        # On message send
        self.topic_messages_sent = Counter(
            f"{prefix}topic_messages_sent",
            "Total messages sent per topic",
            ["topic", *self.common_label_keys],
        )
        self.total_sent_messages = Counter(
            f"{prefix}total_sent_messages",
            "Total messages sent",
            [*self.common_label_keys],
        )
        self.producer_send_latency = Histogram(
            f"{prefix}producer_send_latency",
            "Producer send latency in ms",
            buckets=self.DEFAULT_LATENCY_WIDE_BUCKET,
            labelnames=[*self.common_label_keys],
        )
        self.total_error_messages_sent = Counter(
            f"{prefix}total_error_messages_sent",
            "Total error messages sent",
            [*self.common_label_keys],
        )
        self.producer_error_send_latency = Histogram(
            f"{prefix}producer_error_send_latency",
            "Producer error send latency in ms",
            [*self.common_label_keys],
        )

        # Assignment
        self.assignment_operations = Counter(
            f"{prefix}assignment_operations",
            "Total assigment operations (completed/error)",
            ["operation"],
        )
        self.assign_latency = Histogram(
            f"{prefix}assign_latency", "Assignment latency in ms"
        )

        # Rebalances
        self.total_rebalances = Gauge(f"{prefix}total_rebalances", "Total rebalances")
        self.total_rebalances_recovering = Gauge(
            f"{prefix}total_rebalances_recovering", "Total rebalances recovering"
        )
        self.rebalance_done_consumer_latency = Histogram(
            f"{prefix}rebalance_done_consumer_latency",
            "Consumer replying that rebalance is done to broker in ms",
        )
        self.rebalance_done_latency = Histogram(
            f"{prefix}rebalance_done_latency",
            "Rebalance finished latency in ms",
            [*self.common_label_keys],
        )
        self.health = Gauge(
            f"{prefix}health",
            "Current state of application.",
            labelnames=[
                *common_label_keys,
            ],
        )

        # Count Metrics by name
        self.count_metrics_by_name = Gauge(
            f"{prefix}metrics_by_name", "Total metrics by name", ["metric"]
        )

        # Web
        self.http_status_codes = Counter(
            f"{prefix}http_status_codes", "Total http_status code", ["status_code"]
        )
        self.http_latency = Histogram(
            f"{prefix}http_latency", "Http response latency in ms"
        )

        # Topic/Partition Offsets
        self.topic_partition_end_offset = Gauge(
            f"{prefix}topic_partition_end_offset",
            "Offset ends per topic/partition",
            ["topic", "partition", *self.common_label_keys],
        )
        self.topic_partition_offset_commited = Gauge(
            f"{prefix}topic_partition_offset_commited",
            "Offset commited per topic/partition",
            ["topic", "partition", *self.common_label_keys],
        )
        self.consumer_commit_latency = Histogram(
            f"{prefix}consumer_commit_latency",
            "Consumer commit latency in ms",
            buckets=self.DEFAULT_LATENCY_WIDE_BUCKET,
        )

        # App information
        self.app_info = Gauge(
            f"{prefix}app_info",
            "Application info",
            labelnames=[
                "group_id",
                "kaspr_version",
                "kms_enabled",
                *common_label_keys,
            ],
        )

        # App infra metrics
        self.cpu_usage_percent = Gauge(
            f"{prefix}cpu_utiliztion_percent",
            "CPU usage.",
            labelnames=[
                *common_label_keys,
            ],
        )
        self.resident_memory_total = Gauge(
            f"{prefix}resident_memory_total",
            "Total resident memory used.",
            labelnames=[
                *common_label_keys,
            ],
        )
        self.virtual_memory_total = Gauge(
            f"{prefix}virtual_memory_total",
            "Total available virtual memory",
            labelnames=[
                *common_label_keys,
            ],
        )
        self.virtual_memory_used = Gauge(
            f"{prefix}virtual_memory_used",
            "Total virtual memory used",
            labelnames=[
                *common_label_keys,
            ],
        )
        self.virtual_memory_utilization = Gauge(
            f"{prefix}virtual_memory_utilization",
            "Application virtual memory utilization",
            labelnames=[
                *common_label_keys,
            ],
        )
        self.swap_memory_total = Gauge(
            f"{prefix}swap_memory_total",
            "Total available swap memory",
            labelnames=[
                *common_label_keys,
            ],
        )
        self.swap_memory_used = Gauge(
            f"{prefix}swap_memory_used",
            "Total swap memory used",
            labelnames=[
                *common_label_keys,
            ],
        )
        self.swap_memory_utilization = Gauge(
            f"{prefix}swap_memory_utilization",
            "Application virtual memory utilization",
            labelnames=[
                *common_label_keys,
            ],
        )

        # Disk space
        self.disk_space_total = Gauge(
            f"{prefix}disk_space_total",
            "Disk space total bytes",
            labelnames=[
                *common_label_keys,
            ],
        )
        self.disk_space_free = Gauge(
            f"{prefix}disk_space_free",
            "Disk space free bytes",
            labelnames=[
                *common_label_keys,
            ],
        )
        self.disk_space_used = Gauge(
            f"{prefix}disk_space_used",
            "Disk space used bytes",
            labelnames=[
                *common_label_keys,
            ],
        )

        if self.app.conf.scheduler_enabled:
            # Scheduler metrics
            self.dispatcher_info = Gauge(
                f"{prefix}kms_dispatcher_info",
                "Dispatcher start time and info.",
                labelnames=[
                    *common_label_keys,
                    "partition",
                    "state",
                ],
            )
            self.janitor_info = Gauge(
                f"{prefix}kms_janitor_info",
                "Janitor start time and info.",
                labelnames=[
                    *common_label_keys,
                    "partition",
                    "state",
                ],
            )
            self.last_location_time = Gauge(
                f"{prefix}kms_last_location_time",
                "Latest timekey evaluated",
                labelnames=[*common_label_keys, "type", "partition"],
            )
            self.last_location_seq = Gauge(
                f"{prefix}kms_last_location_seq",
                "Latest timekey sequence evaluated",
                labelnames=[*common_label_keys, "type", "partition"],
            )
            self.location_lag = Gauge(
                f"{prefix}kms_last_location_lag",
                "Seconds behind highwater",
                labelnames=[*common_label_keys, "type", "partition"],
            )
            self.timetable_size = Gauge(
                f"{prefix}kms_timetable_size",
                "Count of keys in  timetable",
                labelnames=[
                    *common_label_keys,
                ],
            )          
            self.messages_scheduled = Counter(
                f"{prefix}kms_messages_scheduled",
                "Total messages scheduled",
                labelnames=[
                    *common_label_keys,
                ],
            )
            self.messages_delivered = Counter(
                f"{prefix}kms_messages_delivered",
                "Total messages delivered",
                labelnames=[
                    *common_label_keys,
                ],
            )
            self.messages_removed = Counter(
                f"{prefix}kms_messages_removed",
                "Total messages removed.",
                labelnames=[
                    *common_label_keys,
                ],
            )
            self.messages_delivered_instant = Counter(
                f"{prefix}kms_messages_delivered_instant",
                "Total messages delivered without scheduling",
                labelnames=[
                    *common_label_keys,
                ],
            )
            self.messages_replaced = Counter(
                f"{prefix}kms_messages_replaced",
                "Total REPLACE actions that replaced an existing schedule",
                labelnames=[
                    *common_label_keys,
                ],
            )
            self.messages_replace_noop = Counter(
                f"{prefix}kms_messages_replace_noop",
                "Total REPLACE actions that were no-ops due to identical schedules",
                labelnames=[
                    *common_label_keys,
                ],
            )
            self.messages_canceled = Counter(
                f"{prefix}kms_messages_canceled",
                "Total CANCEL actions that removed an existing schedule",
                labelnames=[
                    *common_label_keys,
                ],
            )
            self.active_deliveries = Gauge(
                f"{prefix}kms_active_deliveries",
                "Messages delievered during uptime.",
                labelnames=[*common_label_keys, "partition"],
            )
            self.active_removals = Gauge(
                f"{prefix}kms_active_removals",
                "Messages removed during uptime.",
                labelnames=[*common_label_keys, "partition"],
            )

        if self.app.conf.scheduler_enabled and self.app.conf.scheduler_cron_enabled:
            # Cron scheduler metrics
            self.cron_registered_total = Counter(
                f"{prefix}kms_cron_registered",
                "Total cron schedules registered",
                labelnames=[*common_label_keys],
            )
            self.cron_canceled_total = Counter(
                f"{prefix}kms_cron_canceled",
                "Total cron schedules canceled",
                labelnames=[*common_label_keys],
            )
            self.cron_paused_total = Counter(
                f"{prefix}kms_cron_paused",
                "Total cron schedules paused",
                labelnames=[*common_label_keys],
            )
            self.cron_resumed_total = Counter(
                f"{prefix}kms_cron_resumed",
                "Total cron schedules resumed",
                labelnames=[*common_label_keys],
            )
            self.cron_fires_materialized_total = Counter(
                f"{prefix}kms_cron_fires_materialized",
                "Total cron fires materialized into timetable",
                labelnames=[*common_label_keys],
            )
            self.cron_fires_missed_total = Counter(
                f"{prefix}kms_cron_fires_missed",
                "Total missed cron fires recovered during catchup",
                labelnames=[*common_label_keys],
            )
            self.cron_fires_skipped_total = Counter(
                f"{prefix}kms_cron_fires_skipped",
                "Total cron fires skipped due to skip policy",
                labelnames=[*common_label_keys],
            )
            self.cron_ticker_last_tick = Gauge(
                f"{prefix}kms_cron_ticker_last_tick",
                "Epoch timestamp of last completed tick per partition",
                labelnames=[*common_label_keys, "partition"],
            )
            self.cron_registry_size = Gauge(
                f"{prefix}kms_cron_registry_size",
                "Total number of cron schedules in registry",
                labelnames=[*common_label_keys],
            )

    def on_app_started(self, app: KasprAppT):
        """App has initilized, completed rebalance/recovery and ready for processing."""
        super().on_app_started(app)

    def on_dispatcher_assigned(self, dispatcher: DispatcherT):
        """Call when dispatcher is assigned to worker."""
        super().on_dispatcher_assigned(dispatcher)
        self.dispatcher_info.labels(
            partition=dispatcher.partition,
            state=self.ACTIVE if dispatcher.flow_active else self.PAUSED,
            **self.common_labels,
        ).set_to_current_time()

    def on_janitor_assigned(self, janitor: JanitorT):
        """Call when janitor is assigned to worker."""
        super().on_janitor_assigned(janitor)
        self.janitor_info.labels(
            partition=janitor.partition,
            state=self.ACTIVE if janitor.flow_active else self.PAUSED,
            **self.common_labels,
        ).set_to_current_time()

    def on_dispatcher_paused(self, dispatcher: DispatcherT):
        """Dispatcher entered paused state."""
        super().on_dispatcher_paused(dispatcher)
        self.dispatcher_info.labels(
            partition=dispatcher.partition, state=self.PAUSED, **self.common_labels
        ).set(0)

    def on_janitor_paused(self, janitor: JanitorT):
        """Janitor entered paused state."""
        super().on_janitor_paused(janitor)
        self.janitor_info.labels(
            partition=janitor.partition, state=self.PAUSED, **self.common_labels
        ).set(0)

    def on_dispatcher_resumed(self, dispatcher: DispatcherT):
        """Dispatcher continues processing."""
        super().on_dispatcher_resumed(dispatcher)
        self.dispatcher_info.labels(
            partition=dispatcher.partition, state=self.ACTIVE, **self.common_labels
        ).set_to_current_time()

    def on_janitor_resumed(self, janitor: JanitorT):
        """Janitor continues processing."""
        super().on_janitor_resumed(janitor)
        self.janitor_info.labels(
            partition=janitor.partition, state=self.ACTIVE, **self.common_labels
        ).set_to_current_time()

    def on_dispatcher_revoked(self, dispatcher: DispatcherT):
        """Dispatcher revoked."""
        super().on_dispatcher_revoked(dispatcher)
        self.dispatcher_info.labels(
            partition=dispatcher.partition, state=self.REVOKED, **self.common_labels
        ).set(0)
        # Remove partition-level gauge metrics so stale values are no longer reported
        partition = str(dispatcher.partition)
        label_values = (*self.common_labels.values(), self.DISPATCHER, partition)
        self.last_location_time.remove(*label_values)
        self.last_location_seq.remove(*label_values)
        self.location_lag.remove(*label_values)
        self.active_deliveries.remove(*self.common_labels.values(), partition)

    def on_janitor_revoked(self, janitor: JanitorT):
        """Janitor revoked."""
        super().on_janitor_revoked(janitor)
        self.janitor_info.labels(
            partition=janitor.partition, state=self.REVOKED, **self.common_labels
        ).set(0)
        # Remove partition-level gauge metrics so stale values are no longer reported
        partition = str(janitor.partition)
        label_values = (*self.common_labels.values(), self.JANITOR, partition)
        self.last_location_time.remove(*label_values)
        self.last_location_seq.remove(*label_values)
        self.location_lag.remove(*label_values)
        self.active_removals.remove(*self.common_labels.values(), partition)


    def on_dispatcher_checkpoint_updated(
        self, dispatcher: DispatcherT, checkpoint: TTLocation
    ):
        """Dispatcher checkpoint updated."""
        super().on_dispatcher_checkpoint_updated(dispatcher, checkpoint)

    def on_janitor_checkpoint_updated(self, janitor: JanitorT, checkpoint: TTLocation):
        """Janitor checkpoint updated."""
        super().on_janitor_checkpoint_updated(janitor, checkpoint)

    def on_dispatcher_state_updated(self, dispatcher: DispatcherT):
        super().on_dispatcher_state_updated(dispatcher)
        state = self.dispatchers[dispatcher.name]
        last_location = state.last_location
        partition = str(dispatcher.partition)
        self.last_location_time.labels(
            **self.common_labels, type=self.DISPATCHER, partition=partition
        ).set(last_location.time_key)
        self.last_location_seq.labels(
            **self.common_labels, type=self.DISPATCHER, partition=partition
        ).set(last_location.sequence)
        self.location_lag.labels(
            **self.common_labels, type=self.DISPATCHER, partition=partition
        ).set(state.lag)
        self.active_deliveries.labels(**self.common_labels, partition=partition).set(
            state.messages_delivered
        )

    def on_janitor_state_updated(self, janitor: JanitorT):
        super().on_janitor_state_updated(janitor)
        state = self.janitors[janitor.name]
        last_location = state.last_location
        partition = str(janitor.partition)
        self.last_location_time.labels(
            **self.common_labels, type=self.JANITOR, partition=partition
        ).set(last_location.time_key)
        self.last_location_seq.labels(
            **self.common_labels, type=self.JANITOR, partition=partition
        ).set(last_location.sequence)
        self.location_lag.labels(
            **self.common_labels, type=self.JANITOR, partition=partition
        ).set(state.lag)
        self.active_removals.labels(**self.common_labels, partition=partition).set(
            state.messages_removed
        )

    def on_message_scheduled(self, location: TTLocation):
        """Call when a message is added to the Timetable."""
        super().on_message_scheduled(location)
        self.messages_scheduled.labels(**self.common_labels).inc()

    def on_message_delivered(
        self, dispatchor: Optional[DispatcherT] = None, partition: int = None
    ):
        """Call when a message is delivered to destination topic."""
        super().on_message_delivered(dispatchor, partition)
        self.messages_delivered.labels(**self.common_labels).inc()
        if partition is not None:
            self.messages_delivered_instant.labels(**self.common_labels).inc()

    def on_message_removed(self, janitor: JanitorT, location: TTLocation):
        """Call when a message is removed from Timetable."""
        super().on_message_removed(janitor, location)
        self.messages_removed.labels(**self.common_labels).inc()

    def on_message_replaced(self, partition: int):
        """Call when a REPLACE action updates an existing schedule entry."""
        super().on_message_replaced(partition)
        self.messages_replaced.labels(**self.common_labels).inc()

    def on_message_canceled(self, partition: int):
        """Call when a CANCEL action removes an existing schedule entry."""
        super().on_message_canceled(partition)
        self.messages_canceled.labels(**self.common_labels).inc()

    def on_message_replace_noop(self, partition: int):
        """Call when REPLACE short-circuits because schedule is identical."""
        super().on_message_replace_noop(partition)
        self.messages_replace_noop.labels(**self.common_labels).inc()

    def on_cron_registered(self, partition: int):
        """Call when a cron schedule is registered."""
        super().on_cron_registered(partition)
        if self.app.conf.scheduler_cron_enabled:
            self.cron_registered_total.labels(**self.common_labels).inc()

    def on_cron_canceled(self, partition: int):
        """Call when a cron schedule is canceled."""
        super().on_cron_canceled(partition)
        if self.app.conf.scheduler_cron_enabled:
            self.cron_canceled_total.labels(**self.common_labels).inc()

    def on_cron_paused(self, partition: int):
        """Call when a cron schedule is paused."""
        super().on_cron_paused(partition)
        if self.app.conf.scheduler_cron_enabled:
            self.cron_paused_total.labels(**self.common_labels).inc()

    def on_cron_resumed(self, partition: int):
        """Call when a cron schedule is resumed."""
        super().on_cron_resumed(partition)
        if self.app.conf.scheduler_cron_enabled:
            self.cron_resumed_total.labels(**self.common_labels).inc()

    def on_cron_fire_materialized(self, partition: int):
        """Call when a cron fire is materialized into the timetable."""
        super().on_cron_fire_materialized(partition)
        if self.app.conf.scheduler_cron_enabled:
            self.cron_fires_materialized_total.labels(**self.common_labels).inc()

    def on_cron_fires_missed(self, partition: int, count: int = 1):
        """Call when missed cron fires are recovered during catchup."""
        super().on_cron_fires_missed(partition, count)
        if self.app.conf.scheduler_cron_enabled:
            self.cron_fires_missed_total.labels(**self.common_labels).inc(count)

    def on_cron_fires_skipped(self, partition: int, count: int = 1):
        """Call when cron fires are skipped due to skip policy."""
        super().on_cron_fires_skipped(partition, count)
        if self.app.conf.scheduler_cron_enabled:
            self.cron_fires_skipped_total.labels(**self.common_labels).inc(count)

    def on_cron_ticker_tick(self, partition: int):
        """Call when cron ticker completes a tick cycle."""
        super().on_cron_ticker_tick(partition)
        if self.app.conf.scheduler_cron_enabled:
            self.cron_ticker_last_tick.labels(
                **self.common_labels, partition=str(partition)
            ).set_to_current_time()

    def on_cron_inventory_refreshed(self, total_count: int):
        """Call when cron registry size is refreshed."""
        if self.app.conf.scheduler_cron_enabled:
            self.cron_registry_size.labels(**self.common_labels).set(total_count)

    def on_memory_stats_refreshed(self):
        """Memory usage stats updated."""
        self.resident_memory_total.labels(**self.common_labels).set(
            self.infra.process_rss_bytes
        )
        self.virtual_memory_utilization.labels(**self.common_labels).set(
            self.infra.vm_utilization
        )
        self.virtual_memory_total.labels(**self.common_labels).set(self.infra.vm_total)
        self.virtual_memory_used.labels(**self.common_labels).set(self.infra.vm_used)
        self.swap_memory_total.labels(**self.common_labels).set(self.infra.sm_total)
        self.swap_memory_used.labels(**self.common_labels).set(self.infra.sm_used)
        self.swap_memory_utilization.labels(**self.common_labels).set(
            self.infra.sm_utilization
        )

    def on_cpu_stats_refreshed(self):
        """CPU usage updated."""
        self.cpu_usage_percent.labels(**self.common_labels).set(
            self.infra.cpu_utilization
        )

    def on_disk_stats_refreshed(self):
        """Disk space stats updated."""
        self.disk_space_total.labels(**self.common_labels).set(
            self.infra.disk_space_total_bytes
        )
        self.disk_space_used.labels(**self.common_labels).set(
            self.infra.disk_space_used_bytes
        )
        self.disk_space_free.labels(**self.common_labels).set(
            self.infra.disk_space_free_bytes
        )

    def on_timetable_size_refreshed(self, table: KasprTableT):
        """Count of keys in Timetable is refreshed."""
        self.timetable_size.labels(**self.common_labels).set(self.count_timetable_keys)

    def on_table_key_count_refreshed(self, table: KasprTableT):
        """Count of keys in table is refreshed."""
        self.table_key_count.labels(table=f"table.{table.name}", **self.common_labels).set(self.count_table_keys[table])

    def on_message_in(self, tp: TP, offset: int, message: Message) -> None:
        """Call before message is delegated to streams."""
        super().on_message_in(tp, offset, message)

        self.messages_received.labels(**self.common_labels).inc()
        self.active_messages.inc()
        self.messages_received_per_topics.labels(
            topic=tp.topic, **self.common_labels
        ).inc()
        self.messages_received_per_topics_partition.labels(
            topic=tp.topic, partition=tp.partition, **self.common_labels
        ).set(offset)

    def on_stream_event_in(
        self, tp: TP, offset: int, stream: StreamT, event: EventT
    ) -> typing.Optional[typing.Dict]:
        """Call when stream starts processing an event."""
        state = super().on_stream_event_in(tp, offset, stream, event)
        self.last_event_received_at = now()
        self.total_events.inc()
        self.total_active_events.inc()
        self.total_events_per_stream.labels(
            stream=f"stream.{self._stream_label(stream)}.events"
        ).inc()

        return state

    def _stream_label(self, stream: StreamT) -> str:
        return (
            self._normalize(
                stream.shortlabel.lstrip("Stream:"),
            )
            .strip("_")
            .lower()
        )

    def on_stream_event_out(
        self,
        tp: TP,
        offset: int,
        stream: StreamT,
        event: EventT,
        state: typing.Dict = None,
    ) -> None:
        """Call when stream is done processing an event."""
        super().on_stream_event_out(tp, offset, stream, event, state)
        self.total_active_events.dec()
        if state is not None:
            self.events_runtime_latency.observe(
                self.secs_to_ms(self.events_runtime[-1])
            )

    def on_message_out(self, tp: TP, offset: int, message: Message) -> None:
        """Call when message is fully acknowledged and can be committed."""
        super().on_message_out(tp, offset, message)
        self.active_messages.dec()

    def on_table_get(self, table: CollectionT, key: typing.Any) -> None:
        """Call when value in table is retrieved."""
        super().on_table_get(table, key)
        self.table_operations.labels(
            table=f"table.{table.name}",
            operation=self.KEYS_RETRIEVED,
            **self.common_labels,
        ).inc()

    def on_table_set(
        self, table: CollectionT, key: typing.Any, value: typing.Any
    ) -> None:
        """Call when new value for key in table is set."""
        super().on_table_set(table, key, value)
        self.table_operations.labels(
            table=f"table.{table.name}",
            operation=self.KEYS_UPDATED,
            **self.common_labels,
        ).inc()

    def on_table_del(self, table: CollectionT, key: typing.Any) -> None:
        """Call when key in a table is deleted."""
        super().on_table_del(table, key)
        self.table_operations.labels(
            table=f"table.{table.name}",
            operation=self.KEYS_DELETED,
            **self.common_labels,
        ).inc()

    def on_commit_completed(self, consumer: ConsumerT, state: typing.Any) -> None:
        """Call when consumer commit offset operation completed."""
        super().on_commit_completed(consumer, state)
        self.consumer_commit_latency.observe(self.ms_since(typing.cast(float, state)))

    def on_send_initiated(
        self,
        producer: ProducerT,
        topic: str,
        message: PendingMessage,
        keysize: int,
        valsize: int,
    ) -> typing.Any:
        """Call when message added to producer buffer."""
        self.topic_messages_sent.labels(
            topic=f"topic.{topic}", **self.common_labels
        ).inc()

        return super().on_send_initiated(producer, topic, message, keysize, valsize)

    def on_send_completed(
        self, producer: ProducerT, state: typing.Any, metadata: RecordMetadata
    ) -> None:
        """Call when producer finished sending message."""
        super().on_send_completed(producer, state, metadata)
        self.total_sent_messages.labels(**self.common_labels).inc()
        self.producer_send_latency.labels(**self.common_labels).observe(
            self.ms_since(typing.cast(float, state))
        )

    def on_send_error(
        self, producer: ProducerT, exc: BaseException, state: typing.Any
    ) -> None:
        """Call when producer was unable to publish message."""
        super().on_send_error(producer, exc, state)
        self.total_error_messages_sent.labels(**self.common_labels).inc()
        self.producer_error_send_latency.labels(**self.common_labels).observe(
            self.ms_since(typing.cast(float, state))
        )

    def on_assignment_error(
        self, assignor: PartitionAssignorT, state: typing.Dict, exc: BaseException
    ) -> None:
        """Partition assignor did not complete assignor due to error."""
        super().on_assignment_error(assignor, state, exc)
        self.assignment_operations.labels(operation=self.ERROR).inc()
        self.assign_latency.observe(self.ms_since(state["time_start"]))

    def on_assignment_completed(
        self, assignor: PartitionAssignorT, state: typing.Dict
    ) -> None:
        """Partition assignor completed assignment."""
        super().on_assignment_completed(assignor, state)
        self.assignment_operations.labels(operation=self.COMPLETED).inc()
        self.assign_latency.observe(self.ms_since(state["time_start"]))

    def on_rebalance_start(self, app: KasprAppT) -> typing.Dict:
        """Cluster rebalance in progress."""
        state = super().on_rebalance_start(app)
        self.total_rebalances.inc()
        self.health.labels(**self.common_labels).set(self.REBALANCING)
        return state

    def on_rebalance_return(self, app: KasprAppT, state: typing.Dict) -> None:
        """Consumer replied assignment is done to broker."""
        super().on_rebalance_return(app, state)
        self.total_rebalances.dec()
        self.total_rebalances_recovering.inc()
        self.rebalance_done_consumer_latency.observe(
            self.ms_since(state["time_return"])
        )
        if self.tables:
            self.health.labels(**self.common_labels).set(self.RECOVERING)

    def on_rebalance_end(self, app: KasprAppT, state: typing.Dict) -> None:
        """Cluster rebalance fully completed (including recovery)."""
        super().on_rebalance_end(app, state)
        self.total_rebalances_recovering.dec()
        self.rebalance_done_latency.labels(**self.common_labels).observe(self.ms_since(state["time_start"]))
        self.health.labels(**self.common_labels).set(self.OK)

    def count(self, metric_name: str, count: int = 1) -> None:
        """Count metric by name."""
        super().count(metric_name, count=count)
        self.count_metrics_by_name.labels(metric=metric_name).inc(count)

    def on_tp_commit(self, tp_offsets: TPOffsetMapping) -> None:
        """Call when offset in topic partition is committed."""
        super().on_tp_commit(tp_offsets)
        for tp, offset in tp_offsets.items():
            self.topic_partition_offset_commited.labels(
                topic=tp.topic, partition=tp.partition, **self.common_labels
            ).set(offset)

    def track_tp_end_offset(self, tp: TP, offset: int) -> None:
        """Track new topic partition end offset for monitoring lags."""
        super().track_tp_end_offset(tp, offset)
        self.topic_partition_end_offset.labels(
            topic=tp.topic, partition=tp.partition, **self.common_labels
        ).set(offset)

    def on_web_request_end(
        self,
        app: KasprAppT,
        request: web.Request,
        response: typing.Optional[web.Response],
        state: typing.Dict,
        *,
        view: web.View = None,
    ) -> None:
        """Web server finished working on request."""
        super().on_web_request_end(app, request, response, state, view=view)
        status_code = int(state["status_code"])
        self.http_status_codes.labels(status_code=status_code).inc()
        self.http_latency.observe(self.ms_since(state["time_end"]))

    def expose_metrics(self) -> None:
        """Expose promethues metrics using the current aiohttp application."""

        @self.app.page(self.pattern)
        async def metrics_handler(self, request):
            headers = {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"}

            return Response(body=generate_latest(REGISTRY), headers=headers, status=200)

    def _normalize(
        self,
        name: str,
        *,
        pattern: Pattern = RE_NORMALIZE,
        substitution: str = RE_NORMALIZE_SUBSTITUTION,
    ) -> str:
        return pattern.sub(substitution, name)

    @cached_property
    def common_label_keys(self) -> List[str]:
        if self.common_labels:
            return list(self.common_labels.keys())
