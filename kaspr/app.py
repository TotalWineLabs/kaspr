from datetime import timedelta
from faust.exceptions import ImproperlyConfigured
from kaspr import KasprApp
from kaspr.core import coordinator_patch
from kaspr.sensors.prometheus_monitor import PrometheusMonitor
from kaspr.types.settings import _getenv

# Must run before any consumer is constructed.
coordinator_patch.apply()


app_name = _getenv("APP_NAME", None)
if not app_name:
    raise ImproperlyConfigured("Missing required configuration: `APP_NAME`")

app = KasprApp(
    app_name,
    autodiscover=["kaspr"],
    origin="kaspr",
    store="rocksdb://",
    processing_guarantee="exactly_once",
    # How long time it takes before we warn that the Kafka commit offset
    # has not advanced (only when processing messages).
    broker_commit_livelock_soft_timeout=timedelta(seconds=30),
    # The number of acknowledgments the producer requires the leader to
    # have received before considering a request complete (0,1,-1)
    table_cleanup_interval=10,
    Agent="kaspr.core.agent.KasprAgent",
    Table="kaspr.core.table.KasprTable",
    GlobalTable="kaspr.core.table.KasprGlobalTable",
    Stream="kaspr.core.stream.KasprStream",
    LeaderAssignor="kaspr.core.leader_assignor.KasprLeaderAssignor",
    PartitionAssignor="kaspr.core.partition_assignor.KasprPartitionAssignor",
)

app.monitor = PrometheusMonitor(
    app,
    pattern=f"{app.conf.web_metrics_base_path}/metrics",
)

# Add web endpoints
app.web.blueprints.add('/status', 'kaspr.blueprints.status.blueprint')
app.web.blueprints.add('/signal', 'kaspr.blueprints.signal.blueprint')