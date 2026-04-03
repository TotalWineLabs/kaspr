import faust
from datetime import datetime
from kaspr.utils.functional import utc_now
from typing import Optional, Iterable
from faust.app import BootStrategy
from faust.types import ServiceT
from mode.utils.objects import cached_property
from mode import SyncSignal
from kaspr.types import CustomSettings, KasprAppT, MessageSchedulerT, AppBuilderT
from kaspr.scheduler import MessageScheduler


class CustomBootStrategy(BootStrategy):
    """App startup strategy.

    The startup strategy defines the graph of services
    to start when the Faust worker for an app starts.
    """

    app: KasprAppT
    enable_scheduler: Optional[bool] = None

    boot_time: datetime = None

    def __init__(
        self, app: KasprAppT, *args, enable_scheduler: bool = None, **kwargs
    ) -> None:
        super().__init__(app, *args, **kwargs)

        self.boot_time = utc_now()

        if enable_scheduler is not None:
            self.enable_scheduler = enable_scheduler

    def server(self) -> Iterable[ServiceT]:
        """Return services to start when app is in scheduler mode."""
        server_services = list(super().server())
        return self._chain(
            # All other server services
            server_services,
            # Message Scheduler (app.MessageScheduler)
            self.scheduler(),
        )

    def scheduler(self) -> Iterable[ServiceT]:
        """Return list of services required to start scheduler."""
        if self._should_enable_message_scheduler():
            return [self.app.scheduler]
        return []

    def _should_enable_message_scheduler(self) -> bool:
        if self.enable_scheduler is None:
            return self.app.conf.scheduler_enabled
        return self.enable_scheduler


class KasprApp(KasprAppT, faust.App):
    """Main app"""

    BootStrategy = CustomBootStrategy
    Settings = CustomSettings

    on_rebalance_started: SyncSignal = SyncSignal()

    _named_channels: dict = None

    def _init_signals(self) -> None:
        super()._init_signals()
        self.on_rebalance_started = self.on_rebalance_started.with_default_sender(self)

    def on_init_dependencies(self):
        dependencies = list(super().on_init_dependencies())
        return dependencies

    async def on_first_start(self) -> None:
        """Call first time app starts in this process."""
        await super().on_first_start()

        if self.conf.scheduler_enabled:
            await self.scheduler.maybe_create_topics()

        if self.conf.app_builder_enabled:
            self.builder.build()
            await self.builder.maybe_create_topics()

    async def on_start(self) -> None:
        """Call every time app start/restarts."""
        await super().on_start()

    async def on_started(self) -> None:
        await super().on_started()
        self.monitor.on_app_started(self)

    async def on_stop(self) -> None:
        """Call when application stops."""
        await super().on_stop()

    def on_rebalance_start(self) -> None:
        """Call when rebalancing starts"""
        super().on_rebalance_start()
        self.on_rebalance_started.send()

    def on_rebalance_end(self) -> None:
        """Call when rebalancing is done."""
        super().on_rebalance_end()

    def register_named_channel(self, name: str, channel) -> None:
        """Register a named channel (e.g., from a KasprJoin output)."""
        if self._named_channels is None:
            self._named_channels = {}
        self._named_channels[name] = channel

    def resolve_named_channel(self, name: str):
        """Resolve a named channel by name. Returns None if not found."""
        if self._named_channels is None:
            return None
        return self._named_channels.get(name)

    def _create_directories(self) -> None:
        super()._create_directories()
        self.conf.definitionssdir.mkdir(exist_ok=True)
    
    @cached_property
    def scheduler(self) -> MessageSchedulerT:
        """Kafka message scheduler service."""
        return MessageScheduler(
            app=self,
            loop=self.loop,
            beacon=self.beacon,
        )

    @cached_property
    def builder(self) -> AppBuilderT:
        """App builder."""
        return self.conf.AppBuilder(self)