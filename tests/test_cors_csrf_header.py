"""Regression coverage for the CORS/CSRF middleware contract."""

import pydantic_settings
from fastapi.testclient import TestClient

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from src.main import app
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


def test_cors_preflight_allows_required_csrf_header():
    response = TestClient(app).options(
        "/api/keys",
        headers={
            "Origin": "http://localhost:8000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-token",
        },
    )

    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "x-csrf-token" in {header.strip() for header in allowed.split(",")}


def test_cors_preflight_allows_api_key_delete_method():
    response = TestClient(app).options(
        "/api/keys/1",
        headers={
            "Origin": "http://localhost:8000",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "x-csrf-token",
        },
    )

    assert response.status_code == 200
    allowed = response.headers["access-control-allow-methods"]
    assert "DELETE" in {method.strip() for method in allowed.split(",")}
