"""Automatic, folder-driven handler registration (design D8).

Walks `workflows/*/workflow.yaml` and `skills/*/SKILL.md`, assembling them into
one global catalog. Each manifest's `description` becomes the card the router
sees. Any malformed manifest or name collision raises immediately rather than
failing silently.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import yaml

from dataclasses import dataclass, field

from .contracts import Descriptor, Handler
from .handlers import StepSpec, WorkflowHandler
from .handlers.base import HandlerDeps, NullRetriever, Retriever
from .runner import build_runner
from .runner.base import ModelClient, ModelSettings
from .tools_loading import LoadedTool, load_tools


class RegistryError(RuntimeError):
    pass


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        return yaml.safe_load(fm) or {}, body.strip()
    return {}, text.strip()


def _load_file(path: Path, prefix: str):
    spec = importlib.util.spec_from_file_location(f"{prefix}_{path.parent.name}_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RegistryError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_step(step_path: Path):
    module = _load_file(step_path, "chatdemo_step")
    if not hasattr(module, "run"):
        raise RegistryError(f"step {step_path} has no `run` function")
    return module.run


def _load_retriever(folder: Path, cfg: dict | None) -> Retriever:
    """Retrieval is workflow-owned: try loading `<folder>/retriever.py::build(cfg,
    folder)`. If there's no retriever.py present, fall back to NullRetriever."""
    rp = folder / "retriever.py"
    if not rp.exists():
        return NullRetriever()
    module = _load_file(rp, "chatdemo_retriever")
    if not hasattr(module, "build"):
        raise RegistryError(f"{rp} has no `build(cfg, folder)` function")
    return module.build(cfg or {}, folder)


class Registry:
    """Builds each workflow into a self-contained handler sourced entirely from its
    own folder — the manifest there defines its runner, retriever, and tools.
    `client` is the shared model client used by every provider call; `model_override`
    swaps a manifest's placeholder `mock` model for the live one once a real
    endpoint is configured (this is CHATDEMO_WF_MODEL, the workflow-step model).
    Passing `related` gives the related-questions step its own dedicated runner
    (CHATDEMO_RELATED_MODEL) rather than sharing the step runner; a non-empty
    `wf_api_style` overrides the manifest's `runner.api_style` for every workflow
    uniformly."""

    def __init__(
        self,
        root: Path,
        client: ModelClient,
        model_override: str = "",
        related: ModelSettings | None = None,
        wf_api_style: str = "",
    ) -> None:
        self._root = root
        self._client = client
        self._model_override = model_override
        self._related = related
        self._wf_api_style = wf_api_style

    def discover(self) -> dict[str, Handler]:
        catalog: dict[str, Handler] = {}
        for handler in self._discover_workflows():
            hid = handler.descriptor.id
            if hid in catalog:
                raise RegistryError(f"duplicate handler name: {hid}")
            catalog[hid] = handler
        return catalog

    def _discover_workflows(self) -> list[Handler]:
        out: list[Handler] = []
        wdir = self._root / "workflows"
        if not wdir.exists():
            return out
        for folder in sorted(p for p in wdir.iterdir() if p.is_dir()):
            manifest_path = folder / "workflow.yaml"
            if not manifest_path.exists():
                continue
            m = yaml.safe_load(manifest_path.read_text()) or {}
            self._require(m, ["name", "description", "steps"], manifest_path)
            settings = ModelSettings(**(m.get("runner") or {}))
            model = self._resolve_model(settings.model)
            updates: dict[str, str] = {}
            if model != settings.model:
                updates["model"] = model
            if self._wf_api_style:  # CHATDEMO_WF_API_STYLE, when set, overrides the API style across every workflow
                updates["api_style"] = self._wf_api_style
            if updates:
                settings = settings.model_copy(update=updates)
            desc = Descriptor(
                id=m["name"], name=m["name"], description=m["description"], kind="workflow"
            )
            # The runner and retriever are constructed per-workflow from that
            # workflow's own folder; the related-questions step gets a dedicated
            # runner only if CHATDEMO_RELATED_MODEL is set — otherwise it piggybacks
            # on the same runner used for regular steps
            runner = build_runner(settings, self._client)
            related_runner = build_runner(self._related, self._client) if self._related else runner
            deps = HandlerDeps(
                runner=runner,
                retriever=_load_retriever(folder, m.get("retriever")),
                related_runner=related_runner,
                handler_id=m["name"],
            )
            # Action tools declared by the workflow get registered as functions scoped to this owner
            if m.get("tools"):
                deps.tools = load_tools(self._root, folder, list(m["tools"]))
            steps = [self._parse_step(st, folder, manifest_path) for st in m["steps"]]
            out.append(WorkflowHandler(desc, steps, m.get("output") or {}, deps))
        return out

    def _resolve_model(self, raw: str) -> str:
        """Work out the actual model id for a workflow runner. A `${VAR}` reference
        is expanded from the environment, letting a workflow name its own env var;
        if the result is `mock` (or the referenced `${VAR}` was never set), it falls
        back to CHATDEMO_MODEL via `model_override`; any literal id passes through unchanged."""
        v = (raw or "").strip()
        if v.startswith("${") and v.endswith("}"):
            v = os.environ.get(v[2:-1], "").strip()  # missing var resolves to an empty string
        if not v or v == "mock":
            return self._model_override or "mock"
        return v

    @staticmethod
    def _parse_step(st: dict, folder: Path, where: Path) -> StepSpec:
        if "id" not in st:
            raise RegistryError(f"{where}: every step needs an `id` (got {st})")
        sid = st["id"]
        fn = _load_step(folder / "steps" / f"{st.get('fn', sid)}.py")
        return StepSpec(
            id=sid,
            fn=fn,
            inputs=st.get("in") or {},
            out=st.get("out"),
            when=st.get("when"),
            after=list(st.get("after") or []),
            retry=int(st.get("retry", 0)),
        )

    @staticmethod
    def _require(obj: dict, keys: list[str], where: Path) -> None:
        missing = [k for k in keys if k not in obj]
        if missing:
            raise RegistryError(f"{where}: missing required field(s): {', '.join(missing)}")


