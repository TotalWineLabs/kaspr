from typing import Optional, Dict, TypeVar, Callable, Union, Awaitable, OrderedDict, Tuple
from kaspr.types.models.base import SpecComponent
from kaspr.types.app import KasprAppT
from kaspr.types.topic import KasprTopicT
from kaspr.types.models.topicselector import (
    TopicNameSelector,
    TopicKeySelector,
    TopicValueSelector,
    TopicPartitionSelector,
    TopicHeadersSelector,
    TopicPredicate,
)

T = TypeVar("T")
Function = Callable[[T], Union[T, Awaitable[T]]]


class TopicOutSpec(SpecComponent):
    """Output topic specification."""

    name: Optional[str]
    name_selector: Optional[TopicNameSelector]
    pass_through: Optional[bool]
    ack: Optional[bool]
    key_serializer: Optional[str]
    value_serializer: Optional[str]
    key_selector: Optional[TopicKeySelector]
    value_selector: Optional[TopicValueSelector]
    partition_selector: Optional[TopicPartitionSelector]
    headers_selector: Optional[TopicHeadersSelector]
    predicate: Optional[TopicPredicate]

    app: KasprAppT = None
    _topics: Dict[str, KasprTopicT] = dict()
    _name_selector_func: Function = None
    _key_selector_func: Function = None
    _value_selector_func: Function = None
    _partition_selector_func: Function = None
    _headers_selector_func: Function = None
    _predicate_func: Function = None

    async def send(self, value: T, **kwargs) -> Union[None, OrderedDict, Tuple[T, OrderedDict]]:
        """Send value to topic according to spec.

        If ack is True, returns metadata (offset, timestamp, etc).
        of the sent message.
        """
        res = await self.get_topic(value).send(
            key_serializer=self.key_serializer,
            value_serializer=self.value_serializer,
            key=self.get_key(value, **kwargs),
            value=self.get_value(value, **kwargs),
            partition=self.get_partition(value, **kwargs),
            headers=self.get_headers(value, **kwargs),
        )
        if self.pass_through:
            if self.ack:
                return value, (await res)._asdict()
            return value
        if self.ack:
            return (await res)._asdict()

    def get_topic_name(self, value: T, **kwargs) -> KasprTopicT:
        """Get topic from value"""
        name = self.name
        if self.name_selector:
            name = self.name_selector_func(value, **kwargs)
            if not name:
                raise ValueError("Topic name selector function returned None")
        return name

    def get_key(self, value: T, **kwargs) -> T:
        """Get key from value"""
        if self.key_selector_func is not None:
            return self.key_selector_func(value, **kwargs)

    def get_value(self, value: T, **kwargs) -> T:
        """Get value from value"""
        if self.value_selector_func is not None:
            return self.value_selector_func(value, **kwargs)
        else:
            return value

    def get_partition(self, value: T, **kwargs) -> T:
        """Get partition from value"""
        if self.partition_selector_func is not None:
            return self.partition_selector_func(value, **kwargs)

    def get_headers(self, value: T, **kwargs) -> T:
        """Get headers from value"""
        if self.headers_selector_func is not None:
            return self.headers_selector_func(value, **kwargs)

    def should_skip(self, value: T, **kwargs) -> bool:
        """Check if value should be skipped"""
        if self.predicate_func is not None:
            return not self.predicate_func(value, **kwargs)
        else:
            return False

    def prepare_topic(self, name: str, **kwargs) -> KasprTopicT:
        """Prepare topic instance"""
        return self.app.topic(
            name,
            key_serializer=self.key_serializer,
            value_serializer=self.value_serializer,
        )

    def get_topic(self, value: T, **kwargs) -> KasprTopicT:
        """Get topic instance"""
        name = self.get_topic_name(value, **kwargs)
        if not name or type(name) is not str:
            raise ValueError(f"Invalid topic name `{name}`")
        if name not in self._topics:
            self._topics[name] = self.prepare_topic(name, **kwargs)
        return self._topics[name]

    @property
    def name_selector_func(self):
        """Name selector function"""
        if self._name_selector_func is None and self.name_selector:
            self._name_selector_func = self.name_selector.func
        return self._name_selector_func

    @property
    def key_selector_func(self):
        """Key selector function"""
        if self._key_selector_func is None and self.key_selector:
            self._key_selector_func = self.key_selector.func
        return self._key_selector_func

    @property
    def value_selector_func(self):
        """Value selector function"""
        if self._value_selector_func is None and self.value_selector:
            self._value_selector_func = self.value_selector.func
        return self._value_selector_func

    @property
    def partition_selector_func(self):
        """Partition selector function"""
        if self._partition_selector_func is None and self.partition_selector:
            self._partition_selector_func = self.partition_selector.func
        return self._partition_selector_func

    @property
    def headers_selector_func(self):
        """Headers selector function"""
        if self._headers_selector_func is None and self.headers_selector:
            self._headers_selector_func = self.headers_selector.func
        return self._headers_selector_func

    @property
    def predicate_func(self):
        """Predicate function"""
        if self._predicate_func is None and self.predicate:
            self._predicate_func = self.predicate.func
        return self._predicate_func

    @property
    def label(self) -> str:
        """Return description, used in graphs and logs."""
        return f"{type(self).__name__}: {','.join(self.topic.topics)}"

    @property
    def shortlabel(self) -> str:
        """Return short description."""
        return self.label
