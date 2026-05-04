"""Feature engineering, labels, validation, and selection utilities."""

from .builders import build_feature_panel
from .labels import build_label_panel
from .microstructure import build_microstructure_feature_panel
from .registry import (
    build_default_feature_registry,
    build_default_label_registry,
    build_microstructure_feature_registry,
    label_registry_to_dict,
    registry_to_dict,
)
from .selection import compute_ic_summary
from .validation import validate_feature_label_panel

__all__ = [
    "build_feature_panel",
    "build_label_panel",
    "build_microstructure_feature_panel",
    "build_default_feature_registry",
    "build_default_label_registry",
    "build_microstructure_feature_registry",
    "compute_ic_summary",
    "label_registry_to_dict",
    "registry_to_dict",
    "validate_feature_label_panel",
]
