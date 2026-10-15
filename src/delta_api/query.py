"""Target query grammar, version target-query/1.

A query is text with tokens separated by white space. Every token must match.
- "field>value", "field<value", "field=value", or "field:value" compares a number.
- Any other token matches part of the planet name or the TIC identifier, without regard to case.
A target without a value for a field does not match a comparison on that field.
A token with an unknown field or no number is an error.

The frontend applies the same grammar while the user types.
tests/vectors/target_query.json holds the cases that both implementations must pass.
"""
from dataclasses import dataclass
import math
import re

GRAMMAR = 'target-query/1'

# Field name, target attribute, and unit.
FIELDS: dict[str, tuple[str, str]] = {
    'snr': ('depth_snr', '1'),
    'err': ('relative_period_error', '1'),
    'p': ('period_days', 'd'),
    'pref': ('reference_period_days', 'd'),
    'tmag': ('tmag', 'mag'),
    'sector': ('sector', '1'),
}
# Fields that come from known-object labels. A blind policy withholds them.
LABEL_FIELDS = {'err', 'pref'}
# Fields that come from the measurements of a run. A query without a run cannot use them.
RUN_FIELDS = {'snr', 'err', 'p'}

TOKEN = re.compile(r'^([a-z]+)([<>=:])(.*)$', re.IGNORECASE)


@dataclass(frozen=True)
class Comparison:
    field: str
    op: str
    value: float


@dataclass(frozen=True)
class Text:
    needle: str


@dataclass(frozen=True)
class Query:
    terms: tuple
    errors: tuple


NUMBER = re.compile(r'^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$')


def _number(text: str) -> float | None:
    """A decimal number, for example 20, -1.5, .5, or 1e-3. Other forms are errors."""
    if not NUMBER.match(text):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def parse(text: str) -> Query:
    terms, errors = [], []
    for token in text.split():
        m = TOKEN.match(token)
        if m:
            name, op, rest = m.group(1).lower(), m.group(2), m.group(3)
            if name not in FIELDS:
                errors.append(f'unknown field "{m.group(1)}". Fields: {", ".join(FIELDS)}')
                continue
            value = _number(rest)
            if value is None:
                errors.append(f'"{token}" needs a number')
                continue
            terms.append(Comparison(name, '=' if op == ':' else op, value))
        else:
            terms.append(Text(token.lower()))
    return Query(tuple(terms), tuple(errors))


def matches(query: Query, target: dict) -> bool:
    """Evaluate a query on one target dict. The SQL form in to_sql gives the same result."""
    for term in query.terms:
        if isinstance(term, Comparison):
            value = target.get(FIELDS[term.field][0])
            if value is None:
                return False
            if term.op == '>' and not value > term.value:
                return False
            if term.op == '<' and not value < term.value:
                return False
            if term.op == '=' and not value == term.value:
                return False
        else:
            haystack = f"{target.get('planet_name') or ''} {target.get('target_id') or ''}".lower()
            if term.needle not in haystack:
                return False
    return True


def to_sql(query: Query, columns: dict[str, str]) -> tuple[list[str], list]:
    """WHERE clauses and arguments. columns maps target attributes to SQL expressions."""
    where, args = [], []
    for term in query.terms:
        if isinstance(term, Comparison):
            column = columns[FIELDS[term.field][0]]
            where.append(f'{column} {term.op} %s')
            args.append(term.value)
        else:
            where.append(f"position(%s in lower(coalesce({columns['planet_name']}, '') || ' ' || {columns['target_id']})) > 0")
            args.append(term.needle)
    return where, args


def describe() -> dict:
    return {
        'id': GRAMMAR, 'kind': 'query',
        'summary': 'Tokens separated by spaces. Every token must match. A token field>value, field<value, '
                   'field=value, or field:value compares a number. Any other token matches part of the planet '
                   'name or the TIC identifier, without regard to case. A missing value does not match.',
        'fields': {name: {'attribute': attr, 'unit': unit, 'requires_labels': name in LABEL_FIELDS,
                          'requires_run': name in RUN_FIELDS}
                   for name, (attr, unit) in FIELDS.items()},
    }
