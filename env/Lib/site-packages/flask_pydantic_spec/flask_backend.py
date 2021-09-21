import logging

from typing import Optional, Mapping, Callable, Any, Tuple, List, Type, Iterable, Dict
from dataclasses import dataclass

from pydantic import ValidationError, BaseModel
from flask import (
    request,
    abort,
    make_response,
    jsonify,
    Request as FlaskRequest,
    Flask,
    Response as FlaskResponse,
)
from werkzeug.datastructures import Headers

from .config import Config
from .page import PAGES
from .types import ResponseBase, RequestBase
from .utils import parse_multi_dict


@dataclass
class Context:
    query: Optional[BaseModel]
    body: Optional[BaseModel]
    headers: Optional[BaseModel]
    cookies: Optional[BaseModel]


class FlaskBackend:
    def __init__(self, validator: Any) -> None:
        self.validator = validator
        self.config: Config = validator.config
        self.logger: logging.Logger = logging.getLogger(__name__)

    def find_routes(self) -> Any:
        for rule in self.app.url_map.iter_rules():
            if any(
                str(rule).startswith(path)
                for path in (f"/{self.config.PATH}", "/static")
            ):
                continue
            yield rule

    def bypass(self, func: Callable, method: str) -> bool:
        if method in ["HEAD", "OPTIONS"]:
            return True
        return False

    def parse_func(self, route: Any) -> Any:
        func = self.app.view_functions[route.endpoint]
        for method in route.methods:
            yield method, func

    def parse_path(self, route: Any) -> Tuple[str, List[Any]]:
        from werkzeug.routing import parse_rule, parse_converter_args

        subs = []
        parameters = []

        for converter, arguments, variable in parse_rule(str(route)):
            if converter is None:
                subs.append(variable)
                continue
            subs.append(f"{{{variable}}}")

            args: Iterable[Any] = []
            kwargs: Dict[str, Any] = {}

            if arguments:
                args, kwargs = parse_converter_args(arguments)

            schema = None
            if converter == "any":
                schema = {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": args,
                    },
                }
            elif converter == "int":
                schema = {
                    "type": "integer",
                    "format": "int32",
                }
                if "max" in kwargs:
                    schema["maximum"] = kwargs["max"]
                if "min" in kwargs:
                    schema["minimum"] = kwargs["min"]
            elif converter == "float":
                schema = {
                    "type": "number",
                    "format": "float",
                }
            elif converter == "uuid":
                schema = {
                    "type": "string",
                    "format": "uuid",
                }
            elif converter == "path":
                schema = {
                    "type": "string",
                    "format": "path",
                }
            elif converter == "string":
                schema = {
                    "type": "string",
                }
                for prop in ["length", "maxLength", "minLength"]:
                    if prop in kwargs:
                        schema[prop] = kwargs[prop]
            elif converter == "default":
                schema = {"type": "string"}

            parameters.append(
                {
                    "name": variable,
                    "in": "path",
                    "required": True,
                    "schema": schema,
                }
            )

        return "".join(subs), parameters

    def request_validation(
        self,
        request: FlaskRequest,
        query: Optional[Type[BaseModel]],
        body: Optional[RequestBase],
        headers: Optional[Type[BaseModel]],
        cookies: Optional[Type[BaseModel]],
    ) -> None:
        raw_query = request.args or None
        if raw_query is not None:
            req_query = parse_multi_dict(raw_query)
        else:
            req_query = {}
        if request.content_type == "application/json":
            parsed_body = request.get_json() or {}
        else:
            parsed_body = request.get_data() or {}
        req_headers: Optional[Headers] = request.headers or None
        req_cookies: Optional[Mapping[str, str]] = request.cookies or None
        setattr(
            request,
            "context",
            Context(
                query=query.parse_obj(req_query) if query else None,
                body=getattr(body, "model").parse_obj(parsed_body)
                if body and getattr(body, "model")
                else None,
                headers=headers.parse_obj(req_headers or {}) if headers else None,
                cookies=cookies.parse_obj(req_cookies or {}) if cookies else None,
            ),
        )

    def validate(
        self,
        func: Callable,
        query: Optional[Type[BaseModel]],
        body: Optional[RequestBase],
        headers: Optional[Type[BaseModel]],
        cookies: Optional[Type[BaseModel]],
        resp: Optional[ResponseBase],
        before: Callable,
        after: Callable,
        *args: List[Any],
        **kwargs: Mapping[str, Any],
    ) -> FlaskResponse:
        response, req_validation_error, resp_validation_error = None, None, None
        try:
            self.request_validation(request, query, body, headers, cookies)
        except ValidationError as err:
            req_validation_error = err
            response = make_response(
                jsonify(err.errors()), self.config.VALIDATION_ERROR_CODE
            )

        before(request, response, req_validation_error, None)
        if req_validation_error:
            abort(response)  # type: ignore

        response = make_response(func(*args, **kwargs))

        if resp and resp.has_model() and getattr(resp, "validate"):
            model = resp.find_model(response.status_code)
            if model:
                try:
                    model.validate(response.get_json())
                except ValidationError as err:
                    resp_validation_error = err
                    response = make_response(
                        jsonify({"message": "response validation error"}), 500
                    )

        after(request, response, resp_validation_error, None)

        return response

    def register_route(self, app: Flask) -> None:
        self.app = app
        from flask import jsonify

        self.app.add_url_rule(
            self.config.spec_url,
            "openapi",
            lambda: jsonify(self.validator.spec),
        )

        for ui in PAGES:
            self.app.add_url_rule(
                f"/{self.config.PATH}/{ui}",
                f"doc_page_{ui}",
                lambda ui=ui: PAGES[ui].format(self.config),
            )
