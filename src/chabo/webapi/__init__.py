"""FastAPI JSON API for the Chabo web console."""

from typing import Any


def create_web_api(*args: Any, **kwargs: Any) -> Any:
    from .main import create_web_api as _create_web_api

    return _create_web_api(*args, **kwargs)

__all__ = ["create_web_api"]
