import abc
import typing
from faust.types import ServiceT, TopicT
from mode.utils.objects import cached_property
from mode.utils.locks import Event
from .table import KasprTableT
from .checkpoint import CheckpointT

if typing.TYPE_CHECKING:
    from .app import KasprAppT as _KasprAppT
else:

    class _KasprAppT:
        ...  # noqa

    
class SchedulerPartT:
    janitor: str
    dispatcher: str

class MessageSchedulerT(ServiceT):
    """Abstract type for the kafka message scheduler service."""

    app: _KasprAppT

    topics_created: Event
    timetable_recovered: Event

    @cached_property
    @abc.abstractmethod
    def checkpoints(self) -> CheckpointT:
        ...

    @cached_property
    @abc.abstractmethod
    def schedule_rejections_topic(self) -> TopicT:
        ...

    @cached_property
    @abc.abstractmethod
    def schedule_requests_topic(self) -> TopicT:
        ...

    @cached_property
    @abc.abstractmethod
    def schedule_actions_topic(self) -> TopicT:
        ...    
        
    @cached_property
    @abc.abstractmethod
    def timetable(self) -> KasprTableT:
        ...            

    @abc.abstractmethod
    async def maybe_create_topics(self):
        ...

    @abc.abstractmethod
    async def wait_until_topics_created(self):
        ...

    @abc.abstractmethod
    async def wait_until_timetable_recovered(self):
        ...