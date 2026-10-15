"""Endpoints that give agents the values and rules that the frontend shows (ROADMAP R8)."""
import gzip
import json
import uuid

from conftest import add_job, add_observation, add_run

from test_rules import load


def test_observation_list_has_sky_position_and_magnitude(client, conn):
    add_observation(conn, header={'RA_OBJ': 24.35431, 'DEC_OBJ': -45.67788, 'TESSMAG': 8.829})
    add_observation(conn, tic=5, header={'RA_OBJ': 'not a number'})
    items = {i['target_id']: i for i in client.get('/api/observations').json()['items']}
    assert (items['TIC:100100827']['ra_deg'], items['TIC:100100827']['dec_deg'], items['TIC:100100827']['tmag']) == \
        (24.35431, -45.67788, 8.829)
    assert items['TIC:5']['ra_deg'] is None and items['TIC:5']['tmag'] is None


def target_fixture(conn):
    """The targets of tests/vectors/target_query.json as observations with one run of measurements."""
    data = load('target_query.json')
    run_id = add_run(conn)
    ids = {}
    for t in data['targets']:
        labels = {'class': 'planet_host' if t['planet_name'] else 'no_known_planet'}
        if t['planet_name']:
            labels['planets'] = [{'name': t['planet_name'], 'period_days': t['reference_period_days']}]
        oid = add_observation(conn, tic=int(t['target_id'].split(':')[1]), sector=t['sector'], labels=labels,
                              header={'TESSMAG': t['tmag'], 'RA_OBJ': 10.0, 'DEC_OBJ': -5.0})
        ids[str(oid)] = t['id']
        measurements = [('search', 'period', t['period_days']), ('search', 'depth_snr', t['depth_snr']),
                        ('reference_comparison', 'relative_period_error', t['relative_period_error'])]
        if t['period_days'] is not None:
            add_job(conn, run_id, oid, measurements=[m for m in measurements if m[2] is not None])
    return run_id, ids, data


def test_targets_query_in_sql_matches_the_shared_vectors(client, conn):
    run_id, ids, data = target_fixture(conn)
    for case in data['cases']:
        response = client.get('/api/targets', params={'run_id': str(run_id), 'q': case['q'], 'sort': 'target_id'})
        if case['error_count']:
            assert response.status_code == 422, case
            assert len(response.json()['detail']) == case['error_count']
            continue
        assert response.status_code == 200, (case, response.text)
        got = sorted(ids[i['observation_id']] for i in response.json()['items'])
        assert got == sorted(case['match']), case


def test_targets_values_status_sort_and_paging(client, conn):
    run_id, ids, _ = target_fixture(conn)
    body = client.get('/api/targets', params={'run_id': str(run_id)}).json()
    assert body['status_rule'] == 'period-agreement/1' and body['grammar'] == 'target-query/1'
    assert body['units']['period_days'] == 'd' and body['total'] == 5
    rows = body['items']
    # Default order: depth S/N, highest first. A null value sorts last.
    assert [ids[r['observation_id']] for r in rows] == ['b', 'a', 'd', 'c', 'e']
    by = {ids[r['observation_id']]: r for r in rows}
    assert by['a']['status'] == 'match' and by['b']['status'] == 'signal' and by['c']['status'] == 'miss'
    assert by['e']['status'] == 'quiet' and by['e']['job_state'] is None
    assert by['a']['planet_name'] == 'WASP-18 b' and by['a']['reference_period_days'] == 0.9415
    assert by['a']['tmag'] == 8.83 and by['a']['ra_deg'] == 10.0
    page = client.get('/api/targets', params={'run_id': str(run_id), 'limit': 2, 'offset': 2}).json()
    assert page['total'] == 5 and [ids[r['observation_id']] for r in page['items']] == ['d', 'c']
    hosts = client.get('/api/targets', params={'run_id': str(run_id), 'class': 'no_known_planet'}).json()
    assert sorted(ids[r['observation_id']] for r in hosts['items']) == ['b', 'd']
    assert client.get('/api/targets', params={'sort': 'colour'}).status_code == 422
    # Without a run, the measurement fields are null.
    plain = client.get('/api/targets').json()['items']
    assert all(r['period_days'] is None for r in plain)


def test_default_run_rule(client, conn):
    assert client.get('/api/runs/default').status_code == 404
    o1, o2 = add_observation(conn, tic=1), add_observation(conn, tic=2)
    big = add_run(conn, label='all', created_at='2026-10-12T00:00:00Z')
    small = add_run(conn, label='one', created_at='2026-10-14T00:00:00Z')
    add_job(conn, big, o1)
    add_job(conn, big, o2)
    add_job(conn, small, o1)
    body = client.get('/api/runs/default').json()
    assert body['rule'] == 'default-run/1' and body['run']['id'] == str(big) and body['run']['job_states'] == {'complete': 2}
    newer = add_run(conn, label='tie', created_at='2026-10-15T00:00:00Z')
    add_job(conn, newer, o1)
    add_job(conn, newer, o2)
    assert client.get('/api/runs/default').json()['run']['id'] == str(newer)
    assert client.get('/api/runs/default', params={'dataset_id': 'other'}).status_code == 404
    assert client.get('/api/runs/default', params={'method_id': 'tess-spoc-index'}).status_code == 404


