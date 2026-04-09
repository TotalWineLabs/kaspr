import socket
import re
import random
import os
import psutil
import math
import ssl
from pathlib import Path
from yarl import URL
from typing import Any, Sequence, Optional, Union, Type, cast
from faust import SASLCredentials, SSLCredentials
from faust.types.settings import Settings
from faust.types.auth import SASLMechanism, CredentialsT, AuthProtocol
from faust.exceptions import ImproperlyConfigured
from mode import Seconds, want_seconds
from mode.utils.imports import SymbolArg, symbol_by_name
from kaspr.types.builder import AppBuilderT

PREFICES: Sequence[str] = ["KASPR_", "K_"]
_TRUE, _FALSE = {"True", "true"}, {"False", "false"}


def _getenv(name: str, *default: Any, prefices: Sequence[str] = PREFICES) -> Any:
    for prefix in prefices:
        try:
            v = os.environ[prefix + name]
            if v in _TRUE:
                return True
            elif v in _FALSE:
                return False
            else:
                return v
        except KeyError:
            pass
    if default:
        return default[0]
    raise KeyError(prefices[0] + name)


def _subenv(input: str):
    """
    Substitutes dynamic variables found in input with environment variable(s).

    For example, my-app-{APP_NAME} converts to my-app-0 if ORDINAL_NUM
    is a defined variable.
    """
    environ = os.environ
    found = re.findall(r"{([^{}]*?)}", input)
    for v in found:
        if v in environ:
            input = input.replace(f"{{{v}}}", str(environ[v]))
    return input


def _getmem():
    """Return total available memory (RAM) in bytes"""
    return psutil.virtual_memory().total


# ------------------------------------------------
# ---- Defaults and environment variables ----
# ------------------------------------------------

#: Number that differentiates this worker from other workers in a multi-worker application
WORKER_ORDINAL_NUMBER = _getenv("WORKER_ORDINAL_NUMBER", None)

#: Default transport used when no scheme specified.
DEFAULT_BROKER_SCHEME = "kafka"

#: Kafka bootstram server URLs
KAFKA_BOOTSTRAP_SERVERS = (
    f"kafka://{_getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')}"
)

#: Kafka security protocol
KAFKA_SECURITY_PROTOCOL = _getenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")

#: Kafka SASL mechanism
KAFKA_SASL_MECHANISM = _getenv("KAFKA_SASL_MECHANISM", None)

#: Kafka authentication username
KAFKA_AUTH_USERNAME = _getenv("KAFKA_AUTH_USERNAME", None)

#: Kafka authentication password
KAFKA_AUTH_PASSWORD = _getenv("KAFKA_AUTH_PASSWORD", None)

#: Kafka TLS authentication cert
KAFKA_AUTH_CAFILE = _getenv("KAFKA_AUTH_CAFILE", None)

#: Kafka TLS authentication cert
KAFKA_AUTH_CAPATH = _getenv("KAFKA_AUTH_CAPATH", None)

#: Kafka TLS authentication cert
KAFKA_AUTH_CADATA = _getenv("KAFKA_AUTH_CADATA", None)

#: Kafka client request timeout.
#: Note: The request timeout must not be less than the broker_session_timeout.
BROKER_REQUEST_TIMEOUT = int(_getenv("BROKER_REQUEST_TIMEOUT", 180))

#: Commit offset every n messages.
#: See also broker_commit_interval, which is how frequently we commit on a timer when 
#: there are few messages being received.
BROKER_COMMIT_EVERY = int(_getenv("BROKER_COMMIT_EVERY", 10_000))

#: How often we commit messages that have been fully processed (acked).
BROKER_COMMIT_INTERVAL = int(_getenv("BROKER_COMMIT_INTERVAL", 2.8))

#: How often we send heartbeats to the broker, and also how often we expect to receive heartbeats from the broker.
#: If any of these time out, you should increase this setting.
BROKER_HEARTBEAT_INTERVAL = int(_getenv("BROKER_HEARTBEAT_INTERVAL", 3.0))

