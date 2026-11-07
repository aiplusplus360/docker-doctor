"""Audit a Dockerfile for production hazards."""

__version__ = "0.1.0"

from .core import (
    SEVERITIES,
    Finding,
    Instruction,
    Stage,
    lint,
    main_stage,
    parse_dockerfile,
    severity_at_least,
)

__all__ = [
    "SEVERITIES",
    "Finding",
    "Instruction",
    "Stage",
    "lint",
    "main_stage",
    "parse_dockerfile",
    "severity_at_least",
]
