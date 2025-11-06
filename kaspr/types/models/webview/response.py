from typing import TypeVar, Optional, Mapping, Union, Awaitable, Callable
from kaspr.types.models.base import BaseModel
from kaspr.types.models.pycode import PyCode
from kaspr.types.webview import KasprWeb, KasprWebResponse
from kaspr.exceptions import KasprProcessingError

T = TypeVar("T")
Function = Callable[[T], Union[T, Awaitable[T]]]

CONTENT_TYPE = {
    "html": "text/html",
    "json": "application/json",
    "plain": "text/plain",
    "binary": "application/octet-stream",
}


class WebViewResponseSelector(BaseModel):
    on_success: Optional[PyCode]
    on_error: Optional[PyCode]


class WebViewResponseSpec(BaseModel):
    content_type: Optional[str]
    status_code: Optional[int]
    headers: Optional[Mapping[str, str]]
    body_selector: Optional[WebViewResponseSelector]
    status_code_selector: Optional[WebViewResponseSelector]
    headers_selector: Optional[WebViewResponseSelector]

    _body_selector_success_func: Function = None
    _body_selector_error_func: Function = None
    _status_code_selector_success_func: Function = None
    _status_code_selector_error_func: Function = None
    _headers_selector_success_func: Function = None
    _headers_selector_error_func: Function = None

    def build_success(self, web: KasprWeb, data: T = None) -> KasprWebResponse:
        """Build response for success condition."""
        content_type = self.content_type or CONTENT_TYPE["plain"]

        if self.status_code_selector_success_func:
            status_code = self.status_code_selector_success_func(data)
        elif self.status_code:
            status_code = self.status_code
        else:
            status_code = 200

        if self.headers_selector_success_func:
            headers = self.headers_selector_success_func(data)
        elif self.headers:
            headers = self.headers
        else:
            headers = None

        if self.body_selector_success_func:
            data = self.body_selector_success_func(data)

        if self.content_type == CONTENT_TYPE["html"]:
            response = web.html
        elif self.content_type == CONTENT_TYPE["json"]:
            response = web.json
        elif self.content_type == CONTENT_TYPE["plain"]:
            response = web.text
        elif self.content_type == CONTENT_TYPE["binary"]:
            response = web.bytes
        else:
            response = web.text

        return response(
            data, content_type=content_type, status=status_code, headers=headers
        )

    def build_error(
        self, web: KasprWeb, error: KasprProcessingError
    ) -> KasprWebResponse:
        """Build response for error condition."""
        content_type = self.content_type or CONTENT_TYPE["plain"]

        _err = error.to_dict()
        if self.status_code_selector_error_func:
            status_code = self.status_code_selector_error_func(_err)
        else:
            status_code = 500

        if self.headers_selector_error_func:
            headers = self.headers_selector_error_func(_err)
        elif self.headers:
            headers = self.headers
        else:
            headers = None

        if self.body_selector_error_func:
            data = self.body_selector_error_func(_err)
        else:
            data = _err

        if self.content_type == CONTENT_TYPE["html"]:
            response = web.html
        elif self.content_type == CONTENT_TYPE["json"]:
            response = web.json
        elif self.content_type == CONTENT_TYPE["plain"]:
            response = web.text
        elif self.content_type == CONTENT_TYPE["binary"]:
            response = web.bytes
        else:
            response = web.text

        return response(
            data, content_type=content_type, status=status_code, headers=headers
        )

    @property
    def body_selector_success_func(self):
        """Body selector function on success condition"""
        if (
            self._body_selector_success_func is None
            and self.body_selector
            and self.body_selector.on_success
        ):
            self._body_selector_success_func = self.body_selector.on_success.func
        return self._body_selector_success_func

    @property
    def body_selector_error_func(self):
        """Body selector function on error condition"""
        if (
            self._body_selector_error_func is None
            and self.body_selector
            and self.body_selector.on_error
        ):
            self._body_selector_error_func = self.body_selector.on_error.func
        return self._body_selector_error_func

    @property
    def status_code_selector_success_func(self):
        """Status code selector function on success condition"""
        if (
            self._status_code_selector_success_func is None
            and self.status_code_selector
            and self.status_code_selector.on_success
        ):
            self._status_code_selector_success_func = (
                self.status_code_selector.on_success.func
            )
        return self._status_code_selector_success_func

    @property
    def status_code_selector_error_func(self):
        """Status code selector function on error condition"""
        if (
            self._status_code_selector_error_func is None
            and self.status_code_selector
            and self.status_code_selector.on_error
        ):
            self._status_code_selector_error_func = (
                self.status_code_selector.on_error.func
            )
        return self._status_code_selector_error_func

    @property
    def headers_selector_success_func(self):
        """Headers selector function on success condition"""
        if (
            self._headers_selector_success_func is None
            and self.headers_selector
            and self.headers_selector.on_success
        ):
            self._headers_selector_success_func = self.headers_selector.on_success.func
        return self._headers_selector_success_func

    @property
    def headers_selector_error_func(self):
        """Headers selector function on error condition"""
        if (
            self._headers_selector_error_func is None
            and self.headers_selector
            and self.headers_selector.on_error
        ):
            self._headers_selector_error_func = self.headers_selector.on_error.func
        return self._headers_selector_error_func