#: How long to wait for a node to finish rebalancing before the broker will consider it 
#: dysfunctional and remove it from the cluster.
#: Increase this if you experience the cluster being in a state of constantly rebalancing,
#: but make sure you also increase the broker_heartbeat_interval at the same time.
#: Note: The session timeout must not be greater than the broker_request_timeout.
BROKER_SESSION_TIMEOUT = int(_getenv("BROKER_SESSION_TIMEOUT", 120))

#: The maximum number of records returned in a single call to poll(). 
# If you find that your application needs more time to process messages you may want to
# adjust broker_max_poll_records to tune the number of records that must be handled on 
# every loop iteration.
BROKER_MAX_POLL_RECORDS = int(_getenv("BROKER_MAX_POLL_RECORDS", 100))

#: The maximum allowed time (in seconds) between calls to consume messages If this interval
#: is exceeded the consumer is considered failed and the group will rebalance in order to 
#: reassign the partitions to another consumer group member. If API methods block waiting 
#: for messages, that time does not count against this timeout.
#: See KIP-62 for technical details.
BROKER_MAX_POLL_INTERVAL = int(_getenv("BROKER_MAX_POLL_INTERVAL", 1000.0))

#: Table state directory path used as default
#: This path will be treated as relative to datadir, unless the provided
#: path is absolute.
TABLE_DIR = _getenv("TABLE_DIR", "tables")

#: The default replication factor for topics created by the application.
TOPIC_REPLICATION_FACTOR = int(_getenv("TOPIC_REPLICATION_FACTOR", 3))

#: Default number of partitions for new topics.
TOPIC_PARTITIONS = int(_getenv("TOPIC_PARTITIONS", 3))

#: This setting disables auto creation of internal topics.
TOPIC_ALLOW_DECLARE = bool(_getenv("TOPIC_ALLOW_DECLARE", True))

#: The number of acknowledgments the producer requires the leader to have received 
#: before considering a request complete. This controls the durability of records 
#: that are sent. The following settings are common: 
#: 0: Producer will not wait for any acknowledgment from the server at all. The message 
#: will immediately be considered sent. (Not recommended)
#; 1: The broker leader will write the record to its local log but will respond without 
#: awaiting full acknowledgment from all followers. In this case should the leader fail 
#: immediately after acknowledging the record but before the followers have replicated it
#:  then the record will be lost.
#: -1: The broker leader will wait for the full set of in-sync replicas to acknowledge 
#: the record. This guarantees that the record will not be lost as long as at least one
#: in-sync replica remains alive. This is the strongest available guarantee.
PRODUCER_ACKS = int(_getenv("PRODUCER_ACKS", -1))

#: Name of the group instance ID used for static membership (KIP-345)
#: If set, the consumer will use static membership, which means a rebalance will not be 
#: immediately triggered when the consumer leaves the group.
#: The consumer has up to `broker_session_timeout` to rejoin the group before it is 
#: considered dead and the group is rebalanced. If not set, the consumer will use dynamic 
#: membership, which means that it will immediately trigger a rebalance when a consumer 
#: leaves the group.
CONSUMER_GROUP_INSTANCE_ID = _getenv("CONSUMER_GROUP_INSTANCE_ID", None)

#: This setting control back pressure to streams and agents reading from streams.
#: If set to 4096 (default) this means that an agent can only keep at most 4096 
#: unprocessed items in the stream buffer. Essentially this will limit the number 
#: of messages a stream can “prefetch”. Higher numbers gives better throughput, but
#: do note that if your agent sends messages or update tables (which sends changelog
#: messages). This means that if the buffer size is large, the broker_commit_interval
#: or broker_commit_every settings must be set to commit frequently, avoiding back 
#: pressure from building up. A buffer size of 131_072 may let you process over 30,000
#: events a second as a baseline, but be careful with a buffer size that large when you
#: also send messages or update tables.
STREAM_BUFFER_MAXSIZE = int(_getenv("STREAM_BUFFER_MAXSIZE", 4096))

