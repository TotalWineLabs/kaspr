import asyncio
from types import SimpleNamespace

import pytest

from kaspr.types.models.agent.operations import (
    AgentProcessorMapOperator,
    AgentProcessorOperation,
    AgentProcessorTopicSendOperator,
)
from kaspr.types.models.agent.output import AgentOutputSpec
from kaspr.types.models.agent.processor import AgentProcessorSpec
from kaspr.types.models.task.operations import (
    TaskProcessorMapOperator,
    TaskProcessorOperation,
    TaskProcessorTopicSendOperator,
)
from kaspr.types.models.task.processor import TaskProcessorSpec
from kaspr.types.models.topicout import TopicOutSpec
from kaspr.types.models.webview.operations import (
    WebViewProcessorMapOperator,
    WebViewProcessorOperation,
    WebViewProcessorTopicSendOperator,
)
from kaspr.types.models.webview.processor import WebViewProcessorSpec
from kaspr.types.models.webview.response import CONTENT_TYPE, WebViewResponseSpec
from kaspr.types.schemas.agent.operations import AgentProcessorOperationSchema


class DummyAck:
    def __init__(self, metadata):
        self._metadata = metadata

    def __await__(self):
        async def _wait():
            return SimpleNamespace(_asdict=lambda: self._metadata)

        return _wait().__await__()


class DummyTopic:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.calls = []

    async def send(self, **kwargs):
        if self.should_fail:
            raise RuntimeError("publish failed")
        self.calls.append(kwargs)
        return DummyAck({"offset": 7, "partition": 2})


class DummyApp:
    def __init__(self, topic):
        self._topic = topic
        self.tables = {}

    def topic(self, name, **kwargs):
        if isinstance(self._topic, dict):
            return self._topic[name]
        return self._topic


class DummyStream:
    def __init__(self, values, event):
        self._values = iter(values)
        self.current_event = event

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._values)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class DummyWeb:
    def json(self, data, content_type=None, status=None, headers=None):
        return {
            "data": data,
            "content_type": content_type,
            "status": status,
            "headers": headers,
        }


def make_topic_spec(topic, **kwargs):
    params = {
        "name": "materialization-requests",
        "name_selector": None,
        "pass_through": False,
        "ack": False,
        "key_serializer": None,
        "value_serializer": None,
        "key_selector": None,
        "value_selector": None,
        "partition_selector": None,
        "headers_selector": None,
        "predicate": None,
        "app": DummyApp(topic),
    }
    params.update(kwargs)
    spec = TopicOutSpec(
        **params,
    )
    spec._topics = {}
    spec._key_selector_func = lambda value, **_kwargs: value["request_event"][
        "materialization_id"
    ]
    spec._value_selector_func = lambda value, **_kwargs: value["request_event"]
    return spec


def test_topic_send_default_behavior_returns_none():
    payload = {
        "response": {"status": "accepted"},
        "request_event": {"materialization_id": "mat-123", "phase": "requested"},
    }
    topic = DummyTopic()
    spec = make_topic_spec(topic)

    result = asyncio.run(spec.send(payload))

    assert result is None
    assert topic.calls == [
        {
            "key_serializer": None,
            "value_serializer": None,
            "key": "mat-123",
            "value": payload["request_event"],
            "partition": None,
            "headers": None,
        }
    ]


def test_topic_send_pass_through_returns_original_value():
    payload = {
        "response": {"materialization_id": "mat-123", "status_url": "/materializations/mat-123"},
        "request_event": {"materialization_id": "mat-123", "phase": "requested"},
    }
    topic = DummyTopic()
    spec = make_topic_spec(topic, pass_through=True)

    result = asyncio.run(spec.send(payload))

    assert result is payload
    assert topic.calls[0]["key"] == "mat-123"
    assert topic.calls[0]["value"] == payload["request_event"]


def test_topic_send_pass_through_allows_none_value():
    topic = DummyTopic()
    spec = TopicOutSpec(
        name="materialization-requests",
        name_selector=None,
        pass_through=True,
        ack=False,
        key_serializer=None,
        value_serializer=None,
        key_selector=None,
        value_selector=None,
        partition_selector=None,
        headers_selector=None,
        predicate=None,
        app=DummyApp(topic),
    )
    spec._topics = {}

    result = asyncio.run(spec.send(None))

    assert result is None
    assert topic.calls == [
        {
            "key_serializer": None,
            "value_serializer": None,
            "key": None,
            "value": None,
            "partition": None,
            "headers": None,
        }
    ]