def test_estimate_checks_like_create_and_writes_nothing(client, conn):
    oid = add_observation(conn, time_start=1354.111, time_stop=1381.517)
    other = add_observation(conn, tic=7, time_start=1354.111, time_stop=1354.111 + 54.8)
    request = {'method': {'id': 'tess-bls', 'version': 1}, 'observation_ids': [str(oid), str(other)]}
    body = client.post('/api/runs/estimate', json=request).json()
    est = body['estimate']
    assert body['observation_count'] == 2 and est['rule'] == 'bls-grid/1' and est['limit'] == 100000
    assert est['trials']['min'] == 35677 and est['over_limit'] == 1 and est['items'][1]['over_limit']
    assert est['seconds_per_trial'] is None and est['worker_seconds'] is None
    assert conn.execute('SELECT count(*) AS n FROM run').fetchone()['n'] == 0
    # The same 422 as POST /runs.
    bad = {**request, 'parameters': {'search': {'minimum_period': 12}}}
    estimate, create = client.post('/api/runs/estimate', json=bad), client.post('/api/runs', json=bad)
    assert estimate.status_code == create.status_code == 422
    assert estimate.json() == create.json()
    assert estimate.json()['detail'][0]['rule'] == 'search.minimum_period < search.maximum_period'


def test_estimate_uses_measured_rate(client, conn):
    oid = add_observation(conn, time_start=1354.111, time_stop=1381.517)
    run_id = add_run(conn)
    for elapsed in (6.0, 7.0, 8.0):
        add_job(conn, run_id, add_observation(conn, tic=int(elapsed * 10)), result={
            'contract': 'result/1', 'outcome': 'complete', 'measurements': [], 'artifacts': [],
            'diagnostics': {'grid_trials': 35000}, 'resources': {'elapsed_seconds': elapsed}})
    est = client.post('/api/runs/estimate', json={'method': {'id': 'tess-bls', 'version': 1},
                                                  'observation_ids': [str(oid)]}).json()['estimate']
    assert est['seconds_per_trial']['jobs'] == 3
    assert est['seconds_per_trial']['median'] == 7.0 / 35000
    assert est['worker_seconds'] == 7.0 / 35000 * 35677


def write_artifact(conn, artifact_root, job_id, observation_id, kind, document):
    path = f'jobs/{job_id}/1/{kind}.json.gz'
    data = gzip.compress(json.dumps(document).encode())
    (artifact_root / path).parent.mkdir(parents=True, exist_ok=True)
    (artifact_root / path).write_bytes(data)
    conn.execute("""INSERT INTO artifact (job_id, observation_id, kind, media_type, content_encoding, path, bytes, sha256)
                    VALUES (%s, %s, %s, 'application/json', 'gzip', %s, %s, %s)""",
                 [job_id, observation_id, kind, path, len(data), '0' * 64])


def test_fold_endpoint_matches_the_shared_vectors(client, conn, artifact_root):
    data = load('fold.json')
    case = data['cases'][1]
    oid = add_observation(conn)
    run_id = add_run(conn)
    job_id = add_job(conn, run_id, oid)
    write_artifact(conn, artifact_root, job_id, oid, 'prepared_series', {'format': 'columns/1', 'length': len(data['time']), 'columns': [
        {'name': 'time', 'unit': 'd', 'dtype': 'float64', 'values': data['time']},
        {'name': 'flattened_flux', 'unit': '1', 'dtype': 'float64', 'values': data['flux']}]})
    write_artifact(conn, artifact_root, job_id, oid, 'search_summary', {'best': {
        'period': case['period'], 'transit_time': case['t0'], 'duration': case['duration_days'], 'depth': 0.01}})
    url = f'/api/runs/{run_id}/observations/{oid}/fold'
    lo, hi = case['time_range']
    body = client.get(url, params={'t_min': lo, 't_max': hi}).json()
    assert body['display_calculation'] and body['stored_ephemeris'] and body['rule'] == 'fold-window/1'
    assert body['window_hours'] == case['window_hours'] and body['n_points'] == case['n_points']
    assert [b['n'] for b in body['bins']] == [b['n'] for b in case['bins']]
    assert [round(b['mean'], 12) for b in body['bins']] == [round(b['mean'], 12) for b in case['bins']]
    assert body['box_model'] == {'depth': 0.01, 'duration_hours': case['duration_days'] * 24}
    display = client.get(url, params={'period': 0.75, 'window_hours': data['cases'][2]['window_hours'], 'points': 'true'}).json()
    assert not display['stored_ephemeris'] and display['box_model'] is None
    assert display['n_points'] == data['cases'][2]['n_points'] == len(display['points']['x_hours'])
    assert client.get(f'/api/runs/{run_id}/observations/{uuid.uuid4()}/fold').status_code == 404


def test_results_filter_by_observation(client, conn):
    o1, o2 = add_observation(conn, tic=1), add_observation(conn, tic=2)
    run_id = add_run(conn)
    add_job(conn, run_id, o1, measurements=[('search', 'period', 1.0)])
    add_job(conn, run_id, o2, measurements=[('search', 'period', 2.0)])
    jobs = client.get(f'/api/runs/{run_id}/results', params={'observation_id': str(o2)}).json()['jobs']
    assert len(jobs) == 1 and jobs[0]['measurements']['search'][0]['value'] == 2.0
    assert len(client.get(f'/api/runs/{run_id}/results').json()['jobs']) == 2


def test_rules_endpoint(client):
    rules = {r['id']: r for r in client.get('/api/rules').json()}
    assert set(rules) == {'period-agreement/1', 'default-run/1', 'target-query/1', 'fold-window/1', 'bls-grid/1'}
    assert rules['period-agreement/1']['thresholds'] == {'match_below': 0.01, 'near_below': 0.05, 'signal_snr_above': 100.0}
    assert rules['target-query/1']['fields']['err']['requires_labels']


def test_pool_commits_each_write_before_the_response(client):
    # Autocommit connections make each 'with conn.transaction()' a real transaction.
    with client.app.state.pool.connection() as conn:
        assert conn.autocommit
