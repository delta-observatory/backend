"""Evaluate the x-constraints rules of a method parameter schema.

A rule has the form "<term> <op> <term>". A term is a parameter path such as
"search.minimum_period", max(path) or min(path) of a number array, or a decimal number.
The operators are <, <=, >, and >=. The frontend evaluates the same grammar
while the user types. tests/vectors/parameters.json holds the shared cases.
"""
import re

RULE = re.compile(r'^(.+?)\s*(<=|>=|<|>)\s*(.+)$')
TERM = re.compile(r'^(?:(max|min)\(([\w.]+)\)|([A-Za-z_][\w.]*))$')
NUMBER = re.compile(r'^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$')


def _get(document: dict, path: str):
    value = document
    for key in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _term(text: str, parameters: dict):
    if NUMBER.match(text.strip()):
        return float(text), text.strip()
    m = TERM.match(text.strip())
    if not m:
        raise ValueError(f'unsupported constraint term "{text}"')
    path = m.group(2) or m.group(3)
    value = _get(parameters, path)
    if m.group(1):
        value = (max if m.group(1) == 'max' else min)(value) if isinstance(value, list) and value else None
    return (value if isinstance(value, (int, float)) and not isinstance(value, bool) else None), path


def failed(rules: list[str], parameters: dict) -> list[dict]:
    """Errors for the rules that fail. loc is the path of the first term. A rule with a missing value is skipped.
    paths lists the parameter paths of the rule, without number literals."""
    errors = []
    for rule in rules:
        m = RULE.match(rule)
        if not m:
            raise ValueError(f'unsupported constraint "{rule}"')
        (a, path_a), op, (b, path_b) = _term(m.group(1), parameters), m.group(2), _term(m.group(3), parameters)
        if a is None or b is None:
            continue
        ok = {'<': a < b, '<=': a <= b, '>': a > b, '>=': a >= b}[op]
        if not ok:
            paths = [p for p in (path_a, path_b) if not NUMBER.match(p)]
            errors.append({'loc': ['parameters', *paths[0].split('.')] if paths else ['parameters'],
                           'msg': f'rule failed: {rule}', 'rule': rule, 'paths': paths})
    return errors