def test_topic_send_pass_through_with_ack_returns_value_and_metadata():
    payload = {
        "response": {"materialization_id": "mat-123"},
        "request_event": {"materialization_id": "mat-123", "phase": "requested"},
    }
    topic = DummyTopic()
    spec = make_topic_spec(topic, pass_through=True, ack=True)

    result = asyncio.run(spec.send(payload))

    assert result == (payload, {"offset": 7, "partition": 2})
    assert topic.calls[0]["key"] == "mat-123"
    assert topic.calls[0]["value"] == payload["request_event"]


def test_topic_send_failure_keeps_error_behavior_with_pass_through():
    payload = {
        "response": {"status": "accepted"},
        "request_event": {"materialization_id": "mat-123"},
    }
    spec = make_topic_spec(DummyTopic(should_fail=True), pass_through=True)

    with pytest.raises(RuntimeError, match="publish failed"):
        asyncio.run(spec.send(payload))


def test_agent_topic_send_schema_loads_topic_send_operator():
    schema = AgentProcessorOperationSchema()

    result = schema.load(
        {
            "name": "publish",
            "topic_send": {
                "name": "materialization-requests",
                "pass_through": True,
            },
        }
    )

    assert result.name == "publish"
    assert result.topic_send.name == "materialization-requests"
    assert result.topic_send.pass_through is True


def test_agent_topic_send_without_pass_through_stops_downstream_pipeline():
    publish_topic = DummyTopic()
    output_topic = DummyTopic()
    app = DummyApp(
        {
            "materialization-requests": publish_topic,
            "agent-output": output_topic,
        }
    )

    prepare_payload = AgentProcessorMapOperator(
        python="""
def prepare_payload(value):
    return {
        'current': value,
        'request_event': {
            'materialization_id': value['id'],
            'phase': 'requested',
        },
    }
""",
        entrypoint="prepare_payload",
    )
    send_request_event = AgentProcessorTopicSendOperator(
        name="materialization-requests",
        name_selector=None,
        pass_through=False,
        ack=False,
        key_serializer=None,
        value_serializer=None,
        key_selector=None,
        value_selector=None,
        partition_selector=None,
        headers_selector=None,
        predicate=None,
        app=app,
    )
    send_request_event._key_selector_func = lambda value, **_kwargs: value[
        "request_event"
    ]["materialization_id"]
    send_request_event._value_selector_func = lambda value, **_kwargs: value[
        "request_event"
    ]
    send_request_event._topics = {}
    should_not_run = AgentProcessorMapOperator(
        python="""
def should_not_run(value):
    raise AssertionError('downstream operation should not run')
""",
        entrypoint="should_not_run",
    )
    processor = AgentProcessorSpec(
        pipeline=["prepare", "publish", "after-publish"],
        init=None,
        operations=[
            AgentProcessorOperation(
                name="prepare",
                map=prepare_payload,
                topic_send=None,
                filter=None,
                table_refs=[],
                app=app,
            ),
            AgentProcessorOperation(
                name="publish",
                map=None,
                topic_send=send_request_event,
                filter=None,
                table_refs=[],
                app=app,
            ),
            AgentProcessorOperation(
                name="after-publish",
                map=should_not_run,
                topic_send=None,
                filter=None,
                table_refs=[],
                app=app,
            ),
        ],
        app=app,
    )
    processor.input = SimpleNamespace(buffer_spec=None)
    processor.output = AgentOutputSpec(
        topics_spec=[
            TopicOutSpec(
                name="agent-output",
                name_selector=None,
                pass_through=False,
                ack=False,
                key_serializer=None,
                value_serializer=None,
                key_selector=None,
                value_selector=None,
                partition_selector=None,
                headers_selector=None,
                predicate=None,
                app=app,
            )
        ],
        app=app,
    )

    asyncio.run(
        processor.processor(
            DummyStream(
                [{"id": "mat-123", "payload": {"schema": "proposal"}}],
                event=SimpleNamespace(key="mat-123"),
            )
        )
    )

    assert publish_topic.calls == [
        {
            "key_serializer": None,
            "value_serializer": None,
            "key": "mat-123",
            "value": {
                "materialization_id": "mat-123",
                "phase": "requested",
            },
            "partition": None,
            "headers": None,
        }
    ]
    assert output_topic.calls == []


