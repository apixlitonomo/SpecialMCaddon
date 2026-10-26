"""Read Blender preferences without changing the user's theme."""
from dataclasses import replace
from .core import Theme
from .motion import blend

# Retained for later; shipped UI uses the original fixed dark appearance.
THEME_SETTINGS_ENABLED = False


def rgba(owner, name, fallback):
    value = getattr(owner, name, None)
    if value is None:
        return fallback
    values = tuple(value)
    return values[:3] + (values[3] if len(values) > 3 else 1.0,)


def resolve_theme(preferences, mode='BLENDER'):
    # The sidebar sits one step lighter than the World Exporter ground, on the
    # #1C1C1C panel grey MiEx uses for its sidebar, so it reads closer to Blender.
    # Controls default to 32 px so the sidebar stays on its 4 px grid.
    base = replace(Theme(), radius=4, control_height=32, text_muted=(.6,.6,.6,1),
                   canvas=_hex('1C1C1C'), surface=_hex('242424'), surface_raised=_hex('2A2A2A'),
                   surface_hover=_hex('2A2A2A'), surface_pressed=_hex('202020'),
                   input_surface=_hex('242424'), menu_surface=_hex('2A2A2A'),
                   border=_hex('303030'),
                   accent_hover=(133/255,180/255,1,1),
                   accent_pressed=(57/255,118/255,221/255,1))
    if not THEME_SETTINGS_ENABLED or mode == 'ORIGINAL_DARK' or not getattr(preferences, 'themes', None):
        return base
    source = preferences.themes[0]
    ui = source.user_interface
    regular = ui.wcol_regular
    tool = getattr(ui, 'wcol_tool', regular)
    space = source.view_3d.space
    # Blender 5.2 moved sidebar colors out of the individual editor spaces.
    sidebars = getattr(getattr(source, 'regions', None), 'sidebars', None)
    canvas = rgba(sidebars, 'back', rgba(space, 'button', rgba(space, 'back', base.canvas)))
    canvas = canvas[:3] + (1.0,)
    text = rgba(ui, 'panel_text', rgba(space, 'button_text', rgba(regular, 'text', base.text)))
    surface = rgba(regular, 'inner', base.surface)
    accent = rgba(tool, 'inner_sel', base.accent)
    selected_text = rgba(tool, 'text_sel', base.text)
    field = getattr(ui, 'wcol_text', regular)
    menu = getattr(ui, 'wcol_menu', regular)
    return replace(base, follows_blender=True, canvas=canvas, surface=surface,
        control_text=rgba(tool, 'text', text),
        input_surface=rgba(field, 'inner', surface), input_text=rgba(field, 'text', text),
        menu_surface=rgba(menu, 'inner', surface), menu_text=rgba(menu, 'text', text),
        surface_raised=rgba(tool, 'inner', surface),
        surface_hover=blend(surface, text, .10), surface_pressed=blend(surface, text, .17),
        border=rgba(regular, 'outline', base.border), text=text,
        text_muted=blend(text, canvas, .25), accent=accent, selected=accent,
        selected_text=selected_text, accent_hover=blend(accent, selected_text, .12),
        accent_pressed=blend(accent, canvas, .15),
        radius=4 * getattr(regular, 'roundness', 1.0))


def _hex(value, alpha=1.0):
    return tuple(int(value[i:i+2], 16)/255 for i in (0, 2, 4)) + (alpha,)


def miex_theme():
    """World Exporter palette: the same Sparkle tokens as the sidebar and RP Manager.

    Ground #141414, recessed tracks #1C1C1C, raised controls #242424, #282828
    hairlines and Sparkle Blue for the selection, the open step and the one
    primary action (DESIGN.md).
    """
    return replace(resolve_theme(None), radius=4, control_height=34,
        canvas=_hex('141414'), surface=_hex('1C1C1C'), surface_raised=_hex('242424'),
        surface_hover=_hex('242424'), surface_pressed=_hex('181818'),
        input_surface=_hex('242424'), input_text=_hex('F2F2F2'),
        menu_surface=_hex('242424'), menu_text=_hex('F2F2F2'), control_text=_hex('F2F2F2'),
        border=_hex('282828'), text=_hex('F2F2F2'), text_muted=_hex('999999'),
        accent=_hex('5294FF'), accent_hover=_hex('85B4FF'), accent_pressed=_hex('3976DD'),
        selected=_hex('202C40'), selected_text=_hex('F2F2F2'))
