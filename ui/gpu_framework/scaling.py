"""One logical-to-region-pixel scale for GPU drawing, fonts and input."""
import math


def ui_scale(preferences):
    # Blender computes this from OS DPI and the user's display scale.
    # pixel_size is not another DPI multiplier; view.ui_scale is already included.
    value = float(preferences.system.ui_scale)
    return value if math.isfinite(value) and value > 0 else 1.0


def viewport_scale(preferences, height, *, adaptive=True, multiplier=1.0):
    """Optional density adjustment, separate from Blender's DPI conversion."""
    native = ui_scale(preferences)
    # Short views get denser, but not below 80 %: further down, text stops being readable.
    density = min(1.0, max(.8, height / native / 720.0)) if adaptive else 1.0
    return native * density * multiplier


def sidebar_tab_width(preferences):
    """Region pixels taken by the sidebar's category tabs: one Blender widget unit."""
    pixel = getattr(preferences.system, 'pixel_size', 1.0)
    pixel = pixel if isinstance(pixel, (int, float)) and math.isfinite(pixel) and pixel > 0 else 1.0
    return float(round(18.0 * ui_scale(preferences)) + 2 * pixel)
