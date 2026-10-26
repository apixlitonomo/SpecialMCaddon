"""Overlay menus and small code-drawn pixel icons; no Blender dependencies."""

from .core import Button, Column, Rect, Row, Size, Style, Widget
from .motion import tween
from pathlib import Path
from dataclasses import replace


class PixelIcon(Widget):
    PATTERNS = {
        "ASSETS": ("01110", "11011", "10101", "11011", "01110"),
        "PACKS": ("11011", "11011", "00000", "11011", "11011"),
        "QUICK": ("00100", "00100", "10101", "01110", "00100"),
        "SCHEMATIC": ("00100", "01110", "11111", "10101", "11111"),
        "PARTICLE": ("10001", "00100", "01110", "00100", "10001"),
        "SKY": ("10101", "01110", "11111", "01110", "10101"),
    }

    def __init__(self, key, kind, *, style=None):
        super().__init__(key, style=style or Style(width=24, height=30))
        self.kind = kind

    def draw(self, renderer, state, theme):
        pattern = self.PATTERNS.get(self.kind, self.PATTERNS["ASSETS"])
        unit = max(1, min(3, min(self.rect.width, self.rect.height) / 5))
        x = self.rect.x + (self.rect.width - unit * 5) / 2
        y = self.rect.y + (self.rect.height - unit * 5) / 2
        for row, line in enumerate(pattern):
            for col, pixel in enumerate(line):
                if pixel == "1":
                    renderer.rect(Rect(x + col * unit, y + (4-row) * unit, unit, unit), self.style.text_color or theme.accent)


class PageIcon(PixelIcon):
    """Smooth navigation glyphs, rendered from the same SVG assets as search."""
    FILES = {'PACKS': 'packs', 'QUICK': 'import', 'SCHEMATIC': 'schematics', 'PARTICLE': 'particles', 'SKY': 'sun',
             'OUTLINES': 'frame-corners'}

    def draw(self, renderer, state, theme):
        if self.kind == 'POSTERIZE':
            # Three source-color bands: deliberately distinct from library icons.
            for index, color in enumerate(((.18,.55,.58,1),(.12,.38,.42,1),(.08,.22,.29,1))):
                renderer.rect(Rect(self.rect.x+index*self.rect.width/3,self.rect.y,self.rect.width/3,self.rect.height),color)
            return
        name = self.FILES.get(self.kind, 'packs')
        path = Path(__file__).resolve().parents[2] / 'assets/ui/icons' / (name + '.png')
        tint = getattr(renderer, 'icon_tint', None)
        renderer.icon_tint = (self.style.text_color or theme.text) if theme.follows_blender else None
        try:
            renderer.image(self.rect, str(path))
        finally:
            renderer.icon_tint = tint



class Dropdown(Button):
    keep_open = False
    row_height = 42

    def checked(self, value):
        return None

    def __init__(self, key, getter, setter, choices, *, style=None, icons=False):
        super().__init__(key, "", lambda context: None, style=style)
        self.getter, self.setter = getter, setter
        self.choices = tuple(choices)
        self.icons = icons

    def activate(self, context, state):
        if not self.style.enabled or not self.choices:
            return False
        if state.get("_menu", {}).get("key") == self.key:
            state.pop("_menu", None)
        else:
            index = next((i for i, (v, _) in enumerate(self.choices) if v == self.getter(context)), 0)
            state["_menu"] = {"key": self.key, "index": index, "scroll": 0}
            state.setdefault('_motion', {}).pop('app.menu.enter', None)
        return True

    def draw(self, renderer, state, theme):
        value = self.getter(state.get("_runtime_context"))
        self.text = next((label for v, label in self.choices if v == value), str(value))
        # Reserve a distinct trailing column so long labels cannot cover the chevron.
        self.style = self.style.with_updates(padding=(10,0,30,0))
        super().draw(renderer, state, replace(theme, surface_raised=theme.menu_surface, control_text=theme.menu_text))
        previous_tint = getattr(renderer, 'icon_tint', None)
        renderer.icon_tint = theme.menu_text if theme.follows_blender else None
        try:
            renderer.image(Rect(self.rect.right-18,self.rect.y+(self.rect.height-10)/2,10,10),
                           str(Path(__file__).resolve().parents[2]/'assets/ui/icons/chevron.png'))
        finally:
            renderer.icon_tint = previous_tint