def test_agent_topic_send_pass_through_preserves_pipeline_value_for_downstream_ops():
    publish_topic = DummyTopic()
    output_topic = DummyTopic()
    app = DummyApp(
        {
            "schema-proposals": publish_topic,
            "agent-output": output_topic,
        }
    )

    prepare_payload = AgentProcessorMapOperator(
        python="""
def prepare_payload(value):
    return {
        'current': value,
        'schema_proposal': {
            'materialization_id': value['id'],
            'schema': value['payload']['schema'],
        },
    }
""",
        entrypoint="prepare_payload",
    )
    send_schema_proposal = AgentProcessorTopicSendOperator(
        name="schema-proposals",
        name_selector=None,
        pass_through=True,
        ack=False,
        key_serializer=None,
        value_serializer=None,
        key_selector=None,
        value_selector=None,
        partition_selector=None,
        headers_selector=None,
        predicate=None,
        app=app,
    )
    send_schema_proposal._key_selector_func = lambda value, **_kwargs: value[
        "schema_proposal"
    ]["materialization_id"]
    send_schema_proposal._value_selector_func = lambda value, **_kwargs: value[
        "schema_proposal"
    ]
    send_schema_proposal._headers_selector_func = lambda value, **_kwargs: {
        "x-source": "agent"
    }
    send_schema_proposal._topics = {}
    finish = AgentProcessorMapOperator(
        python="""
def finish(value):
    return {
        'materialization_id': value['current']['id'],
        'schema': value['schema_proposal']['schema'],
        'status': 'processed',
    }
""",
        entrypoint="finish",
    )
    output_spec = TopicOutSpec(
        name="agent-output",
        name_selector=None,
        pass_through=False,
        ack=False,
        key_serializer=None,
        value_serializer=None,
        key_selector=None,
        value_selector=None,
        partition_selector=None,
        headers_selector=None,
        predicate=None,
        app=app,
    )
    output_spec._topics = {}
    processor = AgentProcessorSpec(
        pipeline=["prepare", "publish", "finish"],
        init=None,
        operations=[
            AgentProcessorOperation(
                name="prepare",
                map=prepare_payload,
                topic_send=None,
                filter=None,
                table_refs=[],
                app=app,
            ),
            AgentProcessorOperation(
                name="publish",
                map=None,
                topic_send=send_schema_proposal,
                filter=None,
                table_refs=[],
                app=app,
            ),
            AgentProcessorOperation(
                name="finish",
                map=finish,
                topic_send=None,
                filter=None,
                table_refs=[],
                app=app,
            ),
        ],
        app=app,
    )
    processor.input = SimpleNamespace(buffer_spec=None)
    processor.output = AgentOutputSpec(topics_spec=[output_spec], app=app)

    asyncio.run(
        processor.processor(
            DummyStream(
                [{"id": "mat-123", "payload": {"schema": "user.created.v1"}}],
                event=SimpleNamespace(key="mat-123"),
            )
        )
    )

    assert publish_topic.calls == [
        {
            "key_serializer": None,
            "value_serializer": None,
            "key": "mat-123",
            "value": {
                "materialization_id": "mat-123",
                "schema": "user.created.v1",
            },
            "partition": None,
            "headers": {"x-source": "agent"},
        }
    ]
    assert output_topic.calls == [
        {
            "key_serializer": None,
            "value_serializer": None,
            "key": None,
            "value": {
                "materialization_id": "mat-123",
                "schema": "user.created.v1",
                "status": "processed",
            },
            "partition": None,
            "headers": None,
        }
    ]


def test_agent_topic_send_predicate_skip_stops_publish_and_downstream():
    publish_topic = DummyTopic()
    app = DummyApp({"schema-proposals": publish_topic})

    send_schema_proposal = AgentProcessorTopicSendOperator(
        name="schema-proposals",
        name_selector=None,
        pass_through=False,
        ack=False,
        key_serializer=None,
        value_serializer=None,
        key_selector=None,
        value_selector=None,
        partition_selector=None,
        headers_selector=None,
        predicate=None,
        app=app,
    )
    send_schema_proposal._predicate_func = lambda value, **_kwargs: False
    send_schema_proposal._topics = {}
    downstream = AgentProcessorMapOperator(
        python="""
def downstream(value):
    raise AssertionError('downstream operation should not run when predicate is false')
""",
        entrypoint="downstream",
    )
    processor = AgentProcessorSpec(
        pipeline=["publish", "downstream"],
        init=None,
        operations=[
            AgentProcessorOperation(
                name="publish",
                map=None,
                topic_send=send_schema_proposal,
                filter=None,
                table_refs=[],
                app=app,
            ),
            AgentProcessorOperation(
                name="downstream",
                map=downstream,
                topic_send=None,
                filter=None,
                table_refs=[],
                app=app,
            ),
        ],
        app=app,
    )
    processor.input = SimpleNamespace(buffer_spec=None)
    processor.output = None

    asyncio.run(
        processor.processor(
            DummyStream([{"id": "mat-123", "payload": {}}], event=SimpleNamespace())
        )
    )

    assert publish_topic.calls == []


