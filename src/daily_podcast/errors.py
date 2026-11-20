"""Failures the pipeline expects and can explain; the CLI prints them as one clean line."""

from __future__ import annotations


class PodcastError(RuntimeError):
    """Base class for every expected failure (configuration, gateway, voice, mastering…)."""


class CheckpointError(PodcastError):
    """A checkpoint on disk cannot be read, usually because it is corrupt or outdated."""
