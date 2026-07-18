import asyncio
from types import SimpleNamespace

from kaspr.types.models.task.operations import TaskProcessorMapOperator, TaskProcessorOperation
from kaspr.types.models.task.processor import TaskProcessorSpec
from kaspr.types.models.tableref import TableRefSpec


def test_task_first_operation_receives_injected_tables():
    processing_state = {}
    schema_state = {"name": "schema"}
    app = SimpleNamespace(
        tables={
            "my-processing-state": processing_state,
            "my-schema-state": schema_state,
        }
    )

    operation = TaskProcessorOperation(
        name="emit",
        map=TaskProcessorMapOperator(
            python="""
def emit_snapshot(processing_state_table, schema_state_table):
    processing_state_table['schema_name'] = schema_state_table['name']
    return []
""",
            entrypoint="emit_snapshot",
        ),
        filter=None,
        topic_send=None,
        table_refs=[
            TableRefSpec(name="my-processing-state", param_name="processing_state_table"),
            TableRefSpec(name="my-schema-state", param_name="schema_state_table"),
        ],
        app=app,
    )
    processor = TaskProcessorSpec(
        pipeline=["emit"],
        init=None,
        operations=[operation],
        app=app,
    )

    asyncio.run(processor.processor(None))

    assert processing_state == {"schema_name": "schema"}


def test_task_later_operation_receives_value_and_tables():
    processing_state = {}
    app = SimpleNamespace(tables={"my-processing-state": processing_state})

    first = TaskProcessorOperation(
        name="start",
        map=TaskProcessorMapOperator(
            python="""
def start():
    return [1]
""",
            entrypoint="start",
        ),
        filter=None,
        topic_send=None,
        table_refs=[],
        app=app,
    )
    second = TaskProcessorOperation(
        name="step2",
        map=TaskProcessorMapOperator(
            python="""
def step2(value, processing_state_table):
    processing_state_table['value'] = value
    return value
""",
            entrypoint="step2",
        ),
        filter=None,
        topic_send=None,
        table_refs=[
            TableRefSpec(name="my-processing-state", param_name="processing_state_table")
        ],
        app=app,
    )
    processor = TaskProcessorSpec(
        pipeline=["start", "step2"],
        init=None,
        operations=[first, second],
        app=app,
    )

    asyncio.run(processor.processor(None))

    assert processing_state == {"value": [1]}