def test_agent_topic_send_predicate_skip_with_pass_through_continues_downstream():
    publish_topic = DummyTopic()
    output_topic = DummyTopic()
    app = DummyApp(
        {
            "schema-proposals": publish_topic,
            "agent-output": output_topic,
        }
    )

    send_schema_proposal = AgentProcessorTopicSendOperator(
        name="schema-proposals",
        name_selector=None,
        pass_through=True,
        ack=False,
        key_serializer=None,
        value_serializer=None,
        key_selector=None,
        value_selector=None,
        partition_selector=None,
        headers_selector=None,
        predicate=None,
        app=app,
    )
    send_schema_proposal._predicate_func = lambda value, **_kwargs: False
    send_schema_proposal._topics = {}
    downstream = AgentProcessorMapOperator(
        python="""
def downstream(value):
    return {
        'materialization_id': value['id'],
        'continued': True,
    }
""",
        entrypoint="downstream",
    )
    output_spec = TopicOutSpec(
        name="agent-output",
        name_selector=None,
        pass_through=False,
        ack=False,
        key_serializer=None,
        value_serializer=None,
        key_selector=None,
        value_selector=None,
        partition_selector=None,
        headers_selector=None,
        predicate=None,
        app=app,
    )
    output_spec._topics = {}
    processor = AgentProcessorSpec(
        pipeline=["publish", "downstream"],
        init=None,
        operations=[
            AgentProcessorOperation(
                name="publish",
                map=None,
                topic_send=send_schema_proposal,
                filter=None,
                table_refs=[],
                app=app,
            ),
            AgentProcessorOperation(
                name="downstream",
                map=downstream,
                topic_send=None,
                filter=None,
                table_refs=[],
                app=app,
            ),
        ],
        app=app,
    )
    processor.input = SimpleNamespace(buffer_spec=None)
    processor.output = AgentOutputSpec(topics_spec=[output_spec], app=app)

    asyncio.run(
        processor.processor(
            DummyStream([{"id": "mat-123", "payload": {}}], event=SimpleNamespace())
        )
    )

    assert publish_topic.calls == []
    assert output_topic.calls == [
        {
            "key_serializer": None,
            "value_serializer": None,
            "key": None,
            "value": {
                "materialization_id": "mat-123",
                "continued": True,
            },
            "partition": None,
            "headers": None,
        }
    ]


def test_agent_topic_send_failure_surfaces_processor_error():
    app = DummyApp({"schema-proposals": DummyTopic(should_fail=True)})
    send_schema_proposal = AgentProcessorTopicSendOperator(
        name="schema-proposals",
        name_selector=None,
        pass_through=True,
        ack=False,
        key_serializer=None,
        value_serializer=None,
        key_selector=None,
        value_selector=None,
        partition_selector=None,
        headers_selector=None,
        predicate=None,
        app=app,
    )
    send_schema_proposal._topics = {}
    processor = AgentProcessorSpec(
        pipeline=["publish"],
        init=None,
        operations=[
            AgentProcessorOperation(
                name="publish",
                map=None,
                topic_send=send_schema_proposal,
                filter=None,
                table_refs=[],
                app=app,
            )
        ],
        app=app,
    )
    processor.input = SimpleNamespace(buffer_spec=None)
    processor.output = None

    with pytest.raises(RuntimeError, match="publish failed"):
        asyncio.run(
            processor.processor(
                DummyStream([{"id": "mat-123", "payload": {}}], event=SimpleNamespace())
            )
        )


