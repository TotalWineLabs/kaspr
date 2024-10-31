from kaspr.types.schemas.base import BaseSchema
from marshmallow import fields
from kaspr.types.models import PyCode

class PyCodeSchema(BaseSchema):
    __model__ = PyCode

    init = fields.String(data_key="init", required=False, allow_none=True, load_default=None)
    python = fields.String(data_key="python", required=True)