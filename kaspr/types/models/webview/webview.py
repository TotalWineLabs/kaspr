from collections import OrderedDict
from typing import Callable, TypeVar, Union, Awaitable, Type, Optional, List, Dict
from kaspr.types.models.base import SpecComponent
from kaspr.types.models.webview.request import WebViewRequestSpec
from kaspr.types.models.webview.response import WebViewResponseSpec
from kaspr.types.models.webview.processor import WebViewProcessorSpec
from kaspr.types.app import KasprAppT
from kaspr.types.webview import KasprWebViewT, View

T = TypeVar("T")
Function = Callable[[T], Union[T, Awaitable[T]]]


class WebViewSpec(SpecComponent):
    name: str
    description: Optional[str]
    request: WebViewRequestSpec
    response: WebViewResponseSpec
    processors: WebViewProcessorSpec

    app: KasprAppT = None

    _webview: KasprWebViewT = None

    def prepare_method_handler(self) -> Function:
        processors = self.processors
        processors.response = self.response
        return processors.processor

    def prepare_webview(self) -> KasprWebViewT:
        self.log.info("Preparing...")
        return self.app.page(self.request.path, name=self.name)(
            self.prepare_request_handler()
        )

    def prepare_request_handler(self) -> Type[View]:
        """Returns a class type that implements handler required HTTP verb."""

        return type("KasprWebView", (View,), {
            self.request.method.lower(): self.prepare_method_handler()
        })

    @classmethod
    def prepare_webviews(cls, webviews: List["WebViewSpec"]) -> List[KasprWebViewT]:
        grouped_webviews: Dict[str, List["WebViewSpec"]] = OrderedDict()

        for webview in webviews:
            grouped_webviews.setdefault(webview.request.path, []).append(webview)

        return [
            cls.prepare_combined_webview(path_webviews)
            for path_webviews in grouped_webviews.values()
        ]

    @classmethod
    def prepare_combined_webview(
        cls, webviews: List["WebViewSpec"]
    ) -> KasprWebViewT:
        if not webviews:
            raise ValueError("Expected at least one webview to register.")

        first = webviews[0]
        path = first.request.path
        handlers = {}

        for webview in webviews:
            if webview.request.path != path:
                raise ValueError(
                    "All grouped webviews must share the same request path."
                )

            method = webview.request.method.lower()
            if method in handlers:
                raise ValueError(
                    f"Duplicate WebView method '{webview.request.method}' for path '{path}'."
                )

            handlers[method] = webview.prepare_method_handler()

        return first.app.page(path, name=first.name)(
            type("KasprWebView", (View,), handlers)
        )

    @property
    def webview(self) -> KasprWebViewT:
        if self._webview is None:
            self._webview = self.prepare_webview()
        return self._webview

    @property
    def label(self) -> str:
        """Return description of component, used in logs."""
        return f"{type(self).__name__}: {self.__repr__()}"

    @property
    def shortlabel(self) -> str:
        """Return short description of processor."""
        return f"{type(self).__name__}: {self.name}"
