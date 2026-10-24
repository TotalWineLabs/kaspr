from typing import Dict
from kaspr.types.schemas.base import BaseSchema
from kaspr.types.models.webview import (
    WebViewResponseSpec,
    WebViewResponseSelector,
    CONTENT_TYPE,
)
from kaspr.types.schemas.pycode import PyCodeSchema
from marshmallow import fields, validate, validates_schema


class WebViewResponseSelectorSchema(BaseSchema):
    __model__ = WebViewResponseSelector

    on_success = fields.Nested(
        PyCodeSchema(),
        data_key="on_success",
        allow_none=True,
        load_default=None,
    )
    on_error = fields.Nested(
        PyCodeSchema(),
        data_key="on_error",
        allow_none=True,
        load_default=None,
    )


class WebViewResponseSpecSchema(BaseSchema):
    __model__ = WebViewResponseSpec

    content_type = fields.Str(
        data_key="content_type",
        allow_none=True,
        load_default=None,
        validate=validate.OneOf(CONTENT_TYPE.values()),
    )
    error_content_type = fields.Str(
        data_key="error_content_type",
        allow_none=True,
        load_default=None,
        validate=validate.OneOf(CONTENT_TYPE.values()),
    )
    status_code = fields.Int(data_key="status_code", allow_none=True, load_default=None)
    headers = fields.Mapping(
        keys=fields.Str(required=True),
        values=fields.Str(required=True),
        data_key="headers",
        allow_none=True,
        load_default={},
    )
    body_selector = fields.Nested(
        WebViewResponseSelectorSchema(),
        data_key="body_selector",
        allow_none=True,
        load_default=None,
    )
    status_code_selector = fields.Nested(
        WebViewResponseSelectorSchema(),
        data_key="status_code_selector",
        allow_none=True,
        load_default=None,
    )
    headers_selector = fields.Nested(
        WebViewResponseSelectorSchema(),
        data_key="headers_selector",
        allow_none=True,
        load_default=None,
    )

    @validates_schema
    def validate_schema(self, data: Dict, **kwargs):
        if data.get("status_code") and data.get("status_code_selector"):
            raise ValueError("Only one of 'status_code' or 'status_code_selector' can be specified")
        if data.get("headers") and data.get("headers_selector"):
            raise ValueError("Only one of 'headers' or 'headers_selector' can be specified")   