"""The mod import panel: a mod JAR's models in a thumbnail grid over the 3D viewport.

Dropping a ``.jar`` on the 3D viewport, or File > Import > Minecraft Mod
Model, opens it over that viewport. It is there only while importing: Import,
a double click, its close button or Esc closes it. It is the Figma "Import
panel — Alex's Mobs" (ORB file, Blender Addon page) made compact, on an 8 px
grid:

- 16 px padding, 8 px between sections;
- one 32 px top row: the mod's own logo, the add-on's search bar (category
  dropdown + search field, "Search <mod>…") and close. The category starts at
  All: mobs, then models, blocks and items;
- cards 112 x 120: a 96 px tile, 8 px, a 16 px name; 8 px gaps, so columns
  step 120 px and rows 128 px. Two and a half rows show, the last one fading;
- Default texture across all columns but the last, Import under the last.

It sizes itself to the viewport, in Sparkle's interface scale: whole columns
(504, 384 or 264 px for 4, 3 or 2; a category with fewer cards takes fewer,
three at least) within 40 % of the viewport's width and down to one and a
half rows within 60 % of its height. It centres in the part
of the viewport the toolbar, sidebar and header leave free; dragging its
header moves it there, and it opens where it was left.
"""

import math
import os
import time
import traceback

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty

from .gpu_framework.core import Button, Rect, Row, ScrollView, Size, Style, VirtualGrid, Widget
from .gpu_framework.motion import tween
from .gpu_framework.navigation import Application, Dropdown
from .gpu_framework.renderer import BlenderRenderer
from .gpu_framework.scaling import viewport_scale
from .gpu_framework.theme_adapter import resolve_theme
from .make_panels import ICONS, AssetFilterDropdown, IconButton, search_row
from . import mod_thumbnails

GRID = 8
PADDING = 2 * GRID
SECTION_GAP = GRID
CONTROL_HEIGHT = 4 * GRID
TILE_HEIGHT = 12 * GRID
LABEL_HEIGHT = 2 * GRID
CARD_WIDTH = 14 * GRID
CARD_HEIGHT = TILE_HEIGHT + GRID + LABEL_HEIGHT
GAP = GRID
PITCH = CARD_WIDTH + GAP  # columns
ROW_PITCH = CARD_HEIGHT + GAP
CATEGORY_WIDTH = 11 * GRID
FEW_COLUMNS = 3  # a category with fewer cards still gets this many columns
# Everything but the grid: padding, the top row, the actions and the gaps between.
CHROME_HEIGHT = 2 * PADDING + 2 * CONTROL_HEIGHT + 2 * SECTION_GAP
MAX_ROWS, MIN_ROWS = 2.5, 1.5
WIDTH_SHARE, HEIGHT_SHARE = 0.4, 0.6
FADE_HEIGHT = 6 * GRID
KINDS = (("all", "All"), ("entity", "Mobs"), ("model", "Models"), ("block", "Blocks"), ("item", "Items"))
KIND_ORDER = {kind: index for index, (kind, _label) in enumerate(KINDS)}
# All shows a name two kinds share with its kind, like the dialog: "Critter Nest (Block)".
KIND_NAMES = {"entity": "Mob", "model": "Model", "block": "Block", "item": "Item"}
ORDER_LIMIT = 240  # thumbnails asked for at once: the cards on screen and about 60 rows on
SEARCH = "mod.search"
_POINTER_EVENTS = {
    "LEFTMOUSE", "MIDDLEMOUSE", "RIGHTMOUSE", "BUTTON4MOUSE", "BUTTON5MOUSE", "BUTTON6MOUSE", "BUTTON7MOUSE",
    "MOUSEMOVE", "INBETWEEN_MOUSEMOVE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "WHEELINMOUSE", "WHEELOUTMOUSE",
    "TRACKPADPAN", "TRACKPADZOOM", "MOUSEROTATE", "MOUSESMARTZOOM",
}
_MENU_KEYS = {"ESC", "UP_ARROW", "DOWN_ARROW", "HOME", "END", "RET", "NUMPAD_ENTER"}
TOOLTIP_DELAY = 1.0
NARROW_WIDTH = 300  # two columns: the logo makes room for the search field

_PANEL = {"session": None, "offset": (0.0, 0.0)}  # where it was dragged, from the centre


# -- size --------------------------------------------------------------------------

def panel_width(columns):
    """Panel width for ``columns`` cards: 504, 384 and 264 px for 4, 3 and 2."""
    return 2 * PADDING + columns * PITCH - GAP


def grid_height(rows):
    """The grid's height for ``rows`` rows, down to the 8 px grid: 320 for 2.5."""
    return math.floor(rows * ROW_PITCH / GRID) * GRID


def fit(view_width, view_height):
    """``(columns, grid height)`` that fit a free viewport area (logical px)."""
    columns = next((count for count in (4, 3, 2) if panel_width(count) <= view_width * WIDTH_SHARE), 2)
    room = (view_height * HEIGHT_SHARE - CHROME_HEIGHT) // GRID * GRID
    return columns, max(grid_height(MIN_ROWS), min(grid_height(MAX_ROWS), room))