#: Number of seconds to sleep before continuing after rebalance. We wait for a bit to allow
#: for more nodes to join/leave before starting recovery tables and then processing streams.
#: This to minimize the chance of errors rebalancing loops.
STREAM_RECOVERY_DELAY = int(_getenv("STREAM_RECOVERY_DELAY", 10.0))

#: This setting controls whether the worker should wait for the currently processing task in 
#: an agent to complete before rebalancing or shutting down. On rebalance/shut down we clear
#: the stream buffers. Those events will be reprocessed after the rebalance anyway, but we may
#: have already started processing one event in every agent, and if we rebalance we will process
#: that event again. By default we will wait for the currently active tasks, but if your streams
#: are idempotent you can disable it using this setting.
STREAM_WAIT_EMPTY = bool(_getenv("STREAM_WAIT_EMPTY", True))

#: RocksDB configirations
#: (https://github.com/EighteenZi/rocksdb_wiki/blob/master/Memory-usage-in-RocksDB.md)
# ------------------------------------------------
#: This is the maximum write buffer size.
#: It represents the amount of data to build up in memory
#: before converting to a sorted on-disk file. The default is 64 MB.
STORE_ROCKSDB_WRITE_BUFFER_SIZE = int(
    _getenv("STORE_ROCKSDB_WRITE_BUFFER_SIZE", 64 << 20)
)

#: Maximum number of write buffers (memtables)
#: that can be built in memory at the same time.
STORE_ROCKSDB_MAX_WRITE_BUFFER_NUMBER = int(
    _getenv("STORE_ROCKSDB_MAX_WRITE_BUFFER_NUMBER", 3)
)

#: Target size for files at level-1 in the LSM tree
#: Used to determine the size of the SST (Sorted String Table)
#: files that RocksDB generates during compactions.
STORE_ROCKSDB_TARGET_FILE_SIZE_BASE = int(
    _getenv("STORE_ROCKSDB_TARGET_FILE_SIZE_BASE", 64 << 20)
)

#: Size for caching uncompressed data.
#: Defauls to about 1/3 of your total memory budget
STORE_ROCKSDB_BLOCK_CACHE_SIZE = int(
    _getenv("STORE_ROCKSDB_BLOCK_CACHE_SIZE", math.floor(_getmem() / 3))
)

#: Size for caching compressed data.
#: Defaults to 254MB.
STORE_ROCKSDB_BLOCK_CACHE_COMPRESSED_SIZE = int(
    _getenv("STORE_ROCKSDB_BLOCK_CACHE_COMPRESSED_SIZE", 256 << 20)
)

#: A Bloom filter in RocksDB is used to quickly check whether a
#: key might be in an SST (Sorted String Table) file without
#: actually reading the file, which can significantly improve
#: read performance. Defaults to 3.
STORE_ROCKSDB_BLOOM_FILTER_SIZE = int(
    _getenv("STORE_ROCKSDB_BLOCK_CACHE_COMPRESSED_SIZE", 3)
)
#: If set to true, index and filter blocks will be stored in block cache,
#: together with all other data blocks.
STORE_ROCKSDB_SET_CACHE_INDEX_AND_FILTER_BLOCKS = _getenv(
    "STORE_ROCKSDB_SET_CACHE_INDEX_AND_FILTER_BLOCKS", False
)

#: Enable kafka message scheduler service
SCHEDULER_ENABLED = bool(_getenv("SCHEDULER_ENABLED", False))

#: Enable printing runtime scheduler statistics to log
SCHEDULER_DEBUG_STATS_ENABLED = bool(_getenv("SCHEDULER_DEBUG_STATS_ENABLED", False))

#: Default number of partitions for KMS related topics
#: If not specified, will use app's default topic partition configuration.
_scheduler_tps = _getenv("SCHEDULER_TOPIC_PARTITIONS", None)
SCHEDULER_TOPIC_PARTITIONS = int(_scheduler_tps) if _scheduler_tps is not None else None

