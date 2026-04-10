"""JoinSpec schema for key joins between tables."""

from marshmallow import fields
from kaspr.types.schemas.base import BaseSchema
from kaspr.types.schemas.pycode import PyCodeSchema
from kaspr.types.models.join import JoinSpec


class JoinSpecSchema(BaseSchema):
    __model__ = JoinSpec

    name = fields.String(data_key="name", required=True)
    description = fields.String(
        data_key="description", allow_none=True, load_default=None
    )
    left_table = fields.String(data_key="left_table", required=True)
    right_table = fields.String(data_key="right_table", required=True)
    extractor = fields.Nested(
        PyCodeSchema(), data_key="extractor", required=True
    )
    join_type = fields.String(
        data_key="type", allow_none=True, load_default="inner"
    )
    output_channel = fields.String(
        data_key="output_channel", allow_none=True, load_default=None
    )
