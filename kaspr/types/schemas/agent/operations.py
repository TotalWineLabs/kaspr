from kaspr.types.schemas.base import BaseSchema
from marshmallow import fields
from kaspr.types.models import (
    AgentProcessorOperation,
    AgentProcessorTopicSendOperator,
    AgentProcessorFilterOperator,
    AgentProcessorMapOperator,
)
from kaspr.types.schemas.pycode import PyCodeSchema
from kaspr.types.schemas.tableref import TableRefSpecSchema
from kaspr.types.schemas.topicout import TopicOutSpecSchema


class AgentProcessorTopicSendOperatorSchema(TopicOutSpecSchema):
    __model__ = AgentProcessorTopicSendOperator


class AgentProcessorFilterOperatorSchema(PyCodeSchema):
    __model__ = AgentProcessorFilterOperator


class AgentProcessorMapOperatorSchema(PyCodeSchema):
    __model__ = AgentProcessorMapOperator


class AgentProcessorOperationSchema(BaseSchema):
    __model__ = AgentProcessorOperation

    name = fields.String(data_key="name", required=True)
    topic_send = fields.Nested(
        AgentProcessorTopicSendOperatorSchema(),
        data_key="topic_send",
        allow_none=True,
        load_default=None,
    )
    filter = fields.Nested(
        AgentProcessorFilterOperatorSchema(),
        data_key="filter",
        allow_none=True,
        load_default=None,
    )
    map = fields.Nested(
        AgentProcessorMapOperatorSchema(),
        data_key="map",
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
