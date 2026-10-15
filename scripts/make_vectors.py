"""Write the shared test vectors in tests/vectors/ from the API code.

The vectors hold cases that the API and the frontend must both pass:
the target query grammar, the fold and its bins, and the parameter checks.
Other vectors (status, default run, BLS grid) cover API rules that only the API applies.
Run: uv run python scripts/make_vectors.py
Review the diff of tests/vectors/ before a commit. A changed vector means a changed rule.
"""
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from delta_api import constraints, fold, query  # noqa: E402
from delta_api.contracts import Contracts, _errors, deep_merge  # noqa: E402
from delta_api.rules import BLS_GRID, DEFAULT_RUN, FOLD_WINDOW, PERIOD_AGREEMENT  # noqa: E402

OUT = ROOT / 'tests' / 'vectors'


def write(name: str, data: dict):
    (OUT / name).write_text(json.dumps(data, indent=1) + '\n')


def target_query():
    targets = [
        {'id': 'a', 'target_id': 'TIC:100100827', 'planet_name': 'WASP-18 b', 'depth_snr': 652.4, 'relative_period_error': 7.2e-5,
         'period_days': 0.9414, 'reference_period_days': 0.9415, 'tmag': 8.83, 'sector': 2},
        {'id': 'b', 'target_id': 'TIC:25372748', 'planet_name': None, 'depth_snr': 1143.0, 'relative_period_error': None,
         'period_days': 0.5305, 'reference_period_days': None, 'tmag': 9.70, 'sector': 1},
        {'id': 'c', 'target_id': 'TIC:232540264', 'planet_name': 'HD 135694 b', 'depth_snr': 12.8, 'relative_period_error': 0.5313,
         'period_days': 7.4638, 'reference_period_days': 15.9235, 'tmag': 8.47, 'sector': 14},
        {'id': 'd', 'target_id': 'TIC:21639186', 'planet_name': None, 'depth_snr': 51.0, 'relative_period_error': None,
         'period_days': 4.7173, 'reference_period_days': None, 'tmag': 10.0, 'sector': 10},
        {'id': 'e', 'target_id': 'TIC:211438925', 'planet_name': 'WASP-20 b', 'depth_snr': None, 'relative_period_error': None,
         'period_days': None, 'reference_period_days': 4.8996, 'tmag': 10.25, 'sector': 2},
    ]
    texts = ['', 'wasp', 'WASP-1', '2163', 'tic:2', 'snr>100', 'snr>100 wasp', 'err<0.01', 'err>0.01', 'p<1', 'pref>10',
             'sector:2', 'sector=2', 'tmag<9', 'tmag<9 sector:14', 'snr<20', 'snr>-1', 'p<.6', 'err<1e-4',
             'depthx>2', 'snr>', 'snr>abc wasp', 'p<0x10', 'SNR>600', '  wasp   b  ', 'b']
    cases = []
    for text in texts:
        parsed = query.parse(text)
        cases.append({'q': text, 'match': [t['id'] for t in targets if query.matches(parsed, t)],
                      'error_count': len(parsed.errors)})
    write('target_query.json', {'grammar': query.GRAMMAR, 'targets': targets, 'cases': cases})


def fold_vectors():
    # 1500 points over 3 days at 0.002 d with a box dip of 0.01 at period 1.5 d, epoch 0.3 d, and two missing values.
    time = [round(0.002 * i, 10) for i in range(1500)]
    flux = []
    for i, t in enumerate(time):
        if i in (17, 555):
            flux.append(None)
            continue
        h = fold.offset_hours(t, 0.3, 1.5)
        flux.append(round(1.0 - (0.01 if abs(h) < 2.0 else 0.0) + 0.0007 * math.sin(i * 1.7), 12))
    cases = []
    for period, t0, duration, time_range in ((1.5, 0.3, 0.08, None), (1.5, 0.3, 0.2, [1.0, 4.0]), (0.75, 0.3, 0.08, None),
                                              (1.5, 0.3, 0.01, None), (3.0, 0.31, 0.08, [0.0, 3.0])):
        window = FOLD_WINDOW.window_hours(duration, period)
        width = FOLD_WINDOW.bin_hours(window)
        xs, ys = fold.fold(time, flux, t0, period, window, tuple(time_range) if time_range else None)
        cases.append({'period': period, 't0': t0, 'duration_days': duration, 'time_range': time_range,
                      'window_hours': window, 'bin_hours': width, 'n_points': len(xs),
                      'bins': fold.bins(xs, ys, window, width)})
    offsets = [{'t': t, 't0': t0, 'period': p, 'hours': fold.offset_hours(t, t0, p)}
               for t, t0, p in ((10.1, 10, 1), (10.9, 10, 1), (13, 10, 1), (9.4, 10, 1), (1354.4595 + 0.47, 1354.4595, 0.9414))]
    write('fold.json', {'rule': FOLD_WINDOW.id, 'time': time, 'flux': flux, 'cases': cases, 'offsets': offsets})


