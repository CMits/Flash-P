"""
Schema for pipeline provenance manifest.

Records what was done, when, by which model, and what was produced.
One manifest per network, updated after each pipeline step.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .common import FlashPMetadata


class FileRecord(BaseModel):
    """Record of a produced file."""
    path: str = Field(description="Relative path from network root")
    checksum_sha256: str = Field(default="", description="SHA-256 hash of file contents")
    size_bytes: int = 0
    created: str = Field(default="", description="ISO timestamp")


class StepRecord(BaseModel):
    """Record of one pipeline step execution."""
    step: int = Field(description="Pipeline step number 1-5")
    step_name: str = Field(description="Literature Review, Builder, Perturbation, Validator, Refinement")
    started: str = Field(default="", description="ISO timestamp")
    completed: str = Field(default="", description="ISO timestamp")
    model: str = Field(default="", description="AI model used, e.g. 'claude-opus-4-6'")
    model_version: str = Field(default="", description="Specific model version/ID")
    flash_p_version: str = Field(default="1.0")
    inputs: List[FileRecord] = Field(default_factory=list)
    outputs: List[FileRecord] = Field(default_factory=list)
    notes: str = ""


class PipelineManifest(BaseModel):
    """Complete provenance record for one network.

    NOTE on the two "version" fields here -- they are NOT the same concept:
      * metadata.flash_p_version  -> the Flash-P pipeline RELEASE that produced
        this manifest (git-tag-derived; see flashp_version.get_version()).
      * manifest_schema_version   -> the SHAPE/format of this JSON document
        itself, bumped only if PipelineManifest's fields change. Unrelated to
        the pipeline release number.
    """
    metadata: FlashPMetadata
    manifest_schema_version: str = "1"
    network_directory: str = ""
    steps: List[StepRecord] = Field(default_factory=list)
    current_step: int = Field(
        default=0,
        description="Last completed step (0 = not started)"
    )