def content_height(count, columns):
    """Height of ``count`` cards in ``columns`` columns (one row at least)."""
    rows = max(1, -(-count // columns))
    return rows * CARD_HEIGHT + (rows - 1) * GAP


def columns_for(view_width, count):
    """The columns that fit the viewport, and no more than the cards need (three at least)."""
    return min(fit(view_width, 0)[0], max(FEW_COLUMNS, count))


def panel_rect(view, count, offset=(0.0, 0.0)):
    """The panel's rect in a free viewport area for a category of ``count`` cards.

    Its top edge stays where a full panel's would be, so switching to a
    category with fewer cards shortens it without moving the header.
    ``offset`` moves it from the centre (dragging), always inside ``view``.
    """
    cap = fit(view.width, view.height)[1]
    columns = columns_for(view.width, count)
    width = panel_width(columns)
    height = CHROME_HEIGHT + min(cap, content_height(count, columns))
    x = view.x + (view.width - width) / 2 + offset[0]
    top = view.y + (view.height + CHROME_HEIGHT + cap) / 2 + offset[1]
    x = max(view.x, min(x, view.right - width))
    top = min(view.top, max(top, view.y + height))
    return Rect(round(x), round(top) - height, width, height)


def _kinds(catalog):
    present = {asset.kind for asset in catalog.assets} if catalog else set()
    return [(kind, label) for kind, label in KINDS if kind in present or (kind == "all" and present)]


def _pointer(value):
    try:
        return int(value.as_pointer())
    except (AttributeError, ReferenceError):
        return 0


# -- widgets ---------------------------------------------------------------------

class ModIcon(Widget):
    """The mod's own logo (else its first model's thumbnail) on a 32 px tile."""

    def __init__(self, jar, first_key, name):
        super().__init__("mod.icon", style=Style(width=CONTROL_HEIGHT, height=CONTROL_HEIGHT), tooltip=name)
        self.jar, self.first_key = jar, first_key

    def preferred_size(self, available, state, theme):
        return Size(CONTROL_HEIGHT, CONTROL_HEIGHT)

    def draw(self, renderer, state, theme):
        renderer.rect(self.rect, theme.surface, radius=4)
        path = mod_thumbnails.mod_icon(self.jar) or (
            mod_thumbnails.thumbnail(self.jar, self.first_key) if self.first_key else "")
        if path:
            renderer.image(self.rect.inset((2, 2, 2, 2)), path, radius=2, fit=True, nearest=True)
        else:
            renderer.image(Rect(self.rect.x + 8, self.rect.y + 8, 16, 16), str(ICONS / "cube.png"))


class CloseButton(IconButton):
    """A bare × at the end of the top row (its 32 px square is the hit area only)."""

    def draw(self, renderer, state, theme):
        r = self.rect
        renderer.image(Rect(r.x + 10, r.y + 10, 12, 12), str(ICONS / "close.png"))


class KindDropdown(AssetFilterDropdown):
    """The search bar's category segment, one grid-wide width for every label."""

    compact = False

    def preferred_size(self, available, state, theme):
        # Two columns leave the search field room for a few letters.
        return Size(10 * GRID if self.compact else CATEGORY_WIDTH, CONTROL_HEIGHT)


class TextureDropdown(Dropdown):
    def draw(self, renderer, state, theme):
        r = self.rect
        open_menu = state.get("_menu", {}).get("key") == self.key
        hovered = state.get("_runtime_hover") == self.key and self.style.enabled
        renderer.rect(r, theme.surface_hover if open_menu or hovered else theme.surface_raised, radius=4)
        label = next((name for value, name in self.choices if value == self.getter(state.get("_runtime_context"))),
                     self.choices[0][1] if self.choices else "")
        color = theme.text if self.style.enabled else (*theme.text_muted[:3], 0.45)
        renderer.text(Rect(r.x + 12, r.y, max(0, r.width - 40), r.height), label, color, size=12, weight="Medium")
        renderer.image(Rect(r.right - 20, r.y + (r.height - 10) / 2, 10, 10), str(ICONS / "chevron.png"))


class ImportButton(Button):
    """Sparkle Blue while a card is picked, 12 px like the panel's other labels."""

    def draw(self, renderer, state, theme):
        if not self.style.enabled:
            background, color = theme.surface, (*theme.text_muted[:3], 0.45)
        elif state.get("_runtime_pressed") == self.key:
            background, color = theme.accent_pressed, theme.selected_text
        elif state.get("_runtime_hover") == self.key:
            background, color = theme.accent_hover, theme.selected_text
        else:
            background, color = theme.accent, theme.selected_text
        renderer.rect(self.rect, background, radius=4)
        renderer.text(self.rect, self.text, color, size=12, weight="Medium", align="CENTER", cap_center=True)


class ModCard(Button):
    def __init__(self, panel, asset, index, label):
        self.jar, self.asset = panel.jar, asset
        state = panel.state

        def select(context):
            state["mod.selection"] = None if state.get("mod.selection") == asset.key else asset.key
            state.pop("mod.texture", None)

        super().__init__(f"mod.card.{index}", label, select, style=Style(), tooltip=label)
        self.double_action = lambda context: panel.import_asset(context, asset)
        self.detect_double_press = True
        self.double_click_identity = asset.key

    def draw(self, renderer, state, theme):
        r = self.rect
        tile = Rect(r.x, r.top - TILE_HEIGHT, r.width, TILE_HEIGHT)
        if state.get("mod.selection") == self.asset.key:
            color = theme.accent
        else:
            color = theme.surface_raised if state.get("_runtime_hover") == self.key else theme.surface
        renderer.rect(tile, color, radius=4)
        path = mod_thumbnails.thumbnail(self.jar, self.asset.key)
        if path:
            renderer.image(tile.inset((GRID, GRID, GRID, GRID)), path, fit=True)
        elif not mod_thumbnails.failed(self.jar, self.asset.key):
            renderer.image(Rect(tile.x + (tile.width - 16) / 2, tile.y + (tile.height - 16) / 2, 16, 16),
                           str(ICONS / "cube.png"))
        width = max(0, r.width - 4)
        label = self.text
        if label.endswith(")") and " (" in label and renderer.text_width(label, 12, "Medium") > width:
            # Shorten the name, never "(Block)"/"(Item)": it tells same-named cards apart.
            name, kind = label.rsplit(" (", 1)
            room = width - renderer.text_width(" (" + kind, 12, "Medium")
            label = (renderer._truncate(name, room, 12, "Medium") if room > 12 else "") + " (" + kind
        renderer.text(Rect(r.x + 2, r.y, width, LABEL_HEIGHT), label, theme.text, size=12, weight="Medium")


class ModGrid(VirtualGrid):
    """Cards on the 144 px pitch. Thumbnails render in the order the eye
    meets them: the cards on screen, then the rows below, then above."""

    def __init__(self, panel, assets, labels):
        super().__init__("mod.grid", assets, None, row_height=CARD_HEIGHT, gap=GAP)
        self.jar = panel.jar
        indices = {id(asset): index for index, asset in enumerate(assets)}
        self.factory = lambda asset: ModCard(panel, asset, indices[id(asset)], labels.get(asset.key, asset.name))

    def _metrics(self, width):
        columns = max(1, int((width + GAP) // PITCH))
        card_width = max(0.0, (width - (columns - 1) * GAP) / columns)
        rows = (len(self.items) + columns - 1) // columns
        return columns, card_width, CARD_HEIGHT, rows

    def layout(self, bounds, state, theme):
        super().layout(bounds, state, theme)
        viewport = self.ancestor(ScrollView)
        if viewport is None:
            return
        shown = sorted(index for index, card in self._cards.items()
                       if card.rect.intersect(viewport.rect).height > 0)
        if not shown:
            return
        order = shown + list(range(shown[-1] + 1, len(self.items))) + list(range(shown[0] - 1, -1, -1))
        mod_thumbnails.request(self.jar, [self.items[index].key for index in order[:ORDER_LIMIT]])


class ModGridScroll(ScrollView):
    """The grid's window. More below fades out; the scrollbar sits in the padding."""

    def __init__(self, grid, height):
        super().__init__("mod.grid.scroll", grid, style=Style(height=height), wheel_step=ROW_PITCH / 2)
        self.external_scrollbar = True

    def scrollbar_track(self):
        return Rect(self.rect.right + 6, self.rect.y, 4, self.rect.height)

    def draw_scrollbar(self, renderer, state):
        # The sidebar's grey thumb nearly vanished on this panel's ground.
        thumb = self.scrollbar_thumb(state)
        if thumb:
            shade = 110 / 255 if state.get("_scroll_drag", (None,))[0] == self.key else 80 / 255
            renderer.rect(thumb, (shade, shade, shade, 1), radius=2)

    def draw(self, renderer, state, theme):
        super().draw(renderer, state, theme)
        scroll = state.get(self.key, {}).get("scroll", 0.0)
        if self.content_height - self.rect.height - scroll > 1:
            self._fade(renderer, theme)
        self.draw_scrollbar(renderer, state)
        if mod_thumbnails.gave_up():
            # Otherwise the only word of it is in the console.
            strip = Rect(self.rect.x, self.rect.y, self.rect.width, LABEL_HEIGHT + GRID)
            renderer.rect(strip, theme.canvas)
            renderer.text(strip, "Thumbnails could not render; see the console", theme.text_muted,
                          size=11, align="CENTER", cap_center=True)

    def _fade(self, renderer, theme):
        """The design's progressive blur, as a fade into the panel that is even to the eye.

        Layers stack up from the bottom edge (no seams between bands), each
        reaching a little higher; together they follow a smoothstep. The
        viewport blends in linear light, so the opacity is spread for that.
        They also cover a few pixels past the edge, where clipping rounds to
        whole pixels and would leave one row of the cut-off cards showing.
        """
        r, g, b = theme.canvas[:3]
        layers, covered = 16, 0.0
        for layer in range(layers):
            t = (layers - layer) / layers  # the layer's top, as a share of the fade
            reach = (layer + 1) / layers  # how far into the fade this layer starts to show
            target = reach * reach * (3 - 2 * reach)
            if renderer.linear_output:
                target = 1 - (1 - target) ** 2.2
            alpha = 1.0 if target >= 1 else 1 - (1 - target) / (1 - covered)
            covered = target
            renderer.rect(Rect(self.rect.x, self.rect.y - 3, self.rect.width, FADE_HEIGHT * t + 3),
                          (r, g, b, alpha))


class EmptyGrid(Widget):
    def __init__(self, text, height):
        super().__init__("mod.empty", style=Style(height=height))
        self.text = text

    def draw(self, renderer, state, theme):
        renderer.text(self.rect, self.text, theme.text_muted, size=12, align="CENTER")


class PanelRoot(Application):
    """Menus stay inside the panel: under their dropdown, or above it at the bottom."""

    def layout(self, bounds, state, theme):
        self.overlay_bounds = bounds
        super().layout(bounds, state, theme)
        if self.menu is None:
            return
        source = self.menu_source.rect
        if self.menu_rect.top > source.y:  # no room below: open above the dropdown
            self.overlay_bounds = Rect(bounds.x, source.top - 2, bounds.width, bounds.top - source.top + 2)
            super().layout(bounds, state, theme)
            self.overlay_bounds = bounds
            if self.menu is None:
                return
        menu = self.menu_rect
        x = max(bounds.x + 6, min(source.x, bounds.right - menu.width - 6))
        if x != menu.x:
            self.menu_rect = Rect(x, menu.y, menu.width, menu.height)
            self.menu.layout(self.menu_rect, state, theme)


# -- the panel ---------------------------------------------------------------------

class ModImportPanel:
    """One open panel: its mod, its widgets' state and the viewport it floats over."""

    def __init__(self, operator, window, area, jar):
        self.operator = operator
        self.window, self.area = window, area
        self.region = next(region for region in area.regions if region.type == "WINDOW")
        self.state = {}
        self.tree = None
        self.renderer = None
        self.rect = Rect(0, 0, 0, 0)
        self.scale = 1.0
        self.signature = None
        self.dirty = True
        self.failed = False
        self.closed = False
        self._ticking = False
        self._search = ""
        self._listed = {}  # (category, search) -> the cards, in order
        self._view = Rect(0, 0, 0, 0)
        self._drag = None  # (mouse x, mouse y, offset) when the header was grabbed
        self._cursor = ""
        self.show(jar)
        self.handler = bpy.types.SpaceView3D.draw_handler_add(self._draw, (), "WINDOW", "POST_PIXEL")

    def show(self, jar):
        """Show ``jar``'s models, from the first category and the top."""
        from ..importers.mod_jar import mod_catalog

        self.jar = jar
        self.catalog = mod_catalog(jar)
        self._listed = {}
        kinds = _kinds(self.catalog)
        self.kind = kinds[0][0] if kinds else "all"
        self._search = ""
        for key in ("mod.selection", "mod.texture", "mod.grid.scroll", "_menu", "_runtime_focus",
                    "_runtime_pressed", "_scroll_drag"):
            self.state.pop(key, None)
        self.dirty = True
        self.tag_redraw()

    # The search field reads and writes this, like a property.
    @property
    def search(self):
        return self._search

    @search.setter
    def search(self, value):
        if value != self._search:
            self._search = value
            self.state.pop("mod.grid.scroll", None)
            self.dirty = True

    def _assets(self, filtered=True):
        """The category's cards (All: mobs, then models, blocks and items), searched or not."""
        key = (self.kind, self._search if filtered else None)
        listed = self._listed.get(key)
        if listed is None:
            words = self._search.lower().split() if filtered else ()
            listed = [asset for asset in self.catalog.assets
                      if self.kind in ("all", asset.kind) and all(word in asset.name.lower() for word in words)]
            if self.kind == "all":
                listed.sort(key=lambda asset: KIND_ORDER.get(asset.kind, len(KINDS)))
            if len(self._listed) > 32:
                self._listed.clear()
            self._listed[key] = listed
        return listed

    def _labels(self):
        """All: a name two kinds share gets its kind, as in the dialog."""
        if self.kind != "all":
            return {}
        everything = self._assets(filtered=False)
        counts = {}
        for asset in everything:
            counts[asset.name] = counts.get(asset.name, 0) + 1
        return {asset.key: f"{asset.name} ({KIND_NAMES.get(asset.kind, asset.kind)})"
                for asset in everything if counts[asset.name] > 1}

    def selected(self):
        key = self.state.get("mod.selection")
        return next((asset for asset in self._assets() if asset.key == key), None) if key else None

    # -- building and drawing --

    def _build(self, theme, grid):
        state = self.state
        kinds = _kinds(self.catalog)
        label = dict(KINDS).get(self.kind, "Models").lower()
        assets = self._assets()
        first = next((asset.key for asset in self._assets(filtered=False)), "")

        def set_kind(context, value):
            if value != self.kind:
                self.kind = value
                for key in ("mod.selection", "mod.texture", "mod.grid.scroll"):
                    state.pop(key, None)

        category = KindDropdown("mod.kind", lambda context: self.kind, set_kind, kinds,
                                style=Style(height=CONTROL_HEIGHT, font_size=12))
        narrow = self.rect.width < NARROW_WIDTH
        category.compact = narrow
        # The add-on's own search bar (the inner bar of the Assets page's search
        # row), between the mod's logo and close: the mod's name is its placeholder.
        bar = search_row(SEARCH, self, "search", f"Search {self.catalog.name}…", category).children[0]
        header = Row("mod.header", [
            *([] if narrow else [ModIcon(self.jar, first, self.catalog.name)]),
            bar,
            CloseButton("mod.close", "close", lambda context: self.close(),
                        style=Style(width=CONTROL_HEIGHT, height=CONTROL_HEIGHT), tooltip="Close"),
        ], style=Style(height=CONTROL_HEIGHT, gap=GAP))

        selected = self.selected()
        if selected is None:
            state.pop("mod.selection", None)
        choices = [("", "Default texture")]
        if selected is not None and selected.kind in ("entity", "model"):
            choices += [(texture, texture.rsplit("/", 1)[-1].rsplit(".", 1)[0]) for texture in selected.textures]
        texture = TextureDropdown("mod.texture", lambda context: state.get("mod.texture") or "",
                                  lambda context, value: state.__setitem__("mod.texture", value), choices,
                                  style=Style(height=CONTROL_HEIGHT, flex=1, enabled=len(choices) > 1))
        # Import sits under the last column, the texture menu across the others.
        import_button = ImportButton(
            "mod.import", "Import",
            (lambda context: self.import_asset(context, selected)) if selected is not None else (lambda context: None),
            style=Style(width=CARD_WIDTH, height=CONTROL_HEIGHT, enabled=selected is not None))
        actions = Row("mod.actions", [texture, import_button], style=Style(height=CONTROL_HEIGHT, gap=GAP))
        if assets:
            body = ModGridScroll(ModGrid(self, assets, self._labels()), grid)
        elif self.kind == "all":
            body = EmptyGrid(f"Nothing matches “{self._search.strip()}”", grid)
        else:
            body = EmptyGrid(f"No {label} match “{self._search.strip()}”", grid)
        return PanelRoot("mod.panel", [header, body, actions], style=Style(
            padding=(PADDING, PADDING, PADDING, PADDING), gap=SECTION_GAP,
            background=theme.canvas, border=theme.border, radius=8))

    def _free_area(self):
        """The part of the viewport no toolbar, sidebar or header covers, in logical px."""
        region = self.region
        left, right, bottom, top = 0.0, float(region.width), 0.0, float(region.height)
        for other in self.area.regions:
            if other.type == "WINDOW" or other.width <= 1 or other.height <= 1:
                continue
            # Only regions drawn over the viewport (region overlap) take room from it.
            if not (region.x <= other.x and other.x + other.width <= region.x + region.width
                    and region.y <= other.y and other.y + other.height <= region.y + region.height):
                continue
            x, y = other.x - region.x, other.y - region.y
            if other.height >= other.width:  # toolbar or sidebar
                if x + other.width / 2 < region.width / 2:
                    left = max(left, x + other.width)
                else:
                    right = min(right, x)
            elif y + other.height / 2 > region.height / 2:  # header, tool settings
                top = min(top, y)
            else:  # asset shelf
                bottom = max(bottom, y + other.height)
        scale = self.scale
        return Rect(left / scale, bottom / scale, max(0.0, right - left) / scale, max(0.0, top - bottom) / scale)

    def _draw(self):
        if self.closed or self.failed:
            return
        context = bpy.context
        # Quad View has four WINDOW regions; only the panel's own draws it.
        if _pointer(context.region) != _pointer(self.region):
            return
        try:
            self._paint(context)
        except Exception:
            traceback.print_exc()
            self.failed = True  # the next event closes the panel

    def _paint(self, context):
        from .custom_ui import _addon_preferences

        preferences = _addon_preferences(context)
        theme = resolve_theme(context.preferences, getattr(preferences, "ui_theme", "BLENDER"))
        self.scale = viewport_scale(context.preferences, self.region.height,
                                    adaptive=getattr(preferences, "adaptive_ui_scale", True),
                                    multiplier=getattr(preferences, "custom_ui_scale", 1.0))
        self._view = view = self._free_area()
        self.rect = panel_rect(view, len(self._assets(filtered=False)), _PANEL["offset"])
        grid = self.rect.height - CHROME_HEIGHT
        signature = (theme, self.rect.width, grid)
        if self.tree is None or self.dirty or signature != self.signature:
            self.tree = self._build(theme, grid)
            self.signature, self.dirty = signature, False
        state = self.state
        state["_runtime_context"] = context
        state["_motion_time"] = time.monotonic()
        state["_motion_active"] = False
        if getattr(preferences, "reduce_motion", False):
            state["_motion_disabled"] = True
        else:
            state.pop("_motion_disabled", None)
        self.tree.layout(self.rect, state, theme)
        if self.renderer is None or self.renderer.ui_scale != self.scale:
            self.renderer = BlenderRenderer(context.region, self.scale)
        renderer = self.renderer
        renderer.linear_output = True  # the viewport presents through a display transform
        opening = tween(state, "mod.panel.open", 1.0, 0.16, initial=0.0)
        renderer.opacity = opening
        renderer.offset_y = -8.0 * (1.0 - opening)
        renderer.begin()
        try:
            r = self.rect
            for spread, alpha in ((24, 0.05), (16, 0.07), (8, 0.1), (3, 0.12)):
                renderer.rect(Rect(r.x - spread, r.y - spread - 8, r.width + 2 * spread, r.height + 2 * spread),
                              (0.0, 0.0, 0.0, alpha), radius=8 + spread)
            self.tree.draw(renderer, state, theme)
            self._draw_tooltip(renderer, theme)
        finally:
            renderer.offset_y = 0.0
            renderer.end()
        if state.get("_motion_active"):
            self._animate()

    def _draw_tooltip(self, renderer, theme):
        """The sidebar's tooltip, for card names cut short and the header's buttons."""
        from .gpu_framework.runtime import _wrap_words

        state = self.state
        key = state.get("_runtime_hover")
        if not key or state.get("_menu") or state.get("_runtime_pressed") or self._drag is not None:
            return
        if time.monotonic() - state.get("_hover_since", time.monotonic()) < TOOLTIP_DELAY:
            return
        widget = self._find(key)
        if widget is None or not widget.tooltip:
            return
        x, y = state.get("_runtime_mouse", (0.0, 0.0))
        width = min(260.0, max(90.0, renderer.text_width(widget.tooltip, 11.0, "Medium") + 20.0))
        lines = _wrap_words(renderer, widget.tooltip, width - 16.0, 11.0, "Medium")[:4]
        height = 12.0 + 15.0 * len(lines)
        tip = Rect(min(x + 12.0, self.rect.right - width), max(self.rect.y, y - 6.0 - height), width, height)
        renderer.rect(tip, theme.surface_raised, radius=4.0, border=theme.border)
        for index, line in enumerate(lines):
            renderer.text(Rect(tip.x + 8.0, tip.top - 6.0 - 15.0 * (index + 1), tip.width - 16.0, 15.0),
                          line, theme.text, size=11.0, weight="Medium", cap_center=True)

    def _animate(self):
        if self._ticking:
            return
        self._ticking = True

        def tick():
            if self.closed or not self.state.get("_motion_active"):
                self._ticking = False
                return None
            self.tag_redraw()
            return 1 / 60

        bpy.app.timers.register(tick, first_interval=1 / 60)

    def tag_redraw(self):
        try:
            self.region.tag_redraw()
        except (ReferenceError, AttributeError):
            pass

    def _changed(self):
        self.dirty = True
        self.tag_redraw()

    # -- events --

    def _valid(self):
        try:
            area = next((item for item in self.window.screen.areas if _pointer(item) == _pointer(self.area)), None)
            return (area is not None and area.type == "VIEW_3D"
                    and any(_pointer(item) == _pointer(self.region) for item in area.regions))
        except (ReferenceError, AttributeError):
            return False

    def _rebind(self):
        """Ctrl+Space or another workspace shows the viewport as a new area: follow it."""
        try:
            area = max((item for item in self.window.screen.areas if item.type == "VIEW_3D"),
                       key=lambda item: item.width * item.height, default=None)
        except (ReferenceError, AttributeError):
            return False
        if area is None:
            return False
        self.area = area
        self.region = next(region for region in area.regions if region.type == "WINDOW")
        self.renderer = None
        self._changed()
        return True

    def _find(self, key):
        return next((widget for widget in self.tree.walk() if widget.key == key), None)

    def _mouse(self, event):
        return ((event.mouse_x - self.region.x) / self.scale, (event.mouse_y - self.region.y) / self.scale)

    def _in_area(self, event):
        area = self.area
        return area.x <= event.mouse_x < area.x + area.width and area.y <= event.mouse_y < area.y + area.height

    def modal(self, context, event):
        if self.closed:
            return {"CANCELLED"}
        if self.failed or (not self._valid() and not self._rebind()):
            self.close()
            return {"CANCELLED"}
        tree, state = self.tree, self.state
        if tree is None or event.type in {"TIMER", "TIMER_REPORT", "NONE"}:
            return {"PASS_THROUGH"}
        x, y = self._mouse(event)
        if self._drag is not None:
            self._drag_event(event, x, y)
            return {"RUNNING_MODAL"}
        menu = getattr(tree, "menu", None)
        over = self.rect.contains(x, y) or (menu is not None and tree.menu_rect.contains(x, y))
        # An open menu takes the keys that drive it and clicks; orbiting, the
        # wheel elsewhere and shortcuts still reach Blender.
        if menu is not None and not over and event.type not in _MENU_KEYS and not (
                event.type == "LEFTMOUSE" and event.value == "PRESS"):
            return {"PASS_THROUGH"}

        scroll = self._find("mod.grid.scroll")
        if scroll is not None and (state.get("_scroll_drag") or (
                event.type == "LEFTMOUSE" and event.value == "PRESS" and scroll.scrollbar_contains(x, y))):
            if scroll.pointer_scrollbar(event, x, y, state):
                state.pop("_runtime_pressed", None)
                self.tag_redraw()
                return {"RUNNING_MODAL"}
        if menu is not None and tree.handle_menu_event(context, event, state, x, y):
            self._changed()
            return {"RUNNING_MODAL"}
        if self._text_event(context, event, over):
            return {"RUNNING_MODAL"}
        if event.value == "PRESS" and self._in_area(event):
            # Esc during playback stops the playback, as everywhere in Blender.
            if event.type == "ESC" and (over or not context.screen.is_animation_playing):
                self.close()
                return {"CANCELLED"}
            if event.type in {"RET", "NUMPAD_ENTER"} and self.selected() is not None:
                self.import_asset(context, self.selected())
                return {"FINISHED"} if self.closed else {"RUNNING_MODAL"}
        if over and event.type in _POINTER_EVENTS:
            self._pointer_event(context, event, x, y)
            # No click, drag or wheel over the panel reaches the viewport underneath.
            return {"FINISHED"} if self.closed else {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS" and state.pop("_runtime_focus", None):
            self.tag_redraw()
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            state.pop("_runtime_pressed", None)
        if event.type == "MOUSEMOVE" and not over:
            self._set_cursor("")
            if state.pop("_runtime_hover", None) is not None:
                self.tag_redraw()
        return {"PASS_THROUGH"}  # shortcuts still work with the pointer over the panel

    def _grabs(self, x, y, hit):
        """Over an empty part of the panel (the logo, the padding, the gaps): dragging moves it."""
        return hit is None and self.rect.contains(x, y) and getattr(self.tree, "menu", None) is None

    def _drag_event(self, event, x, y):
        start_x, start_y, offset = self._drag
        if event.type == "MOUSEMOVE":
            wanted = (offset[0] + (event.mouse_x - start_x) / self.scale,
                      offset[1] + (event.mouse_y - start_y) / self.scale)
            count = len(self._assets(filtered=False))
            # Kept inside the free part of the viewport: what the clamp leaves is stored.
            centred = panel_rect(self._view, count)
            moved = panel_rect(self._view, count, wanted)
            _PANEL["offset"] = (moved.x - centred.x, moved.top - centred.top)
            self.tag_redraw()
        elif event.type == "ESC" and event.value == "PRESS":
            _PANEL["offset"] = offset
            self._drag = None
            self._set_cursor("")
            self.tag_redraw()
        elif (event.type == "LEFTMOUSE" and event.value == "RELEASE") or event.type == "WINDOW_DEACTIVATE":
            # Alt+Tab never delivers the release: the panel stays where it was dragged.
            self._drag = None
            self._set_cursor("HAND" if self.rect.contains(x, y) else "")

    def _set_cursor(self, name):
        """The open hand over the header, the closed one while dragging."""
        if name == self._cursor:
            return
        try:
            if name:
                self.window.cursor_modal_set(name)
            else:
                self.window.cursor_modal_restore()
        except (AttributeError, ReferenceError, TypeError):
            pass
        self._cursor = name

    def _text_event(self, context, event, over):
        if event.value != "PRESS":
            return False
        state = self.state
        # Numpad keys navigate the viewport (front view, frame selected), never search.
        typing = not (event.ctrl or event.alt or event.oskey or event.type.startswith("NUMPAD_"))
        if state.get("_runtime_focus") == SEARCH and not over and event.type not in _POINTER_EVENTS:
            # Away from the panel, keys are Blender's again (A, X, Delete in the viewport).
            state.pop("_runtime_focus", None)
            self.tag_redraw()
            return False
        if state.get("_runtime_focus") != SEARCH:
            # Typing over the panel searches.
            if not (over and typing and event.unicode and event.unicode.isprintable()):
                return False
            state["_runtime_focus"] = SEARCH
        if event.type == "BACK_SPACE":
            self.search = self.search[:-1]
        elif event.type == "DEL" or (event.ctrl and event.type == "A"):
            self.search = ""
        elif event.type in {"RET", "NUMPAD_ENTER", "ESC"}:
            state.pop("_runtime_focus", None)
        elif event.ctrl and event.type == "V":
            self.search += context.window_manager.clipboard.replace("\n", " ")
        elif event.ctrl and event.type == "C":
            context.window_manager.clipboard = self.search
        elif typing and event.unicode and event.unicode.isprintable():
            self.search += event.unicode
        elif event.type in _POINTER_EVENTS:
            return False
        self._changed()
        return True  # a focused field keeps every key from Blender's shortcuts

    def _pointer_event(self, context, event, x, y):
        state = self.state
        hit = self.tree.hit_test(x, y)
        if event.type == "MOUSEMOVE":
            self._set_cursor("HAND" if self._grabs(x, y, hit) else "")
            key = hit.key if hit is not None else None
            state["_runtime_mouse"] = (x, y)
            if key != state.get("_runtime_hover"):
                state["_runtime_hover"] = key
                state["_hover_since"] = time.monotonic()
                self.tag_redraw()
                if hit is not None and hit.tooltip:
                    # The panel redraws only on events: show the tooltip when it is due.
                    bpy.app.timers.register(lambda: None if self.closed else self.tag_redraw(),
                                            first_interval=TOOLTIP_DELAY + 0.05)
            return
        if event.type == "LEFTMOUSE" and event.value == "PRESS" and self._grabs(x, y, hit):
            self._drag = (event.mouse_x, event.mouse_y, _PANEL["offset"])
            self._set_cursor("HAND_CLOSED")
            state["_runtime_focus"] = None
            return
        if event.type in {"WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN"}:
            scroll = self._find("mod.grid.scroll")
            if scroll is not None:
                if event.type == "TRACKPADPAN":
                    delta = (event.mouse_y - event.mouse_prev_y) / self.scale
                else:
                    delta = (-1.0 if event.type == "WHEELUPMOUSE" else 1.0) * scroll.wheel_step
                if scroll.scroll_by(delta, state):
                    self.tag_redraw()
            return
        if event.type != "LEFTMOUSE":
            return
        if event.value in {"PRESS", "DOUBLE_CLICK"} and getattr(hit, "detect_double_press", False):
            # Modal handlers can get two presses instead of a double click.
            now, previous = time.monotonic(), state.pop("_double_click", None)
            identity = hit.double_click_identity
            if event.value == "DOUBLE_CLICK" or (
                    previous is not None and previous[0] == identity and now - previous[1] <= 0.4
                    and abs(x - previous[2]) <= 5 and abs(y - previous[3]) <= 5):
                state.pop("_runtime_pressed", None)
                hit.double_action(context)
                self._changed()
                return
            state["_double_click"] = (identity, now, x, y)
        if event.value == "PRESS":
            state["_runtime_pressed"] = state["_runtime_focus"] = hit.key if hit is not None else None
            self.tag_redraw()
        elif event.value == "RELEASE":
            pressed = state.pop("_runtime_pressed", None)
            if hit is not None and hit.key == pressed:
                hit.activate(context, state)
                self._changed()

    # -- import and close --

    def import_asset(self, context, asset):
        """Import ``asset`` at the 3D cursor; the panel closes once it is in."""
        try:
            result = bpy.ops.mcbedrock.import_mod_jar(
                "EXEC_DEFAULT", True, filepath=self.jar, asset_key=asset.key,
                texture_location=self.state.get("mod.texture") or "")
        except RuntimeError as exc:  # the import reported an error: keep the panel to pick another
            self.operator.report({"ERROR"}, str(exc).removeprefix("Error: ").strip())
            return
        if "FINISHED" in result:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.handler is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self.handler, "WINDOW")
            except (ValueError, ReferenceError):
                pass
            self.handler = None
        if _PANEL.get("session") is self:
            _PANEL["session"] = None
        self._drag = None
        self._set_cursor("")
        mod_thumbnails.stop()  # a model already rendering still finishes and is cached
        if self._valid():  # a closed or joined viewport's region is already freed
            self.tag_redraw()


class MCBEDROCK_OT_mod_import_panel(bpy.types.Operator):
    """Pick a mob, block or item of a mod (.jar) in a panel over the 3D viewport"""
    bl_idname = "mcbedrock.mod_import_panel"
    bl_label = "Import Mod Model"
    bl_options = {"INTERNAL"}

    filepath: StringProperty(subtype="FILE_PATH", options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        if context.area is None or context.area.type != "VIEW_3D" or not self.filepath:
            return {"CANCELLED"}
        close_panel()
        self._panel = ModImportPanel(self, context.window, context.area, self.filepath)
        _PANEL["session"] = self._panel
        context.window_manager.modal_handler_add(self)
        self._panel.tag_redraw()
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        return self._panel.modal(context, event)

    def cancel(self, context):
        self._panel.close()


def kept_copy(jar):
    """Sparkle's own copy of a dropped JAR, in its cache, made once per JAR build.

    The panel, its thumbnail workers and Import all read the copy, so moving,
    renaming or deleting the original while the panel is open (or dragging it
    out of a .zip, which Windows deletes right after the drop) breaks nothing.
    The newest eight copies stay.
    """
    import hashlib
    import shutil
    from ..importers.archive_extractor import user_cache_dir

    stat = os.stat(jar)
    folder = user_cache_dir("mod_jars")
    stem = os.path.splitext(os.path.basename(jar))[0]
    digest = hashlib.sha1(repr((os.path.basename(jar), stat.st_size, stat.st_mtime_ns)).encode("utf-8")).hexdigest()[:12]
    target = os.path.join(folder, f"{stem[:48]}_{digest}.jar")
    if not os.path.isfile(target):
        os.makedirs(folder, exist_ok=True)
        shutil.copy2(jar, target + ".part")
        os.replace(target + ".part", target)
        kept = sorted((entry for entry in os.scandir(folder) if entry.name.endswith(".jar")),
                      key=lambda entry: entry.stat().st_atime, reverse=True)
        for entry in kept[8:]:
            try:
                os.remove(entry.path)
            except OSError:
                pass
    # Most recently used stays. Only the access time moves: the catalog and the
    # thumbnails are keyed by the copy's modification time, which must hold.
    os.utime(target, ns=(time.time_ns(), os.stat(target).st_mtime_ns))
    return target


def _has_assets(jar):
    """A quick look at the JAR's listing: only a mod with assets gets a thumbnail worker."""
    import zipfile

    try:
        with zipfile.ZipFile(jar) as archive:
            return any(name.startswith("assets/") for name in archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def open_panel(context, jar):
    """Show ``jar``'s models over the 3D viewport it was dropped on, else the largest one.

    ``False`` when there is no viewport to show them in or the JAR lists none.
    """
    from ..importers.mod_jar import mod_catalog

    window = context.window
    if bpy.app.background or window is None:
        return False
    # Copying and reading a big mod takes a second or two before the panel shows.
    window.cursor_modal_set("WAIT")
    try:
        try:
            jar = kept_copy(jar)
        except OSError as exc:
            print(f"[Sparkle] Could not keep a copy of {jar}: {exc}")
            return False
        if _has_assets(jar):
            mod_thumbnails.prepare(jar)  # a worker starts up while the mod is read
        try:
            catalog = mod_catalog(jar)
        except Exception as exc:  # unreadable or not a mod: the dialog says so
            print(f"[Sparkle] Could not read {jar}: {exc}")
            return False
    finally:
        window.cursor_modal_restore()
    if catalog is None or not catalog.assets:
        return False
    area = context.area if context.area is not None and context.area.type == "VIEW_3D" else max(
        (item for item in window.screen.areas if item.type == "VIEW_3D"),
        key=lambda item: item.width * item.height, default=None)
    if area is None:
        return False
    session = _PANEL.get("session")
    if session is not None and not session.closed and _pointer(session.area) == _pointer(area):
        session.show(jar)
        return True
    region = next((item for item in area.regions if item.type == "WINDOW"), None)
    with context.temp_override(window=window, area=area, region=region):
        result = bpy.ops.mcbedrock.mod_import_panel("INVOKE_DEFAULT", filepath=jar)
    return "RUNNING_MODAL" in result


def close_panel():
    session = _PANEL.get("session")
    if session is not None:
        session.close()


@persistent
def _before_load(*_args):
    close_panel()


classes = (MCBEDROCK_OT_mod_import_panel,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if _before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_before_load)


def unregister():
    close_panel()
    if _before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_before_load)
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