#: How often we save checkpoint to storage (and to changelog topic)
SCHEDULER_CHECKPOINT_SAVE_INTERVAL_SECONDS = float(
    _getenv("SCHEDULER_CHECKPOINT_SAVE_INTERVAL_SECONDS", 1.3)
)

#: Number of days dispatcher looks back to build a default checkpoint
SCHEDULER_DISPATCHER_DEFAULT_CHECKPOINT_LOOKBACK_DAYS = int(
    _getenv("SCHEDULER_DISPATCHER_DEFAULT_CHECKPOINT_LOOKBACK_DAYS", 7)
)

#: How often we checkpoint the dispacher's location in the timetable.
SCHEDULER_DISPATCHER_CHECKPOINT_INTERVAL = float(
    _getenv("SCHEDULER_DISPATCHER_CHECKPOINT_INTERVAL", 10.0)
)

#: How often we checkpoint the janitor's location in the timetable.
SCHEDULER_JANITOR_CHECKPOINT_INTERVAL = float(
    _getenv("SCHEDULER_JANITOR_CHECKPOINT_INTERVAL", 10.0)
)

#: How oftwn we attempt to run the cleaning process.
SCHEDULER_JANITOR_CLEAN_INTERVAL_SECONDS = float(
    _getenv("SCHEDULER_JANITOR_CLEAN_INTERVAL_SECONDS", 3.0)
)

#: Number of seconds to offset the janitor highwater Timetable location.
SCHEDULER_JANITOR_HIGHWATER_OFFSET_SECONDS = float(
    _getenv("SCHEDULER_JANITOR_HIGHWATER_OFFSET_SECONDS", 3600 * 4.0)
)

#: Base http path for serving web requests
WEB_BASE_PATH = _getenv("WEB_BASE_PATH", "")

#: Web hostname or IP address of the web server.
WEB_HOST: str = _getenv("WEB_HOST", socket.gethostname())

#: Port number between 1024 and 65535 to use for the web server.
WEB_PORT: int = int(_getenv("WEB_PORT", "6066"))

#: Base http path for serving metrics
WEB_METRICS_BASE_PATH = _getenv("WEB_METRICS_BASE_PATH", "")

#: Directory path to app component definition file(s).
#: This path will be treated as relative to appdir, unless the provided
#: path is absolute.
DEFINITIONS_DIR = _getenv("DEFINITIONS_DIR", "builders")

#: Serializer used for keys by default when no serializer is specified.
KEY_SERIALIZER = _getenv("KEY_SERIALIZER", "json")

#: Serializer used for values by default when no serializer is specified.
VALUE_SERIALIZER = _getenv("VALUE_SERIALIZER", "json")

#: Enable building of stream processors from definition file(s).
# Set this to False if you don't want to allow defining stream processors with configuration.
APP_BUILDER_ENABLED = bool(_getenv("APP_BUILDER_ENABLED", True))

#: Path to app builder class, used as default for :setting:`AppBuilder`.
APP_BUILDER_TYPE = "kaspr.core.builder.AppBuilder"


