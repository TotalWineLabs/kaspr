from typing import Optional
from kaspr.types.models.base import SpecComponent
from kaspr.types.models.agent.input import AgentInputSpec
from kaspr.types.models.agent.output import AgentOutputSpec
from kaspr.types.models.agent.processor import AgentProcessorSpec
from kaspr.types.app import KasprAppT
from kaspr.types.agent import KasprAgentT


class AgentSpec(SpecComponent):
    name: str
    description: Optional[str]
    isolated_partitions: Optional[bool]
    input: AgentInputSpec
    output: AgentOutputSpec
    processors: AgentProcessorSpec

    app: KasprAppT = None

    _agent: KasprAgentT = None

    def prepare_agent(self) -> KasprAgentT:
        self.log.info("Preparing...")
        self._warn_serializer_mismatch()
        processors = self.processors
        processors.input = self.input
        processors.output = self.output
        return self.app.agent(
            self.input.channel,
            name=self.name,
            isolated_partitions=self.isolated_partitions,
        )(processors.processor)

    def _warn_serializer_mismatch(self) -> None:
        """Log a warning if the input topic's serializers don't match the
        target table's serializers. A mismatch can cause double-encoded keys
        and broken join lookups."""
        topic_spec = getattr(self.input, 'topic_spec', None)
        if topic_spec is None:
            return
        tables = self._collect_table_refs()
        if not tables:
            return
        first_table = tables[0]
        topic_key_ser = topic_spec.key_serializer
        topic_val_ser = topic_spec.value_serializer
        table_key_ser = first_table.key_serializer
        table_val_ser = first_table.value_serializer
        if topic_key_ser is not None and topic_key_ser != table_key_ser:
            self.log.warning(
                "Agent '%s': input topic key_serializer '%s' does not match "
                "table '%s' key_serializer '%s'. This may cause double-encoded "
                "keys and broken join lookups. Consider aligning them.",
                self.name, topic_key_ser, first_table.name, table_key_ser,
            )
        if topic_val_ser is not None and topic_val_ser != table_val_ser:
            self.log.warning(
                "Agent '%s': input topic value_serializer '%s' does not match "
                "table '%s' value_serializer '%s'. This may cause "
                "serialization issues. Consider aligning them.",
                self.name, topic_val_ser, first_table.name, table_val_ser,
            )

    def _collect_table_refs(self):
        """Collect resolved table objects from all operations."""
        tables = []
        for op in self.processors.operations:
            if op.table_refs:
                for ref in op.table_refs:
                    table = self.app.tables.get(ref.name)
                    if table is not None and table not in tables:
                        tables.append(table)
        return tables

    @property
    def agent(self) -> KasprAgentT:
        if self._agent is None:
            self._agent = self.prepare_agent()
        return self._agent

    @property
    def label(self) -> str:
        """Return description of component, used in logs."""
        return f"{type(self).__name__}: {self.__repr__()}"

    @property
    def shortlabel(self) -> str:
        """Return short description of processor."""
        return f"{type(self).__name__}: {self.name}"
