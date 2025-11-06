from typing import Optional, List, Awaitable, Any, Callable, Dict
from inspect import isasyncgen
from kaspr.utils.functional import ensure_generator
from kaspr.types.models.base import SpecComponent
from kaspr.types.models.webview.operations import WebViewProcessorOperation
from kaspr.types.models.webview.response import WebViewResponseSpec
from kaspr.types.models.pycode import PyCode
from kaspr.types.app import KasprAppT
from kaspr.types.webview import KasprWebRequest, KasprWeb
from kaspr.exceptions import KasprProcessingError


class WebViewProcessorSpec(SpecComponent):
    """Processor specification."""

    pipeline: Optional[List[str]]
    init: Optional[PyCode]
    operations: List[WebViewProcessorOperation]

    app: KasprAppT = None

    _processor: Callable[..., Awaitable[Any]] = None
    _response: WebViewResponseSpec = None
    _init_scope: Dict[str, Any] = None

    def prepare_processor(self) -> Callable[..., Awaitable[Any]]:
        operations = {op.name: op for op in self.operations}

        async def _request_processor(
            web: KasprWeb, request: KasprWebRequest, **kwargs: Any
        ) -> Any:
            try:
                context = {"app": self.app}
                if self.init:
                    self.init.with_scope({"context": {**context}})                  
                init_scope = self.init_scope
                for name in self.pipeline:
                    if name not in operations:
                        raise ValueError(f"Operation '{name}' is not defined.")
                ops = [operations[name] for name in self.pipeline]
                operation = None
                # No operations, return the request data
                if not ops:
                    return self.response.build_success(web)
                operation = ops[0]
                operator = operation.operator
                tables = operation.tables
                scope = {
                    **init_scope,
                    "context": {**context},
                }
                operator.with_scope(scope)
                result = await operator.process(request, **tables, **kwargs)
                if result == operator.skip_value:
                    return
                gen = ensure_generator(result, async_gen=isasyncgen(result))
                response_values = []
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
                                    "context": {**context},
                                }
                                operator.with_scope(scope)
                                result = await operator.process(current_value, **tables)
                                if result == operator.skip_value:
                                    continue
                                # Collect all results
                                next_values.extend(
                                    ensure_generator(
                                        result, async_gen=isasyncgen(result)
                                    )
                                )
                            # Update for the next callback
                            current_values = next_values
                        response_values.extend(current_values)

                    if len(response_values) > 0:
                        # Processors can generate multiple values, but we can only return one value in
                        # a web response, so we return the last successful value.
                        return self.response.build_success(web, response_values[-1])
                    else:
                        return self.response.build_success(web)
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
                                    "context": {**context},
                                }
                                operator.with_scope(scope)
                                result = await operator.process(current_value, **tables)
                                if result == operator.skip_value:
                                    continue
                                # Collect all results
                                next_values.extend(
                                    ensure_generator(
                                        result, async_gen=isasyncgen(result)
                                    )
                                )
                            # Update for the next callback
                            current_values = next_values
                        response_values.extend(current_values)

                if len(response_values) > 0:
                    # Processors can generate multiple values, but we can only return one value in
                    # a web response, so we return the last successful value.
                    return self.response.build_success(web, response_values[-1])
                else:
                    return self.response.build_success(web)

            except Exception as ex:
                error = KasprProcessingError(
                    message=str(ex),
                    operation=operation.name if operation else None,
                    cause=ex,
                )
                return self.response.build_error(web, error)

        return _request_processor

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
    def response(self) -> WebViewResponseSpec:
        return self._response

    @response.setter
    def response(self, response: WebViewResponseSpec):
        self._response = response

    @property
    def label(self) -> str:
        """Return description, used in graphs and logs."""
        return f"{type(self).__name__}"

    @property
    def shortlabel(self) -> str:
        """Return short description."""
        return self.label
