"""Viewer plugin discovery and composition (current spec §15).

Plugins are discovered via Python entry points in the group
``"okf_loom.viewer_plugins"``. Each plugin provides
``on_concept_render(concept, html) -> html``; ``on_index_render(html) -> html``
is optional. Plugins are loaded at server/render start and called in
registration order. A plugin that raises during load OR during a hook call is
logged to ``stderr`` and skipped — the viewer must never crash because of a
plugin.

Active-code gating (current spec §14): plugins execute arbitrary Python, so they
run ONLY when the bundle's ``allow_active_code`` is true (read from
:class:`okf_loom.config.OkfConfig`'s ``viewer.allow_active_code``; default
``False``). When the gate is closed the composite is a complete no-op AND the
plugins are not even imported (entry-point load executes module-level code,
so the gate must close before discovery, not just before the hook call).
"""
from __future__ import annotations

import importlib.metadata
import inspect
import sys
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

#: Entry-points group name (spec §12).
ENTRY_POINT_GROUP: str = "okf_loom.viewer_plugins"


@runtime_checkable
class ViewerPluginProtocol(Protocol):
    """Minimum contract: a plugin MUST implement ``on_concept_render``.

    ``on_index_render`` is optional; plugins that do not implement it are
    silently skipped for index pages (handled via ``getattr`` in
    :class:`CompositeViewerPlugin`).
    """

    def on_concept_render(self, concept: Any, html: str) -> str: ...


def _plugin_name(plugin: Any) -> str:
    """Best-effort name for a plugin instance (for stderr log lines)."""
    cls = type(plugin)
    return f"{cls.__module__}.{cls.__qualname__}"


class CompositeViewerPlugin:
    """ViewerPlugin that fans out to children in registration order.

    Each child's hook is wrapped in try/except. A child that raises is
    logged to ``stderr`` and skipped for THAT call only; subsequent
    children still run and the viewer keeps serving. (Spec §12: "a plugin
    that raises is logged and skipped (never crash the viewer)".)

    When ``allow_active_code`` is ``False`` the composite is a complete
    no-op: neither hook iterates children. This is the §11 active-code
    gate — plugins execute arbitrary code, so they must be opt-in per
    bundle.
    """

    def __init__(
        self,
        plugins: Iterable[Any],
        *,
        allow_active_code: bool,
    ) -> None:
        self._plugins: list[Any] = list(plugins)
        self._allow_active_code: bool = bool(allow_active_code)

    @property
    def plugins(self) -> list[Any]:
        """Child plugin instances (in registration order; defensive copy)."""
        return list(self._plugins)

    @property
    def allow_active_code(self) -> bool:
        """Whether the active-code gate is open for this composite."""
        return self._allow_active_code

    def on_concept_render(self, concept: Any, html: str) -> str:
        if not self._allow_active_code:
            return html
        for plugin in self._plugins:
            handler = getattr(plugin, "on_concept_render", None)
            if handler is None:
                continue
            # P1-2 (iter-3): capture prev so a non-string return (None/int/dict)
            # leaves html unchanged. Spec §12: viewer must never crash because
            # of a plugin — a buggy return is the same class as a raise.
            prev = html
            try:
                out = handler(concept, html)
            except Exception as e:  # noqa: BLE001 — plugins are arbitrary code
                print(
                    f"okf: viewer plugin {_plugin_name(plugin)!r} raised in "
                    f"on_concept_render; skipping: {e}",
                    file=sys.stderr,
                )
                continue
            if not isinstance(out, str):
                print(
                    f"okf: viewer plugin {_plugin_name(plugin)!r} returned "
                    f"non-str ({type(out).__name__}) in on_concept_render; "
                    f"skipping",
                    file=sys.stderr,
                )
                html = prev
            else:
                html = out
        return html

    def on_index_render(self, html: str) -> str:
        if not self._allow_active_code:
            return html
        for plugin in self._plugins:
            handler = getattr(plugin, "on_index_render", None)
            if handler is None:
                continue
            prev = html
            try:
                out = handler(html)
            except Exception as e:  # noqa: BLE001
                print(
                    f"okf: viewer plugin {_plugin_name(plugin)!r} raised in "
                    f"on_index_render; skipping: {e}",
                    file=sys.stderr,
                )
                continue
            if not isinstance(out, str):
                print(
                    f"okf: viewer plugin {_plugin_name(plugin)!r} returned "
                    f"non-str ({type(out).__name__}) in on_index_render; "
                    f"skipping",
                    file=sys.stderr,
                )
                html = prev
            else:
                html = out
        return html


# ---------------------------------------------------------------------------
# Entry-point discovery
# ---------------------------------------------------------------------------


