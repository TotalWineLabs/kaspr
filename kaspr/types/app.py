import abc
import typing
from datetime import datetime
from typing import ClassVar, Type, Callable, Any
from faust.types import AppT, WindowT

from mode.utils.objects import cached_property
from kaspr.types.message_scheduler import MessageSchedulerT
from kaspr.types.table import KasprTableT
from mode import SyncSignalT

if typing.TYPE_CHECKING:
    from . import CustomSettings as _CustomSettings
    from kaspr.sensors.prometheus_monitor import PrometheusMonitor as _PrometheusMonitor
else:

    class _CustomSettings:
        ...  # noqa

    class _PrometheusMonitor:
        ...


class CustomBootStrategyT:
    app: "KasprAppT"


class KasprAppT(AppT):
    """Abstract type for the custom application."""

    BootStrategy: ClassVar[Type[CustomBootStrategyT]]
    Settings: ClassVar[Type[_CustomSettings]]

    conf: _CustomSettings
    boot_time: datetime
    monitor: _PrometheusMonitor

    on_rebalance_started: SyncSignalT

    @cached_property
    @abc.abstractmethod
    def scheduler(self) -> MessageSchedulerT:
        ...

    @abc.abstractmethod
    def register_named_channel(self, name: str, channel: Any) -> None:
        ... 

    @abc.abstractmethod
    def resolve_named_channel(self, name: str) -> Any:
        ...

    def Table(
        self,
        name: str,
        *,
        default: Callable[[], Any] = None,
        window: WindowT = None,
        partitions: int = None,
        help: str = None,
        **kwargs: Any,
    ) -> KasprTableT:
        """Define new table.

        Arguments:
            name: Name used for table, note that two tables living in
                the same application cannot have the same name.

            default: A callable, or type that will return a default value
               for keys missing in this table.
            window: A windowing strategy to wrap this window in.

        Examples:
            >>> table = app.Table('user_to_amount', default=int)
            >>> table['George']
            0
            >>> table['Elaine'] += 1
            >>> table['Elaine'] += 1
            >>> table['Elaine']
            2
        """
        return super().Table(
            name=name,
            default=default,
            window=window,
            partitions=partitions,
            help=help,
            **kwargs,
        )