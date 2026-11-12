"""Omitted form fields keep the values the handlers were written against (#347).

FastAPI 0.147 started reading an omitted ``bool`` form field as ``False`` and
0.162 reversed that to "the declared default, or a missing-field error". The
panel's checkboxes (the admin-only Unlimited box on both key forms, the
role/active boxes on the user edit form) are deliberately **not** ``bool``
parameters: they are ``str = Form("")`` or ``str | None = Form(None)`` and the
handler interprets the raw string itself, so neither change reaches them. These
tests pin that, through FastAPI's real form parsing rather than a direct call
of the handler (which would bypass the parsing entirely):

* no form route in the app declares a ``bool`` form parameter, so the next one
  has to be written with the 0.162 rule in mind (default ``False`` for a
  checkbox) and this test updated deliberately;
* for each checkbox-carrying route, the form parameters copied from the real
  endpoint's signature yield ``""`` / ``None`` for an unchecked box (field
  omitted) and the raw ``"1"`` / ``"on"`` for a checked one.
"""
import inspect
import types
import typing

import pydantic_settings
from fastapi import FastAPI
from fastapi.params import Form as FormParam
from fastapi.testclient import TestClient

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from src.api import routes as api_routes
    from src.auth import routes as auth_routes
    from src.control_panel import routes as panel_routes
    from src.control_panel import users as users_routes
    from src.oauth import routes as oauth_routes
    from src.transfer import routes as transfer_routes
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


_ROUTERS = (
    api_routes.router,
    auth_routes.router,
    panel_routes.router,
    users_routes.router,
    oauth_routes.router,
    transfer_routes.router,
)


def _form_params(endpoint) -> list[inspect.Parameter]:
    return [
        p
        for p in inspect.signature(endpoint).parameters.values()
        if isinstance(p.default, FormParam)
    ]


def _is_boolish(annotation) -> bool:
    if annotation is bool:
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _is_boolish(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return any(_is_boolish(a) for a in typing.get_args(annotation))
    return False


def test_no_form_route_declares_a_bool_parameter():
    assert _is_boolish(bool | None) and _is_boolish(typing.Optional[bool])
    offenders = []
    seen = 0
    for router in _ROUTERS:
        for route in router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue
            hints = typing.get_type_hints(endpoint, include_extras=True)
            for p in _form_params(endpoint):
                seen += 1
                if _is_boolish(hints.get(p.name, p.annotation)):
                    offenders.append(f"{route.path} {p.name}")
    # Not vacuous: the panel, auth, users and consent routes declare 30+.
    assert seen >= 30
    assert offenders == [], (
        "A bool form parameter takes its declared default when the browser "
        "omits it (an unchecked checkbox) since FastAPI 0.162; declare it with "
        "default False and update this test deliberately: " + ", ".join(offenders)
    )


def _echo_app(endpoint) -> TestClient:
    """A route whose signature is exactly the real endpoint's form params."""
    params = _form_params(endpoint)
    hints = typing.get_type_hints(endpoint)

    async def echo(**kwargs):
        return kwargs

    echo.__signature__ = inspect.Signature(
        [p.replace(annotation=hints[p.name], kind=inspect.Parameter.KEYWORD_ONLY)
         for p in params]
    )
    app = FastAPI()
    app.post("/echo")(echo)
    return TestClient(app)


def test_create_key_form_unchecked_unlimited_is_empty_string():
    client = _echo_app(panel_routes.create_key_form)
    unchecked = client.post("/echo", data={"name": "k", "daily_request_limit": "5"})
    assert unchecked.status_code == 200
    assert unchecked.json() == {
        "name": "k", "permission": "read", "daily_request_limit": "5", "unlimited": "",
    }
    checked = client.post("/echo", data={"name": "k", "unlimited": "1"})
    assert checked.json()["unlimited"] == "1"
    assert checked.json()["daily_request_limit"] == ""


def test_set_key_limit_form_unchecked_unlimited_is_empty_string():
    client = _echo_app(panel_routes.set_key_limit_form)
    unchecked = client.post("/echo", data={"daily_request_limit": "7"})
    assert unchecked.status_code == 200
    assert unchecked.json() == {"daily_request_limit": "7", "unlimited": ""}
    assert client.post("/echo", data={"unlimited": "1"}).json()["unlimited"] == "1"


def test_edit_user_unchecked_boxes_are_absent_not_true():
    client = _echo_app(users_routes.edit_user_submit)
    unchecked = client.post("/echo", data={"vault_path": "/v"})
    assert unchecked.status_code == 200
    assert unchecked.json() == {
        "vault_path": "/v", "vault_path_custom": "", "is_admin": None, "is_active": None,
    }
    checked = client.post("/echo", data={"is_admin": "on", "is_active": "on"})
    assert checked.json()["is_admin"] == "on"
    assert checked.json()["is_active"] == "on"
