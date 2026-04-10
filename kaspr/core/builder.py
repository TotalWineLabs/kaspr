"""Build stream processors from external definition files."""

import yaml
from typing import List
from pathlib import Path
from kaspr.types import KasprAppT, AppBuilderT
from kaspr.types.models import AppSpec, AgentSpec, WebViewSpec, TableSpec, TaskSpec
from kaspr.types.models.join import JoinSpec
from kaspr.types.schemas import AppSpecSchema
from mode.utils.objects import cached_property


class AppBuilder(AppBuilderT):
    """Build stream processors from definition files."""

    app: KasprAppT = None

    _apps: List[AppSpec] = None
    _agents: List[AgentSpec] = None
    _webviews: List[WebViewSpec] = None
    _tables: List[TableSpec] = None
    _joins: List[JoinSpec] = None
    _tasks: List[TaskSpec] = None

    def __init__(self, app: KasprAppT) -> None:
        self.app = app

    def _files(self, directory: Path) -> List[str]:
        """Find all JSON files in the given directory and its subdirectories."""
        return list(directory.rglob("*.yml")) + list(directory.rglob("*.yaml"))

    def _load(self, files: List[Path]) -> List[AppSpec]:
        """Load the content of definition files and convert them to dictionaries."""
        apps = []
        for file in files:
            with file.open("r") as file:
                try:
                    apps.append(AppSpecSchema.from_file(file, app=self.app))
                except yaml.YAMLError as exc:
                    print(f"Error loading file `{file}`: {exc}")
        return apps

    def _load_apps(self) -> List[AppSpec]:
        """Load app component definitions."""
        return self._load(self._files(self.app.conf.definitionssdir))

    def _prepare_agents(self) -> List[AgentSpec]:
        """Prepare agents from loaded definitions."""
        agents = []
        for app in self.apps:
            agents.extend(app.agents_spec)
        return agents
    
    def _prepare_webviews(self) -> List[WebViewSpec]:
        """Prepare webviews from loaded definitions."""
        webviews = []
        for app in self.apps:
            webviews.extend(app.webviews_spec)
        return webviews
    
    def _prepare_tables(self) -> List[TableSpec]:
        """Prepare tables from loaded definitions."""
        tables = []
        for app in self.apps:
            tables.extend(app.tables_spec)
        return

    def _prepare_joins(self) -> List[JoinSpec]:
        """Prepare joins from loaded definitions."""
        joins = []
        for app in self.apps:
            joins.extend(app.joins_spec or [])
        return joins
    
    def _prepare_tasks(self) -> List[TaskSpec]:
        """Prepare tasks from loaded definitions."""
        tasks = []
        for app in self.apps:
            tasks.extend(app.tasks_spec)
        return tasks

    def build(self) -> None:
        """Build agents, tasks, etc. from definition files."""
        for app in self.apps:
            app.tables
            app.joins
            app.tasks            
            app.agents
            app.webviews

    async def maybe_create_topics(self) -> None:
        """Maybe declare agent input topics."""
        # Ensure producer has starter before creating topics.
        await self.app.producer.maybe_start()
        for app in self.apps:
            for agent in app.agents_spec:
                if agent.input.declare:
                    await agent.input.channel.maybe_declare()

    @cached_property
    def apps(self) -> List[AppSpec]:
        if self._apps is None:
            self._apps = self._load_apps()
        return self._apps

    @cached_property
    def agents(self) -> List[AgentSpec]:
        if self._agents is None:
            self._agents = self._prepare_agents()
        return self._agents
    
    @cached_property
    def webviews(self) -> List[WebViewSpec]:
        if self._webviews is None:
            self._webviews = self._prepare_webviews()
        return self._webviews
    
    @cached_property
    def tables(self) -> List[TableSpec]:
        if self._tables is None:
            self._tables = self._prepare_tables()
        return self._tables

    @cached_property
    def joins(self) -> List[JoinSpec]:
        if self._joins is None:
            self._joins = self._prepare_joins()
        return self._joins
    
    @cached_property
    def tasks(self) -> List[TaskSpec]:
        if self._tasks is None:
            self._tasks = self._prepare_tasks()
        return self._tasks
