"""Safe, typed, version-aware interpreter for Minecraft Bedrock MoLang.

This module deliberately interprets an AST. It never emits or executes Python
source. ``molang_parser`` re-exports the public API for existing add-on users.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import random
import re
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

try:
    from .molang_metadata import DEFAULT_MOLANG_METADATA, MetadataError
except ImportError:
    from molang_metadata import DEFAULT_MOLANG_METADATA, MetadataError


DEFAULT_ENGINE_VERSION = (1, 26, 30)
MAX_LOOP_ITERATIONS = 1024
DEFAULT_MAX_STEPS = 100_000


@dataclass(frozen=True)
class SourceSpan:
    start: int
    end: int
    line: int
    column: int


@dataclass(frozen=True)
class MoLangDiagnostic:
    message: str
    span: SourceSpan
    code: str = "molang.error"
    severity: str = "error"


class MoLangError(Exception):
    def __init__(self, message, span=None, code="molang.error"):
        super().__init__(message)
        self.diagnostic = MoLangDiagnostic(message, span or SourceSpan(0, 0, 1, 1), code)


class MoLangSyntaxError(MoLangError):
    pass


class MoLangRuntimeError(MoLangError):
    pass


class _UnresolvedError(MoLangRuntimeError):
    pass


class _Missing:
    def __repr__(self):
        return "MOLANG_MISSING"


MOLANG_MISSING = _Missing()


class _Unknown:
    """Preview value whose dependent expression cannot yet be decided."""
    def _fail(self, *_args, **_kwargs):
        raise ValueError("MoLang value is unavailable in this preview context")
    __bool__ = __float__ = __eq__ = __ne__ = __lt__ = __le__ = _fail
    __gt__ = __ge__ = __add__ = __radd__ = __sub__ = __rsub__ = _fail
    __mul__ = __rmul__ = __truediv__ = __rtruediv__ = _fail
    def __repr__(self):
        return "MOLANG_UNKNOWN"


MOLANG_UNKNOWN = _Unknown()


@dataclass(frozen=True)
class ResourceRef:
    kind: str
    identifier: str
    value: Any = None


@dataclass
class ActorRef:
    context: MutableMapping[str, Any]
    value: Any = None


class ActorArray(list):
    pass


def _version(value):
    if value is None:
        return DEFAULT_ENGINE_VERSION
    parts = value.split(".") if isinstance(value, str) else list(value if isinstance(value, Sequence) else [value])
    result = []
    for part in parts[:3]:
        match = re.match(r"\d+", str(part))
        result.append(int(match.group()) if match else 0)
    return tuple((result + [0, 0, 0])[:3])


def _joined(first, last):
    return SourceSpan(first.start, last.end, first.line, first.column)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    value: Any
    span: SourceSpan


class Lexer:
    """Contiguous lexer: unsupported source is diagnosed, never skipped."""
    NUMBER = re.compile(r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fF]?")
    IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
    OPS = (("??", "COALESCE"), ("&&", "AND"), ("||", "OR"),
           ("==", "EQ"), ("!=", "NE"), ("<=", "LE"), (">=", "GE"),
           ("->", "ARROW"), ("+", "PLUS"), ("-", "MINUS"), ("*", "STAR"),
           ("/", "SLASH"), ("!", "NOT"), ("=", "ASSIGN"), ("<", "LT"),
           (">", "GT"), ("?", "QUESTION"), (":", "COLON"), ("(", "LPAREN"),
           (")", "RPAREN"), ("[", "LBRACKET"), ("]", "RBRACKET"),
           ("{", "LBRACE"), ("}", "RBRACE"), (",", "COMMA"), (";", "SEMI"))

    def __init__(self, source):
        self.source = str(source)
        self.tokens_list = self.tokenize()

    def span(self, start, end):
        prefix = self.source[:start]
        line_start = prefix.rfind("\n")
        return SourceSpan(start, end, prefix.count("\n") + 1,
                          start + 1 if line_start < 0 else start - line_start)

    def tokenize(self):
        out, pos, length = [], 0, len(self.source)
        while pos < length:
            ch = self.source[pos]
            if ch.isspace():
                pos += 1
                continue
            if ch == "'":
                end = self.source.find("'", pos + 1)
                if end < 0:
                    raise MoLangSyntaxError("Unterminated string literal", self.span(pos, length), "molang.unterminated_string")
                text = self.source[pos:end + 1]
                out.append(Token("STRING", text, text[1:-1], self.span(pos, end + 1)))
                pos = end + 1
                continue
            match = self.NUMBER.match(self.source, pos)
            if match:
                text = match.group()
                out.append(Token("NUMBER", text, float(text[:-1] if text[-1:] in "fF" else text), self.span(pos, match.end())))
                pos = match.end()
                continue
            match = self.IDENT.match(self.source, pos)
            if match:
                text = match.group()
                out.append(Token("IDENT", text, text.lower(), self.span(pos, match.end())))
                pos = match.end()
                continue
            for text, kind in self.OPS:
                if self.source.startswith(text, pos):
                    out.append(Token(kind, text, text, self.span(pos, pos + len(text))))
                    pos += len(text)
                    break
            else:
                raise MoLangSyntaxError("Unexpected character {!r}".format(ch), self.span(pos, pos + 1), "molang.unexpected_character")
        out.append(Token("EOF", "", None, self.span(length, length)))
        return out


@dataclass(frozen=True)
class Node:
    kind: str
    value: Any = None
    children: Tuple["Node", ...] = ()
    span: SourceSpan = field(default_factory=lambda: SourceSpan(0, 0, 1, 1))


@dataclass(frozen=True)
class MoLangProgram:
    source: str
    statements: Tuple[Node, ...]
    min_engine_version: Tuple[int, int, int]
    simple_expression: bool


class Parser:
    """Recursive-descent parser with documented legacy precedence gates."""
    def __init__(self, tokens: Iterable[Token], min_engine_version=None, source=""):
        self.tokens, self.pos = list(tokens), 0
        self.min_engine_version, self.source = _version(min_engine_version), source

    @property
    def current(self):
        return self.tokens[self.pos]

    def take(self, kind=None):
        token = self.current
        if kind and token.kind != kind:
            raise MoLangSyntaxError("Expected {}, got {} {!r}".format(kind, token.kind, token.text), token.span, "molang.expected_token")
        self.pos += 1
        return token

    def match(self, *kinds):
        return self.take() if self.current.kind in kinds else None

    def parse(self):
        statements, terminated = self.statement_list("EOF")
        self.take("EOF")
        simple = len(statements) == 1 and not terminated and statements[0].kind == "expression"
        return MoLangProgram(self.source, tuple(statements), self.min_engine_version, simple)

    def statement_list(self, end):
        statements, any_semi, last_semi = [], False, False
        while self.current.kind != end:
            if self.current.kind == "EOF":
                raise MoLangSyntaxError("Expected {} before end of expression".format(end), self.current.span, "molang.unterminated_block")
            statements.append(self.statement())
            last_semi = bool(self.match("SEMI"))
            any_semi |= last_semi
            if not last_semi and self.current.kind != end:
                raise MoLangSyntaxError("Expected ';' between statements", self.current.span, "molang.expected_semicolon")
        if len(statements) > 1 and not last_semi:
            raise MoLangSyntaxError("Every complex statement must end with ';'", statements[-1].span, "molang.expected_semicolon")
        return statements, any_semi

    def statement(self):
        if self.current.kind == "IDENT" and self.current.value in {"return", "break", "continue"}:
            token = self.take()
            if token.value in {"break", "continue"}:
                return Node(token.value, span=token.span)
            value = Node("number", 0.0, span=token.span) if self.current.kind in {"SEMI", "RBRACE", "EOF"} else self.assignment()
            return Node("return", children=(value,), span=_joined(token.span, value.span))
        value = self.assignment()
        return Node("expression", children=(value,), span=value.span)

    def assignment(self):
        left = self.coalesce()
        if self.match("ASSIGN"):
            right = self.assignment()
            return Node("assign", children=(left, right), span=_joined(left.span, right.span))
        return left

    def coalesce(self):
        node = self.ternary()
        while self.match("COALESCE"):
            right = self.ternary()
            node = Node("coalesce", children=(node, right), span=_joined(node.span, right.span))
        return node

    def ternary(self):
        node = self.logical()
        if self.min_engine_version < (1, 18, 10):
            while self.match("QUESTION"):
                yes = self.assignment()
                if self.match("COLON"):
                    no = self.logical()
                    node = Node("ternary", children=(node, yes, no), span=_joined(node.span, no.span))
                else:
                    node = Node("binary_conditional", children=(node, yes), span=_joined(node.span, yes.span))
            return node
        if self.match("QUESTION"):
            yes = self.assignment()
            if self.match("COLON"):
                no = self.ternary()
                return Node("ternary", children=(node, yes, no), span=_joined(node.span, no.span))
            return Node("binary_conditional", children=(node, yes), span=_joined(node.span, yes.span))
        return node

    def logical(self):
        if self.min_engine_version < (1, 18, 20):
            node = self.legacy_comparison()
            while self.current.kind in {"AND", "OR"}:
                op, right = self.take(), self.legacy_comparison()
                node = Node("binary", op.text, (node, right), _joined(node.span, right.span))
            return node
        return self.logical_or()

    def logical_or(self):
        node = self.logical_and()
        while self.current.kind == "OR":
            op, right = self.take(), self.logical_and()
            node = Node("binary", op.text, (node, right), _joined(node.span, right.span))
        return node

    def logical_and(self):
        node = self.equality()
        while self.current.kind == "AND":
            op, right = self.take(), self.equality()
            node = Node("binary", op.text, (node, right), _joined(node.span, right.span))
        return node

    def equality(self):
        node = self.relational()
        while self.current.kind in {"EQ", "NE"}:
            op, right = self.take(), self.relational()
            node = Node("binary", op.text, (node, right), _joined(node.span, right.span))
        return node

    def relational(self):
        node = self.additive()
        while self.current.kind in {"LT", "GT", "LE", "GE"}:
            op, right = self.take(), self.additive()
            node = Node("binary", op.text, (node, right), _joined(node.span, right.span))
        return node

    def legacy_comparison(self):
        node = self.additive()
        while self.current.kind in {"EQ", "NE", "LT", "GT", "LE", "GE"}:
            op, right = self.take(), self.additive()
            node = Node("binary", op.text, (node, right), _joined(node.span, right.span))
        return node

    def additive(self):
        node = self.multiplicative()
        while self.current.kind in {"PLUS", "MINUS"}:
            op, right = self.take(), self.multiplicative()
            node = Node("binary", op.text, (node, right), _joined(node.span, right.span))
        return node

    def multiplicative(self):
        node = self.unary()
        while self.current.kind in {"STAR", "SLASH"}:
            op, right = self.take(), self.unary()
            node = Node("binary", op.text, (node, right), _joined(node.span, right.span))
        return node

    def unary(self):
        if self.current.kind in {"PLUS", "MINUS", "NOT"}:
            op, value = self.take(), self.unary()
            return Node("unary", op.text, (value,), _joined(op.span, value.span))
        return self.arrow()

    def arrow(self):
        node = self.postfix()
        if self.match("ARROW"):
            right = self.unary()
            return Node("arrow", children=(node, right), span=_joined(node.span, right.span))
        return node

    def postfix(self):
        node = self.primary()
        while self.match("LBRACKET"):
            index, end = self.assignment(), self.take("RBRACKET")
            node = Node("index", children=(node, index), span=_joined(node.span, end.span))
        return node

    def primary(self):
        token = self.current
        if token.kind in {"NUMBER", "STRING"}:
            self.take()
            return Node(token.kind.lower(), token.value, span=token.span)
        if token.kind == "LPAREN":
            start, node = self.take().span, self.assignment()
            end = self.take("RPAREN")
            return Node("group", children=(node,), span=_joined(start, end.span))
        if token.kind == "LBRACE":
            start = self.take().span
            statements, _terminated = self.statement_list("RBRACE")
            end = self.take("RBRACE")
            return Node("block", children=tuple(statements), span=_joined(start, end.span))
        if token.kind == "IDENT":
            self.take()
            if token.value in {"break", "continue"}:
                return Node(token.value, span=token.span)
            node = Node("identifier", token.value, span=token.span)
            if self.match("LPAREN"):
                args = []
                if self.current.kind != "RPAREN":
                    args.append(self.assignment())
                    while self.match("COMMA"):
                        args.append(self.assignment())
                end = self.take("RPAREN")
                node = Node("call", token.value, tuple(args), _joined(token.span, end.span))
            return node
        raise MoLangSyntaxError("Unexpected token {} {!r}".format(token.kind, token.text), token.span, "molang.unexpected_token")


class _Return(Exception):
    def __init__(self, value):
        self.value = value


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


SCOPES = {"c": ("c", "context"), "context": ("c", "context"),
          "v": ("v", "variable"), "variable": ("v", "variable"),
          "q": ("q", "query"), "query": ("q", "query"),
          "t": ("t", "temp"), "temp": ("t", "temp")}
WRITABLE = {"v", "variable", "t", "temp"}
RESOURCES = {"array", "geometry", "material", "texture"}
PURE_QUERIES = {"all", "any", "approx_eq", "count", "in_range", "combine_entities"}




class MoLangRuntime:
    """AST interpreter over typed host values with bounded execution."""
    def __init__(self, context=None, *, rng=None, seed=None, diagnostics=None,
                 max_steps=DEFAULT_MAX_STEPS, engine_version=None,
                 query_set="default", metadata=None, fresh_temp=False):
        self.context = context if isinstance(context, MutableMapping) else {}
        self.rng = rng if rng is not None else (random.Random(seed) if seed is not None else random)
        self.sink, self.diagnostics = diagnostics, []
        self.engine_version = _version(engine_version)
        self.query_set = query_set
        self.metadata = metadata or DEFAULT_MOLANG_METADATA
        self.max_steps, self.steps, self.context_stack = max(1, int(max_steps)), 0, []
        self.ensure_aliases(reset_temp=fresh_temp)

    def ensure_aliases(self, reset_temp=False):
        for short, long_name in (("c", "context"), ("v", "variable"),
                                 ("q", "query"), ("t", "temp")):
            short_key = self.mapping_key(self.context, short)
            long_key = self.mapping_key(self.context, long_name)
            bucket = None if reset_temp and short == "t" else (
                self.context.get(short_key) if short_key is not None else None
            )
            if not isinstance(bucket, MutableMapping):
                bucket = None if reset_temp and short == "t" else (
                    self.context.get(long_key) if long_key is not None else None
                )
            if not isinstance(bucket, MutableMapping):
                bucket = {}
            self.context[short] = self.context[long_name] = bucket

    def emit(self, message, span, code="molang.runtime"):
        diagnostic = MoLangDiagnostic(message, span, code)
        self.diagnostics.append(diagnostic)
        if isinstance(self.sink, list):
            self.sink.append(diagnostic)
        elif callable(self.sink):
            self.sink(diagnostic)

    def step(self, node):
        self.steps += 1
        if self.steps > self.max_steps:
            raise MoLangRuntimeError("MoLang instruction budget exceeded", node.span, "molang.instruction_budget")

    def evaluate(self, program):
        result = 0.0
        try:
            for statement in program.statements:
                result = self.eval(statement)
        except _Return as signal:
            return signal.value
        except (_Break, _Continue) as signal:
            keyword = "break" if isinstance(signal, _Break) else "continue"
            raise MoLangRuntimeError("{} used outside a loop".format(keyword), code="molang.invalid_control_flow")
        if program.simple_expression and result is MOLANG_MISSING:
            span = program.statements[0].span if program.statements else SourceSpan(0, 0, 1, 1)
            self.emit("Missing reference evaluates to 0", span, "molang.missing_reference")
        return result if program.simple_expression else 0.0

    def eval(self, node):
        self.step(node)
        kind = node.kind
        if kind in {"number", "string"}:
            return node.value
        if kind in {"group", "expression"}:
            return self.eval(node.children[0])
        if kind == "identifier":
            return self.resolve(node.value, node)
        if kind == "assign":
            value = self.eval(node.children[1])
            self.assign(node.children[0], value, node)
            return value
        if kind == "unary":
            value = self.eval(node.children[0])
            if node.value == "!":
                return 0.0 if self.truth(value, node) else 1.0
            value = self.number(value, node)
            return value if node.value == "+" else -value
        if kind == "binary":
            return self.binary(node)
        if kind == "coalesce":
            left = self.eval(node.children[0])
            return self.eval(node.children[1]) if left is MOLANG_MISSING or left is MOLANG_UNKNOWN or left is None else left
        if kind in {"ternary", "binary_conditional"}:
            condition = self.truth(self.eval(node.children[0]), node)
            if condition:
                return self.eval(node.children[1])
            return self.eval(node.children[2]) if kind == "ternary" else 0.0
        if kind == "call":
            return self.call(node)
        if kind == "index":
            return self.index(self.eval(node.children[0]), self.eval(node.children[1]), node)
        if kind == "arrow":
            return self.arrow(node)
        if kind == "block":
            result = 0.0
            for statement in node.children:
                result = self.eval(statement)
            return result
        if kind == "return":
            raise _Return(self.eval(node.children[0]))
        if kind == "break":
            raise _Break()
        if kind == "continue":
            raise _Continue()
        raise MoLangRuntimeError("Unsupported AST node {}".format(kind), node.span, "molang.unsupported_node")

    def binary(self, node):
        op, left = node.value, self.eval(node.children[0])
        if op == "&&":
            if not self.truth(left, node):
                return 0.0
            return 1.0 if self.truth(self.eval(node.children[1]), node) else 0.0
        if op == "||":
            if self.truth(left, node):
                return 1.0
            return 1.0 if self.truth(self.eval(node.children[1]), node) else 0.0
        right = self.eval(node.children[1])
        if op in {"==", "!="}:
            if left is MOLANG_UNKNOWN or right is MOLANG_UNKNOWN:
                raise _UnresolvedError("Expression depends on an unavailable preview value", node.span, "molang.unresolved")
            left = 0.0 if left is MOLANG_MISSING else left
            right = 0.0 if right is MOLANG_MISSING else right
            equal = left == right
            return 1.0 if (equal if op == "==" else not equal) else 0.0
        left, right = self.number(left, node), self.number(right, node)
        if op == "+": return left + right
        if op == "-": return left - right
        if op == "*": return left * right
        if op == "/":
            if right == 0.0:
                self.emit("Division by zero evaluates to 0", node.span, "molang.division_by_zero")
                return 0.0
            return left / right
        result = {"<": left < right, ">": left > right,
                  "<=": left <= right, ">=": left >= right}.get(op)
        if result is not None:
            return 1.0 if result else 0.0
        raise MoLangRuntimeError("Unsupported operator {}".format(op), node.span, "molang.unsupported_operator")

    def truth(self, value, node):
        if value is MOLANG_UNKNOWN:
            raise _UnresolvedError("Condition depends on an unavailable preview value", node.span, "molang.unresolved")
        if value is MOLANG_MISSING or value is None:
            return False
        if isinstance(value, str):
            raise MoLangRuntimeError("Strings support only == and !=", node.span, "molang.type_error")
        return self.number(value, node) != 0.0

    def number(self, value, node):
        if value is MOLANG_UNKNOWN:
            raise _UnresolvedError("Expression depends on an unavailable preview value", node.span, "molang.unresolved")
        if value is MOLANG_MISSING or value is None:
            self.emit("Missing reference evaluates to 0", node.span, "molang.missing_reference")
            return 0.0
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            raise MoLangRuntimeError("Expected a number, got {}".format(type(value).__name__), node.span, "molang.type_error")

    def integer(self, value, node):
        try:
            return int(self.number(value, node))
        except (ValueError, OverflowError):
            raise MoLangRuntimeError("Expected a finite integer-compatible value", node.span, "molang.type_error")

    def bucket(self, scope):
        bucket = self.context.get(SCOPES[scope][0])
        return bucket if isinstance(bucket, MutableMapping) else {}

    @staticmethod
    def mapping_key(mapping, key):
        if key in mapping:
            return key
        folded = str(key).casefold()
        return next((item for item in mapping if str(item).casefold() == folded), None)

    @classmethod
    def read_path(cls, bucket, path):
        joined = ".".join(path)
        key = cls.mapping_key(bucket, joined)
        if key is not None:
            return bucket[key]
        value = bucket
        for part in path:
            if not isinstance(value, Mapping):
                return MOLANG_MISSING
            key = cls.mapping_key(value, part)
            if key is None:
                return MOLANG_MISSING
            value = value[key]
        return value

    def validate_query(self, name, argument_count, node):
        if self.metadata.definition(name) is None:
            return True
        try:
            self.metadata.validate_call(
                name, argument_count, version=self.engine_version,
                query_set=self.query_set,
            )
            return True
        except MetadataError as error:
            self.emit(str(error), node.span, "molang.query_metadata")
            return False

    def resolve(self, identifier, node=None, call_zero=True):
        node = node or Node("identifier", identifier)
        if identifier in {"true", "yes"}: return 1.0
        if identifier in {"false", "no", "null"}: return 0.0
        if identifier == "this":
            return self.context.get(self.mapping_key(self.context, "this"), 0.0)
        if identifier == "math.pi": return math.pi
        parts, scope = identifier.split("."), identifier.split(".")[0]
        if scope in SCOPES and len(parts) > 1:
            if scope in {"q", "query"} and not self.validate_query(identifier, 0, node):
                return 0.0
            value = self.read_path(self.bucket(scope), parts[1:])
        elif scope in RESOURCES and len(parts) > 1:
            source_key = self.mapping_key(self.context, scope)
            source = self.context.get(source_key, {}) if source_key is not None else {}
            value = self.read_path(source, parts[1:]) if isinstance(source, Mapping) else MOLANG_MISSING
            if value is MOLANG_MISSING and scope != "array":
                return ResourceRef(scope, ".".join(parts[1:]))
        else:
            key = self.mapping_key(self.context, identifier)
            value = self.context.get(key, MOLANG_MISSING) if key is not None else MOLANG_MISSING
        if callable(value) and call_zero:
            try:
                return value()
            except Exception as error:
                self.emit("Query {} failed: {}".format(identifier, error), node.span, "molang.query_error")
                return 0.0
        return value

    def assign(self, target, value, statement):
        while target.kind == "group": target = target.children[0]
        if target.kind == "identifier":
            parts = target.value.split(".")
            if len(parts) < 2 or parts[0] not in SCOPES:
                raise MoLangRuntimeError("Assignment target must be variable or temp", target.span, "molang.invalid_assignment")
            if parts[0] not in WRITABLE:
                raise MoLangRuntimeError("{} is read-only".format(parts[0]), target.span, "molang.read_only_scope")
            bucket, path = self.bucket(parts[0]), parts[1:]
            joined = ".".join(path)
            key = self.mapping_key(bucket, joined)
            if key is not None or len(path) == 1:
                bucket[key if key is not None else joined] = value
                return
            current = bucket
            for part in path[:-1]:
                key = self.mapping_key(current, part)
                if key is None or not isinstance(current.get(key), MutableMapping):
                    key = part
                    current[key] = {}
                current = current[key]
            key = self.mapping_key(current, path[-1])
            current[key if key is not None else path[-1]] = value
            return
        if target.kind == "index":
            values, raw = self.eval(target.children[0]), self.eval(target.children[1])
            if isinstance(values, list) and values:
                values[max(0, self.integer(raw, target)) % len(values)] = value
                return
        raise MoLangRuntimeError("Expression is not assignable", statement.span, "molang.invalid_assignment")

    def index(self, values, raw_index, node):
        if values is MOLANG_MISSING or values is MOLANG_UNKNOWN or values is None: return 0.0
        if not isinstance(values, (list, tuple, ActorArray)):
            raise MoLangRuntimeError("Array indexing requires an array", node.span, "molang.type_error")
        if not values: return 0.0
        return values[max(0, self.integer(raw_index, node)) % len(values)]

    def arrow(self, node):
        actor = self.eval(node.children[0])
        if actor is MOLANG_UNKNOWN or actor is MOLANG_MISSING or actor is None or actor == 0: return 0.0
        actor_context = actor.context if isinstance(actor, ActorRef) else actor if isinstance(actor, MutableMapping) else None
        if actor_context is None: return 0.0
        self.context_stack.append(self.context)
        self.context = actor_context
        self.ensure_aliases()
        try: return self.eval(node.children[1])
        finally: self.context = self.context_stack.pop()

    def call(self, node):
        name = node.value
        if name == "loop": return self.call_loop(node)
        if name == "for_each": return self.call_for_each(node)
        args = [self.eval(child) for child in node.children]
        if name.startswith("math."): return self.math(name[5:], args, node)
        parts = name.split(".")
        if parts[0] in SCOPES and len(parts) > 1:
            function = self.read_path(self.bucket(parts[0]), parts[1:])
            if parts[0] in {"q", "query"}:
                if not self.validate_query(name, len(args), node):
                    return 0.0
            if callable(function):
                try: return function(*args)
                except Exception as error:
                    self.emit("Query {} failed: {}".format(name, error), node.span, "molang.query_error")
                    return 0.0
            if function is not MOLANG_MISSING or parts[-1] not in PURE_QUERIES:
                if not args: return function
                self.emit("{} is not callable".format(name), node.span, "molang.not_callable")
                return 0.0
        short = name.split(".", 1)[-1]
        if short in {"all", "any"}:
            if any(value is MOLANG_UNKNOWN for value in args):
                raise _UnresolvedError("Query depends on an unavailable preview value", node.span, "molang.unresolved")
            values = [0.0 if value is MOLANG_MISSING else value for value in args]
            matches = (value == values[0] for value in values[1:])
            result = all(matches) if short == "all" else any(matches)
            return 1.0 if result else 0.0
        if short == "approx_eq" and len(args) >= 2:
            if any(value is MOLANG_UNKNOWN for value in args):
                raise _UnresolvedError("Query depends on an unavailable preview value", node.span, "molang.unresolved")
            first = self.number(args[0], node)
            return 1.0 if all(abs(first - self.number(v, node)) <= 1e-7 for v in args[1:]) else 0.0
        if short == "in_range":
            value, minimum, maximum = (self.number(value, node) for value in args)
            return 1.0 if minimum <= value <= maximum else 0.0
        if short == "count":
            return float(sum(len(value) if isinstance(value, (list, tuple, ActorArray)) else 1 for value in args))
        if short == "combine_entities":
            combined = ActorArray()
            seen = set()
            for value in args:
                values = value if isinstance(value, (list, tuple, ActorArray)) else (value,)
                for actor in values:
                    if not isinstance(actor, (ActorRef, MutableMapping)):
                        continue
                    identity = id(actor)
                    if identity not in seen:
                        seen.add(identity)
                        combined.append(actor)
            return combined
        self.emit("Unknown function {}".format(name), node.span, "molang.unknown_function")
        return 0.0

    def call_loop(self, node):
        if len(node.children) != 2:
            raise MoLangRuntimeError("loop expects 2 arguments", node.span, "molang.arity")
        count = max(0, min(MAX_LOOP_ITERATIONS, self.integer(self.eval(node.children[0]), node)))
        result = 0.0
        for _index in range(count):
            try: result = self.eval(node.children[1])
            except _Continue: continue
            except _Break: break
        return result

    def call_for_each(self, node):
        if len(node.children) != 3:
            raise MoLangRuntimeError("for_each expects 3 arguments", node.span, "molang.arity")
        target, source, body = node.children
        values = self.eval(source)
        if values is MOLANG_MISSING or values is MOLANG_UNKNOWN or values is None: return 0.0
        if not isinstance(values, (list, tuple, ActorArray)):
            raise MoLangRuntimeError("for_each expects an array", source.span, "molang.type_error")
        result = 0.0
        for value in values[:MAX_LOOP_ITERATIONS]:
            self.assign(target, value, node)
            try: result = self.eval(body)
            except _Continue: continue
            except _Break: break
        return result

    def math(self, name, values, node):
        args = [self.number(value, node) for value in values]
        if self.metadata.definition("math." + name) is None:
            self.emit("Unknown math function math.{}".format(name), node.span, "molang.unknown_math")
            return 0.0
        try:
            self.metadata.validate_call(
                "math." + name, len(args), version=self.engine_version
            )
        except MetadataError as error:
            raise MoLangRuntimeError(str(error), node.span, "molang.arity")
        def arity(minimum, maximum=None):
            maximum = minimum if maximum is None else maximum
            if not minimum <= len(args) <= maximum:
                raise MoLangRuntimeError("math.{} expects {}-{} arguments".format(name, minimum, maximum), node.span, "molang.arity")
        try:
            if name == "pi": arity(0); return math.pi
            if name == "abs": arity(1); return abs(args[0])
            if name == "sin": arity(1); return math.sin(math.radians(args[0]))
            if name == "cos": arity(1); return math.cos(math.radians(args[0]))
            if name == "tan": arity(1); return math.tan(math.radians(args[0]))
            if name == "asin": arity(1); return math.degrees(math.asin(args[0]))
            if name == "acos": arity(1); return math.degrees(math.acos(args[0]))
            if name == "atan": arity(1); return math.degrees(math.atan(args[0]))
            if name == "atan2": arity(2); return math.degrees(math.atan2(args[0], args[1]))
            if name == "ceil": arity(1); return float(math.ceil(args[0]))
            if name == "floor": arity(1); return float(math.floor(args[0]))
            if name == "trunc": arity(1); return float(math.trunc(args[0]))
            if name == "round": arity(1); return float(math.floor(args[0] + .5) if args[0] >= 0 else math.ceil(args[0] - .5))
            if name == "sqrt": arity(1); return math.sqrt(args[0])
            if name == "exp": arity(1); return math.exp(args[0])
            if name == "ln": arity(1); return math.log(args[0])
            if name == "pow": arity(2); return math.pow(args[0], args[1])
            if name == "min": arity(2); return min(args)
            if name == "max": arity(2); return max(args)
            if name == "clamp": arity(3); return max(args[1], min(args[0], args[2]))
            if name == "copy_sign": arity(2); return math.copysign(args[0], args[1])
            if name == "sign": arity(1); return 1.0 if args[0] > 0 else -1.0 if args[0] < 0 else 0.0
            if name == "mod": arity(2); return 0.0 if args[1] == 0 else math.fmod(args[0], args[1])
            if name == "lerp": arity(3); return args[0] + (args[1] - args[0]) * args[2]
            if name == "inverse_lerp": arity(3); return 0.0 if args[1] == args[0] else (args[2] - args[0]) / (args[1] - args[0])
            if name == "min_angle": arity(1); return min_angle(args[0])
            if name == "lerprotate": arity(3); return args[0] + min_angle(args[1] - args[0]) * args[2]
            if name == "hermite_blend": arity(1); return args[0] ** 2 * (3.0 - 2.0 * args[0])
            if name == "random": arity(2); return float(self.rng.uniform(args[0], args[1]))
            if name == "random_integer": arity(2); return float(self.rng.randint(int(args[0]), int(args[1])))
            if name in {"die_roll", "die_roll_integer"}:
                arity(3); count = max(0, min(MAX_LOOP_ITERATIONS, int(args[0])))
                if name == "die_roll": return sum(self.rng.uniform(args[1], args[2]) for _ in range(count))
                return float(sum(self.rng.randint(int(args[1]), int(args[2])) for _ in range(count)))
            if name in EASINGS:
                arity(3)
                amount = easing(name, args[2])
                return args[0] + (args[1] - args[0]) * amount
        except (ValueError, OverflowError, ZeroDivisionError) as error:
            self.emit("math.{} failed: {}".format(name, error), node.span, "molang.math_domain")
            return 0.0
        self.emit("Unknown math function math.{}".format(name), node.span, "molang.unknown_math")
        return 0.0


def min_angle(value):
    angle = math.fmod(value, 360.0)
    if angle > 180.0: angle -= 360.0
    elif angle < -180.0: angle += 360.0
    return angle


def _bounce_out(value):
    n, d = 7.5625, 2.75
    if value < 1 / d: return n * value * value
    if value < 2 / d:
        value -= 1.5 / d; return n * value * value + .75
    if value < 2.5 / d:
        value -= 2.25 / d; return n * value * value + .9375
    value -= 2.625 / d
    return n * value * value + .984375


def easing(name, value, *parameters):
    x, family, mode = float(value), name, "in"
    if name.startswith("ease_in_out_"): mode, family = "in_out", name[12:]
    elif name.startswith("ease_out_"): mode, family = "out", name[9:]
    elif name.startswith("ease_in_"): family = name[8:]
    def ease_in(t):
        if family == "sine": return 1 - math.cos(t * math.pi / 2)
        if family == "quad": return t ** 2
        if family == "cubic": return t ** 3
        if family == "quart": return t ** 4
        if family == "quint": return t ** 5
        if family == "expo": return 0.0 if t == 0 else 2 ** (10 * t - 10)
        if family == "circ": return 1 - math.sqrt(max(0, 1 - t * t))
        if family == "back":
            c = parameters[0] if parameters else 1.70158
            return (c + 1) * t ** 3 - c * t ** 2
        if family == "bounce": return 1 - _bounce_out(1 - t)
        if family == "elastic":
            if t in {0.0, 1.0}: return t
            period = parameters[0] if parameters else 2 * math.pi / 3
            return -(2 ** (10 * t - 10)) * math.sin((10 * t - 10.75) * period)
        raise ValueError("unknown easing family")
    if mode == "in": return ease_in(x)
    if mode == "out": return 1 - ease_in(1 - x)
    return ease_in(2 * x) / 2 if x < .5 else 1 - ease_in(2 - 2 * x) / 2


EASINGS = {"{}_{}".format(prefix, family)
           for prefix in ("ease_in", "ease_out", "ease_in_out")
           for family in ("back", "bounce", "circ", "cubic", "elastic",
                          "expo", "quad", "quart", "quint", "sine")}


def _publish(sink, diagnostic):
    if isinstance(sink, list):
        if not sink or sink[-1] is not diagnostic: sink.append(diagnostic)
    elif callable(sink): sink(diagnostic)


class MoLangEngine:
    """Version-keyed compiled-program cache and runtime factory."""
    def __init__(self):
        self.cache: Dict[Tuple[str, Tuple[int, int, int]], MoLangProgram] = {}

    def compile(self, source, min_engine_version=None, diagnostics=None):
        text, version = "" if source is None else str(source), _version(min_engine_version)
        key = (text, version)
        if key in self.cache: return self.cache[key]
        try:
            program = Parser(Lexer(text).tokens_list, version, text).parse()
        except RecursionError:
            error = MoLangSyntaxError("Expression nesting is too deep", SourceSpan(0, len(text), 1, 1), "molang.nesting_limit")
            _publish(diagnostics, error.diagnostic)
            raise error
        except MoLangError as error:
            _publish(diagnostics, error.diagnostic)
            raise
        self.cache[key] = program
        return program

    def evaluate_typed(self, expression, context=None, default=0.0, *,
                       min_engine_version=None, rng=None, seed=None,
                       diagnostics=None, strict=False,
                       max_steps=DEFAULT_MAX_STEPS, query_set="default",
                       metadata=None, fresh_temp=False):
        try:
            program = self.compile(expression, min_engine_version, diagnostics)
            return MoLangRuntime(context, rng=rng, seed=seed, diagnostics=diagnostics,
                                 max_steps=max_steps,
                                 engine_version=program.min_engine_version,
                                 query_set=query_set, metadata=metadata,
                                 fresh_temp=fresh_temp).evaluate(program)
        except MoLangError as error:
            if not isinstance(error, MoLangSyntaxError): _publish(diagnostics, error.diagnostic)
            if strict: raise
            return default


_engine = MoLangEngine()


class MoLangCompiler:
    """Compatibility facade for legacy users that constructed a compiler."""
    def __init__(self):
        self.engine = MoLangEngine()
        self.cache = self.engine.cache
    def compile(self, code_str, min_engine_version=None):
        program = self.engine.compile(code_str, min_engine_version)
        return lambda context: MoLangRuntime(context, engine_version=program.min_engine_version).evaluate(program)


def _source(value):
    if isinstance(value, list):
        return " ".join(str(item) if str(item).rstrip().endswith(";") else str(item) + ";" for item in value)
    return value if isinstance(value, str) else ""


def compile_molang(expression, min_engine_version=None, diagnostics=None):
    return _engine.compile(_source(expression), min_engine_version, diagnostics)


def evaluate_typed(expression, ctx=None, default=0.0, *, min_engine_version=None,
                   rng=None, seed=None, diagnostics=None, strict=False,
                   max_steps=DEFAULT_MAX_STEPS, query_set="default",
                   metadata=None, fresh_temp=False):
    """Evaluate without discarding strings, resources, arrays, or actors."""
    if not isinstance(expression, (str, list)): return default
    return _engine.evaluate_typed(_source(expression), ctx, default,
        min_engine_version=min_engine_version, rng=rng, seed=seed,
        diagnostics=diagnostics, strict=strict, max_steps=max_steps,
        query_set=query_set, metadata=metadata, fresh_temp=fresh_temp)


def evaluate(expression, ctx=None, default=0.0, *, min_engine_version=None,
             rng=None, seed=None, diagnostics=None, strict=False,
             max_steps=DEFAULT_MAX_STEPS, query_set="default",
             metadata=None, fresh_temp=False):
    """Compatibility numeric boundary used by existing importer code."""
    value = evaluate_typed(expression, ctx, default,
        min_engine_version=min_engine_version, rng=rng, seed=seed,
        diagnostics=diagnostics, strict=strict, max_steps=max_steps,
        query_set=query_set, metadata=metadata, fresh_temp=fresh_temp)
    try:
        return 0.0 if value is MOLANG_MISSING else float(value)
    except (TypeError, ValueError, OverflowError):
        if strict: raise MoLangRuntimeError("Expression result is not numeric", code="molang.type_error")
        return default


def execute_statements(code, ctx=None, *, min_engine_version=None, rng=None,
                       seed=None, diagnostics=None, strict=False,
                       max_steps=DEFAULT_MAX_STEPS, query_set="default",
                       metadata=None, fresh_temp=False):
    """Execute statements while preserving the historical ``None`` return."""
    evaluate_typed(code, ctx, 0.0, min_engine_version=min_engine_version,
        rng=rng, seed=seed, diagnostics=diagnostics, strict=strict,
        max_steps=max_steps, query_set=query_set, metadata=metadata,
        fresh_temp=fresh_temp)


__all__ = [
    "ActorArray", "ActorRef", "DEFAULT_ENGINE_VERSION", "Lexer",
    "MAX_LOOP_ITERATIONS", "MOLANG_MISSING", "MOLANG_UNKNOWN",
    "MoLangCompiler", "MoLangDiagnostic", "MoLangEngine", "MoLangError",
    "MoLangProgram", "MoLangRuntime", "MoLangRuntimeError",
    "MoLangSyntaxError", "Parser", "ResourceRef", "SourceSpan",
    "compile_molang", "evaluate", "evaluate_typed", "execute_statements",
]
