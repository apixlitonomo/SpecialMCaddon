"""MCblend-inspired Bedrock import pipeline.

The bpy-dependent entrypoints are imported lazily so the pure modules
(uv, render_controller) stay unit-testable outside Blender.
"""

try:
    from .animation import (
        apply_bedrock_animations,
        apply_evaluated_animation_tracks,
    )
    from .geometry import build_bedrock_model
    from .importer import build_bedrock_model_from_file, import_client_entity

    __all__ = [
        "apply_bedrock_animations",
        "apply_evaluated_animation_tracks",
        "build_bedrock_model",
        "build_bedrock_model_from_file",
        "import_client_entity",
    ]
except ModuleNotFoundError:  # pragma: no cover - plain Python test path (no bpy)
    __all__ = []