def test_task_topic_send_first_op_pass_through_preserves_none_value():
    topic = DummyTopic()
    app = DummyApp({"task-events": topic})

    send_event = TaskProcessorTopicSendOperator(
        name="task-events",
        name_selector=None,
        pass_through=True,
        ack=False,
        key_serializer=None,
        value_serializer=None,
        key_selector=None,
        value_selector=None,
        partition_selector=None,
        headers_selector=None,
        predicate=None,
        app=app,
    )
    send_event._topics = {}
    finish = TaskProcessorMapOperator(
        python="""
def finish(value):
    if value is not None:
        raise AssertionError('expected None to be preserved')
    return {'saw_none': True}
""",
        entrypoint="finish",
    )
    processor = TaskProcessorSpec(
        pipeline=["publish", "finish"],
        init=None,
        operations=[
            TaskProcessorOperation(
                name="publish",
                map=None,
                topic_send=send_event,
                filter=None,
                table_refs=[],
                app=app,
            ),
            TaskProcessorOperation(
                name="finish",
                map=finish,
                topic_send=None,
                filter=None,
                table_refs=[],
                app=app,
            ),
        ],
        app=app,
    )

    asyncio.run(processor.processor(None))

    assert topic.calls == [
        {
            "key_serializer": None,
            "value_serializer": None,
            "key": None,
            "value": None,
            "partition": None,
            "headers": None,
        }
    ]


def test_webview_pipeline_preserves_response_object_with_topic_send_pass_through():
    topic = DummyTopic()
    app = DummyApp(topic)
    response = WebViewResponseSpec(
        content_type=CONTENT_TYPE["json"],
        status_code=202,
        headers=None,
        body_selector=None,
        status_code_selector=None,
        headers_selector=None,
    )

    prepare_request = WebViewProcessorMapOperator(
        python="""
def prepare_request(value):
    return {
        'response': {
            'materialization_id': 'mat-123',
            'request_id': 'req-123',
            'desired_phase': 'running',
            'observed_phase': 'requested',
            'phase_reason': 'requested',
            'status_url': '/materializations/mat-123',
            'accepted_at': '2026-07-16T14:00:00Z',
        },
        'request_event': {
            'materialization_id': 'mat-123',
            'request_id': 'req-123',
            'desired_phase': 'running',
        },
    }
""",
        entrypoint="prepare_request",
    )
    send_request_event = WebViewProcessorTopicSendOperator(
        name="materialization-requests",
        name_selector=None,
        pass_through=True,
        ack=False,
        key_serializer=None,
        value_serializer=None,
        key_selector=None,
        value_selector=None,
        partition_selector=None,
        headers_selector=None,
        predicate=None,
        app=app,
    )
    send_request_event._key_selector_func = lambda value, **_kwargs: value[
        "request_event"
    ]["materialization_id"]
    send_request_event._value_selector_func = lambda value, **_kwargs: value[
        "request_event"
    ]
    send_request_event._topics = {}
    return_response = WebViewProcessorMapOperator(
        python="""
def return_response(value):
    return value['response']
""",
        entrypoint="return_response",
    )

    operations = [
        WebViewProcessorOperation(
            name="prepare-request",
            map=prepare_request,
            topic_send=None,
            filter=None,
            table_refs=[],
            app=app,
        ),
        WebViewProcessorOperation(
            name="send-request-event",
            map=None,
            topic_send=send_request_event,
            filter=None,
            table_refs=[],
            app=app,
        ),
        WebViewProcessorOperation(
            name="return-response",
            map=return_response,
            topic_send=None,
            filter=None,
            table_refs=[],
            app=app,
        ),
    ]
    processor = WebViewProcessorSpec(
        pipeline=[op.name for op in operations],
        init=None,
        operations=operations,
        app=app,
    )
    processor.response = response

    result = asyncio.run(processor.processor(DummyWeb(), object()))

    assert topic.calls == [
        {
            "key_serializer": None,
            "value_serializer": None,
            "key": "mat-123",
            "value": {
                "materialization_id": "mat-123",
                "request_id": "req-123",
                "desired_phase": "running",
            },
            "partition": None,
            "headers": None,
        }
    ]
    assert result["status"] == 202
    assert result["content_type"] == CONTENT_TYPE["json"]
    assert result["data"] == {
        "materialization_id": "mat-123",
        "request_id": "req-123",
        "desired_phase": "running",
        "observed_phase": "requested",
        "phase_reason": "requested",
        "status_url": "/materializations/mat-123",
        "accepted_at": "2026-07-16T14:00:00Z",
    }