class CustomSettings(Settings):
    """Application settings"""

    worker_ordinal_number: int = WORKER_ORDINAL_NUMBER
    worker_name_format: str = "{app_name}-{ordinal_number}"

    kafka_boostrap_servers: str = KAFKA_BOOTSTRAP_SERVERS
    kafka_security_protocol: str = KAFKA_SECURITY_PROTOCOL
    kafka_sasl_mechanism: str = KAFKA_SASL_MECHANISM
    kafka_auth_username: str = KAFKA_AUTH_USERNAME
    kafka_auth_password: str = KAFKA_AUTH_PASSWORD
    kafka_auth_cafile: str = KAFKA_AUTH_CAFILE
    kafka_auth_capath: str = KAFKA_AUTH_CAPATH
    kafka_auth_cadata: str = KAFKA_AUTH_CADATA

    broker_request_timeout: int = BROKER_REQUEST_TIMEOUT
    broker_commit_every: int = BROKER_COMMIT_EVERY
    broker_commit_interval: float = BROKER_COMMIT_INTERVAL
    broker_heartbeat_interval: float = BROKER_HEARTBEAT_INTERVAL
    broker_session_timeout: int = BROKER_SESSION_TIMEOUT
    broker_max_poll_records: int = BROKER_MAX_POLL_RECORDS
    broker_max_poll_interval: int = BROKER_MAX_POLL_INTERVAL    

    table_dir: str = TABLE_DIR

    topic_replication_factor: int = TOPIC_REPLICATION_FACTOR
    topic_partitions: int = TOPIC_PARTITIONS
    topic_allow_declare: bool = TOPIC_ALLOW_DECLARE

    producer_acks: int = PRODUCER_ACKS

    consumer_group_instance_id: str = CONSUMER_GROUP_INSTANCE_ID

    stream_buffer_maxsize: int = STREAM_BUFFER_MAXSIZE
    stream_recovery_delay: float = STREAM_RECOVERY_DELAY
    stream_wait_empty: bool = STREAM_WAIT_EMPTY

    store_rocksdb_write_buffer_size: int = STORE_ROCKSDB_WRITE_BUFFER_SIZE
    store_rocksdb_max_write_buffer_number: int = STORE_ROCKSDB_MAX_WRITE_BUFFER_NUMBER
    store_rocksdb_target_file_size_base: int = STORE_ROCKSDB_TARGET_FILE_SIZE_BASE
    store_rocksdb_block_cache_size: int = STORE_ROCKSDB_BLOCK_CACHE_SIZE
    store_rocksdb_block_cache_compressed_size: int = (
        STORE_ROCKSDB_BLOCK_CACHE_COMPRESSED_SIZE
    )
    store_rocksdb_bloom_filter_size: int = STORE_ROCKSDB_BLOOM_FILTER_SIZE
    store_rocksdb_set_cache_index_and_filter_blocks: bool = (
        STORE_ROCKSDB_SET_CACHE_INDEX_AND_FILTER_BLOCKS
    )

    scheduler_enabled: bool = SCHEDULER_ENABLED
    scheduler_debug_stats_enabled: bool = SCHEDULER_DEBUG_STATS_ENABLED
    scheduler_topic_partitions: Optional[int] = SCHEDULER_TOPIC_PARTITIONS
    scheduler_checkpoint_save_interval_seconds: float = SCHEDULER_CHECKPOINT_SAVE_INTERVAL_SECONDS
    scheduler_dispatcher_default_checkpoint_lookback_days: int = (
        SCHEDULER_DISPATCHER_DEFAULT_CHECKPOINT_LOOKBACK_DAYS
    )
    scheduler_dispatcher_checkpoint_interval: float = SCHEDULER_DISPATCHER_CHECKPOINT_INTERVAL
    scheduler_janitor_checkpoint_interval: float = SCHEDULER_JANITOR_CHECKPOINT_INTERVAL
    scheduler_janitor_clean_interval_seconds: float = SCHEDULER_JANITOR_CLEAN_INTERVAL_SECONDS
    scheduler_janitor_highwater_offset_seconds: float = SCHEDULER_JANITOR_HIGHWATER_OFFSET_SECONDS

    key_serializer: str = KEY_SERIALIZER
    value_serializer: str = VALUE_SERIALIZER

    web_base_path: str = WEB_BASE_PATH
    web_host: str = WEB_HOST
    web_port: int = WEB_PORT
    web_metrics_base_path: str = WEB_METRICS_BASE_PATH

    app_builder_enabled: bool = APP_BUILDER_ENABLED

    _worker_name: str = None
    _kafka_credentials: CredentialsT = None
    _definitionsdir: Path = None
    _canonical_url: URL = cast(URL, None)

    def __init__(
        self,
        *args,
        worker_ordinal_number: int = None,
        worker_name_format: str = None,
        kafka_boostrap_servers: str = None,
        kafka_security_protocol: str = None,
        kafka_sasl_mechanism: str = None,
        kafka_auth_username: str = None,
        kafka_auth_password: str = None,
        kafka_auth_cafile: str = None,
        kafka_auth_capath: str = None,
        kafka_auth_cadata: str = None,
        broker_request_timeout: int = None,
        broker_commit_every: int = None,
        broker_commit_interval: float = None,
        broker_heartbeat_interval: float = None,
        broker_session_timeout: int = None,
        broker_max_poll_records: int = None,
        broker_max_poll_interval: int = None,
        producer_acks: int = None,
        consumer_group_instance_id: str = None,
        stream_buffer_maxsize: int = None,
        stream_recovery_delay: float = None,
        stream_wait_empty: bool = None,
        table_dir: str = None,
        store_rocksdb_write_buffer_size: int = None,
        store_rocksdb_max_write_buffer_number: int = None,
        store_rocksdb_target_file_size_base: int = None,
        store_rocksdb_block_cache_size: int = None,
        store_rocksdb_block_cache_compressed_size: int = None,
        store_rocksdb_bloom_filter_size: int = None,
        store_rocksdb_set_cache_index_and_filter_blocks: bool = None,
        scheduler_enabled: bool = None,
        scheduler_debug_stats_enabled: bool = None,
        scheduler_topic_partitions: int = None,
        scheduler_checkpoint_save_interval_seconds: Seconds = None,
        scheduler_dispatcher_default_checkpoint_lookback_days: int = None,
        scheduler_dispatcher_checkpoint_interval: float = None,
        scheduler_janitor_checkpoint_interval: float = None,
        scheduler_janitor_clean_interval_seconds: Seconds = None,
        scheduler_janitor_highwater_offset_seconds: Seconds = None,
        web_base_path: str = None,
        web_host: str = None,
        web_port: int = None,
        web_metrics_base_path: str = None,
        key_serializer: str = None,
        value_serializer: str = None,
        definitions_dir: str = None,
        app_builder_enabled: bool = None,
        canonical_url: Union[str, URL] = None,
        AppBuilder: SymbolArg[Type[AppBuilderT]] = None,
        **kwargs,
    ):
        # Apply settings that exist in base class before we pass them down.
        if kafka_boostrap_servers is not None:
            self.kafka_boostrap_servers = kafka_boostrap_servers

        if kafka_security_protocol is not None:
            self.kafka_security_protocol = kafka_security_protocol

        if kafka_sasl_mechanism is not None:
            self.kafka_sasl_mechanism = kafka_sasl_mechanism

        if kafka_auth_username is not None:
            self.kafka_auth_username = kafka_auth_username

        if kafka_auth_password is not None:
            self.kafka_auth_password = kafka_auth_password

        if kafka_auth_cafile is not None:
            self.kafka_auth_cafile = kafka_auth_cafile

        if kafka_auth_capath is not None:
            self.kafka_auth_capath = kafka_auth_capath

        if kafka_auth_cadata is not None:
            self.kafka_auth_cadata = kafka_auth_cadata

        if broker_request_timeout is not None:
            self.broker_request_timeout = broker_request_timeout

        if broker_commit_every is not None:
            self.broker_commit_every = broker_commit_every

        if broker_commit_interval is not None:
            self.broker_commit_interval = broker_commit_interval

        if broker_heartbeat_interval is not None:
            self.broker_heartbeat_interval = broker_heartbeat_interval

        if broker_session_timeout is not None:
            self.broker_session_timeout = broker_session_timeout

        if broker_max_poll_records is not None:
            self.broker_max_poll_records = broker_max_poll_records

        if broker_max_poll_interval is not None:
            self.broker_max_poll_interval = broker_max_poll_interval

        if producer_acks is not None:
            self.producer_acks = producer_acks

        if consumer_group_instance_id is not None:
            self.consumer_group_instance_id = consumer_group_instance_id

        if stream_buffer_maxsize is not None:
            self.stream_buffer_maxsize = stream_buffer_maxsize

        if stream_recovery_delay is not None:
            self.stream_recovery_delay = stream_recovery_delay
        
        if stream_wait_empty is not None:
            self.stream_wait_empty = stream_wait_empty

        self.kafka_credentials = self._prepare_kafka_credentials()

        if table_dir is not None:
            self.table_dir = table_dir

        if web_host is not None:
            self.web_host = web_host

        if web_port is not None:
            self.web_port = web_port

        if key_serializer is not None:
            self.key_serializer = key_serializer

        if value_serializer is not None:
            self.value_serializer = value_serializer

        if canonical_url:
            self.canonical_url = canonical_url

        super().__init__(
            *args,
            tabledir=self.table_dir,
            broker=self.kafka_boostrap_servers,
            broker_credentials=self.kafka_credentials,
            broker_request_timeout=self.broker_request_timeout,
            broker_commit_every=self.broker_commit_every,
            broker_commit_interval=self.broker_commit_interval,
            broker_heartbeat_interval=self.broker_heartbeat_interval,
            broker_session_timeout=self.broker_session_timeout,
            broker_max_poll_records=self.broker_max_poll_records,
            broker_max_poll_interval=self.broker_max_poll_interval,
            producer_acks=self.producer_acks,
            consumer_group_instance_id=self.consumer_group_instance_id,
            stream_buffer_maxsize=self.stream_buffer_maxsize,
            stream_recovery_delay=self.stream_recovery_delay,
            stream_wait_empty=self.stream_wait_empty,
            key_serializer=self.key_serializer,
            value_serializer=self.value_serializer,
            web_host=self.web_host,
            web_port=self.web_port,
            canonical_url=self.canonical_url,
            **kwargs,
        )

        self.definitionssdir = cast(Path, definitions_dir or DEFINITIONS_DIR)

        if app_builder_enabled is not None:
            self.app_builder_enabled = app_builder_enabled

        if worker_ordinal_number is not None:
            self.worker_ordinal_number = int(worker_ordinal_number)

        if worker_name_format is not None:
            self.worker_name_format = worker_name_format

        self.worker_name = self._prepare_worker_name(name=self.name)

        if store_rocksdb_write_buffer_size is not None:
            self.store_rocksdb_write_buffer_size = store_rocksdb_write_buffer_size

        if store_rocksdb_max_write_buffer_number is not None:
            self.store_rocksdb_max_write_buffer_number = (
                store_rocksdb_max_write_buffer_number
            )

        if store_rocksdb_target_file_size_base is not None:
            self.store_rocksdb_target_file_size_base = (
                store_rocksdb_target_file_size_base
            )

        if store_rocksdb_block_cache_size is not None:
            self.store_rocksdb_block_cache_size = store_rocksdb_block_cache_size

        if store_rocksdb_block_cache_compressed_size is not None:
            self.store_rocksdb_block_cache_compressed_size = (
                store_rocksdb_block_cache_compressed_size
            )

        if store_rocksdb_bloom_filter_size is not None:
            self.store_rocksdb_bloom_filter_size = store_rocksdb_bloom_filter_size

        if store_rocksdb_set_cache_index_and_filter_blocks is not None:
            self.store_rocksdb_set_cache_index_and_filter_blocks = (
                store_rocksdb_set_cache_index_and_filter_blocks
            )

        if scheduler_enabled is not None:
            self.scheduler_enabled = scheduler_enabled

        if scheduler_debug_stats_enabled is not None:
            self.scheduler_debug_stats_enabled = scheduler_debug_stats_enabled

        if scheduler_topic_partitions is not None:
            self.scheduler_topic_partitions = scheduler_topic_partitions

        if scheduler_checkpoint_save_interval_seconds is not None:
            self.scheduler_checkpoint_save_interval_seconds = want_seconds(
                scheduler_checkpoint_save_interval_seconds
            )

        if scheduler_dispatcher_default_checkpoint_lookback_days is not None:
            self.scheduler_dispatcher_default_checkpoint_lookback_days = (
                scheduler_dispatcher_default_checkpoint_lookback_days
            )

        if scheduler_dispatcher_checkpoint_interval is not None:
            self.scheduler_dispatcher_checkpoint_interval = want_seconds(
                scheduler_dispatcher_checkpoint_interval
            )

        if scheduler_janitor_checkpoint_interval is not None:
            self.scheduler_janitor_checkpoint_interval = want_seconds(
                scheduler_janitor_checkpoint_interval
            )

        if scheduler_janitor_clean_interval_seconds is not None:
            self.scheduler_janitor_clean_interval_seconds = want_seconds(
                scheduler_janitor_clean_interval_seconds
            )

        if scheduler_janitor_highwater_offset_seconds is not None:
            self.scheduler_janitor_highwater_offset_seconds = want_seconds(
                scheduler_janitor_highwater_offset_seconds
            )

        if web_base_path is not None:
            self.web_base_path = web_base_path

        if web_metrics_base_path is not None:
            self.web_metrics_base_path = web_metrics_base_path

        self.AppBuilder = cast(
            Type[AppBuilderT], AppBuilder or APP_BUILDER_TYPE
        )

    def _prepare_kafka_credentials(self) -> CredentialsT:
        security_protocol = AuthProtocol(self.kafka_security_protocol)
        if security_protocol == AuthProtocol.PLAINTEXT:
            return None
        elif security_protocol in [
            AuthProtocol.SASL_PLAINTEXT,
            AuthProtocol.SASL_SSL,
        ]:
            return SASLCredentials(
                username=self.kafka_auth_username,
                password=self.kafka_auth_password,
                ssl_context=ssl.create_default_context()
                if security_protocol == AuthProtocol.SASL_SSL
                else None,
                mechanism=SASLMechanism(self.kafka_sasl_mechanism)
                if self.kafka_sasl_mechanism
                else None,
            )
        elif security_protocol in [AuthProtocol.SSL]:
            ssl_auth = {
                "cafile": self.kafka_auth_cafile,
                "capath": self.kafka_auth_capath,
                "cadata": self.kafka_auth_cadata,
            }
            return SSLCredentials(context=ssl.create_default_context(), **ssl_auth)
        else:
            raise ImproperlyConfigured(
                f"Unknown or unsupported auth protocol: {security_protocol}"
            )

    def _prepare_worker_name(self, name: str):
        return self.worker_name_format.format(
            app_name=name, ordinal_number=self._get_ordinal_number()
        )

    def _get_ordinal_number(self) -> int:
        if self.worker_ordinal_number is not None:
            return self.worker_ordinal_number
        else:
            # try to extract from hostname (k8s statefulsets)
            result = re.match(".*-([0-9]+)$", socket.gethostname())
            if result is not None:
                try:
                    return int(result.group(1))
                except Exception:
                    pass
            # last resort: generate a random number
            return random.randint(1000, 1999)

    def _prepare_definitionsdir(self, definitionsdir: Union[str, Path]) -> Path:
        return self._appdir_path(self._Path(definitionsdir))

    @property
    def kafka_credentials(self) -> CredentialsT:
        return self._kafka_credentials

    @kafka_credentials.setter
    def kafka_credentials(self, credentials: CredentialsT):
        self._kafka_credentials = credentials

    @property
    def definitionssdir(self) -> Path:
        return self._definitionsdir

    @definitionssdir.setter
    def definitionssdir(self, definitionsdir: Union[Path, str]) -> None:
        self._definitionsdir = self._prepare_definitionsdir(definitionsdir)

    @property
    def worker_name(self):
        """Unique name for worker in a multi-worker application."""
        return self._worker_name

    @worker_name.setter
    def worker_name(self, name: str):
        self._worker_name = name

    @property
    def AppBuilder(self) -> Type[AppBuilderT]:
        return self._AppBuilder

    @AppBuilder.setter
    def AppBuilder(self, AppBuilder: SymbolArg[Type[AppBuilderT]]) -> None:
        self._AppBuilder = symbol_by_name(AppBuilder)

    @property
    def canonical_url(self) -> URL:
        if not self._canonical_url:
            self._canonical_url = URL(
                f'http://{self.web_host}:{self.web_port}')
        return self._canonical_url

    @canonical_url.setter
    def canonical_url(self, canonical_url: Union[URL, str]) -> None:
        self._canonical_url = URL(canonical_url)