"""Small declarative GPU UI framework for Blender add-ons.

The package intentionally has no dependency on Sparkle business
logic.  Add-ons provide a callable which builds a widget tree; the runtime
owns Blender draw handlers, input routing, and retained interaction state.
"""

from .core import (
    ActionRow,
    Button,
    Column,
    Divider,
    EnumButton,
    Image,
    Label,
    NumberStepper,
    Panel,
    Rect,
    Row,
    ScrollView,
    Size,
    Spacer,
    Style,
    TextField,
    Theme,
    Toggle,
    Widget,
)

__all__ = [
    "ActionRow",
    "Button",
    "Column",
    "Divider",
    "EnumButton",
    "Image",
    "Label",
    "NumberStepper",
    "Panel",
    "Rect",
    "Row",
    "ScrollView",
    "Size",
    "Spacer",
    "Style",
    "TextField",
    "Theme",
    "Toggle",
    "Widget",
]
