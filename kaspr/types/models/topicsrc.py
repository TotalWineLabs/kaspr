from typing import Optional, Dict
from kaspr.types.models.base import SpecComponent
from kaspr.types.app import KasprAppT
from kaspr.types.topic import KasprTopicT


class TopicSrcSpec(SpecComponent):
    """Source topic specification."""

    name: str
    pattern: Optional[str]
    key_serializer: Optional[str]
    value_serializer: Optional[str]
    partitions: Optional[int]
    retention: Optional[int]
    compacting: Optional[bool]
    deleting: Optional[bool]
    replicas: Optional[int]
    config: Optional[Dict]

    app: KasprAppT = None
    _topic: KasprTopicT = None

    def prepare_topic(self, **kwargs) -> KasprTopicT:
        """Prepare topic based on name and pattern."""
        options = {
            "key_serializer": self.key_serializer,
            "value_serializer": self.value_serializer,
            "partitions": self.partitions,
            "retention": self.retention,
            "compacting": self.compacting,
            "deleting": self.deleting,
            "replicas": self.replicas,
            "config": self.config,
        }
        if self.name:
            topics = [t.strip() for t in self.name.split(',')]
            return self.app.topic(*topics, **options)
        else:
            # XXX pattern does not work yet
            return self.app.topic(pattern=self.pattern, **options)

    @property
    def topic(self) -> KasprTopicT:
        if self._topic is None:
            self._topic = self.prepare_topic()
        return self._topic

    @property
    def label(self) -> str:
        """Return description, used in graphs and logs."""
        return f"{type(self).__name__}: {','.join(self.topic.topics)}"

    @property
    def shortlabel(self) -> str:
        """Return short description."""
        return self.label
