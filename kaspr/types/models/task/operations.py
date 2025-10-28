"""Defines all possible operations in a TaskProcessor.

The first operation in the pipeline is always called with no input value
because there is no source value for tasks.
"""

from typing import Optional, TypeVar, Dict, List, Union
from kaspr.utils.functional import maybe_async
from kaspr.types.models.base import SpecComponent
from kaspr.types.models.topicout import TopicOutSpec
from kaspr.types.models.pycode import PyCode
from kaspr.types.operation import ProcessorOperatorT
from kaspr.types.models.tableref import TableRefSpec
from kaspr.types.table import KasprTableT, KasprGlobalTableT
from kaspr.types.app import KasprAppT

T = TypeVar("T")
Table = Union[KasprTableT, KasprGlobalTableT]

class TaskProcessorTopicSendOperator(ProcessorOperatorT, TopicOutSpec):
    """Operator to send a message to a topic."""
    def with_scope(self, scope: Dict[str, T]):
        # Not applicable for this operator
        ...

    def execute(self): 
        # Not applicable for this operator
        ...

    async def process(self, value: T, first_op=False, **kwargs) -> T:
        if first_op:
            predicate = self.should_skip(**kwargs)
        else:
            predicate = self.should_skip(value, **kwargs)
        if predicate:
            return self.skip_value
        return await self.send(value, **kwargs)

class TaskProcessorMapOperator(ProcessorOperatorT, PyCode):
    """Operator to reformat a value."""
    async def process(self, value, first_op=False, **kwargs) -> T:
        if first_op:
            return await maybe_async(self.func(**kwargs))
        return await maybe_async(self.func(value, **kwargs))

class TaskProcessorFilterOperator(ProcessorOperatorT, PyCode):

    async def process(self, value, first_op=False, **kwargs) -> T:
        if first_op:
            predicate = await maybe_async(self.func(**kwargs))
        else:
            predicate = await maybe_async(self.func(value, **kwargs))
        
        if not predicate:
            return self.skip_value
        else:
            return value

class TaskProcessorOperation(SpecComponent):
    """Defines all possible operations in a TaskProcessor."""
    name: str
    topic_send: Optional[TaskProcessorTopicSendOperator]
    map: Optional[TaskProcessorMapOperator]
    filter: Optional[TaskProcessorFilterOperator]
    table_refs: Optional[List[TableRefSpec]]

    app: KasprAppT = None

    _operator: ProcessorOperatorT = None
    _tables: Dict[str, Table] = None

    def get_operator(self) -> ProcessorOperatorT:
        """Get the specific operator type for this operation block."""
        return next((x for x in [self.topic_send, self.map, self.filter] if x is not None), None)

    def prepare_tables(self) -> Dict[str, Table]:
        """Tables for operation keyed by argument name."""
        for table_ref in self.table_refs:
            if table_ref.name not in self.app.tables:
                raise ValueError(
                    f"Table '{table_ref.name}' is not a registered table name."
                )
        return {
            table_ref.param_name: self.app.tables.get(table_ref.name)
            for table_ref in self.table_refs
        }
    
    @property
    def operator(self) -> ProcessorOperatorT:
        if self._operator is None:
            self._operator = self.get_operator()
        return self._operator

    @property
    def tables(self) -> Dict[str, Table]:
        if self._tables is None:
            self._tables = self.prepare_tables()
        return self._tables

    @property
    def label(self) -> str:
        """Return description, used in graphs and logs."""
        return f"{type(self).__name__}: {self.name}"

    @property
    def shortlabel(self) -> str:
        """Return short description."""
        return self.label
