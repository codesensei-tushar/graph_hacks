"""The suite under ``TENANCY_WRITE_STRICT=1``, as production runs it.

A test is a script that works on the legacy org on purpose, the way ``scripts/seed_graph.py`` is,
so each test method, its ``setUp``/``tearDown`` and its class's ``setUpClass``/``tearDownClass``
run inside ``org_context(legacy_org())``, resolved when they run (so a graph a class patched in
is the one used). A class that tests what happens with no org sets ``NO_ORG_CONTEXT = True``.

A request through ``TestClient`` does not inherit that context: it starts with none, exactly as a
request reaching uvicorn does, and only the legacy-only paths get the legacy org
(``tenancy.LegacyOrgRoutes``, as ``main.app`` installs it). So a route that forgot its org fails
here the way it would in production, and so does a thread started without ``tenancy.bind``.

Installed by ``tests/__init__.py`` when ``TENANCY_WRITE_STRICT`` is set; CI runs the suite both ways.
"""

import functools
import unittest

from starlette import testclient

from src.core import tenancy


def _legacy(fn):
    @functools.wraps(fn)
    def run(*args, **kwargs):
        with tenancy.org_context(tenancy.legacy_org()):
            return fn(*args, **kwargs)

    return run


class _AsProduction:
    """ASGI: an HTTP request starts with no org in context, then the legacy-only paths get the legacy org."""

    def __init__(self, app) -> None:
        self.app = tenancy.LegacyOrgRoutes(app)
        self.inner = app

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.inner(scope, receive, send)
            return
        token = tenancy._current.set(None)
        try:
            await self.app(scope, receive, send)
        finally:
            tenancy._current.reset(token)


def install() -> None:
    original_run = unittest.TestCase.run

    def run(self, result=None):
        if getattr(type(self), "NO_ORG_CONTEXT", False):
            return original_run(self, result)
        with tenancy.org_context(tenancy.legacy_org()):
            return original_run(self, result)

    unittest.TestCase.run = run

    original_init_subclass = unittest.TestCase.__dict__.get("__init_subclass__")

    @classmethod
    def init_subclass(cls, **kwargs):
        if original_init_subclass is not None:
            original_init_subclass.__func__(cls, **kwargs)
        else:
            super(unittest.TestCase, cls).__init_subclass__(**kwargs)
        if getattr(cls, "NO_ORG_CONTEXT", False):
            return
        for name in ("setUpClass", "tearDownClass"):
            # Wherever it is defined: on the class, or on a mixin that is no TestCase.
            owner = next(klass for klass in cls.__mro__ if name in klass.__dict__)
            method = owner.__dict__[name].__func__
            if owner is unittest.TestCase or getattr(method, "_legacy", False):
                continue
            wrapped = _legacy(method)
            wrapped._legacy = True
            setattr(cls, name, classmethod(wrapped))

    unittest.TestCase.__init_subclass__ = init_subclass

    original_client_init = testclient.TestClient.__init__

    def client_init(self, app, *args, **kwargs):
        original_client_init(self, _AsProduction(app), *args, **kwargs)

    testclient.TestClient.__init__ = client_init
