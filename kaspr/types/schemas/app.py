import yaml
from marshmallow import fields
from kaspr.types.schemas.base import BaseSchema
from kaspr.types.schemas.agent.agent import AgentSpecSchema
from kaspr.types.schemas.webview import WebViewSpecSchema
from kaspr.types.schemas.table import TableSpecSchema
from kaspr.types.schemas.join import JoinSpecSchema
from kaspr.types.schemas.task import TaskSpecSchema
from kaspr.types.models import AppSpec
from kaspr.types import KasprAppT


class AppSpecSchema(BaseSchema):
    __model__ = AppSpec

    agents_spec = fields.List(
        fields.Nested(AgentSpecSchema(), allow_none=False),
        data_key="agents",
        load_default=[],
        required=False
    )

    webviews_spec = fields.List(
        fields.Nested(WebViewSpecSchema(), allow_none=False),
        data_key="webviews",
        load_default=[],
        required=False
    )

    tables_spec = fields.List(
        fields.Nested(TableSpecSchema(), allow_none=False),
        data_key="tables",
        load_default=[],
        required=False
    )

    joins_spec = fields.List(
        fields.Nested(JoinSpecSchema(), allow_none=False),
        data_key="joins",
        load_default=[],
        required=False
    )

    tasks_spec = fields.List(
        fields.Nested(TaskSpecSchema(), allow_none=False),
        data_key="tasks",
        load_default=[],
        required=False
    )

    @classmethod
    def from_file(cls, file, app: KasprAppT) -> AppSpec:
        """Load an AppSpec from a file."""
        return AppSpecSchema(context={"app": app}).load(yaml.safe_load(file))
