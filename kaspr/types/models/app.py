from typing import Optional, List, Union, Callable, Awaitable
from kaspr.types.models.base import SpecComponent
from kaspr.types.models.agent import AgentSpec
from kaspr.types.models.webview import WebViewSpec
from kaspr.types.models.table import TableSpec
from kaspr.types.models.join import JoinSpec
from kaspr.types.models.task import TaskSpec
from kaspr.types.app import KasprAppT
from kaspr.types.agent import KasprAgentT
from kaspr.types.webview import KasprWebViewT
from kaspr.types.table import KasprTableT

TaskArg = Union[Callable[["KasprAppT"], Awaitable], Callable[[], Awaitable]]

class AppSpec(SpecComponent):
    agents_spec: Optional[List[AgentSpec]]
    webviews_spec: Optional[List[WebViewSpec]]
    tables_spec: Optional[List[TableSpec]]
    joins_spec: Optional[List[JoinSpec]]
    tasks_spec: Optional[List[TaskSpec]]

    app: KasprAppT = None
    _agents: List[KasprAgentT] = None
    _webviews: List[KasprWebViewT] = None
    _tables: List[KasprTableT] = None
    _joins: list = None
    _tasks: List[TaskArg] = None

    @property
    def agents(self) -> List[KasprAgentT]:
        if self.app:
            if self._agents is None:
                self._agents = [agent.agent for agent in self.agents_spec]
            return self._agents

    @property
    def webviews(self) -> List[WebViewSpec]:

        if self.app:
            if self._webviews is None:
                self._webviews = [webview.webview for webview in self.webviews_spec]
            return self._webviews
        
    @property
    def tables(self) -> List[TableSpec]:
        if self.app:
            if self._tables is None:
                self._tables = [table.table for table in self.tables_spec]
            return self._tables

    @property
    def joins(self) -> list:
        if self.app:
            if self._joins is None:
                self._joins = [join.join for join in self.joins_spec]
            return self._joins
        
    @property
    def tasks(self) -> List[TaskSpec]:
        if self.app:
            if self._tasks is None:
                self._tasks = [task.task for task in self.tasks_spec]
            return self._tasks

    @property
    def label(self) -> str:
        """Return description, used in graphs and logs."""
        return f"{type(self).__name__}: {self.app.conf.name}"

    @property
    def shortlabel(self) -> str:
        """Return short description."""
        return self.label
