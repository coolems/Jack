"""Run CLIENT-side tests against CLIENT's `app` package, then put things back.

This repo ships TWO packages named `app`:

  * the SERVER tree   <repo>/app          (app.providers.llama, app.initialization, ...)
  * the CLIENT tree   <repo>/CLIENT/app   (app.routers, app.security_events, ...)

One interpreter can only have one of them loaded as `app`, so a test that loads a CLIENT
module must say so explicitly instead of hoping its file happens to be imported before the
other side's test. `client_app()` swaps the CLIENT tree in; `client_app_scope` (a fixture
each CLIENT test module imports) wraps every test in that swap and restores exactly what was
there before, so no test can depend on collection order and the SERVER-side tests keep their
own `app`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
CLIENT = REPO / "CLIENT"


def _app_module_names() -> list:
    """Names of every already-imported `app` submodule (either tree)."""
    return [name for name in sys.modules if name == "app" or name.startswith("app.")]


@contextlib.contextmanager
def client_app():
    """Make <repo>/CLIENT the import root for `app` inside the block."""
    saved = {name: sys.modules[name] for name in _app_module_names()}
    for name in saved:
        del sys.modules[name]
    client_root = str(CLIENT)
    sys.path.insert(0, client_root)
    try:
        yield CLIENT
    finally:
        if client_root in sys.path:
            sys.path.remove(client_root)
        for name in _app_module_names():
            del sys.modules[name]
        sys.modules.update(saved)


@pytest.fixture(autouse=True)
def client_app_scope():
    """Autouse in every module that imports it: the test runs on CLIENT's `app`."""
    with client_app():
        yield


def load_module(rel_path: str, module_name: str) -> ModuleType:
    """Execute *rel_path* (repo-relative, e.g. 'CLIENT/app/workspace_access.py').

    Call it from a test or a fixture, i.e. while `client_app_scope` is active: the source
    file's own ``from app import ...`` statements must resolve to the CLIENT tree.
    """
    spec = importlib.util.spec_from_file_location(module_name, REPO / rel_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {rel_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
