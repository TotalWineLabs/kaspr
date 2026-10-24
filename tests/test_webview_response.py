"""A WebView's error responses: their own content type when the view names one, the view's otherwise."""

import pytest
from marshmallow import ValidationError

from kaspr.exceptions import KasprProcessingError
from kaspr.types.models.pycode import PyCode
from kaspr.types.models.webview import CONTENT_TYPE, WebViewResponseSelector, WebViewResponseSpec
from kaspr.types.schemas.webview.response import WebViewResponseSpecSchema


class RecordingWeb:
    """Records which kind of response a view built, with what."""

    def _record(self, kind):
        def build(data, content_type=None, status=None, headers=None):
            return {"kind": kind, "data": data, "content_type": content_type, "status": status, "headers": headers}

        return build

    def __getattr__(self, name):
        if name in ("json", "bytes", "text", "html"):
            return self._record(name)
        raise AttributeError(name)


class Refusal(Exception):
    """An error a handler raises, carrying its HTTP answer as Kaspr reads it from the cause."""

    def __init__(self, status_code, body):
        super().__init__("refused")
        self.status_code = status_code
        self._body = body

    def to_response(self):
        return self._body


def spec(**fields):
    base = {"content_type": None, "status_code": None, "headers": None, "body_selector": None, "status_code_selector": None, "headers_selector": None}
    base.update(fields)
    return WebViewResponseSpec(**base)


def refused(status_code=409, body=None):
    body = body if body is not None else {"error": {"code": "conflict", "message": "no", "details": {"reason": "tail_rebalancing"}}}
    return KasprProcessingError("failed", cause=Refusal(status_code, body))


def test_a_binary_view_answers_its_errors_as_json_when_it_says_so():
    view = spec(content_type=CONTENT_TYPE["binary"], error_content_type=CONTENT_TYPE["json"])

    success = view.build_success(RecordingWeb(), b"PAR1...PAR1")
    error = view.build_error(RecordingWeb(), refused())

    assert (success["kind"], success["content_type"]) == ("bytes", "application/octet-stream")
    assert (error["kind"], error["content_type"], error["status"]) == ("json", "application/json", 409)
    assert error["data"] == {"error": {"code": "conflict", "message": "no", "details": {"reason": "tail_rebalancing"}}}


def test_without_one_errors_take_the_views_content_type_as_before():
    binary = spec(content_type=CONTENT_TYPE["binary"])
    error = binary.build_error(RecordingWeb(), refused(body=b'{"error": {}}'))
    assert (error["kind"], error["content_type"]) == ("bytes", "application/octet-stream")

    untyped = spec()
    error = untyped.build_error(RecordingWeb(), refused(body="refused"))
    assert (error["kind"], error["content_type"]) == ("text", "text/plain")

    # A spec built in code, without the field at all, reads as before too.
    built = WebViewResponseSpec(content_type=CONTENT_TYPE["json"], status_code=None, headers=None, body_selector=None, status_code_selector=None, headers_selector=None)
    assert built.build_error(RecordingWeb(), refused())["kind"] == "json"


def test_an_error_body_selector_answers_in_the_error_content_type():
    selector = WebViewResponseSelector(
        on_success=None,
        on_error=PyCode(python="def body(error):\n    return {'reason': error['details']['error']['details']['reason']}\n", entrypoint="body"),
    )
    view = spec(content_type=CONTENT_TYPE["binary"], error_content_type=CONTENT_TYPE["json"], body_selector=selector)
    cause = Refusal(503, None)
    cause.details = {"error": {"details": {"reason": "tail_not_ready"}}}

    error = view.build_error(RecordingWeb(), KasprProcessingError("failed", cause=cause))

    assert (error["kind"], error["data"], error["status"]) == ("json", {"reason": "tail_not_ready"}, 503)


def test_the_schema_reads_the_error_content_type_and_refuses_one_it_does_not_know():
    loaded = WebViewResponseSpecSchema().load({"content_type": "application/octet-stream", "error_content_type": "application/json"})
    assert (loaded.content_type, loaded.error_content_type) == ("application/octet-stream", "application/json")
    assert WebViewResponseSpecSchema().load({"content_type": "application/json"}).error_content_type is None
    with pytest.raises(ValidationError):
        WebViewResponseSpecSchema().load({"error_content_type": "application/xml"})
