"""Budgeted execution of Bedrock particle event graphs."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

from .diagnostics import Diagnostic, DiagnosticBag, Severity, SourceRef
from .ir import FrozenDict, thaw_json
from .registry import Registry, SupportLevel


class RandomSource(Protocol):
    def sample(self, *tokens: object) -> float:
        ...


ExpressionResolver = Callable[[Any, "EventContext"], float]


def literal_event_resolver(value: Any, context: "EventContext") -> float:
    del context
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return 0.0


@dataclass(frozen=True)
class EventActionSpec:
    name: str
    support: SupportLevel
    notes: str = ""


def create_default_event_action_registry() -> Registry[EventActionSpec]:
    registry: Registry[EventActionSpec] = Registry()
    registry.register(
        "sequence",
        EventActionSpec(
            "sequence",
            SupportLevel.IMPLEMENTED,
            "Ordered graph traversal is implemented in the core.",
        ),
    )
    registry.register(
        "randomize",
        EventActionSpec(
            "randomize",
            SupportLevel.IMPLEMENTED,
            "Weighted deterministic selection is implemented in the core.",
        ),
    )
    for name in ("expression", "log", "particle_effect", "sound_effect"):
        registry.register(
            name,
            EventActionSpec(
                name,
                SupportLevel.INFRASTRUCTURE,
                "Validated and budgeted; host integration supplies the action handler.",
            ),
        )
    return registry.freeze()


DEFAULT_EVENT_ACTION_REGISTRY = create_default_event_action_registry()


@dataclass(frozen=True)
class EventBudget:
    max_depth: int = 16
    max_events: int = 4096
    max_same_event: int = 64
    max_actions: int = 8192
    max_emitter_spawns: int = 512
    max_particle_spawns: int = 8192

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not isinstance(value, int) or value < 1:
                raise ValueError("%s must be a positive integer" % name)


@dataclass
class EventCounters:
    events: int = 0
    actions: int = 0
    emitter_spawns: int = 0
    particle_spawns: int = 0
    suppressed: int = 0
    visits: Counter = field(default_factory=Counter)


@dataclass(frozen=True)
class EventContext:
    effect_identifier: str = ""
    emitter_id: int = 0
    particle_id: Optional[int] = None
    variables: Mapping[str, Any] = field(default_factory=dict)
    source: SourceRef = SourceRef()


@dataclass(frozen=True)
class EventAction:
    kind: str
    payload: Any
    event_name: str
    depth: int
    source: SourceRef


@dataclass(frozen=True)
class EventDispatchResult:
    actions: Tuple[EventAction, ...]
    diagnostics: Tuple[Diagnostic, ...]
    counters: EventCounters


EventHandler = Callable[[EventAction, EventContext, "EventSession"], None]


class EventExecutor:
    """Reusable event graph executor.

    Graph structure and budgets are core-owned.  Rendering, sound, nested effect
    creation, and Molang side effects are delegated through handlers/providers.
    """

    def __init__(
        self,
        events: Mapping[str, Any],
        random: RandomSource,
        *,
        expression_resolver: ExpressionResolver = literal_event_resolver,
        budget: EventBudget = EventBudget(),
        action_specs: Registry[
            EventActionSpec
        ] = DEFAULT_EVENT_ACTION_REGISTRY,
    ) -> None:
        self.events = events
        self.random = random
        self.expression_resolver = expression_resolver
        self.budget = budget
        self.action_specs = action_specs
        self.handlers: Dict[str, EventHandler] = {}

    def register_handler(
        self, action_kind: str, handler: EventHandler, *, replace: bool = False
    ) -> None:
        if action_kind in self.handlers and not replace:
            raise ValueError("Event handler already registered: %s" % action_kind)
        self.handlers[action_kind] = handler

    def dispatch(
        self, event_name: str, context: EventContext = EventContext()
    ) -> EventDispatchResult:
        session = EventSession(self, context)
        session.dispatch(event_name)
        return EventDispatchResult(
            tuple(session.actions),
            session.diagnostics.snapshot(),
            session.counters,
        )


class EventSession:
    """One event dispatch transaction, exposed to integration handlers."""

    def __init__(self, executor: EventExecutor, context: EventContext) -> None:
        self.executor = executor
        self.context = context
        self.counters = EventCounters()
        self.actions = []
        self.diagnostics = DiagnosticBag()

    def _suppress(
        self, code: str, message: str, source: SourceRef, severity=Severity.WARNING
    ) -> None:
        self.counters.suppressed += 1
        self.diagnostics.add(code, message, severity, source)

    def dispatch(self, event_name: str, depth: int = 0) -> None:
        source = self.context.source.child("events", event_name)
        budget = self.executor.budget
        if depth > budget.max_depth:
            self._suppress(
                "particle.event.depth_budget",
                "Event depth budget exceeded at %s" % event_name,
                source,
                Severity.ERROR,
            )
            return
        if self.counters.events >= budget.max_events:
            self._suppress(
                "particle.event.count_budget",
                "Event execution budget exceeded",
                source,
                Severity.ERROR,
            )
            return
        if self.counters.visits[event_name] >= budget.max_same_event:
            self._suppress(
                "particle.event.recursion_budget",
                "Repeated event budget exceeded for %s" % event_name,
                source,
                Severity.ERROR,
            )
            return
        try:
            node = self.executor.events[event_name]
        except KeyError:
            self._suppress(
                "particle.event.unknown",
                "Unknown particle event: %s" % event_name,
                source,
            )
            return
        self.counters.events += 1
        self.counters.visits[event_name] += 1
        self._execute_node(node, event_name, depth, source)

    def _execute_node(
        self, raw_node: Any, event_name: str, depth: int, source: SourceRef
    ) -> None:
        if isinstance(raw_node, FrozenDict):
            node = dict(raw_node.items())
        else:
            node = raw_node
        if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for index, child in enumerate(node):
                self._execute_node(
                    child, event_name, depth + 1, source.child("sequence", index)
                )
            return
        if not isinstance(node, Mapping):
            self._suppress(
                "particle.event.node_type",
                "Event node must be an object or sequence",
                source,
            )
            return

        # Preserve JSON member order. This matters when expression actions mutate
        # variables before a later host-side effect action is handled.
        for kind, raw_payload in node.items():
            action_source = source.child(kind)
            if kind == "sequence":
                if not isinstance(raw_payload, Sequence) or isinstance(
                    raw_payload, (str, bytes)
                ):
                    self._suppress(
                        "particle.event.sequence_type",
                        "sequence must be an array",
                        action_source,
                    )
                    continue
                for index, child in enumerate(raw_payload):
                    self._execute_node(
                        child,
                        event_name,
                        depth + 1,
                        action_source.child(index),
                    )
            elif kind == "randomize":
                self._execute_randomize(
                    raw_payload, event_name, depth + 1, action_source
                )
            else:
                self._emit_action(kind, raw_payload, event_name, depth, action_source)

    def _execute_randomize(
        self, choices: Any, event_name: str, depth: int, source: SourceRef
    ) -> None:
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)):
            self._suppress(
                "particle.event.randomize_type",
                "randomize must be an array",
                source,
            )
            return
        weighted = []
        total = 0.0
        for index, choice in enumerate(choices):
            if isinstance(choice, FrozenDict):
                choice = dict(choice.items())
            if not isinstance(choice, Mapping):
                self._suppress(
                    "particle.event.randomize_choice_type",
                    "randomize choice must be an object",
                    source.child(index),
                )
                continue
            weight = self.executor.expression_resolver(
                choice.get("weight", 1.0), self.context
            )
            if not math.isfinite(weight) or weight <= 0.0:
                continue
            node = {key: value for key, value in choice.items() if key != "weight"}
            total += weight
            weighted.append((total, node, index))
        if not weighted or total <= 0.0:
            self._suppress(
                "particle.event.randomize_empty",
                "randomize has no positive-weight choices",
                source,
            )
            return
        roll = self.executor.random.sample(
            "particle-event",
            self.context.effect_identifier,
            self.context.emitter_id,
            self.context.particle_id,
            event_name,
            self.counters.events,
            depth,
        ) * total
        selected_total, selected, selected_index = weighted[-1]
        for cumulative, candidate, index in weighted:
            if roll < cumulative:
                selected_total, selected, selected_index = (
                    cumulative,
                    candidate,
                    index,
                )
                break
        del selected_total
        self._execute_node(
            selected, event_name, depth, source.child(selected_index)
        )

    def _emit_action(
        self,
        kind: str,
        payload: Any,
        event_name: str,
        depth: int,
        source: SourceRef,
    ) -> None:
        budget = self.executor.budget
        if self.counters.actions >= budget.max_actions:
            self._suppress(
                "particle.event.action_budget",
                "Event action budget exceeded",
                source,
                Severity.ERROR,
            )
            return
        spec = self.executor.action_specs.get(kind)
        if spec is None:
            self._suppress(
                "particle.event.action_unknown",
                "Unknown event action %s was not executed" % kind,
                source,
            )
            return
        thawed = thaw_json(payload) if isinstance(payload, (FrozenDict, tuple)) else payload
        if kind == "particle_effect" and isinstance(thawed, Mapping):
            effect_type = thawed.get("type", "emitter")
            if effect_type in ("emitter", "emitter_bound"):
                if self.counters.emitter_spawns >= budget.max_emitter_spawns:
                    self._suppress(
                        "particle.event.emitter_spawn_budget",
                        "Nested emitter spawn budget exceeded",
                        source,
                        Severity.ERROR,
                    )
                    return
                self.counters.emitter_spawns += 1
            elif effect_type in ("particle", "particle_with_velocity"):
                if self.counters.particle_spawns >= budget.max_particle_spawns:
                    self._suppress(
                        "particle.event.particle_spawn_budget",
                        "Nested particle spawn budget exceeded",
                        source,
                        Severity.ERROR,
                    )
                    return
                self.counters.particle_spawns += 1
        action = EventAction(kind, thawed, event_name, depth, source)
        self.counters.actions += 1
        self.actions.append(action)
        handler = self.executor.handlers.get(kind)
        if handler is not None:
            handler(action, self.context, self)

