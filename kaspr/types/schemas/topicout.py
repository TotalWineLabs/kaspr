from typing import Dict
from marshmallow import fields, validates_schema
from kaspr.types.schemas.base import BaseSchema
from kaspr.types.models.topicout import TopicOutSpec
from kaspr.types.schemas.topicselector import (
    TopicNameSelectorSchema,
    TopicKeySelectorSchema,
    TopicValueSelectorSchema,
    TopicPartitionSelectorSchema,
    TopicHeadersSelectorSchema,
    TopicPredicateSchema,
)


class TopicOutSpecSchema(BaseSchema):
    __model__ = TopicOutSpec

    name = fields.Str(data_key="name", allow_none=True, load_default=None)
    name_selector = fields.Nested(
        TopicNameSelectorSchema(),
        data_key="name_selector",
        allow_none=True,
        load_default=None,
    )
    pass_through = fields.Bool(
        data_key="pass_through", allow_none=True, load_default=False
    )
    ack = fields.Bool(data_key="ack", allow_none=True, load_default=False)
    key_serializer = fields.Str(
        data_key="key_serializer", allow_none=True, load_default=None
    )
    value_serializer = fields.Str(
        data_key="value_serializer", allow_none=True, load_default=None
    )
    key_selector = fields.Nested(
        TopicKeySelectorSchema(),
        data_key="key_selector",
        allow_none=True,
        load_default=None,
    )
    value_selector = fields.Nested(
        TopicValueSelectorSchema(),
        data_key="value_selector",
        allow_none=True,
        load_default=None,
    )
    partition_selector = fields.Nested(
        TopicPartitionSelectorSchema(),
        data_key="partition_selector",
        allow_none=True,
        load_default=None,
    )
    headers_selector = fields.Nested(
        TopicHeadersSelectorSchema(),
        data_key="headers_selector",
        allow_none=True,
        load_default=None,
    )
    predicate = fields.Nested(
        TopicPredicateSchema(),
        data_key="predicate",
        allow_none=True,
        load_default=None,
    )

    @validates_schema
    def validate_name(self, data: Dict, **kwargs):
        if data.get("name") and data.get("name_selector"):
            raise ValueError(
                "Only one of 'name' or 'name_selector' can be provided, but not both."
            )
        if not data.get("name") and not data.get("name_selector"):
            raise ValueError("One of 'name' or 'name_selector' must be provided.")
