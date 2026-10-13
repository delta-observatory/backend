import gzip
import json
import uuid

from conftest import add_finished_job, add_observation


def test_health(client):
    response = client.get('/api/health')
    assert response.status_code == 200
    assert response.json()['database'] == 'ok'


def test_methods_and_defaults(client):
    methods = {m['id']: m for m in client.get('/api/methods').json()}
    assert set(methods) == {'tess-bls', 'tess-spoc-index'}
    bls = client.get('/api/methods/tess-bls/1').json()
    assert bls['kind'] == 'analysis'
    assert 'Provisional' in bls['scientific_status']
    assert bls['defaults']['search']['minimum_period'] == 0.5
    assert bls['defaults']['flatten']['window_length'] == 1001
    assert bls['defaults']['search']['duration_days'] == [0.04, 0.06, 0.08, 0.1, 0.12, 0.16, 0.2]
    assert client.get('/api/methods/tess-bls/2').status_code == 404


def test_create_run_applies_defaults(client, conn):
    oid = add_observation(conn)
    response = client.post('/api/runs', json={
        'method': {'id': 'tess-bls', 'version': 1},
        'parameters': {'search': {'maximum_period': 8.0}},
        'observation_ids': [str(oid)], 'label': 'test'})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run['parameters']['search']['maximum_period'] == 8.0
    assert run['parameters']['search']['minimum_period'] == 0.5
    assert run['inputs'] == [str(oid)]
    assert [j['state'] for j in run['jobs']] == ['queued']
    job = client.get(f"/api/jobs/{run['jobs'][0]['id']}").json()
    assert job['document']['contract'] == 'job/1'
    assert job['document']['limits'] == {'maximum_seconds': 600, 'maximum_memory_mib': 2048}
    assert job['document']['inputs'] == [{'observation_id': str(oid)}]
    runs = client.get('/api/runs').json()
    assert runs[0]['job_states'] == {'queued': 1}


def test_create_run_rejects_invalid_parameters(client, conn):
    oid = add_observation(conn)
    response = client.post('/api/runs', json={
        'method': {'id': 'tess-bls', 'version': 1},
        'parameters': {'flatten': {'window_length': 1000}, 'search': {'objective': 'chi2'}},
        'observation_ids': [str(oid)]})
    assert response.status_code == 422
    locs = [e['loc'] for e in response.json()['detail']]
    assert ['parameters', 'flatten', 'window_length'] in locs
    assert ['parameters', 'search', 'objective'] in locs
    assert conn.execute('SELECT count(*) AS n FROM run').fetchone()['n'] == 0


def test_create_run_rejects_constraint_violation(client, conn):
    oid = add_observation(conn)
    response = client.post('/api/runs', json={
        'method': {'id': 'tess-bls', 'version': 1},
        'parameters': {'search': {'minimum_period': 0.1}},
        'observation_ids': [str(oid)]})
    assert response.status_code == 422
    assert response.json()['detail'][0]['loc'] == ['parameters', 'search', 'duration_days']


def test_create_run_rejects_unready_missing_and_index_method(client, conn):
    invalid = add_observation(conn, state='invalid', tic=1)
    response = client.post('/api/runs', json={
        'method': {'id': 'tess-bls', 'version': 1},
        'observation_ids': [str(invalid), str(uuid.uuid4())]})
    assert response.status_code == 422
    messages = [e['msg'] for e in response.json()['detail']]
    assert 'observation state is invalid, not ready' in messages
    assert 'observation not found' in messages
    response = client.post('/api/runs', json={
        'method': {'id': 'tess-spoc-index', 'version': 1}, 'observation_ids': [str(invalid)]})
    assert response.status_code == 422


def test_index_conflict(client, conn):
    first = client.post('/api/datasets/tess-spoc-light-curves/index')
    assert first.status_code == 202, first.text
    second = client.post('/api/datasets/tess-spoc-light-curves/index')
    assert second.status_code == 409
    assert second.json()['detail']['job']['id'] == first.json()['job_id']
    datasets = client.get('/api/datasets').json()
    assert datasets[0]['id'] == 'tess-spoc-light-curves'
    assert client.post('/api/datasets/other-dataset/index').status_code == 422
    # A finished index job no longer blocks a new one.
    client.post(f"/api/jobs/{first.json()['job_id']}/cancel")
    assert client.post('/api/datasets/tess-spoc-light-curves/index').status_code == 202


