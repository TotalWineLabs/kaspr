from typing import Optional, TypeVar, List, Dict, Union
from kaspr.utils.functional import maybe_async
from kaspr.types.models.base import SpecComponent
from kaspr.types.models.pycode import PyCode
from kaspr.types.operation import ProcessorOperatorT
from kaspr.types.models.tableref import TableRefSpec
from kaspr.types.table import KasprTableT, KasprGlobalTableT
from kaspr.types.app import KasprAppT

T = TypeVar("T")
Table = Union[KasprTableT, KasprGlobalTableT]

class AgentProcessorFilterOperator(ProcessorOperatorT, PyCode):
    
    async def process(self, value: T, **kwargs) -> T:
        if not await maybe_async(self.func(value, **kwargs)):
            return self.skip_value
        else:
            return value


class AgentProcessorMapOperator(ProcessorOperatorT, PyCode):
    async def process(self, value: T, **kwargs) -> T:
        return await maybe_async(self.func(value, **kwargs))


class AgentProcessorOperation(SpecComponent):
    name: str
    filter: Optional[AgentProcessorFilterOperator]
    map: Optional[AgentProcessorMapOperator]
    table_refs: Optional[List[TableRefSpec]]

    app: KasprAppT = None

    _operator: ProcessorOperatorT = None
    _tables: Dict[str, Table] = None

    def get_operator(self) -> ProcessorOperatorT:
        """Get the specific operator type for this operation block."""
        return next((x for x in [self.filter, self.map] if x is not None), None)

    def prepare_tables(self) -> Dict[str, Table]:
        """Tables for operation keyed by argument name."""
        for table_ref in self.table_refs:
            if table_ref.name not in self.app.tables:
                raise ValueError(
                    f"Table '{table_ref.name}' is not a registered table name."
                )
        return {
            table_ref.arg_name: self.app.tables.get(table_ref.name)
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
        return f'{type(self).__name__}: {self.name}'

    @property
    def shortlabel(self) -> str:
        """Return short description."""
        return self.label