"""Loads typed tools from disk (per design decisions D4, D8).

Given a tool name, looks it up either under a shared top-level
`tools/<name>/` directory or a handler-specific `<folder>/tools/<name>/`
directory. Every tool directory must contain a `tool.yaml` (holding the
ToolSpec) plus a `handler.py` that defines `run(arguments, user)` as its
executor. Manifests reference shared tools by name so the same tool doesn't
need to be duplicated across handlers.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

import yaml

from .contracts import ToolExecutor, ToolSpec


@dataclass
class LoadedTool:
    spec: ToolSpec
    executor: ToolExecutor


class ToolLoadError(RuntimeError):
    pass


def _load_executor(handler_path: Path) -> ToolExecutor:
    spec = importlib.util.spec_from_file_location(f"chatdemo_tool_{handler_path.parent.name}", handler_path)
    if spec is None or spec.loader is None:
        raise ToolLoadError(f"cannot load tool executor {handler_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "run"):
        raise ToolLoadError(f"tool {handler_path} has no `run(arguments, user)` function")
    return module.run


def _load_one(tool_dir: Path) -> LoadedTool:
    manifest = tool_dir / "tool.yaml"
    handler = tool_dir / "handler.py"
    if not manifest.exists():
        raise ToolLoadError(f"missing tool.yaml in {tool_dir}")
    data = yaml.safe_load(manifest.read_text()) or {}
    spec = ToolSpec(**data)
    if not handler.exists():
        raise ToolLoadError(f"missing handler.py in {tool_dir} for tool {spec.name}")
    return LoadedTool(spec=spec, executor=_load_executor(handler))


def load_tools(root: Path, handler_folder: Path, names: list[str]) -> dict[str, LoadedTool]:
    """Look up every requested name, checking the handler-local tools/ directory before falling back to the shared tools/ directory."""
    out: dict[str, LoadedTool] = {}
    search = [handler_folder / "tools", root / "tools"]
    for name in names:
        found = next((base / name for base in search if (base / name / "tool.yaml").exists()), None)
        if found is None:
            raise ToolLoadError(f"tool '{name}' not found in {[str(b) for b in search]}")
        out[name] = _load_one(found)
    return out