def _entry_points_for(group: str) -> list[importlib.metadata.EntryPoint]:
    """Return entry points in ``group`` across Python 3.11+ signatures.

    ``importlib.metadata.entry_points`` gained the ``select(group=...)`` API
    in 3.10 and the ``group=...`` keyword in 3.12; this shim supports both
    shapes plus the older dict return (Python <3.10). Discovery errors are
    logged to stderr and treated as "no plugins" — the viewer keeps running.
    """
    try:
        eps = importlib.metadata.entry_points()
    except Exception as e:  # pragma: no cover — environment-dependent
        print(
            f"okf: failed to enumerate entry_points group {group!r}: {e}",
            file=sys.stderr,
        )
        return []
    # 3.10+: EntryPoints has a .select(group=...) method.
    if hasattr(eps, "select"):
        try:
            return list(eps.select(group=group))
        except Exception:  # pragma: no cover
            return []
    # Older dict shape (Python <3.10).
    if isinstance(eps, dict):  # pragma: no cover
        return list(eps.get(group, []))
    return list(eps)


def _coerce_to_instance(loaded: Any, ep_name: str) -> Any:
    """Coerce a loaded entry-point object to a plugin instance.

    Accepted shapes (checked in order):
        * A class → instantiated once with no args (the conventional form).
        * A non-class instance already exposing ``on_concept_render`` → as-is.
        * Any other callable (factory) → called once; result must expose
          ``on_concept_render``.
    Anything else is logged to stderr and skipped (returns ``None``).

    Note: ``hasattr(SomeClass, "on_concept_render")`` is True for the class
    itself (methods are class attributes), so the ``inspect.isclass`` check
    MUST come before the attribute check to avoid returning the class
    un-instantiated.
    """
    if inspect.isclass(loaded):
        try:
            instance = loaded()
        except Exception as e:  # noqa: BLE001 — plugin constructor errors
            print(
                f"okf: viewer plugin entry point {ep_name!r} constructor "
                f"raised; skipping: {e}",
                file=sys.stderr,
            )
            return None
        if hasattr(instance, "on_concept_render"):
            return instance
        print(
            f"okf: viewer plugin entry point {ep_name!r} instance does not "
            f"provide on_concept_render; skipping",
            file=sys.stderr,
        )
        return None
    if hasattr(loaded, "on_concept_render"):
        return loaded
    if callable(loaded):
        try:
            instance = loaded()
        except Exception as e:  # noqa: BLE001
            print(
                f"okf: viewer plugin entry point {ep_name!r} factory raised; "
                f"skipping: {e}",
                file=sys.stderr,
            )
            return None
        if hasattr(instance, "on_concept_render"):
            return instance
    print(
        f"okf: viewer plugin entry point {ep_name!r} does not provide "
        f"on_concept_render; skipping",
        file=sys.stderr,
    )
    return None


def load_viewer_plugins() -> list[Any]:
    """Discover viewer plugins via the ``okf_loom.viewer_plugins`` group.

    Returns plugin instances in entry-point registration order. Plugins whose
    entry point fails to load OR whose factory does not produce something
    with ``on_concept_render`` are logged to stderr and skipped.

    P2-9 (security, defense-in-depth): consults operator consent internally
    and short-circuits to ``[]`` when closed, so a direct caller cannot
    trigger entry-point import (which runs arbitrary module-level code) when
    the operator has not consented to active code. Callers SHOULD still gate
    via :func:`build_viewer_plugin` (which also checks the bundle cfg); this
    internal check is the backstop for embedders/notebooks that call
    ``load_viewer_plugins()`` directly.
    """
    # P2-9: defense-in-depth at the privileged boundary. ep.load() runs
    # arbitrary module-level code; do not execute it unless the operator has
    # consented to active code.
    try:
        from .assets import operator_consent
        if not operator_consent():
            return []
    except Exception:
        # If consent cannot be determined, fail closed.
        return []
    plugins: list[Any] = []
    for ep in _entry_points_for(ENTRY_POINT_GROUP):
        try:
            loaded = ep.load()
        except Exception as e:  # noqa: BLE001 — plugin load errors are runtime
            print(
                f"okf: viewer plugin entry point {ep.name!r} failed to load; "
                f"skipping: {e}",
                file=sys.stderr,
            )
            continue
        instance = _coerce_to_instance(loaded, ep.name)
        if instance is not None:
            plugins.append(instance)
    return plugins


def build_viewer_plugin(
    bundle_root: str | Path | None = None,
    *,
    allow_active_code: bool | None = None,
) -> CompositeViewerPlugin:
    """Build the composite plugin for a bundle (current spec §15 + §14 gate).

    If ``allow_active_code`` is ``None`` it is read from the bundle's
    ``okf-loom.config.yaml`` via :class:`okf_loom.config.OkfConfig` (default
    ``False`` when the file is absent or unreadable). When the gate is closed
    NO entry-point discovery happens (defence in depth — module import itself
    is arbitrary code execution).
    """
    if allow_active_code is None:
        try:
            from ..config import OkfConfig

            if bundle_root is not None:
                allow_active_code = OkfConfig.load(bundle_root).viewer.allow_active_code
            else:
                allow_active_code = False
        except Exception:
            # Any config-read failure ⇒ fail-closed (no plugins run).
            allow_active_code = False
    plugins = load_viewer_plugins() if allow_active_code else []
    return CompositeViewerPlugin(plugins, allow_active_code=allow_active_code)


__all__ = [
    "ENTRY_POINT_GROUP",
    "ViewerPluginProtocol",
    "CompositeViewerPlugin",
    "load_viewer_plugins",
    "build_viewer_plugin",
]
