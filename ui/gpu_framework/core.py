"""Pure-Python widget tree, flex layout, and input primitives."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Optional
from .motion import blend, tween


Color = tuple[float, float, float, float]
Insets = tuple[float, float, float, float]  # left, top, right, bottom


@dataclass(frozen=True)
class Size:
    width: float
    height: float


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def top(self) -> float:
        return self.y + self.height

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.right and self.y <= y <= self.top

    def inset(self, values: Insets) -> "Rect":
        left, top, right, bottom = values
        return Rect(
            self.x + left,
            self.y + bottom,
            max(0.0, self.width - left - right),
            max(0.0, self.height - top - bottom),
        )

    def intersect(self, other: "Rect") -> "Rect":
        left = max(self.x, other.x)
        bottom = max(self.y, other.y)
        right = min(self.right, other.right)
        top = min(self.top, other.top)
        return Rect(
            left,
            bottom,
            max(0.0, right - left),
            max(0.0, top - bottom),
        )


@dataclass(frozen=True)
class Style:
    width: Optional[float] = None
    height: Optional[float] = None
    min_width: float = 0.0
    min_height: float = 0.0
    max_width: Optional[float] = None
    max_height: Optional[float] = None
    flex: float = 0.0
    padding: Insets = (0.0, 0.0, 0.0, 0.0)
    gap: float = 0.0
    background: Optional[Color] = None
    border: Optional[Color] = None
    border_width: float = 1.0
    radius: float = 3.0
    text_color: Optional[Color] = None
    font_size: Optional[float] = None
    font_weight: str = "Regular"
    text_cap_center: bool = False
    ghost: bool = False
    align: str = "LEFT"
    enabled: bool = True

    def with_updates(self, **kwargs) -> "Style":
        return replace(self, **kwargs)


@dataclass(frozen=True)
class Theme:
    """Compact charcoal surfaces with a blue action accent."""

    follows_blender: bool = False
    selected_text: Color = (242/255,242/255,242/255,1.0)
    control_text: Color = (242/255,242/255,242/255,1.0)
    input_surface: Color = (28/255,28/255,28/255,1.0)
    input_text: Color = (242/255,242/255,242/255,1.0)
    menu_surface: Color = (36/255,36/255,36/255,1.0)
    menu_text: Color = (242/255,242/255,242/255,1.0)
    canvas: Color = (20/255, 20/255, 20/255, 1.0)
    surface: Color = (28/255, 28/255, 28/255, 1.0)
    surface_raised: Color = (36/255, 36/255, 36/255, 1.0)
    surface_hover: Color = (42/255, 42/255, 42/255, 1.0)
    # Buttons and segmented options under the pointer; clearer than surface_hover.
    surface_lift: Color = (54/255, 54/255, 54/255, 1.0)
    surface_pressed: Color = (24/255, 24/255, 24/255, 1.0)
    border: Color = (48/255, 48/255, 48/255, 1.0)
    selected: Color = (32/255, 44/255, 64/255, 1.0)
    edge_highlight: Color = (0.392, 0.475, 0.557, 0.78)
    edge_shadow: Color = (0.020, 0.027, 0.035, 0.95)
    text: Color = (242/255, 242/255, 242/255, 1.0)
    text_muted: Color = (146/255, 146/255, 146/255, 1.0)
    accent: Color = (82/255, 148/255, 1.0, 1.0)
    accent_hover: Color = (115/255, 169/255, 1.0, 1.0)
    accent_pressed: Color = (57/255, 118/255, 221/255, 1.0)
    accent_highlight: Color = (0.294, 0.682, 1.0, 1.0)
    danger: Color = (0.94, 0.30, 0.32, 1.0)
    success: Color = (0.28, 0.76, 0.45, 1.0)
    radius: float = 8.0
    spacing: float = 12.0
    font_size: float = 12.0
    small_font_size: float = 10.0
    control_height: float = 34.0


class Widget:
    """Base retained layout node with a stable declarative key."""

    interactive = False

    def __init__(
        self,
        key: str,
        *,
        style: Optional[Style] = None,
        tooltip: str = "",
    ):
        if not key:
            raise ValueError("Widget keys must be non-empty")
        self.key = key
        self.style = style or Style()
        self.tooltip = tooltip
        self.rect = Rect(0.0, 0.0, 0.0, 0.0)
        self.parent: Optional[Widget] = None
        self.children: list[Widget] = []
        self.clip_rect: Optional[Rect] = None

    def set_children(self, children: Iterable["Widget"]) -> None:
        self.children = [child for child in children if child is not None]
        for child in self.children:
            child.parent = self

    def preferred_size(
        self,
        available: Size,
        state: dict,
        theme: Theme,
    ) -> Size:
        width = self.style.width if self.style.width is not None else available.width
        height = self.style.height if self.style.height is not None else self.style.min_height
        return Size(self._clamp_width(width), self._clamp_height(height))

    def _clamp_width(self, value: float) -> float:
        value = max(self.style.min_width, value)
        if self.style.max_width is not None:
            value = min(value, self.style.max_width)
        return value

    def _clamp_height(self, value: float) -> float:
        value = max(self.style.min_height, value)
        if self.style.max_height is not None:
            value = min(value, self.style.max_height)
        return value

    def layout(self, bounds: Rect, state: dict, theme: Theme) -> None:
        width = self.style.width if self.style.width is not None else bounds.width
        height = self.style.height if self.style.height is not None else bounds.height
        self.rect = Rect(
            bounds.x,
            bounds.y,
            min(bounds.width, self._clamp_width(width)),
            min(bounds.height, self._clamp_height(height)),
        )

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        if self.style.background is not None:
            renderer.rect(
                self.rect,
                self.style.background,
                radius=self.style.radius,
                border=self.style.border,
                border_width=self.style.border_width,
            )
        for child in self.children:
            if child.clip_rect is None or child.rect.intersect(child.clip_rect).height > 0:
                child.draw(renderer, state, theme)

    def hit_test(self, x: float, y: float) -> Optional["Widget"]:
        if self.clip_rect is not None and not self.clip_rect.contains(x, y):
            return None
        if not self.rect.contains(x, y):
            return None
        for child in reversed(self.children):
            hit = child.hit_test(x, y)
            if hit is not None:
                return hit
        return self if self.interactive and self.style.enabled else None

    def ancestor(self, widget_type):
        current: Optional[Widget] = self
        while current is not None:
            if isinstance(current, widget_type):
                return current
            current = current.parent
        return None

    def activate(self, context, state: dict) -> bool:
        return False

    def handle_wheel(self, delta: float, state: dict) -> bool:
        return False


class Flex(Widget):
    def __init__(
        self,
        key: str,
        children: Iterable[Widget] = (),
        *,
        direction: str,
        style: Optional[Style] = None,
    ):
        super().__init__(key, style=style)
        self.direction = direction
        self.set_children(children)

    def preferred_size(self, available: Size, state: dict, theme: Theme) -> Size:
        inner_width = max(
            0.0,
            available.width - self.style.padding[0] - self.style.padding[2],
        )
        inner_height = max(
            0.0,
            available.height - self.style.padding[1] - self.style.padding[3],
        )
        sizes = [
            child.preferred_size(Size(inner_width, inner_height), state, theme)
            for child in self.children
        ]
        gap = self.style.gap * max(0, len(sizes) - 1)
        if self.direction == "ROW":
            width = sum(size.width for size in sizes) + gap
            height = max((size.height for size in sizes), default=0.0)
        else:
            width = max((size.width for size in sizes), default=0.0)
            height = sum(size.height for size in sizes) + gap
        width += self.style.padding[0] + self.style.padding[2]
        height += self.style.padding[1] + self.style.padding[3]
        if self.style.width is not None:
            width = self.style.width
        if self.style.height is not None:
            height = self.style.height
        return Size(self._clamp_width(width), self._clamp_height(height))

    def layout(self, bounds: Rect, state: dict, theme: Theme) -> None:
        self.rect = Rect(
            bounds.x,
            bounds.y,
            min(bounds.width, self._clamp_width(
                self.style.width if self.style.width is not None else bounds.width
            )),
            min(bounds.height, self._clamp_height(
                self.style.height if self.style.height is not None else bounds.height
            )),
        )
        inner = self.rect.inset(self.style.padding)
        if not self.children:
            return
        available = Size(inner.width, inner.height)
        sizes = [
            child.preferred_size(available, state, theme)
            for child in self.children
        ]
        gap_total = self.style.gap * max(0, len(self.children) - 1)
        if self.direction == "ROW":
            fixed = sum(
                size.width for child, size in zip(self.children, sizes)
                if child.style.flex <= 0.0
            )
            flex_total = sum(max(0.0, child.style.flex) for child in self.children)
            remaining = max(0.0, inner.width - fixed - gap_total)
            x = inner.x
            for child, size in zip(self.children, sizes):
                width = (
                    remaining * child.style.flex / flex_total
                    if child.style.flex > 0.0 and flex_total > 0.0
                    else size.width
                )
                child.layout(Rect(x, inner.y, width, inner.height), state, theme)
                x += width + self.style.gap
        else:
            fixed = sum(
                size.height for child, size in zip(self.children, sizes)
                if child.style.flex <= 0.0
            )
            flex_total = sum(max(0.0, child.style.flex) for child in self.children)
            remaining = max(0.0, inner.height - fixed - gap_total)
            y = inner.top
            for child, size in zip(self.children, sizes):
                height = (
                    remaining * child.style.flex / flex_total
                    if child.style.flex > 0.0 and flex_total > 0.0
                    else size.height
                )
                y -= height
                child.layout(Rect(inner.x, y, inner.width, height), state, theme)
                y -= self.style.gap


class Row(Flex):
    def __init__(self, key: str, children=(), *, style: Optional[Style] = None):
        super().__init__(key, children, direction="ROW", style=style)


class Column(Flex):
    def __init__(self, key: str, children=(), *, style: Optional[Style] = None):
        super().__init__(key, children, direction="COLUMN", style=style)


class Panel(Column):
    def __init__(self, key: str, children=(), *, style: Optional[Style] = None):
        base = Style(
            padding=(14.0, 14.0, 14.0, 14.0),
            gap=12.0,
            radius=10.0,
        )
        super().__init__(key, children, style=style or base)

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        background = self.style.background or theme.surface
        renderer.rect(self.rect, background, radius=max(0, self.style.radius),
                      border=self.style.border, border_width=self.style.border_width)
        for child in self.children:
            if child.clip_rect is None or child.rect.intersect(child.clip_rect).height > 0:
                child.draw(renderer, state, theme)


class ActionRow(Row):
    """Clickable row container for cards, list items, and composed actions."""

    interactive = True

    def __init__(
        self,
        key: str,
        children=(),
        action: Optional[Callable] = None,
        *,
        selected: Optional[Callable] = None,
        style: Optional[Style] = None,
        tooltip: str = "",
    ):
        base = Style(
            height=72.0,
            min_height=72.0,
            padding=(10.0, 10.0, 10.0, 10.0),
            gap=10.0,
            background=(0.190, 0.196, 0.208, 1.0),
            border=(0.255, 0.263, 0.282, 1.0),
            radius=0.0,
        )
        super().__init__(key, children, style=style or base)
        self.action = action
        self.selected = selected
        self.tooltip = tooltip

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        context = state.get("_runtime_context")
        is_selected = bool(self.selected(context)) if self.selected and context else False
        hovered = False
        pressed = state.get("_runtime_pressed") == self.key
        if pressed:
            background = theme.surface_pressed
        elif hovered:
            background = theme.surface_hover
        else:
            background = self.style.background or theme.surface_raised
        hover = tween(state, self.key + '.hover', float(hovered))
        background = blend(self.style.background or theme.surface, theme.surface_hover, hover)
        if is_selected:
            background = theme.selected
        if pressed:
            background = theme.surface_pressed
        border = self.style.border
        renderer.rect(self.rect, background, radius=max(theme.radius,self.style.radius),
                      border=border, border_width=1.0)
        for child in self.children:
            if child.clip_rect is None or child.rect.intersect(child.clip_rect).height > 0:
                child.draw(renderer, state, theme)

    def activate(self, context, state: dict) -> bool:
        if not self.style.enabled or self.action is None:
            return False
        self.action(context)
        return True


class Image(Widget):
    """Bitmap image loaded lazily by the host renderer."""

    def __init__(
        self,
        key: str,
        source,
        *,
        style: Optional[Style] = None,
        fallback: Optional[Color] = None,
    ):
        base = Style(
            width=56.0,
            height=56.0,
            min_width=56.0,
            min_height=56.0,
            background=(0.105, 0.109, 0.117, 1.0),
            border=(0.255, 0.263, 0.282, 1.0),
            radius=0.0,
        )
        super().__init__(key, style=style or base)
        self.source = source
        self.fallback = fallback or (0.105, 0.109, 0.117, 1.0)

    def preferred_size(self, available: Size, state: dict, theme: Theme) -> Size:
        width = self.style.width if self.style.width is not None else 56.0
        height = self.style.height if self.style.height is not None else width
        return Size(self._clamp_width(width), self._clamp_height(height))

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        context = state.get("_runtime_context")
        source = self.source(context) if callable(self.source) and context else self.source
        renderer.rect(
            self.rect,
            self.style.background or self.fallback,
            radius=self.style.radius,
            border=self.style.border or theme.border,
            border_width=self.style.border_width,
        )
        image_rect = self.rect.inset((1.0, 1.0, 1.0, 1.0))
        if not source or not renderer.image(image_rect, str(source)):
            renderer.text(
                image_rect,
                "RP",
                theme.text_muted,
                size=theme.small_font_size,
                align="CENTER",
            )

class Label(Widget):
    def __init__(
        self,
        key: str,
        text: str,
        *,
        style: Optional[Style] = None,
        muted: bool = False,
    ):
        default = Style(height=20.0, min_height=20.0)
        super().__init__(key, style=style or default)
        self.text = str(text)
        self.muted = muted

    def preferred_size(self, available: Size, state: dict, theme: Theme) -> Size:
        height = self.style.height or max(18.0, (self.style.font_size or theme.font_size) + 7.0)
        return Size(
            self._clamp_width(
                self.style.width if self.style.width is not None else available.width
            ),
            self._clamp_height(height),
        )

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        super().draw(renderer, state, theme)
        color = (
            self.style.text_color
            or (theme.text_muted if self.muted else theme.text)
        )
        renderer.text(
            self.rect.inset(self.style.padding),
            self.text,
            color,
            size=self.style.font_size or theme.font_size,
            weight=self.style.font_weight,
            align=self.style.align,
            cap_center=self.style.text_cap_center,
        )


class Spacer(Widget):
    def __init__(self, key: str, height: float = 8.0):
        super().__init__(key, style=Style(height=height, min_height=height))


class Divider(Widget):
    def __init__(self, key: str):
        super().__init__(key, style=Style(height=1.0, min_height=1.0))

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        renderer.rect(self.rect, theme.border)


class Button(Widget):
    interactive = True

    def __init__(
        self,
        key: str,
        text: str,
        action: Callable,
        *,
        style: Optional[Style] = None,
        primary: bool = False,
        tooltip: str = "",
    ):
        base = Style(
            height=30.0,
            min_height=30.0,
            padding=(9.0, 0.0, 9.0, 0.0),
            background=None,
            border=None,
            radius=0.0,
            align="CENTER",
        )
        super().__init__(key, style=style or base, tooltip=tooltip)
        self.text = str(text)
        self.action = action
        self.primary = primary

    def preferred_size(self, available: Size, state: dict, theme: Theme) -> Size:
        width = self.style.width
        if width is None:
            width = max(68.0, min(available.width, len(self.text) * 7.2 + 20.0))
        return Size(self._clamp_width(width), self._clamp_height(
            self.style.height or theme.control_height
        ))

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        # Buttons lift under the pointer, like Blender's own; rows and sections do not.
        hovered = bool(self.style.enabled) and state.get("_runtime_hover") == self.key
        pressed = state.get("_runtime_pressed") == self.key
        focused = state.get("_runtime_focus") == self.key and state.get("_focus_visible", False)
        if not self.style.enabled:
            background = theme.surface
            color = (*theme.text_muted[:3], 0.45)
        elif self.primary:
            if pressed:
                background = theme.accent_pressed
            else:
                background = theme.accent_hover if hovered else theme.accent
            color = theme.selected_text
        elif pressed:
            background = theme.surface_pressed
            color = theme.control_text
        elif hovered:
            background = theme.surface_hover
            color = theme.control_text
        else:
            background = theme.surface_raised
            color = theme.control_text
        hover = tween(state, self.key + '.hover', float(hovered))
        base = theme.accent if self.primary else (self.style.background or theme.surface_raised)
        if self.style.ghost and not self.primary:
            base = theme.canvas
        if self.style.enabled and not pressed:
            background = blend(base, theme.accent_hover if self.primary else theme.surface_lift, hover)
        border = theme.text_muted if focused and getattr(self, "show_focus_outline", True) else self.style.border
        renderer.rect(self.rect, background, radius=theme.radius,
                      border=border, border_width=1.0)
        text_rect = self.rect.inset(self.style.padding if any(self.style.padding) else (8.0, 0.0, 8.0, 0.0))
        font_size = min(self.style.font_size or theme.font_size,
                        10.5 if self.rect.width < 140 else 11.5)
        if self.text and hasattr(renderer, 'text_width'):
            measured = renderer.text_width(self.text, font_size, weight=self.style.font_weight)
            if isinstance(measured, (int, float)) and measured > text_rect.width:
                font_size = max(9.0, font_size * max(0, text_rect.width) / measured)
        renderer.text(
            text_rect,
            self.text,
            self.style.text_color or color,
            size=font_size,
            weight=self.style.font_weight,
            align=self.style.align,
            cap_center=self.style.text_cap_center,
        )

    def activate(self, context, state: dict) -> bool:
        if not self.style.enabled:
            return False
        self.action(context)
        return True


class Toggle(Button):
    def __init__(
        self,
        key: str,
        text: str,
        getter: Callable,
        setter: Callable,
        *,
        style: Optional[Style] = None,
    ):
        super().__init__(key, text, self._toggle, style=style)
        self.getter = getter
        self.setter = setter

    def _toggle(self, context) -> None:
        self.setter(context, not bool(self.getter(context)))

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        context = state.get("_runtime_context")
        enabled = bool(self.getter(context)) if context is not None else False
        progress = tween(state, self.key + '.switch', float(enabled), .16)
        track = Rect(self.rect.right-38, self.rect.y+(self.rect.height-20)/2, 36, 20)
        renderer.text(Rect(self.rect.x, self.rect.y, max(0,self.rect.width-48), self.rect.height),
                      self.text, theme.text_muted, size=theme.font_size)
        renderer.rect(track, blend(theme.surface_hover,theme.accent,progress),radius=10,
                      border=theme.accent if state.get('_runtime_focus')==self.key and state.get('_focus_visible') else None)
        renderer.rect(Rect(track.x+3+16*progress,track.y+3,14,14),theme.text,radius=7)


class EnumButton(Button):
    def __init__(
        self,
        key: str,
        label: str,
        getter: Callable,
        setter: Callable,
        choices: Iterable[tuple[str, str]],
        *,
        style: Optional[Style] = None,
    ):
        super().__init__(key, label, self._cycle, style=style)
        self.label = label
        self.getter = getter
        self.setter = setter
        self.choices = tuple(choices)

    def _cycle(self, context) -> None:
        if not self.choices:
            return
        values = [value for value, _name in self.choices]
        current = self.getter(context)
        try:
            index = values.index(current)
        except ValueError:
            index = -1
        self.setter(context, values[(index + 1) % len(values)])

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        context = state.get("_runtime_context")
        value = self.getter(context) if context is not None else ""
        name = next((name for item, name in self.choices if item == value), str(value))
        old = self.text
        self.text = f"{self.label}: {name}  >" if self.label else f"{name}  >"
        try:
            super().draw(renderer, state, theme)
        finally:
            self.text = old


class TextField(Widget):
    interactive = True

    def __init__(
        self,
        key: str,
        getter: Callable,
        setter: Callable,
        *,
        placeholder: str = "",
        style: Optional[Style] = None,
    ):
        base = Style(
            height=38.0,
            min_height=38.0,
            padding=(9.0, 0.0, 9.0, 0.0),
            radius=0.0,
        )
        super().__init__(key, style=style or base)
        self.getter = getter
        self.setter = setter
        self.placeholder = placeholder

    def preferred_size(self, available: Size, state: dict, theme: Theme) -> Size:
        return Size(
            self._clamp_width(
                self.style.width if self.style.width is not None else available.width
            ),
            self._clamp_height(self.style.height or theme.control_height),
        )

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        context = state.get("_runtime_context")
        value = str(self.getter(context) or "") if context is not None else ""
        focused = state.get("_runtime_focus") == self.key
        field_text = theme.text if self.style.ghost else theme.input_text
        field_background = theme.canvas if self.style.ghost else theme.input_surface
        if not self.style.ghost:
            renderer.rect(self.rect, theme.input_surface, radius=theme.radius,
                          border=theme.accent if focused else None,
                          border_width=1.0)
        text_rect = self.rect.inset((12.0, 0.0, 12.0, 0.0))
        if 'search' in self.key.lower():
            from pathlib import Path
            icon = Path(__file__).resolve().parents[2] / 'assets/ui/icons/search.png'
            inset, icon_size, text_inset = (10, 12, 29) if self.style.ghost else (12, 14, 36)
            previous_tint = getattr(renderer, 'icon_tint', None)
            renderer.icon_tint = field_text if theme.follows_blender else None
            try:
                renderer.image(Rect(self.rect.x+inset,self.rect.y+(self.rect.height-icon_size)/2,icon_size,icon_size),str(icon))
            finally:
                renderer.icon_tint = previous_tint
            text_rect = self.rect.inset((text_inset,0.0,12.0,0.0))
        size = self.style.font_size or theme.font_size
        # While typing, keep the end in view: the caret and newest letters sit there.
        shown = _text_tail(renderer, value, text_rect.width, size) if focused and value else value
        renderer.text(
            text_rect,
            shown if value else self.placeholder,
            field_text if value else (blend(field_text,field_background,.25) if theme.follows_blender else theme.text_muted),
            size=size,
            align="LEFT",
        )
        if focused:
            width = renderer.text_width(shown, size)
            caret = Rect(
                min(text_rect.right - 1.0, text_rect.x + width + 1.0),
                text_rect.y + 6.0,
                1.0,
                max(4.0, text_rect.height - 12.0),
            )
            renderer.rect(caret, theme.accent)

    def set_text(self, context, value: str) -> None:
        self.setter(context, value)

    def get_text(self, context) -> str:
        return str(self.getter(context) or "")


def _text_tail(renderer, value, width, size):
    """The end of value that fits width, led by an ellipsis."""
    if not hasattr(renderer, 'text_width') or renderer.text_width(value, size) <= width:
        return value
    low, high = 1, len(value)
    while low < high:
        middle = (low + high) // 2
        if renderer.text_width('...' + value[middle:], size) <= width:
            high = middle
        else:
            low = middle + 1
    return '...' + value[low:]


class NumberStepper(Row):
    def __init__(
        self,
        key: str,
        label: str,
        getter: Callable,
        setter: Callable,
        *,
        step: float = 0.1,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
    ):
        self.getter = getter
        self.setter = setter
        self.step = step
        self.minimum = minimum
        self.maximum = maximum

        def adjust(direction):
            def action(context):
                value = float(getter(context)) + step * direction
                if minimum is not None:
                    value = max(minimum, value)
                if maximum is not None:
                    value = min(maximum, value)
                setter(context, value)
            return action

        value_label = _DynamicLabel(
            f"{key}.value",
            lambda context: f"{label}: {float(getter(context)):.3g}",
            style=Style(height=30.0, min_height=30.0, flex=1.0),
        )
        super().__init__(
            key,
            [
                value_label,
                Button(f"{key}.minus", "-", adjust(-1), style=Style(width=30.0, height=30.0)),
                Button(f"{key}.plus", "+", adjust(1), style=Style(width=30.0, height=30.0)),
            ],
            style=Style(height=30.0, min_height=30.0, gap=5.0),
        )


class _DynamicLabel(Label):
    def __init__(self, key, getter, *, style=None, muted=False):
        super().__init__(key, "", style=style, muted=muted)
        self.getter = getter

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        context = state.get("_runtime_context")
        self.text = str(self.getter(context)) if context is not None else ""
        super().draw(renderer, state, theme)


class VirtualGrid(Widget):
    """Fixed-row gallery that creates widgets only around the scroll viewport.

    Item indices stay in the original sequence so selection/import callbacks
    retain their identity. Full content height is independent of loaded cards.
    """

    def __init__(self, key, items, factory=None, *, column_breakpoint=240,
                 row_height=178, gap=8):
        super().__init__(key, style=Style(gap=gap))
        self.items = items
        self.factory = factory or (lambda item: item)
        self.column_breakpoint = column_breakpoint
        self.row_height = row_height
        self._cards = {}

    def _metrics(self, width):
        columns = 2 if width >= self.column_breakpoint else 1
        card_width = max(0.0, (width - self.style.gap * (columns - 1)) / columns)
        height = self.row_height if self.row_height is not None else card_width + 43
        rows = (len(self.items) + columns - 1) // columns
        return columns, card_width, height, rows

    def preferred_size(self, available, state, theme):
        _, _, height, rows = self._metrics(available.width)
        return Size(available.width, rows * height + max(0, rows - 1) * self.style.gap)

    def layout(self, bounds, state, theme):
        self.rect = bounds
        columns, width, height, rows = self._metrics(bounds.width)
        viewport = self.ancestor(ScrollView)
        clip = viewport.rect if viewport is not None else bounds
        # One extra row above/below prevents churn at the clipping boundary.
        stride = height + self.style.gap
        if bounds.intersect(clip).height <= 0 or not rows:
            first = stop = 0
        else:
            first = max(0, int((bounds.top - clip.top) // stride) - 1)
            stop = min(rows, max(0, int((bounds.top - clip.y) // stride) + 2))
        cards = {}
        for index in range(first * columns, min(len(self.items), stop * columns)):
            card = self._cards.get(index)
            if card is None:
                card = self.factory(self.items[index])
            cards[index] = card
        self._cards = cards
        self.set_children(cards.values())
        for index, card in cards.items():
            row, column = divmod(index, columns)
            card.layout(Rect(bounds.x + column * (width + self.style.gap),
                             bounds.top - row * stride - height, width, height), state, theme)


class ScrollView(Widget):
    interactive = True

    def __init__(
        self,
        key: str,
        child: Widget,
        *,
        style: Optional[Style] = None,
        wheel_step: float = 44.0,
    ):
        base = Style(flex=1.0, min_height=80.0)
        super().__init__(key, style=style or base)
        self.set_children([child])
        self.child = child
        self.wheel_step = wheel_step
        self.content_height = 0.0
        self.scrollbar_track_rect = None
        self.external_scrollbar = False

    def preferred_size(self, available: Size, state: dict, theme: Theme) -> Size:
        height = self.style.height if self.style.height is not None else available.height
        return Size(available.width, self._clamp_height(height))

    def layout(self, bounds: Rect, state: dict, theme: Theme) -> None:
        self.rect = bounds
        preferred = self.child.preferred_size(
            Size(bounds.width, max(bounds.height, 100000.0)),
            state,
            theme,
        )
        self.content_height = max(bounds.height, preferred.height)
        entry = state.setdefault(self.key, {})
        max_scroll = max(0.0, self.content_height - bounds.height)
        entry["scroll"] = min(max(0.0, float(entry.get("scroll", 0.0))), max_scroll)
        offset = entry["scroll"]
        child_bounds = Rect(
            bounds.x,
            bounds.top - self.content_height + offset,
            bounds.width,
            self.content_height,
        )
        self.child.layout(child_bounds, state, theme)
        self.clip_rect = bounds
        self._set_clip(self.child, bounds)

    def _set_clip(self, widget: Widget, clip: Rect) -> None:
        if isinstance(widget, ScrollView):
            clip = clip.intersect(widget.rect)
        widget.clip_rect = clip
        for child in widget.children:
            self._set_clip(child, clip)

    def scrollbar_track(self):
        return self.scrollbar_track_rect or Rect(self.rect.right-7,self.rect.y,5,self.rect.height)

    def scrollbar_contains(self,x,y):
        track=self.scrollbar_track()
        return (self.content_height>self.rect.height + .5 and track.height>0
                and track.x-5<=x<=track.right+2 and track.y<=y<=track.top)

    def scrollbar_thumb(self, state):
        overflow=max(0.,self.content_height-self.rect.height)
        if overflow <= .5 or self.rect.height<=0:
            return None
        track=self.scrollbar_track()
        height=min(track.height,max(36.,track.height*self.rect.height/self.content_height))
        progress=state.setdefault(self.key,{}).get('scroll',0.)/overflow
        return Rect(track.x,track.top-height-(track.height-height)*progress,track.width,height)

    def pointer_scrollbar(self,event,x,y,state):
        thumb=self.scrollbar_thumb(state)
        if thumb is None:
            return False
        entry=state.setdefault(self.key,{})
        if event.type=='LEFTMOUSE' and event.value=='PRESS':
            if not self.scrollbar_contains(x,y):
                return False
            offset=y-thumb.y if thumb.contains(x,y) else thumb.height/2
            state['_scroll_drag']=(self.key,offset)
        drag=state.get('_scroll_drag')
        if not drag or drag[0]!=self.key:
            return False
        if event.type=='LEFTMOUSE' and event.value=='RELEASE':
            state.pop('_scroll_drag',None)
            return True
        if event.type=='MOUSEMOVE' or (event.type=='LEFTMOUSE' and event.value=='PRESS'):
            track=self.scrollbar_track()
            travel=max(1.,track.height-thumb.height)
            progress=(track.top-thumb.height-(y-drag[1]))/travel
            entry['scroll']=min(1.,max(0.,progress))*(self.content_height-self.rect.height)
            return True
        return False

    def draw(self, renderer, state: dict, theme: Theme) -> None:
        renderer.push_clip(self.rect)
        self.child.draw(renderer, state, theme)
        renderer.pop_clip()
        if not self.external_scrollbar:
            self.draw_scrollbar(renderer, state)

    def draw_scrollbar(self, renderer, state):
        thumb=self.scrollbar_thumb(state)
        if thumb:
            dragging=state.get('_scroll_drag',(None,))[0]==self.key
            mouse = state.get('_runtime_mouse')
            shade=58/255 if dragging else 42/255
            renderer.rect(thumb,(shade,shade,shade,1),radius=2.5)

    def hit_test(self, x: float, y: float) -> Optional[Widget]:
        if self.clip_rect is not None and not self.clip_rect.contains(x, y):
            return None
        if not self.rect.contains(x, y):
            return None
        if self.scrollbar_contains(x,y):
            return self
        hit = self.child.hit_test(x, y)
        return hit or self

    def handle_wheel(self, delta: float, state: dict) -> bool:
        return self.scroll_by(delta * self.wheel_step, state)

    def scroll_by(self, delta: float, state: dict) -> bool:
        """Scroll by logical pixels, preserving small trackpad movements."""
        entry = state.setdefault(self.key, {})
        max_scroll = max(0.0, self.content_height - self.rect.height)
        old = float(entry.get("scroll", 0.0))
        entry["scroll"] = min(max_scroll, max(0.0, old + delta))
        return entry["scroll"] != old
