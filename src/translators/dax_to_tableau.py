"""Translate a small, named subset of DAX into Tableau calculation syntax.

The subset is deliberately tiny and every construct outside it raises
``UnsupportedDax`` naming the offending token. A measure that cannot be
translated is emitted by the generator as a placeholder, never as a guess.

Supported:
- numeric and string literals, parentheses, unary minus
- ``+ - * /`` and comparisons ``= <> < <= > >=``, ``&&`` / ``||``
- measure references ``[Name]`` and column references ``Table[Col]`` / ``'Tab le'[Col]``
- ``SUM / AVERAGE / MIN / MAX / COUNT / COUNTA / DISTINCTCOUNT ( Table[Col] )``
- ``DIVIDE(a, b[, alt])``, ``BLANK()``, ``IF(cond, a[, b])``, ``ABS``, ``ROUND``

Not supported (raises): CALCULATE, FILTER, iterators (SUMX...), VAR/RETURN,
time intelligence, table functions, anything with row context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.models.ir import Measure


class UnsupportedDax(Exception):
    pass


# --- tokenizer ---------------------------------------------------------------

_TOKEN = re.compile(r"""
    (?P<ws>\s+|--[^\n]*|//[^\n]*)
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<colref>'(?:[^']|'')+'\[[^\]]+\]|[A-Za-z_][\w ]*\[[^\]]+\])
  | (?P<bracket>\[[^\]]+\])
  | (?P<op>&&|\|\||<>|<=|>=|[-+*/(),=<>])
  | (?P<ident>[A-Za-z_]\w*)
""", re.VERBOSE)


@dataclass(frozen=True)
class Tok:
    kind: str
    text: str


def tokenize(src: str) -> list[Tok]:
    out, pos = [], 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise UnsupportedDax(f"unrecognised text at: {src[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind != "ws":
            out.append(Tok(kind, m.group()))
    return out


# --- parser -> Tableau text --------------------------------------------------

AGG_FUNCS = {"SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX",
             "COUNT": "COUNT", "COUNTA": "COUNT", "DISTINCTCOUNT": "COUNTD"}
SCALAR_FUNCS = {"ABS": "ABS", "ROUND": "ROUND"}


class Translator:
    """Recursive-descent translator. ``field_name`` maps (table, column|measure) -> Tableau name."""

    def __init__(self, tokens: list[Tok], field_name, measure_table: dict[str, str]):
        self.toks, self.i = tokens, 0
        self.field_name, self.measure_table = field_name, measure_table

    def peek(self) -> Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, text: str | None = None) -> Tok:
        t = self.peek()
        if t is None or (text is not None and t.text != text):
            raise UnsupportedDax(f"expected {text!r}, found {t.text if t else 'end of expression'!r}")
        self.i += 1
        return t

    def translate(self) -> str:
        out = self.logical()
        if self.peek() is not None:
            raise UnsupportedDax(f"unexpected {self.peek().text!r}")
        return out

    def logical(self) -> str:
        left = self.comparison()
        while self.peek() and self.peek().text in ("&&", "||"):
            op = self.take().text
            right = self.comparison()
            left = f"({left} {'AND' if op == '&&' else 'OR'} {right})"
        return left

    def comparison(self) -> str:
        left = self.additive()
        if self.peek() and self.peek().text in ("=", "<>", "<", "<=", ">", ">="):
            op = self.take().text
            right = self.additive()
            return f"{left} {'!=' if op == '<>' else op} {right}"
        return left

    def additive(self) -> str:
        left = self.term()
        while self.peek() and self.peek().text in ("+", "-"):
            op = self.take().text
            left = f"{left} {op} {self.term()}"
        return left

    def term(self) -> str:
        left = self.unary()
        while self.peek() and self.peek().text in ("*", "/"):
            op = self.take().text
            left = f"{left} {op} {self.unary()}"
        return left

    def unary(self) -> str:
        if self.peek() and self.peek().text == "-":
            self.take()
            return f"-{self.unary()}"
        return self.primary()

    def primary(self) -> str:
        t = self.peek()
        if t is None:
            raise UnsupportedDax("unexpected end of expression")
        if t.kind == "num":
            return self.take().text
        if t.kind == "str":
            return "'" + self.take().text[1:-1].replace('""', '"').replace("'", "\\'") + "'"
        if t.text == "(":
            self.take("(")
            inner = self.logical()
            self.take(")")
            return f"({inner})"
        if t.kind == "colref":
            return self.field_name(*_split_colref(self.take().text))
        if t.kind == "bracket":
            name = self.take().text[1:-1]
            table = self.measure_table.get(name)
            if table is None:
                raise UnsupportedDax(f"measure [{name}] is not in the model")
            return self.field_name(table, name)
        if t.kind == "ident":
            nxt = self.toks[self.i + 1] if self.i + 1 < len(self.toks) else None
            if nxt is None or nxt.text != "(":
                raise UnsupportedDax(f"{t.text.upper()} is outside the supported subset")
            return self.call()
        raise UnsupportedDax(f"unexpected {t.text!r}")

    def call(self) -> str:
        name = self.take().text.upper()
        self.take("(")
        if name in AGG_FUNCS:
            col = self.take()
            if col.kind != "colref":
                raise UnsupportedDax(f"{name} over anything but a column (got {col.text!r})")
            self.take(")")
            return f"{AGG_FUNCS[name]}({self.field_name(*_split_colref(col.text))})"
        if name == "BLANK":
            self.take(")")
            return "NULL"
        if name == "DIVIDE":
            a = self.logical(); self.take(",")
            b = self.logical()
            alt = None
            if self.peek() and self.peek().text == ",":
                self.take(","); alt = self.logical()
            self.take(")")
            return f"IFNULL({a} / {b}, {alt})" if alt is not None else f"({a} / {b})"
        if name == "IF":
            c = self.logical(); self.take(",")
            a = self.logical()
            b = "NULL"
            if self.peek() and self.peek().text == ",":
                self.take(","); b = self.logical()
            self.take(")")
            return f"IF {c} THEN {a} ELSE {b} END"
        if name in SCALAR_FUNCS:
            args = [self.logical()]
            while self.peek() and self.peek().text == ",":
                self.take(","); args.append(self.logical())
            self.take(")")
            return f"{SCALAR_FUNCS[name]}({', '.join(args)})"
        raise UnsupportedDax(f"function {name} is outside the supported subset")


def _split_colref(text: str) -> tuple[str, str]:
    table, col = text[:-1].split("[", 1)
    table = table.strip()
    if table.startswith("'"):
        table = table[1:-1].replace("''", "'")
    return table, col


def translate(dax: str, field_name, measure_table: dict[str, str]) -> str:
    """Translate one DAX expression; raises UnsupportedDax outside the subset."""
    return Translator(tokenize(dax), field_name, measure_table).translate()


@dataclass
class Translation:
    measure: Measure
    formula: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.formula is not None


def translate_measures(measures: list[Measure], field_name) -> list[Translation]:
    table_of = {m.name: m.table for m in measures}
    out = []
    for m in measures:
        try:
            out.append(Translation(m, translate(m.expression, field_name, table_of), None))
        except UnsupportedDax as e:
            out.append(Translation(m, None, str(e)))
    return out
