"""Pack-list dragging; preview the order locally and commit only on release."""
import time

from .gpu_framework import Column, Rect, ScrollView, Style


class SchematicPackList(Column):
    def __init__(self, rows, paths, row_height=60, gap=6):
        super().__init__('schematic.stack', rows, style=Style(gap=gap))
        self.row_height, self.step = row_height, row_height+gap
        self.paths = tuple(paths)
        self.rows = dict(zip(self.paths, rows))

    def layout(self, bounds, state, theme):
        drag = state.get('_schematic_pack_drag')
        order = list(self.paths)
        if drag and drag['order'] == self.paths and drag['active']:
            order.remove(drag['path'])
            order.insert(drag['target'], drag['path'])
        self.set_children([self.rows[path] for path in order])
        super().layout(bounds, state, theme)
        if not drag or drag['order'] != self.paths or not drag['active']:
            return
        # Rows that slide aside animate themselves (tween); only auto-scroll
        # needs frames of its own. A row held still no longer redraws at 60 Hz.
        from .gpu_framework.motion import tween
        for slot, path in enumerate(order):
            if path == drag['path']:
                continue
            row = self.rows[path]
            position = tween(state, 'schematic.drag.'+path, slot, .12, initial=self.paths.index(path))
            row.layout(Rect(row.rect.x, self.rect.top-position*self.step-self.row_height,
                            row.rect.width, self.row_height), state, theme)
        row = self.rows[drag['path']]
        self.drop_rect = row.rect
        row.layout(Rect(row.rect.x, drag['y'] - drag['offset'], row.rect.width, row.rect.height), state, theme)
        scroll = self.ancestor(ScrollView)
        now = time.monotonic()
        dt = min(.05, max(0, now - drag['tick']))
        drag['tick'] = now
        if scroll:
            edge = 36
            speed = (min(1., (scroll.rect.y+edge-drag['y'])/edge) if drag['y'] < scroll.rect.y+edge
                     else -min(1., (drag['y']-scroll.rect.top+edge)/edge) if drag['y'] > scroll.rect.top-edge else 0.)
            entry = state.setdefault(scroll.key, {})
            entry['scroll'] = max(0., min(scroll.content_height-scroll.rect.height,
                                         entry.get('scroll', 0.) + speed*360*dt))
            if speed:
                state['_motion_active'] = True
        self._target(drag)

    def _target(self, drag):
        # Use the held row's center, independent of its current preview slot.
        center = drag['y'] - drag['offset'] + self.row_height/2
        drag['target'] = max(0, min(len(self.paths)-1, int((self.rect.top-center)/self.step)))

    def draw(self, renderer, state, theme):
        drag = state.get('_schematic_pack_drag')
        if not drag or not drag['active'] or drag['path'] not in self.rows:
            return super().draw(renderer, state, theme)
        held = self.rows[drag['path']]
        for row in self.children:
            if row is not held and (row.clip_rect is None or row.rect.intersect(row.clip_rect).height > 0):
                row.draw(renderer, state, theme)
        slot = getattr(self, 'drop_rect', held.rect)
        renderer.rect(slot, theme.surface_raised, radius=4)
        renderer.rect(held.rect, theme.surface, radius=4)
        held.draw(renderer, state, theme)

    def pointer(self, context, event, x, y, state):
        drag = state.get('_schematic_pack_drag')
        if drag:
            if drag['order'] != tuple(p.path for p in context.scene.mcbedrock_active_packs):
                state.pop('_schematic_pack_drag', None)
                return True
            if event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'}:
                state.pop('_schematic_pack_drag', None)
                return True
            if event.type == 'MOUSEMOVE':
                drag['x'], drag['y'] = x, y
                drag['active'] |= abs(y-drag['start_y']) > 4
                self._target(drag)
                return True
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                state.pop('_schematic_pack_drag', None)
                clip = self.ancestor(ScrollView).rect
                if drag['active'] and clip.contains(x, y):
                    import bpy
                    bpy.ops.mcbedrock.reorder_schematic_pack('EXEC_DEFAULT',
                        pack_path=drag['path'], target_index=drag['target'])
                return True
            return event.type != 'TIMER'
        if event.type != 'LEFTMOUSE' or event.value != 'PRESS':
            return False
        for index, path in enumerate(self.paths):
            row = self.rows[path]
            card = row.children[0]
            if card.rect.contains(x, y) and (card.clip_rect is None or card.clip_rect.contains(x, y)):
                context.scene.mcbedrock_active_pack_index = index
                for key in list(state.get('_motion', {})):
                    if key.startswith('schematic.drag.'):
                        state['_motion'].pop(key)
                state['_schematic_pack_drag'] = dict(path=path, order=self.paths,
                    x=x, y=y, start_y=y, offset=y-row.rect.y, active=False,
                    target=index, tick=time.monotonic())
                state.pop('_runtime_pressed', None)
                state.pop('_runtime_hover', None)
                state.pop('_hover_since', None)
                return True
        return False
