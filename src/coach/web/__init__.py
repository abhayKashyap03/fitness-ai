"""Local web dashboard (ADR-0014).

Optional: requires the ``[web]`` extra (``pip install -e ".[web]"``). The core
CLI never imports this package, so its dependencies stay optional.
"""

from __future__ import annotations

__all__ = ["create_app"]


def __getattr__(name: str):  # pragma: no cover - thin lazy re-export
    # Imported lazily so `import coach.web` doesn't hard-require FastAPI until
    # the app is actually built (keeps the CLI usable without the extra).
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
