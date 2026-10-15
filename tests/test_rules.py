"""The API rules against the shared vectors in tests/vectors/.

The frontend runs the same vectors (target_query, fold, parameters) against its own code.
"""
import json
from pathlib import Path

import pytest

from delta_api import constraints, fold, query
from delta_api.contracts import Contracts, ValidationFailure
from delta_api.rules import BLS_GRID, DEFAULT_RUN, FOLD_WINDOW, PERIOD_AGREEMENT

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / 'tests' / 'vectors'


def load(name):
    return json.loads((VECTORS / name).read_text())


def test_target_query_vectors():
    data = load('target_query.json')
    for case in data['cases']:
        parsed = query.parse(case['q'])
        assert [t['id'] for t in data['targets'] if query.matches(parsed, t)] == case['match'], case
        assert len(parsed.errors) == case['error_count'], case


def test_status_vectors():
    for case in load('status.json')['cases']:
        args = case['label_class'], case['depth_snr'], case['relative_period_error']
        assert PERIOD_AGREEMENT.status(*args) == case['status'], case
        assert PERIOD_AGREEMENT.status(*args, labels_visible=False) == case['status_without_labels'], case


def test_default_run_vectors():
    for case in load('default_run.json')['cases']:
        chosen = DEFAULT_RUN.choose(case['runs'])
        assert (chosen and chosen['id']) == case['default'], case


def test_bls_grid_vectors():
    for case in load('bls_grid.json')['cases']:
        assert BLS_GRID.trials(case['baseline_days'], case['minimum_period'], case['maximum_period'],
                               case['duration_days'], case['frequency_factor']) == case['trials'], case
    # The recorded grid of run e0534db5 for WASP-18 b has 35678 trials. The estimate is within 0.01 %.
    assert abs(BLS_GRID.trials(27.406, 0.5, 10, [0.04, 0.06, 0.08, 0.1, 0.12, 0.16, 0.2], 1) / 35678 - 1) < 1e-4


def test_fold_vectors():
    data = load('fold.json')
    for o in data['offsets']:
        assert fold.offset_hours(o['t'], o['t0'], o['period']) == pytest.approx(o['hours'], abs=1e-9)
    for case in data['cases']:
        window = FOLD_WINDOW.window_hours(case['duration_days'], case['period'])
        assert window == pytest.approx(case['window_hours'], rel=1e-12)
        assert FOLD_WINDOW.bin_hours(window) == pytest.approx(case['bin_hours'], rel=1e-12)
        time_range = tuple(case['time_range']) if case['time_range'] else None
        xs, ys = fold.fold(data['time'], data['flux'], case['t0'], case['period'], window, time_range)
        assert len(xs) == case['n_points']
        got = fold.bins(xs, ys, window, case['bin_hours'])
        assert len(got) == len(case['bins'])
        for g, e in zip(got, case['bins']):
            assert g['n'] == e['n']
            for key in ('x_hours', 'mean', 'standard_error'):
                assert g[key] == pytest.approx(e[key], rel=1e-9, abs=1e-12)


def test_parameter_vectors():
    contracts = Contracts(ROOT / 'vendor' / 'contracts')
    method = contracts.method('tess-bls', 1)
    for case in load('parameters.json')['cases']:
        try:
            contracts.resolve_parameters(method, case['override'])
            errors = []
        except ValidationFailure as failure:
            errors = failure.errors
        schema = sorted({'.'.join(map(str, e['loc'][1:])) for e in errors if 'rule' not in e})
        rules = [e['rule'] for e in errors if 'rule' in e]
        assert (schema, rules) == (case['schema_paths'], case['rules']), case


def test_constraint_grammar():
    rules = ['a.x < a.y', 'max(a.list) <= a.x', 'min(a.list) > 0']
    assert constraints.failed(rules, {'a': {'x': 1, 'y': 2, 'list': [0.5, 1]}}) == []
    failed = constraints.failed(rules, {'a': {'x': 3, 'y': 2, 'list': [0, 5]}})
    assert [f['rule'] for f in failed] == rules
    assert failed[1]['loc'] == ['parameters', 'a', 'list'] and failed[1]['paths'] == ['a.list', 'a.x']
    assert failed[2]['paths'] == ['a.list']
    # A missing value skips the rule. The schema check reports the missing value.
    assert constraints.failed(rules, {'a': {'x': 1}}) == []
    with pytest.raises(ValueError):
        constraints.failed(['a.x == a.y'], {})
