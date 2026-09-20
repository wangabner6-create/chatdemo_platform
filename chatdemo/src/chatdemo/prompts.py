"""Loads prompt templates from external files, treated as per-product configuration
(design D16).

This follows the same model-config resolution approach used by the reference
pipeline: each template file has top-level keys keyed by model config
(`default`, `<product>`, and so on), and we look up the requested config,
falling back to `default` when it's missing. That's what lets the vendored
`workflows/<wf>/prompts/*.yaml` files be consumed as-is, with no changes needed.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@lru_cache(maxsize=128)
def _load_file(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def load_prompt(prompts_dir: Path, name: str, model_config: str = "default") -> dict[str, Any]:
    data = _load_file(str(prompts_dir / f"{name}.yaml"))
    if model_config in data:
        return data[model_config]
    if "default" in data:
        return data["default"]
    return data


def system_prompt(prompts_dir: Path, name: str, model_config: str = "default") -> str | None:
    return load_prompt(prompts_dir, name, model_config).get("system_prompt")