def test_observations(client, conn):
    a = add_observation(conn, tic=2, labels={'label': 'planet-host'})
    add_observation(conn, tic=3, labels={'label': 'no-known-planet'})
    listed = client.get('/api/observations', params={'label': 'planet-host'}).json()
    assert listed['total'] == 1 and listed['items'][0]['id'] == str(a)
    detail = client.get(f'/api/observations/{a}').json()
    assert detail['target_id'] == 'TIC:2' and detail['artifacts'] == []
    assert client.get(f'/api/observations/{uuid.uuid4()}').status_code == 404


def test_cancel_rules(client, conn):
    oid = add_observation(conn)
    run = client.post('/api/runs', json={'method': {'id': 'tess-bls', 'version': 1},
                                         'observation_ids': [str(oid)]}).json()
    job_id = run['jobs'][0]['id']
    response = client.post(f'/api/jobs/{job_id}/cancel')
    assert response.status_code == 200 and response.json()['state'] == 'cancelled'
    assert client.post(f'/api/jobs/{job_id}/cancel').status_code == 409
    assert client.post(f'/api/jobs/{uuid.uuid4()}/cancel').status_code == 404


def test_results_and_artifact_stream(client, conn, artifact_root):
    oid = add_observation(conn)
    payload = json.dumps({'format': 'columns/1', 'length': 0, 'columns': []}).encode()
    run_id, job_id, artifact_id = add_finished_job(conn, artifact_root, oid, payload=payload)
    results = client.get(f'/api/runs/{run_id}/results').json()
    job = results['jobs'][0]
    assert job['outcome'] == 'complete'
    search = {m['name']: m for m in job['measurements']['search']}
    assert search['period']['uncertainty'] == {'kind': 'unavailable', 'reason': 'grid value'}
    assert search['depth']['uncertainty'] == {'kind': 'standard', 'value': 1.45e-05}
    assert search['depth']['quality_flags'] == ['trend_removal_can_reduce_depth']
    assert search['transit_time']['time_scale'] == 'tdb'
    assert 'time_scale' not in search['period']
    assert job['measurements']['reference_comparison'][0]['value'] is True
    assert job['artifacts'][0]['content_url'] == f'/api/artifacts/{artifact_id}/content'

    meta = client.get(f'/api/artifacts/{artifact_id}').json()
    response = client.get(f'/api/artifacts/{artifact_id}/content', headers={'Accept-Encoding': 'gzip'})
    assert response.status_code == 200
    assert response.headers['content-encoding'] == 'gzip'
    assert response.headers['content-type'].startswith('application/vnd.delta.columns+json')
    assert response.headers['etag'] == f'"{meta["sha256"]}"'
    assert json.loads(response.content) == json.loads(payload)  # httpx decodes gzip
    cached = client.get(f'/api/artifacts/{artifact_id}/content', headers={'If-None-Match': f'"{meta["sha256"]}"'})
    assert cached.status_code == 304


def test_artifact_path_outside_root_is_rejected(client, conn, artifact_root):
    oid = add_observation(conn)
    _, job_id, artifact_id = add_finished_job(conn, artifact_root, oid)
    # The table constraint rejects '..'. A symbolic link is the remaining way out of the root.
    outside = artifact_root.parent / 'secret.txt'
    outside.write_text('secret')
    (artifact_root / 'link.json').symlink_to(outside)
    conn.execute("UPDATE artifact SET path = 'link.json' WHERE id = %s", [artifact_id])
    assert client.get(f'/api/artifacts/{artifact_id}/content').status_code == 400
    conn.execute("UPDATE artifact SET path = 'missing.json' WHERE id = %s", [artifact_id])
    assert client.get(f'/api/artifacts/{artifact_id}/content').status_code == 404
