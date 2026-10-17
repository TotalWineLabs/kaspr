from typing import Optional, List, Awaitable, Any, Callable, Dict
from inspect import isasyncgen
from kaspr.utils.functional import ensure_generator
from kaspr.types.models.base import SpecComponent
from kaspr.types.models.agent.operations import AgentProcessorOperation
from kaspr.types.models.agent.input import AgentInputSpec
from kaspr.types.models.agent.output import AgentOutputSpec
from kaspr.types.models.pycode import PyCode
from kaspr.types.app import KasprAppT
from kaspr.types.stream import KasprStreamT


class AgentProcessorSpec(SpecComponent):
    """Processor specification."""

    pipeline: Optional[List[str]]
    init: Optional[PyCode]
    operations: List[AgentProcessorOperation]

    app: KasprAppT = None

    _processor: Callable[..., Awaitable[Any]] = None
    _output: AgentOutputSpec = None
    _input: AgentInputSpec = None
    _init_scope: Dict[str, Any] = None
    _noack: Optional[bool] = None

    def prepare_processor(self) -> Callable[..., Awaitable[Any]]:
        operations = {op.name: op for op in self.operations}
        input = self.input
        output = self.output
        noack = self._noack

        async def _aprocessor(stream: KasprStreamT):
            context = {"app": self.app}
            if self.init:
                self.init.with_scope({"context": {**context}})
            init_scope = self.init_scope
            if noack:
                stream = stream.noack()
            ops = []
            for name in self.pipeline:
                if name not in operations:
                    raise ValueError(f"Operation '{name}' is not defined.")
                ops.append(operations[name])
            if not ops:
                return
            _stream = stream
            buffered = False
            if input.buffer_spec:
                _stream = _stream.take_events(
                    max_=input.buffer_spec.max_size,
                    within=input.buffer_spec.timeout,
                )
                buffered = True
            try:
                async for value in _stream:
                    operation = ops[0]
                    operator = operation.operator
                    tables = operation.tables
                    event = stream.current_event
                    _value = value
                    if buffered:
                        _value, event = _value
                    scope = {
                        **init_scope,
                        "context": {**context, "event": event},
                    }
                    operator.with_scope(scope)
                    value = await operator.process(_value, **tables)
                    if value == operator.skip_value:
                        continue
                    gen = ensure_generator(value, async_gen=isasyncgen(value))

                    if isasyncgen(gen):
                        async for value in gen:
                            # Start with the initial value
                            current_values = [value]
                            for operation in ops[1:]:
                                next_values = []
                                operator = operation.operator
                                tables = operation.tables
                                for current_value in current_values:
                                    scope = {
                                        **init_scope,
                                        "context": {**context, "event": event},
                                    }
                                    operator.with_scope(scope)
                                    value = await operator.process(current_value, **tables)
                                    if value == operator.skip_value:
                                        continue
                                    # Collect all results
                                    next_values.extend(
                                        ensure_generator(value, async_gen=isasyncgen(value))
                                    )
                                # Update for the next callback
                                current_values = next_values

                            for value in current_values:
                                if output:
                                    await output.send(value)                                
                    else:
                        for value in gen:
                            # Start with the initial value
                            current_values = [value]
                            for operation in ops[1:]:
                                next_values = []
                                operator = operation.operator
                                tables = operation.tables
                                for current_value in current_values:
                                    scope = {
                                        **init_scope,
                                        "context": {**context, "event": event},
                                    }
                                    operator.with_scope(scope)
                                    value = await operator.process(current_value, **tables)
                                    if value == operator.skip_value:
                                        continue
                                    # Collect all results
                                    next_values.extend(
                                        ensure_generator(value, async_gen=isasyncgen(value))
                                    )
                                # Update for the next callback
                                current_values = next_values

                            for value in current_values:
                                if output:
                                    await output.send(value)

            except Exception as e:
                self.on_error(e)
                raise

        return _aprocessor

    def on_error(self, e: Exception):
        """Handle errors in the processor."""
        if self.init:
            self.init.clear_scope()
        self._init_scope = None

    @property
    def init_scope(self) -> Dict[str, Any]:
        """Return the initialization scope."""
        if self._init_scope is None:
            self._init_scope = self.init.execute().scope if self.init else {}
        return self._init_scope
    
    @property
    def processor(self) -> Callable[..., Awaitable[Any]]:
        if self._processor is None:
            self._processor = self.prepare_processor()
        return self._processor

    @property
    def input(self) -> AgentInputSpec:
        return self._input

    @input.setter
    def input(self, input: AgentInputSpec):
        self._input = input

    @property
    def output(self) -> AgentOutputSpec:
        return self._output

    @output.setter
    def output(self, outputs: AgentOutputSpec):
        self._output = outputs

    @property
    def noack(self) -> Optional[bool]:
        return self._noack

    @noack.setter
    def noack(self, noack: Optional[bool]):
        self._noack = noack

    @property
    def label(self) -> str:
        """Return description, used in graphs and logs."""
        return f"{type(self).__name__}"

    @property
    def shortlabel(self) -> str:
        """Return short description."""
        return self.label