@dataclass
class SkillMeta:
    name: str
    description: str
    instructions: str  # SKILL.md body
    references: dict[str, Path]
    folder: Path
    tools: dict[str, LoadedTool] = field(default_factory=dict)  # tools this skill owns directly (D4)
    web_search: bool = False  # setting `web_search: true` in frontmatter enables the Responses hosted-search tool
    scripts: dict[str, Path] = field(default_factory=dict)  # files under scripts/, each exposed via the run_script tool
    source: str = ""  # contents of an optional source.md sidecar, tacked onto every answer as a provenance footer


def list_skills(root: Path) -> list[SkillMeta]:
    """Scan skills/*/SKILL.md and produce the metadata the orchestration runtime
    turns into registered functions.

    A skill lists the tools it wants via a frontmatter `tools:` entry — names are
    looked up first in the skill's own local tools/ folder, then in the shared
    top-level tools/ if not found there. Whether and when to call a tool is up to
    the skill's own model; anything with side effects gets held as a PendingAction
    and only proceeds once explicitly confirmed."""
    out: list[SkillMeta] = []
    sdir = root / "skills"
    if not sdir.exists():
        return out
    for folder in sorted(p for p in sdir.iterdir() if p.is_dir()):
        skill_md = folder / "SKILL.md"
        if not skill_md.exists():
            continue
        fm, body = _parse_frontmatter(skill_md.read_text())
        if "name" not in fm or "description" not in fm:
            raise RegistryError(f"{skill_md}: missing required field(s): name/description")
        # References are strictly READ-ONLY context surfaced via read_reference. Any
        # file extension is accepted here, not just *.md, so a guidance-only import
        # can include a skill's script text as readable material even without an
        # executable scripts/ folder — the model can read the real script to give
        # the user accurate commands, but has no way to actually execute it.
        refs = {p.name: p for p in (folder / "references").glob("*") if p.is_file()}
        tools = load_tools(root, folder, list(fm.get("tools") or []))
        scripts = {p.name: p for p in (folder / "scripts").glob("*") if p.is_file()}
        # source.md is an optional sidecar file, deliberately separate from SKILL.md
        # so that imported skills can remain byte-for-byte unchanged; if present,
        # its raw text is appended to every answer as a provenance footer.
        # In-house skills that don't ship a source.md are unaffected.
        source_md = folder / "source.md"
        source = source_md.read_text().strip() if source_md.exists() else ""
        out.append(
            SkillMeta(
                fm["name"], fm["description"], body, refs, folder,
                tools, bool(fm.get("web_search", False)), scripts, source,
            )
        )
    return out