class MenuOption(Button):
    def __init__(self, *args, selected=False, icon=None, checked=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.selected = selected
        self.icon = icon
        self.checked = checked

    def draw(self, renderer, state, theme):
        self.style = self.style.with_updates(background=theme.surface_raised if self.selected else theme.surface,
                                            text_color=theme.text if self.selected else theme.text_muted)
        if self.icon:
            self.style = self.style.with_updates(padding=(38, 0, 10, 0),
                font_weight='Bold' if self.selected else 'Medium', font_size=12, text_cap_center=True)
        super().draw(renderer, state, theme)
        if self.checked is not None:
            box = Rect(self.rect.x + 9, self.rect.y + (self.rect.height-12)/2, 12, 12)
            renderer.rect(box, theme.accent if self.checked else theme.surface,
                          radius=2, border=theme.text_muted)
            if self.checked:
                renderer.text(box, 'x', theme.text, size=11, align='CENTER')
        if self.icon:
            icon = PageIcon(self.key + '.icon', self.icon, style=Style(text_color=self.style.text_color))
            icon.rect = Rect(self.rect.x + 13, self.rect.y + (self.rect.height - 15) / 2, 15, 15)
            icon.draw(renderer, state, theme)


class Application(Column):
    """Paint and hit-test an active dropdown outside ordinary layout clipping."""
    def close_menu(self, state):
        state.pop('_menu', None)
        self.menu = None
        self.menu_source = None
        self.menu_offset = 0

    def walk(self, node=None):
        node = self if node is None else node
        yield node
        for child in node.children:
            yield from self.walk(child)

    def layout(self, bounds, state, theme):
        super().layout(bounds, state, theme)
        bounds = getattr(self, 'overlay_bounds', bounds)
        self.menu = None
        self.menu_offset = 0
        entry = state.get("_menu", {})
        source = next((w for w in self.walk() if isinstance(w, Dropdown) and w.key == entry.get("key")), None)
        if source is None or not source.choices:
            state.pop("_menu", None)
            return
        self.menu_source = source
        width = min(max(source.rect.width, 228), max(0, bounds.width - 12))
        row_height = source.row_height
        height = min(16 + row_height * len(source.choices), max(0, bounds.height - 12))
        x = max(bounds.x + 6, min(source.rect.right - width, bounds.right - width - 6))
        y = max(bounds.y + 6, min(source.rect.y - height - 4, bounds.top - height - 6))
        self.menu_rect = Rect(x, y, width, height)
        count = max(1, int((height - 16) // row_height))
        entry["index"] = max(0, min(entry.get("index", 0), len(source.choices) - 1))
        start = min(max(0, entry.get("scroll", 0)), max(0, len(source.choices) - count))
        if entry["index"] < start:
            start = entry["index"]
        if entry["index"] >= start + count:
            start = entry["index"] - count + 1
        entry["scroll"] = start
        rows = []
        for i in range(start, min(len(source.choices), start + count)):
            value, label = source.choices[i]
            def choose(context, value=value):
                if state.get('_menu') is not entry:
                    return
                if not source.keep_open:
                    self.close_menu(state)
                state["_runtime_focus"] = source.key
                source.setter(context, value)
            button = MenuOption(f"{source.key}.option.{i}", label, choose,
                            selected=i == entry["index"], icon=value if source.icons else None,
                            checked=source.checked(value),
                            style=Style(height=row_height-4, flex=1, align="LEFT",
                                        padding=(30,0,8,0) if source.checked(value) is not None else (8,0,8,0)))
            children = [button]
            rows.append(Row(f"{source.key}.row.{i}", children, style=Style(height=row_height-4, gap=4)))
        self.menu = Column("app.menu", rows, style=Style(padding=(8,8,8,8),gap=4))
        self.menu.layout(self.menu_rect, state, theme)

    def draw(self, renderer, state, theme):
        super().draw(renderer, state, theme)
        if self.menu:
            progress = tween(state, 'app.menu.enter', 1.0, .18, initial=0.0)
            self.menu_offset = 6 * (1-progress)
            opacity, offset = getattr(renderer,'opacity',1.0), getattr(renderer,'offset_y',0.0)
            renderer.opacity, renderer.offset_y = opacity*progress, offset+self.menu_offset
            try:
                for spread, alpha in [(12,.04),(8,.07),(4,.13)]:
                    r=self.menu_rect
                    renderer.rect(Rect(r.x-spread,r.y-spread-6,r.width+2*spread,r.height+2*spread),
                                  (0,0,0,alpha),radius=12+spread)
                renderer.rect(self.menu_rect, theme.surface, radius=12, border=theme.border)
                renderer.push_clip(self.menu_rect)
                self.menu.draw(renderer, state, theme)
                renderer.pop_clip()
            finally:
                renderer.opacity, renderer.offset_y = opacity, offset

    def hit_test(self, x, y):
        if self.menu:
            return self.menu.hit_test(x, y-self.menu_offset)
        return super().hit_test(x, y)

    def handle_menu_event(self, context, event, state, x, y):
        if not self.menu:
            return False
        entry = state.get("_menu")
        if not entry or entry.get('key') != self.menu_source.key:
            # Events can arrive after a choice closes the menu but before redraw.
            # Discard the old overlay without touching a newly opened menu.
            self.menu = None
            self.menu_source = None
            self.menu_offset = 0
            return False
        if event.type == "ESC" and event.value == "PRESS":
            self.close_menu(state)
        elif event.type in {"UP_ARROW", "DOWN_ARROW", "HOME", "END", "WHEELUPMOUSE", "WHEELDOWNMOUSE"} and event.value == "PRESS":
            delta = -1 if event.type in {"UP_ARROW", "WHEELUPMOUSE"} else 1
            entry["index"] = max(0, min(len(self.menu_source.choices)-1, entry["index"] + delta))
            if event.type in {"HOME", "END"}:
                entry["index"] = 0 if event.type == "HOME" else len(self.menu_source.choices)-1
        elif event.type in {"RET", "NUMPAD_ENTER", "SPACE"} and event.value == "PRESS":
            source = self.menu_source
            value = source.choices[entry["index"]][0]
            if not source.keep_open:
                self.close_menu(state)
            state['_runtime_focus'] = source.key
            source.setter(context, value)
        elif event.type == "LEFTMOUSE" and event.value == "PRESS" and not self.menu_rect.contains(x, y-self.menu_offset):
            self.close_menu(state)
            state["_runtime_pressed"] = None
        elif event.type in {"LEFTMOUSE", "MOUSEMOVE"}:
            return False  # Runtime handles option buttons; underlying content is shielded.
        return True
