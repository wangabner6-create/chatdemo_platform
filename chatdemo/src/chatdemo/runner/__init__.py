"""Adapters that abstract how a model call is made (axis 1: completion vs response style, within a single provider)."""

from .base import ModelClient, ModelRunner, ModelSettings, Runner, RunResult
from .mock import MockModelClient

__all__ = [
    "MockModelClient",
    "ModelClient",
    "ModelRunner",
    "ModelSettings",
    "RunResult",
    "Runner",
    "build_runner",
]


def build_runner(settings: "ModelSettings", client: "ModelClient") -> "Runner":
    """Runner is a single class whose behavior branches on api_style (per design D9)."""
    return ModelRunner(client, settings)