def parameter_vectors():
    contracts = Contracts(ROOT / 'vendor' / 'contracts')
    method = contracts.method('tess-bls', 1)
    validator = contracts._validators[('tess-bls', 1)]
    overrides = [
        {}, {'flatten': {'window_length': 1000}}, {'flatten': {'window_length': 5}}, {'flatten': {'window_length': 1001.5}},
        {'search': {'minimum_period': 12}}, {'search': {'minimum_period': 0.1}},
        {'search': {'maximum_period': 0.4}}, {'search': {'duration_days': [0.1, 0.1]}}, {'search': {'duration_days': []}},
        {'search': {'duration_days': [3]}}, {'search': {'objective': 'chi2'}}, {'flux_column': 'kspsap'},
        {'minimum_rows': 99}, {'search': {'frequency_factor': 0.01}}, {'search': {'maximum_period': 20}},
        {'search': {'minimum_period': 0, 'maximum_period': 101}},
    ]
    cases = []
    for override in overrides:
        params = deep_merge(method.defaults, override)
        schema_paths = sorted({'.'.join(str(p) for p in e['loc'][1:]) for e in _errors(validator, params, ('parameters',))})
        rules = [e['rule'] for e in constraints.failed(method.schema['x-constraints'], params)] if not schema_paths else []
        cases.append({'override': override, 'schema_paths': schema_paths, 'rules': rules})
    write('parameters.json', {'method': {'id': method.id, 'version': method.version}, 'schema': method.schema,
                              'defaults': method.defaults, 'cases': cases})


def status_vectors():
    rows = [('eclipsing_binary', 140.0, None), ('no_known_planet', 1143.0, None), ('no_known_planet', 100.0, None),
            ('no_known_planet', None, None), ('planet_host', 652.0, 7.2e-5), ('planet_host', 40.0, 0.0099999),
            ('planet_host', 40.0, 0.01), ('planet_host', 40.0, 0.0499), ('planet_host', 40.0, 0.05),
            ('planet_host', 40.0, 0.9), ('planet_host', None, None), (None, 300.0, None)]
    cases = [{'label_class': c, 'depth_snr': s, 'relative_period_error': e,
              'status': PERIOD_AGREEMENT.status(c, s, e), 'status_without_labels': PERIOD_AGREEMENT.status(c, s, e, False)}
             for c, s, e in rows]
    write('status.json', {'rule': PERIOD_AGREEMENT.id, 'cases': cases})


def default_run_vectors():
    cases = [
        {'runs': [{'id': 'old', 'created_at': '2026-10-12T02:27:09Z', 'complete': 46},
                  {'id': 'new', 'created_at': '2026-10-14T05:01:24Z', 'complete': 1}], 'default': 'old'},
        {'runs': [{'id': 'a', 'created_at': '2026-10-12T00:00:00Z', 'complete': 5},
                  {'id': 'b', 'created_at': '2026-10-13T00:00:00Z', 'complete': 5}], 'default': 'b'},
        {'runs': [{'id': 'a', 'created_at': '2026-10-12T00:00:00Z', 'complete': 0}], 'default': None},
        {'runs': [], 'default': None},
    ]
    for case in cases:
        chosen = DEFAULT_RUN.choose(case['runs'])
        assert (chosen and chosen['id']) == case['default'], case
    write('default_run.json', {'rule': DEFAULT_RUN.id, 'cases': cases})


def bls_grid_vectors():
    durations = [0.04, 0.06, 0.08, 0.1, 0.12, 0.16, 0.2]
    cases = [{'baseline_days': b, 'minimum_period': lo, 'maximum_period': hi, 'duration_days': d, 'frequency_factor': f,
              'trials': BLS_GRID.trials(b, lo, hi, d, f)}
             for b, lo, hi, d, f in ((27.406, 0.5, 10, durations, 1), (27.406, 0.5, 20, durations, 1),
                                     (27.406, 0.5, 10, durations, 0.5), (54.8, 0.5, 10, durations, 1),
                                     (27.406, 2, 1, durations, 1), (0, 0.5, 10, durations, 1))]
    write('bls_grid.json', {'rule': BLS_GRID.id, 'cases': cases,
                            'note': 'Run e0534db5 recorded 35678 trials for WASP-18 b, baseline 27.406 d, T01 defaults.'})


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    target_query()
    fold_vectors()
    parameter_vectors()
    status_vectors()
    default_run_vectors()
    bls_grid_vectors()
    print('wrote', ', '.join(sorted(p.name for p in OUT.glob('*.json'))))
