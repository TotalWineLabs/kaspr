from kaspr.types.schemas.base import BaseSchema
from marshmallow import fields
from kaspr.types.models.webview import (
    WebViewProcessorOperation,
    WebViewProcessorTopicSendOperator,
    WebViewProcessorMapOperator,
    WebViewProcessorFilterOperator
)
from kaspr.types.schemas.pycode import PyCodeSchema
from kaspr.types.schemas.topicout import TopicOutSpecSchema
from kaspr.types.schemas.tableref import TableRefSpecSchema

class WebViewProcessorTopicSendOperatorSchema(TopicOutSpecSchema):
    __model__ = WebViewProcessorTopicSendOperator


class WebViewProcessorMapOperatorSchema(PyCodeSchema):
    __model__ = WebViewProcessorMapOperator

class WebViewProcessorFilterOperatorSchema(PyCodeSchema):
    __model__ = WebViewProcessorFilterOperator


class WebViewProcessorOperationSchema(BaseSchema):
    __model__ = WebViewProcessorOperation

    name = fields.String(data_key="name", allow_none=True, load_default=None)
    topic_send = fields.Nested(
        WebViewProcessorTopicSendOperatorSchema(),
        data_key="topic_send",
        allow_none=True,
        load_default=None,
    )
    map = fields.Nested(
        WebViewProcessorMapOperatorSchema(),
        data_key="map",
        allow_none=True,
        load_default=None,
    )
    filter = fields.Nested(
        WebViewProcessorFilterOperatorSchema(),
        data_key="filter",
        allow_none=True,
        load_default=None,
    )
    table_refs = fields.List(
        fields.Nested(
            TableRefSpecSchema(), required=True
        ),
        data_key="tables",
        allow_none=False,
        load_default=list,
    )