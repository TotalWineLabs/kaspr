import asyncio
from types import SimpleNamespace

import pytest

from kaspr.types.models.topicout import TopicOutSpec
from kaspr.types.models.webview.operations import (
    WebViewProcessorMapOperator,
    WebViewProcessorOperation,
    WebViewProcessorTopicSendOperator,
)
from kaspr.types.models.webview.processor import WebViewProcessorSpec
from kaspr.types.models.webview.response import CONTENT_TYPE, WebViewResponseSpec


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
        return self._topic


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