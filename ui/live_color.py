"""Colour dialogs that change the scene while the picker moves.

A colour operator opens Blender's own picker on its own `color`. Every change
the picker makes (dragging the wheel or the value slider, typing a value)
runs that property's update, which writes the colour into the scene at once,
so the viewport follows the wheel. OK keeps it, and the operator's one undo
step holds the whole change: the live writes push none. The Cancel button,
Esc and a click outside the dialog all run cancel() (checked in Blender 5.2,
after a drag too), which puts back what was there when the dialog opened.
Moving the pointer away leaves the dialog open.
"""
from time import monotonic

import bpy

INTERVAL = 1 / 30       # live writes per second at most, as the sliders preview
_sessions = {}          # the open dialog: its operator's properties pointer -> _Session


class _Session:
    """One open dialog: the operator's class, what to put back and a colour still to write."""

    def __init__(self, operator, state):
        self.operator, self.state = operator, state
        self.pending, self.written = None, 0.
        self.later = self._later      # one bound method, so the timer can be found again

    def change(self, color):
        self.pending = color
        wait = INTERVAL - (monotonic() - self.written)
        if wait <= 0:
            self.flush()
        elif not bpy.app.timers.is_registered(self.later):
            bpy.app.timers.register(self.later, first_interval=wait)

    def flush(self):
        color, self.pending = self.pending, None
        if color is not None:
            self.written = monotonic()
            self.operator.apply(bpy.context, self.state, color)

    def _later(self):
        # A drag's last colour still lands when the pointer stops between writes.
        if self in _sessions.values():
            self.flush()
        return None

    def close(self):
        self.pending = None
        if bpy.app.timers.is_registered(self.later):
            bpy.app.timers.unregister(self.later)


def _close_all():
    for session in _sessions.values():
        session.close()
    _sessions.clear()


def color_changed(self, context):
    """The dialog's colour changed: show it in the scene now.

    self is the operator's properties (its pointer is the one invoke()
    stored). Values passed to the operator by a script land here too, before
    any dialog exists, and find no session."""
    session = _sessions.get(self.as_pointer())
    if session is not None:
        session.change(tuple(self.color)[:3])


def set_color(owner, name, color):
    """Write a colour unless it is already there: every write runs the setting's update."""
    old = tuple(getattr(owner, name))[:3]
    if any(abs(a - b) > 1e-7 for a, b in zip(old, color)):
        setattr(owner, name, color)


class LiveColor:
    """Mixin for colour operators: Blender's picker in a dialog, followed live.

    A subclass gives begin() (what self.field edits, and its colour now),
    apply() (write a colour) and restore() (put back what begin() found).
    """
    field: bpy.props.StringProperty(options={'HIDDEN'})
    color: bpy.props.FloatVectorProperty(name='Color', subtype='COLOR', size=3, min=0, max=1, update=color_changed)

    def begin(self, context):
        """(state, current colour) for self.field, or None when it cannot be chosen."""
        raise NotImplementedError

    @classmethod
    def apply(cls, context, state, color):
        raise NotImplementedError

    @classmethod
    def restore(cls, context, state):
        raise NotImplementedError

    def invoke(self, context, event):
        begun = self.begin(context)
        if begun is None:
            return {'CANCELLED'}
        state, color = begun
        _close_all()            # one dialog at a time; forget any Blender closed unseen
        self.color = color      # no session yet: opening is not a change
        _sessions[self.properties.as_pointer()] = _Session(type(self), state)
        return context.window_manager.invoke_props_dialog(self, width=280)

    def draw(self, context):
        self.layout.template_color_picker(self, 'color', value_slider=True)
        self.layout.prop(self, 'color', text='')

    def execute(self, context):
        session = _sessions.pop(self.properties.as_pointer(), None)
        if session is not None:
            session.close()
            state = session.state
        else:
            # Without the dialog (scripts, tests, Repeat Last): begin here.
            begun = self.begin(context)
            if begun is None:
                return {'CANCELLED'}
            state = begun[0]
        type(self).apply(context, state, tuple(self.color)[:3])
        return {'FINISHED'}

    def cancel(self, context):
        session = _sessions.pop(self.properties.as_pointer(), None)
        if session is not None:
            session.close()
            session.operator.restore(context, session.state)


class SettingColor(LiveColor):
    """A colour on one of the scene's settings groups: `settings` names the
    group (a Scene attribute) and `fields` the colours it may set."""
    settings = ''
    fields = frozenset()

    def begin(self, context):
        owner = getattr(context.scene, self.settings, None)
        if owner is None or self.field not in self.fields:
            return None
        value = tuple(getattr(owner, self.field))[:3]
        return {'scene': context.scene, 'field': self.field, 'original': value}, value

    @classmethod
    def owner(cls, state):
        return getattr(state['scene'], cls.settings, None)

    @classmethod
    def apply(cls, context, state, color):
        owner = cls.owner(state)
        if owner is not None:
            set_color(owner, state['field'], color)

    @classmethod
    def restore(cls, context, state):
        cls.apply(context, state, state['original'])
