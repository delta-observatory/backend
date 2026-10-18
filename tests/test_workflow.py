"""Workflow controls and access rules: run scope, the free-text rule, cancellation, retries, run comparison,
evidence exports, and import keys."""
import gzip
import hashlib
import io
import json
import subprocess
import sys
import zipfile

from jsonschema import Draft202012Validator
import psycopg
from psycopg.types.json import Jsonb
import pytest

from conftest import ROOT, add_campaign, add_finished_job, add_job, add_observation, add_run, token_for, write_register

LABELS = {'class': 'planet_host', 'planets': [{'name': 'WASP-18 b', 'period_days': 0.94145}]}
HEADER = {'TICID': 100100827, 'OBJECT': 'TIC 100100827', 'RA_OBJ': 24.35, 'SECTOR': 2, 'TESSMAG': 8.8}
BLS = {'id': 'tess-bls', 'version': 1}
SHA = 'a' * 64
VERIFY = ROOT / 'scripts' / 'verify_export.py'
MANIFEST_SCHEMA = ROOT / 'vendor' / 'contracts' / 'exports' / 'manifest' / 'v1.schema.json'


def agent_in(conn, name, **campaign):
    campaign_id = add_campaign(conn, f'c-{name}', **campaign)
    return campaign_id, token_for(conn, name, campaign_id=campaign_id)


def set_campaign(conn, run_id, campaign_id):
    conn.execute('UPDATE run SET campaign_id = %s WHERE id = %s', [campaign_id, run_id])


def fail_job(conn, job_id, message='raw/tess-spoc-light-curves/tess-0000000100100827_lc.fits is unreadable'):
    conn.execute("""UPDATE job SET state = 'failed', finished_at = now(), result = %s WHERE id = %s""",
                 [Jsonb({'contract': 'result/1', 'job_id': str(job_id), 'outcome': 'failed', 'measurements': [],
                         'artifacts': [], 'error': {'kind': 'OSError', 'message': message, 'traceback': message}}), job_id])


def test_agents_see_only_runs_of_their_campaign_or_without_a_campaign(client, conn, artifact_root):
    oid = add_observation(conn)
    person_run, _, _ = add_finished_job(conn, artifact_root, oid)
    campaign_a, agent_a = agent_in(conn, 'agent-a')
    campaign_b, _ = agent_in(conn, 'agent-b')
    other_run, other_job, other_artifact = add_finished_job(conn, artifact_root, oid)
    add_job(conn, other_run, add_observation(conn, tic=7), measurements=[('search', 'period', 1.0)])
    set_campaign(conn, other_run, campaign_b)
    own = client.post('/api/runs', headers=agent_a, json={'method': BLS, 'observation_ids': [str(oid)]}).json()

    assert {r['id'] for r in client.get('/api/runs', headers=agent_a).json()} == {str(person_run), own['id']}
    assert {r['id'] for r in client.get('/api/runs').json()} == {str(person_run), own['id'], str(other_run)}
    # The other campaign's run has more complete jobs, but the agent's default run is the run it sees.
    assert client.get('/api/runs/default').json()['run']['id'] == str(other_run)
    assert client.get('/api/runs/default', headers=agent_a).json()['run']['id'] == str(person_run)
    for method, path in (('get', f'/api/runs/{other_run}'), ('get', f'/api/runs/{other_run}/results'),
                         ('get', f'/api/runs/{other_run}/observations/{oid}/fold'),
                         ('get', f'/api/targets?run_id={other_run}'),
                         ('get', f'/api/runs/compare?run_a={person_run}&run_b={other_run}'),
                         ('get', f'/api/runs/{other_run}/export'), ('post', f'/api/runs/{other_run}/retry'),
                         ('post', f'/api/runs/{other_run}/cancel'), ('get', f'/api/jobs/{other_job}'),
                         ('post', f'/api/jobs/{other_job}/cancel'), ('get', f'/api/artifacts/{other_artifact}'),
                         ('get', f'/api/artifacts/{other_artifact}/content')):
        response = getattr(client, method)(path, headers=agent_a)
        assert response.status_code == 404, (path, response.status_code, response.text)
    # A reference to the other campaign's job is "not found", as in the run views.
    refused = client.post('/api/candidates', headers=agent_a, json={
        'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'platform', 'summary': 'x',
        'observation_ids': [str(oid)], 'evidence': [{'kind': 'job', 'id': str(other_job)}]})
    assert refused.status_code == 422 and refused.json()['detail'][0]['msg'] == f'job {other_job} not found'
    # A person keeps the existing explanation.
    mixed = client.post('/api/candidates', params={'campaign_id': str(campaign_a)}, json={
        'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'platform', 'summary': 'x',
        'observation_ids': [str(oid)], 'evidence': [{'kind': 'job', 'id': str(other_job)}]})
    assert mixed.status_code == 422 and 'another campaign' in mixed.json()['detail'][0]['msg']


def test_agent_reads_need_an_approved_open_campaign(client, conn, artifact_root):
    oid = add_observation(conn)
    run_id, job_id, artifact_id = add_finished_job(conn, artifact_root, oid)
    campaign_id, agent = agent_in(conn, 'agent-wait', approved=False)
    request = {'method': BLS, 'observation_ids': [str(oid)]}
    for method, path, body in (('get', '/api/datasets', None), ('get', '/api/runs', None), ('get', '/api/runs/default', None),
                               ('get', f'/api/runs/{run_id}', None), ('get', f'/api/runs/{run_id}/results', None),
                               ('get', f'/api/jobs/{job_id}', None), ('get', f'/api/artifacts/{artifact_id}', None),
                               ('get', f'/api/runs/{run_id}/export', None),
                               ('get', f'/api/runs/compare?run_a={run_id}&run_b={run_id}', None),
                               ('post', '/api/runs/estimate', request), ('post', f'/api/runs/{run_id}/retry', None)):
        kwargs = {'json': body} if body is not None else {}
        response = getattr(client, method)(path, headers=agent, **kwargs)
        assert response.status_code == 403 and 'not approved' in response.json()['detail'], (path, response.text)
    for path in ('/api/me', '/api/methods', '/api/rules', f'/api/campaigns/{campaign_id}'):
        assert client.get(path, headers=agent).status_code == 200, path
    client.post(f'/api/campaigns/{campaign_id}/approve', json={'campaign_file_sha256': SHA})
    assert client.get('/api/runs', headers=agent).status_code == 200
    client.post(f'/api/campaigns/{campaign_id}/close')
    assert client.get('/api/datasets', headers=agent).status_code == 403
    assert client.get(f'/api/runs/{run_id}', headers=agent).status_code == 403


def test_run_views_leave_out_protected_observations(client, conn, artifact_root, cohorts_path):
    protected, free = add_observation(conn, tic=111), add_observation(conn, tic=222)
    run_id, _, artifact_id = add_finished_job(conn, artifact_root, protected)
    add_job(conn, run_id, free)
    write_register(cohorts_path, [('eval-a', 'evaluation', [], ['TIC:111'])])
    _, agent = agent_in(conn, 'agent-p')
    detail = client.get(f'/api/runs/{run_id}', headers=agent).json()
    assert detail['inputs'] == [str(free)] and detail['protected_excluded'] == 1
    assert [j['observation_id'] for j in detail['jobs']] == [str(free)]
    summary = next(r for r in client.get('/api/runs', headers=agent).json() if r['id'] == str(run_id))
    assert summary['input_count'] == 1 and summary['protected_excluded'] == 1 and summary['job_states'] == {'complete': 1}
    assert client.get(f'/api/runs/{run_id}').json()['protected_excluded'] == 0
    # The estimate refuses a protected observation, as the submission does, and records the refusal.
    estimate = client.post('/api/runs/estimate', headers=agent, json={'method': BLS, 'observation_ids': [str(protected)]})
    assert estimate.status_code == 422 and 'reserved for evaluation' in estimate.json()['detail'][0]['msg']
    assert client.get(f'/api/artifacts/{artifact_id}', headers=agent).status_code == 403
    events = [(e['operation'], e['outcome']) for e in client.get('/api/access-events').json()['items']]
    assert ('run estimate', 'refused') in events and ('artifact metadata', 'refused') in events



def test_run_list_filters_by_observation(client, conn, artifact_root, cohorts_path):
    protected, free, other = add_observation(conn, tic=111), add_observation(conn, tic=222), add_observation(conn, tic=333)
    both, _, _ = add_finished_job(conn, artifact_root, protected)
    add_job(conn, both, free)
    only_other, _, _ = add_finished_job(conn, artifact_root, other)
    assert [r['id'] for r in client.get(f'/api/runs?observation_id={free}').json()] == [str(both)]
    assert [r['id'] for r in client.get(f'/api/runs?observation_id={other}').json()] == [str(only_other)]
    write_register(cohorts_path, [('eval-a', 'evaluation', [], ['TIC:111'])])
    _, agent = agent_in(conn, 'agent-filter')
    assert client.get(f'/api/runs?observation_id={protected}', headers=agent).json() == []
    assert [r['id'] for r in client.get(f'/api/runs?observation_id={free}', headers=agent).json()] == [str(both)]

def test_identity_blinding_withholds_paths_errors_and_provenance(client, conn, artifact_root):
    oid = add_observation(conn, labels=LABELS, header=HEADER)
    conn.execute("""UPDATE observation SET provenance = %s WHERE id = %s""",
                 [Jsonb({'selection': 'datasets/x/selection.json', 'owner': 'research agent',
                         'source_id': 'mast:TESS/product/tess-0000000100100827_lc.fits'}), oid])
    conn.execute("""UPDATE dataset SET findings = %s""",
                 [Jsonb([{'kind': 'unlabeled', 'paths': ['a_lc.fits', 'b_lc.fits'], 'message': 'No label.'},
                         {'kind': 'invalid_file', 'path': 'c_lc.fits', 'findings': []}])])
    run_id = add_run(conn)
    job_id = add_job(conn, run_id, oid, state='queued')
    fail_job(conn, job_id)
    _, blind = agent_in(conn, 'agent-blind', blinding='labels_identity')
    findings = client.get('/api/datasets', headers=blind).json()[0]['findings']
    assert findings[0]['path_count'] == 2 and 'paths' not in findings[0] and findings[1]['path_count'] == 1
    assert client.get('/api/datasets').json()[0]['findings'][0]['paths'] == ['a_lc.fits', 'b_lc.fits']
    error = client.get(f'/api/runs/{run_id}', headers=blind).json()['jobs'][0]['error']
    assert error['kind'] == 'OSError' and error['message'] is None and error['traceback'] is None
    assert client.get(f'/api/runs/{run_id}/results', headers=blind).json()['jobs'][0]['error']['message'] is None
    assert client.get(f'/api/jobs/{job_id}', headers=blind).json()['result']['error']['message'] is None
    assert 'unreadable' in client.get(f'/api/jobs/{job_id}').json()['result']['error']['message']
    assert client.get(f'/api/observations/{oid}', headers=blind).json()['provenance'] is None
    assert client.get(f'/api/observations/{oid}').json()['provenance']['owner'] == 'research agent'
    _, opened = agent_in(conn, 'agent-open', blinding='open')
    assert client.get(f'/api/runs/{run_id}', headers=opened).json()['jobs'][0]['error']['message']


def test_free_text_rule_withholds_other_writers_text_from_blinded_agents(client, conn, artifact_root):
    oid = add_observation(conn, labels=LABELS, header=HEADER)
    run_id, job_id, _ = add_finished_job(conn, artifact_root, oid)
    conn.execute("UPDATE run SET label = 'WASP-18 b check' WHERE id = %s", [run_id])
    campaign_id, agent = agent_in(conn, 'agent-t', blinding='labels')
    lab_file = {'kind': 'external', 'uri': 'lab:wasp-18/packet', 'sha256': SHA, 'bytes': 10, 'description': 'WASP-18 packet'}
    mine = client.post('/api/candidates', headers=agent, json={
        'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'platform', 'summary': 'Agent summary.',
        'observation_ids': [str(oid)], 'evidence': [{'kind': 'job', 'id': str(job_id)}, lab_file]}).json()
    theirs = client.post('/api/candidates', params={'campaign_id': str(campaign_id)}, json={
        'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'platform', 'summary': 'This is WASP-18 b.',
        'label': 'W18', 'observation_ids': [str(oid)], 'evidence': [{'kind': 'job', 'id': str(job_id)}, lab_file]}).json()
    note = client.post('/api/notes', json={'contract': 'note/1', 'body': 'Known planet WASP-18 b.',
                                           'subjects': [{'kind': 'candidate', 'id': mine['id']}]}).json()
    review = client.post('/api/assessments', json={
        'contract': 'assessment/1', 'candidate_id': mine['id'], 'axis': 'evidence', 'status': 'supported',
        'basis': 'review', 'reason': 'Matches WASP-18 b.', 'evidence': [{'kind': 'job', 'id': str(job_id)}]}).json()
    task = client.post('/api/tasks', params={'campaign_id': str(campaign_id)}, json={
        'contract': 'agent-task/1', 'kind': 'vetting', 'title': 'Vet the first candidate.',
        'instructions': 'Use the odd-even test.'}).json()

    seen = client.get(f'/api/candidates/{theirs["id"]}', headers=agent).json()
    assert seen['summary'] is None and seen['label'] is None and seen['text_withheld'] is True
    assert next(e for e in seen['evidence'] if e['kind'] == 'external')['uri'] is None
    assert 'text' in seen['withheld']
    own = client.get(f'/api/candidates/{mine["id"]}', headers=agent).json()
    assert own['summary'] == 'Agent summary.' and own['text_withheld'] is False
    assert next(e for e in own['evidence'] if e['kind'] == 'external')['uri'] == 'lab:wasp-18/packet'
    # The detail of the agent's own candidate holds a person's note and assessment without their text.
    assert own['notes'][0]['body'] is None and own['assessments'][0]['reason'] is None and 'text' in own['withheld']
    listed = client.get('/api/notes', headers=agent).json()
    assert listed['items'][0]['id'] == note['id'] and listed['items'][0]['body'] is None and 'text' in listed['withheld']
    assert client.get(f'/api/assessments/{review["id"]}', headers=agent).json()['reason'] is None
    # Directed text stays: the task title and instructions, and the campaign question.
    claimed = client.post('/api/tasks/claim', headers=agent, json={}).json()['task']
    assert claimed['id'] == task['id'] and claimed['instructions'] == 'Use the odd-even test.'
    assert client.get(f'/api/campaigns/{campaign_id}', headers=agent).json()['question'] == 'Does the test pass?'
    # A person's run label is withheld. The agent's own run label is not.
    assert client.get(f'/api/runs/{run_id}', headers=agent).json()['label'] is None
    made = client.post('/api/runs', headers=agent, json={'method': BLS, 'observation_ids': [str(oid)], 'label': 'mine'}).json()
    assert made['label'] == 'mine' and made['text_withheld'] is False
    # A person sees all text. After the reveal, the agent does too.
    assert client.get(f'/api/candidates/{theirs["id"]}').json()['summary'] == 'This is WASP-18 b.'
    client.post(f'/api/campaigns/{campaign_id}/fix-decisions', headers=agent)
    client.post(f'/api/campaigns/{campaign_id}/reveal', json={'reason': 'Decisions fixed.'})
    assert client.get(f'/api/candidates/{theirs["id"]}', headers=agent).json()['summary'] == 'This is WASP-18 b.'
    assert client.get(f'/api/runs/{run_id}', headers=agent).json()['label'] == 'WASP-18 b check'
    rules = {r['id']: r for r in client.get('/api/rules').json()}
    assert 'does not guarantee scientific blinding' in rules['free-text/1']['limits']


def test_running_jobs_can_be_cancelled_and_the_worker_result_is_discarded(client, conn):
    oid, other, third = add_observation(conn, tic=1), add_observation(conn, tic=2), add_observation(conn, tic=3)
    run = client.post('/api/runs', json={'method': BLS, 'observation_ids': [str(oid), str(other), str(third)]}).json()
    running, _, done = (j['id'] for j in run['jobs'])
    conn.execute("""UPDATE job SET state = 'running', attempts = 1, claimed_by = 'worker-1', started_at = now(),
                        lease_expires_at = now() + interval '1 minute' WHERE id = %s""", [running])
    conn.execute("""UPDATE job SET state = 'complete', attempts = 1, finished_at = now(), result = %s WHERE id = %s""",
                 [Jsonb({'contract': 'result/1', 'job_id': done, 'outcome': 'complete', 'measurements': [], 'artifacts': []}), done])
    cancelled = client.post(f'/api/jobs/{running}/cancel')
    assert cancelled.status_code == 200 and cancelled.json()['was_running'] is True
    row = conn.execute('SELECT state, lease_expires_at, cancelled_by FROM job WHERE id = %s', [running]).fetchone()
    assert row['state'] == 'cancelled' and row['lease_expires_at'] is None and row['cancelled_by'] is not None
    # The worker's lease extension and its final write both require the running state, so they change nothing.
    assert conn.execute("""UPDATE job SET lease_expires_at = now() + interval '1 minute'
                           WHERE id = %s AND claimed_by = 'worker-1' AND state = 'running' RETURNING id""",
                        [running]).fetchone() is None
    with pytest.raises(psycopg.Error) as error:
        conn.execute("UPDATE job SET state = 'complete' WHERE id = %s", [running])
    assert error.value.sqlstate == 'RC002'
    assert client.post(f'/api/jobs/{running}/cancel').status_code == 409
    assert client.post(f'/api/jobs/{done}/cancel').status_code == 409
    whole = client.post(f'/api/runs/{run["id"]}/cancel').json()
    assert whole['cancelled'] == 1 and whole['was_running'] == 0 and whole['job_states'] == {'cancelled': 2, 'complete': 1}
    # A running index job stops only at its end.
    index = client.post('/api/datasets/tess-spoc-light-curves/index').json()
    conn.execute("""UPDATE job SET state = 'running', attempts = 1, lease_expires_at = now() + interval '1 minute'
                    WHERE id = %s""", [index['job_id']])
    assert client.post(f'/api/jobs/{index["job_id"]}/cancel').status_code == 409
    assert client.post(f'/api/runs/{index["run_id"]}/cancel').json()['cancelled'] == 0


def test_retry_repeats_only_failed_and_cancelled_jobs(client, conn):
    ids = [add_observation(conn, tic=t) for t in (1, 2, 3, 4)]
    run = client.post('/api/runs', json={'method': BLS, 'observation_ids': [str(o) for o in ids],
                                         'parameters': {'search': {'maximum_period': 8.0}}}).json()
    jobs = {j['observation_id']: j['id'] for j in run['jobs']}
    fail_job(conn, jobs[str(ids[0])])
    client.post(f'/api/jobs/{jobs[str(ids[1])]}/cancel')
    conn.execute("""UPDATE job SET state = 'complete', finished_at = now(), result = %s WHERE id = %s""",
                 [Jsonb({'contract': 'result/1', 'job_id': jobs[str(ids[2])], 'outcome': 'complete', 'measurements': [],
                         'artifacts': []}), jobs[str(ids[2])]])
    retry = client.post(f'/api/runs/{run["id"]}/retry', json={})
    assert retry.status_code == 201, retry.text
    retried = retry.json()
    assert retried['retry_of'] == run['id'] and retried['parameters_sha256'] == run['parameters_sha256']
    assert sorted(retried['inputs']) == sorted([str(ids[0]), str(ids[1])])
    assert retried['label'].startswith('Retry of')
    assert client.get(f'/api/runs/{run["id"]}').json()['retries'] == [retried['id']]
    assert next(r for r in client.get('/api/runs').json() if r['id'] == retried['id'])['retry_of'] == run['id']
    # The queued fourth job is not repeated, and a run without failed or cancelled jobs gives 409.
    assert client.post(f'/api/runs/{retried["id"]}/retry').status_code == 409
    index = client.post('/api/datasets/tess-spoc-light-curves/index').json()
    assert client.post(f'/api/runs/{index["run_id"]}/retry').status_code == 422
    # The database keeps the method version and parameters of a retry.
    with pytest.raises(psycopg.Error) as error:
        conn.execute("""INSERT INTO run (method_id, method_version, parameters, parameters_sha256, requested_by, retry_of)
                        VALUES ('tess-bls', 1, '{}', repeat('1', 64), 'test', %s)""", [run['id']])
    assert error.value.sqlstate == 'RC003'
    # An agent cannot place a retry of a person's run in its campaign.
    _, agent = agent_in(conn, 'agent-retry')
    refused = client.post(f'/api/runs/{run["id"]}/retry', headers=agent)
    assert refused.status_code == 409 and 'campaign' in refused.json()['detail']


def test_run_comparison_gives_stored_values_and_display_differences(client, conn):
    o1, o2, o3 = (add_observation(conn, tic=t, labels=LABELS) for t in (1, 2, 3))
    a, b = add_run(conn, label='a'), add_run(conn, label='b')
    conn.execute("""UPDATE run SET parameters = '{"search": {"maximum_period": 8.0, "minimum_period": 0.5}}' WHERE id = %s""", [a])
    conn.execute("""UPDATE run SET parameters = '{"search": {"maximum_period": 20.0, "minimum_period": 0.5}}' WHERE id = %s""", [b])
    add_job(conn, a, o1, measurements=[('search', 'period', 1.0), ('search', 'depth_snr', 10.0),
                                       ('reference_comparison', 'relative_period_error', 0.001)])
    add_job(conn, b, o1, measurements=[('search', 'period', 1.5), ('search', 'depth_snr', 12.5),
                                       ('reference_comparison', 'relative_period_error', 0.002)])
    add_job(conn, a, o2, state='failed')
    add_job(conn, b, o2, measurements=[('search', 'period', 2.0)])
    add_job(conn, b, o3, measurements=[('search', 'period', 3.0)])
    compared = client.get('/api/runs/compare', params={'run_a': str(a), 'run_b': str(b)}).json()
    assert compared['rule'] == 'run-comparison/1' and compared['display_calculation'] is True
    assert compared['parameters_equal'] is False
    assert compared['parameter_differences'] == [{'path': 'search.maximum_period', 'a': 8.0, 'b': 20.0}]
    assert compared['summary'] == {'observations': 3, 'in_both': 2, 'only_a': 0, 'only_b': 1, 'state_differs': 1}
    first = next(r for r in compared['rows'] if r['observation_id'] == str(o1))
    assert first['a']['measurements']['search']['period']['value'] == 1.0 and first['a']['measurements']['search']['period']['id']
    period = first['differences']['search']['period']
    assert period['value'] == pytest.approx(0.5) and period['display_calculation'] is True
    assert period['uncertainty']['kind'] == 'unavailable' and 'correlated' in period['uncertainty']['reason']
    assert 'reference_comparison' in first['differences']
    only_b = next(r for r in compared['rows'] if r['observation_id'] == str(o3))
    assert only_b['a'] is None and only_b['differences'] == {}
    one = client.get('/api/runs/compare', params={'run_a': str(a), 'run_b': str(b), 'observation_id': str(o2)}).json()
    assert [r['observation_id'] for r in one['rows']] == [str(o2)] and one['rows'][0]['a']['state'] == 'failed'
    # A blinded agent gets no comparison with known objects.
    _, agent = agent_in(conn, 'agent-cmp', blinding='labels')
    blind = client.get('/api/runs/compare', headers=agent, params={'run_a': str(a), 'run_b': str(b)}).json()
    first = next(r for r in blind['rows'] if r['observation_id'] == str(o1))
    assert 'reference_comparison' not in first['a']['measurements'] and 'reference_comparison' not in first['differences']
    assert blind['run_a']['label'] is None and blind['withheld'] == ['labels']
    index = client.post('/api/datasets/tess-spoc-light-curves/index').json()
    assert client.get('/api/runs/compare', params={'run_a': str(a), 'run_b': index['run_id']}).status_code == 422


def write_artifact(conn, artifact_root, job_id, observation_id, kind, document):
    data = json.dumps(document).encode()
    path = f'jobs/{job_id}/1/{kind}.json'
    (artifact_root / path).parent.mkdir(parents=True, exist_ok=True)
    (artifact_root / path).write_bytes(data)
    return conn.execute("""INSERT INTO artifact (job_id, observation_id, kind, media_type, content_encoding, path, bytes, sha256)
                           VALUES (%s, %s, %s, 'application/json', 'identity', %s, %s, %s) RETURNING id""",
                        [job_id, observation_id, kind, path, len(data), hashlib.sha256(data).hexdigest()]).fetchone()['id']


def verify(tmp_path, data: bytes) -> tuple[int, dict]:
    path = tmp_path / f'{hashlib.sha256(data).hexdigest()[:12]}.zip'
    path.write_bytes(data)
    done = subprocess.run([sys.executable, str(VERIFY), str(path), '--schema', str(MANIFEST_SCHEMA)],
                          check=False, capture_output=True, text=True)
    return done.returncode, json.loads(done.stdout)


def test_run_export_is_reproducible_and_verifiable(client, conn, artifact_root, tmp_path):
    oid = add_observation(conn, labels=LABELS, header=HEADER)
    run_id, job_id, artifact_id = add_finished_job(conn, artifact_root, oid)
    summary_id = write_artifact(conn, artifact_root, job_id, oid, 'search_summary',
                                {'best': {'period': 0.94}, 'reference': {'name': 'WASP-18 b'}, 'source_sha256': SHA})
    m = conn.execute("SELECT id FROM measurement WHERE job_id = %s AND name = 'period'", [job_id]).fetchone()['id']
    made = client.post('/api/candidates', json={
        'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'platform', 'summary': 'Periodic dips.',
        'observation_ids': [str(oid)], 'evidence': [{'kind': 'measurement', 'id': m}]}).json()
    client.post('/api/assessments', json={'contract': 'assessment/1', 'candidate_id': made['id'], 'axis': 'evidence',
                                          'status': 'supported', 'basis': 'review', 'reason': 'Clear dips.',
                                          'evidence': [{'kind': 'measurement', 'id': m}]})

    first = client.get(f'/api/runs/{run_id}/export', params={'artifacts': 'all'})
    second = client.get(f'/api/runs/{run_id}/export', params={'artifacts': 'all'})
    assert first.status_code == 200 and first.headers['content-type'] == 'application/zip'
    assert first.content == second.content
    assert 'attachment' in first.headers['content-disposition']
    archive = zipfile.ZipFile(io.BytesIO(first.content))
    manifest_bytes = archive.read('manifest.json')
    assert hashlib.sha256(manifest_bytes).hexdigest() == first.headers['x-export-manifest-sha256']
    manifest = json.loads(manifest_bytes)
    assert not list(Draft202012Validator(json.loads(MANIFEST_SCHEMA.read_text())).iter_errors(manifest))
    assert manifest['subject'] == {'kind': 'run', 'id': str(run_id)} and manifest['withheld'] == []
    paths = {f['path']: f for f in manifest['files']}
    stored = paths[f'artifacts/{artifact_id}/periodogram.json.gz']
    assert stored['sha256'] == stored['stored_sha256'] and 'redacted' not in stored
    assert archive.read(stored['path']) == (artifact_root / f'jobs/{job_id}/1/periodogram.json.gz').read_bytes()
    measurements = json.loads(archive.read('measurements.json'))
    depth = next(x for x in measurements if x['name'] == 'depth')
    assert depth['unit'] == '1' and depth['uncertainty'] == {'kind': 'standard', 'value': 1.45e-05}
    assert depth['quality_flags'] == ['trend_removal_can_reduce_depth'] and depth['target_id'] == 'TIC:100100827'
    assert archive.read('measurements.csv').decode().splitlines()[0].startswith('measurement_id,job_id,observation_id')
    run_doc = json.loads(archive.read('run.json'))
    assert run_doc['jobs'][0]['result']['software']['worker_version'] == 'test'
    records = json.loads(archive.read('records.json'))
    assert [c['id'] for c in records['candidates']] == [made['id']]
    assert records['candidates'][0]['assessments'][0]['actor']['name'] == 'richard'
    assert any('provisional' in item.lower() or 'tess-bls' in item for item in manifest['limitations'])
    status, report = verify(tmp_path, first.content)
    assert status == 0 and report['passed'] and report['files_checked'] == len(manifest['files'])

    # A changed file fails the check.
    tampered = io.BytesIO()
    with zipfile.ZipFile(tampered, 'w') as out:
        for name in archive.namelist():
            data = archive.read(name)
            out.writestr(name, data + b' ' if name == 'run.json' else data)
    status, report = verify(tmp_path, tampered.getvalue())
    assert status == 1 and any('run.json' in f for f in report['failures'])

    # A blinded agent's export applies the same redaction as the views, and the check passes with the redaction mark.
    _, agent = agent_in(conn, 'agent-x', blinding='labels_identity')
    blind = client.get(f'/api/runs/{run_id}/export', headers=agent, params={'artifacts': 'all'})
    blind_zip = zipfile.ZipFile(io.BytesIO(blind.content))
    blind_manifest = json.loads(blind_zip.read('manifest.json'))
    assert blind_manifest['withheld'] == ['labels', 'identity'] and blind_manifest['exported_by']['kind'] == 'agent'
    redacted = next(f for f in blind_manifest['files'] if f.get('artifact_id') == str(summary_id))
    assert redacted['redacted'] is True and redacted['sha256'] != redacted['stored_sha256']
    assert 'reference' not in json.loads(blind_zip.read(redacted['path']))
    assert all(x['role'] != 'reference_comparison' and x['target_id'] is None
               for x in json.loads(blind_zip.read('measurements.json')))
    assert json.loads(blind_zip.read('observations.json'))[0]['target_id'] is None
    assert verify(tmp_path, blind.content)[0] == 0
    # Without artifacts, the export lists them with their SHA-256 only.
    listed = zipfile.ZipFile(io.BytesIO(client.get(f'/api/runs/{run_id}/export').content))
    assert not any(n.startswith('artifacts/') for n in listed.namelist())
    assert str(artifact_id) in {a['id'] for a in json.loads(listed.read('artifacts.json'))}


def test_candidate_export_holds_the_evidence_and_the_records(client, conn, artifact_root, tmp_path):
    oid = add_observation(conn, labels=LABELS, header=HEADER)
    run_id, job_id, artifact_id = add_finished_job(conn, artifact_root, oid)
    sha = conn.execute('SELECT sha256 FROM artifact WHERE id = %s', [artifact_id]).fetchone()['sha256']
    made = client.post('/api/candidates', json={
        'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'platform', 'summary': 'Periodic dips.',
        'observation_ids': [str(oid)], 'evidence': [{'kind': 'job', 'id': str(job_id)},
                                                    {'kind': 'artifact', 'id': str(artifact_id), 'sha256': sha},
                                                    {'kind': 'external', 'uri': 'lab:x', 'sha256': SHA, 'bytes': 3}]}).json()
    client.post('/api/notes', json={'contract': 'note/1', 'body': 'Check sector 2.',
                                    'subjects': [{'kind': 'candidate', 'id': made['id']}]})
    response = client.get(f'/api/candidates/{made["id"]}/export', params={'artifacts': 'all'})
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    candidate = json.loads(archive.read('candidate.json'))
    assert candidate['notes'][0]['body'] == 'Check sector 2.' and candidate['notes'][0]['actor']['name'] == 'richard'
    runs = json.loads(archive.read('runs.json'))
    assert runs[0]['run']['id'] == str(run_id) and runs[0]['jobs'][0]['id'] == str(job_id)
    assert f'artifacts/{artifact_id}/periodogram.json.gz' in archive.namelist()
    manifest = json.loads(archive.read('manifest.json'))
    assert any('External files' in item for item in manifest['limitations'])
    assert verify(tmp_path, response.content)[0] == 0
    assert response.content == client.get(f'/api/candidates/{made["id"]}/export', params={'artifacts': 'all'}).content


def test_import_keys_prevent_duplicate_imported_records(client, conn, artifact_root):
    oid = add_observation(conn)
    lab = {'kind': 'external', 'uri': 'research:agent/study/report.md', 'sha256': SHA, 'bytes': 10}
    body = {'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'lab', 'summary': 'Imported proposal.',
            'observation_ids': [str(oid)], 'evidence': [lab], 'import_key': 'research:agent/study/c1'}
    importer = token_for(conn, 'research-import', kind='service', permissions=['read', 'record.write'])
    first = client.post('/api/candidates', headers=importer, json=body)
    assert first.status_code == 201 and first.json()['import_key'] == 'research:agent/study/c1'
    assert first.json()['actor'] == {**first.json()['actor'], 'name': 'research-import', 'kind': 'service'}
    again = client.post('/api/candidates', headers=importer, json=body)
    assert again.status_code == 409 and again.json()['detail']['existing_id'] == first.json()['id']
    note = {'contract': 'note/1', 'body': 'Imported.', 'subjects': [{'kind': 'candidate', 'id': first.json()['id']}],
            'import_key': 'research:agent/study/c1/note'}
    assert client.post('/api/notes', headers=importer, json=note).status_code == 201
    assert client.post('/api/notes', headers=importer, json=note).status_code == 409
    _, agent = agent_in(conn, 'agent-imp')
    refused = client.post('/api/candidates', headers=agent, json={**body, 'import_key': 'research:agent/other'})
    assert refused.status_code == 403
    with pytest.raises(psycopg.Error):
        conn.execute("""INSERT INTO candidate (kind, tier, summary, import_key, created_by)
                        SELECT 'transit_signal', 'lab', 'x', 'research:agent/study/c1', id FROM client LIMIT 1""")


# ---------- Regression tests from the review of 231e09a ----------

def test_a_second_retry_does_not_repeat_a_job_again(client, conn):
    oid = add_observation(conn)
    run = client.post('/api/runs', json={'method': BLS, 'observation_ids': [str(oid)]}).json()
    fail_job(conn, run['jobs'][0]['id'])
    first = client.post(f'/api/runs/{run["id"]}/retry', json={})
    second = client.post(f'/api/runs/{run["id"]}/retry', json={})
    assert first.status_code == 201 and second.status_code == 409
    assert conn.execute("SELECT count(*) AS n FROM job WHERE observation_id = %s AND state = 'queued'",
                        [oid]).fetchone()['n'] == 1
    # When the repeat also fails, a new retry of the earlier run takes the observation again.
    fail_job(conn, first.json()['jobs'][0]['id'])
    assert client.post(f'/api/runs/{run["id"]}/retry', json={}).status_code == 201


def test_free_text_rule_covers_decision_reasons_and_import_keys(client, conn):
    oid = add_observation(conn)
    campaign_id, agent = agent_in(conn, 'agent-d', blinding='labels_identity')
    made = client.post('/api/candidates', headers=agent, json={
        'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'lab', 'summary': 's',
        'observation_ids': [str(oid)], 'evidence': [{'kind': 'external', 'uri': 'lab:x', 'sha256': SHA, 'bytes': 1}]}).json()
    d = client.post('/api/decisions', json={
        'contract': 'decision/1', 'candidate_id': made['id'], 'question': {'id': 'triage', 'version': 1},
        'mode': 'advisory', 'judge': 'model', 'model': {'kind': 'unavailable', 'reason': 'TIC 100100827 is WASP-18'},
        'packet': {'kind': 'unavailable', 'reason': 'planet WASP-18 b known'},
        'policy': {'kind': 'unavailable', 'reason': 'target TIC 100100827'},
        'judged_evidence_status': 'supported', 'action': 'no_action', 'reason': 'r',
        'evidence': [{'kind': 'external', 'uri': 'lab:y', 'sha256': SHA, 'bytes': 1}]}).json()
    named = client.post('/api/decisions', json={
        'contract': 'decision/1', 'candidate_id': made['id'], 'question': {'id': 'triage', 'version': 1},
        'mode': 'advisory', 'judge': 'model', 'model': {'kind': 'identified', 'provider': 'wasp-18-lab', 'id': 'tic-100100827'},
        'packet': {'kind': 'sha256', 'sha256': SHA}, 'policy': {'kind': 'unavailable', 'reason': 'none'},
        'judged_evidence_status': 'supported', 'action': 'no_action', 'reason': 'r',
        'evidence': [{'kind': 'external', 'uri': 'lab:y', 'sha256': SHA, 'bytes': 1}]}).json()
    seen = client.get(f'/api/decisions/{d["id"]}', headers=agent).json()
    assert seen['text_withheld'] is True and seen['reason'] is None
    assert seen['model'] == {'kind': 'unavailable', 'reason': None}
    assert seen['packet']['reason'] is None and seen['policy']['reason'] is None
    seen = client.get(f'/api/decisions/{named["id"]}', headers=agent).json()
    assert seen['model'] == {'kind': 'identified', 'provider': None, 'id': None}
    assert client.get(f'/api/decisions/{d["id"]}').json()['model']['reason'] == 'TIC 100100827 is WASP-18'
    imported = client.post('/api/candidates', params={'campaign_id': str(campaign_id)}, json={
        'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'lab', 'summary': 'x',
        'import_key': 'research:tic-100100827/wasp-18b', 'observation_ids': [str(oid)],
        'evidence': [{'kind': 'external', 'uri': 'lab:x', 'sha256': SHA, 'bytes': 1}]}).json()
    seen = client.get(f'/api/candidates/{imported["id"]}', headers=agent).json()
    assert seen['summary'] is None and seen['import_key'] is None
    assert client.get(f'/api/candidates/{imported["id"]}').json()['import_key'] == 'research:tic-100100827/wasp-18b'


def test_findings_keep_only_kinds_and_counts_without_identity(client, conn):
    oid = add_observation(conn, state='invalid')
    conn.execute('UPDATE observation SET findings = %s WHERE id = %s', [Jsonb([
        {'kind': 'selected_target_mismatch', 'message': 'The header gives TIC:100100827. The selection manifest gives TIC:5.'},
        {'kind': 'fits_checksum', 'message': 'tess-0000000100100827_lc.fits: HDU 1 checksum differs.'}]), oid])
    conn.execute('UPDATE dataset SET findings = %s', [Jsonb([
        {'kind': 'selected_file_changed', 'selection': 'datasets/d/selections/x.json', 'path': 'tic100100827_lc.fits',
         'message': f'The manifest gives 2 bytes and {SHA}.'},
        {'kind': 'invalid_selection', 'selection': 'datasets/d/x.json', 'message': 'TIC 100100827 entry', 'files': 3},
        {'kind': 'invalid_file', 'path': 'x_lc.fits', 'findings': [{'kind': 'unreadable', 'message': 'TIC 1'}]}])])
    _, agent = agent_in(conn, 'agent-f', blinding='labels_identity')
    listed = client.get('/api/observations', headers=agent).json()['items'][0]
    detail = client.get(f'/api/observations/{oid}', headers=agent).json()
    for obs in (listed, detail):
        assert obs['findings'] == [{'kind': 'selected_target_mismatch', 'withheld': ['identity']},
                                   {'kind': 'fits_checksum', 'withheld': ['identity']}]
    findings = client.get('/api/datasets', headers=agent).json()[0]['findings']
    assert findings == [{'kind': 'selected_file_changed', 'withheld': ['identity'], 'path_count': 1},
                        {'kind': 'invalid_selection', 'withheld': ['identity'], 'files': 3},
                        {'kind': 'invalid_file', 'withheld': ['identity'], 'path_count': 1, 'finding_count': 1}]
    assert 'TIC:100100827' in client.get(f'/api/observations/{oid}').json()['findings'][0]['message']
    _, opened = agent_in(conn, 'agent-o', blinding='open')
    assert client.get('/api/datasets', headers=opened).json()[0]['findings'][0]['selection']


def test_index_run_results_and_exports_leave_out_protected_light_curves(client, conn, artifact_root, cohorts_path):
    protected, free = add_observation(conn, tic=111), add_observation(conn, tic=222)
    index = client.post('/api/datasets/tess-spoc-light-curves/index').json()
    conn.execute("""UPDATE job SET state = 'complete', attempts = 1, finished_at = now(), result = %s WHERE id = %s""",
                 [Jsonb({'contract': 'result/1', 'job_id': index['job_id'], 'outcome': 'complete', 'measurements': [],
                         'artifacts': []}), index['job_id']])
    ids = {}
    for oid in (protected, free):
        path = f'jobs/{index["job_id"]}/1/light-curve-{oid}.json.gz'
        data = gzip.compress(f'{{"secret": "{oid}"}}'.encode())
        (artifact_root / path).parent.mkdir(parents=True, exist_ok=True)
        (artifact_root / path).write_bytes(data)
        ids[oid] = conn.execute("""INSERT INTO artifact (job_id, observation_id, kind, media_type, content_encoding, path,
                                       bytes, sha256) VALUES (%s, %s, 'light_curve', 'application/json', 'gzip', %s, %s, %s)
                                   RETURNING id""", [index['job_id'], oid, path, len(data),
                                                     hashlib.sha256(data).hexdigest()]).fetchone()['id']
    write_register(cohorts_path, [('eval-a', 'evaluation', [], ['TIC:111'])])
    # With a zero observation budget, the light curve of the free observation is a read past the budget.
    campaign_id, tight = agent_in(conn, 'agent-i', budgets={'observations_read': 0})
    assert client.get(f'/api/runs/{index["run_id"]}/results', headers=tight).status_code == 403
    assert client.get(f'/api/runs/{index["run_id"]}/export', headers=tight, params={'artifacts': 'all'}).status_code == 403
    assert conn.execute('SELECT count(*) AS n FROM campaign_read WHERE campaign_id = %s', [campaign_id]).fetchone()['n'] == 0
    campaign_id, agent = agent_in(conn, 'agent-j')
    results = client.get(f'/api/runs/{index["run_id"]}/results', headers=agent).json()
    assert {a['id'] for j in results['jobs'] for a in j['artifacts']} == {str(ids[free])}
    assert results['protected_excluded'] == 1
    export = client.get(f'/api/runs/{index["run_id"]}/export', headers=agent, params={'artifacts': 'all'})
    archive = zipfile.ZipFile(io.BytesIO(export.content))
    assert not any(str(protected) in name for name in archive.namelist())
    assert f'artifacts/{ids[free]}/light-curve-{free}.json.gz' in archive.namelist()
    assert str(ids[protected]) not in {a['id'] for a in json.loads(archive.read('artifacts.json'))}
    assert {r['observation_id'] for r in conn.execute('SELECT observation_id FROM campaign_read WHERE campaign_id = %s',
                                                      [campaign_id]).fetchall()} == {free}
    # A person's export holds both light curves.
    names = zipfile.ZipFile(io.BytesIO(client.get(f'/api/runs/{index["run_id"]}/export',
                                                  params={'artifacts': 'all'}).content)).namelist()
    assert f'artifacts/{ids[protected]}/light-curve-{protected}.json.gz' in names


def test_run_export_leaves_out_candidates_that_only_hidden_records_link(client, conn, artifact_root):
    oid = add_observation(conn)
    run_id, job_id, _ = add_finished_job(conn, artifact_root, oid)
    _, agent = agent_in(conn, 'agent-h', blinding='labels')
    made = client.post('/api/candidates', headers=agent, json={
        'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'lab', 'summary': 's', 'observation_ids': [str(oid)],
        'evidence': [{'kind': 'external', 'uri': 'lab:x', 'sha256': SHA, 'bytes': 1}]}).json()
    identity = client.post('/api/assessments', json={
        'contract': 'assessment/1', 'candidate_id': made['id'], 'axis': 'identity', 'status': 'matched',
        'basis': 'review', 'reason': 'known', 'evidence': [{'kind': 'job', 'id': str(job_id)}]})
    assert identity.status_code == 201
    records = json.loads(zipfile.ZipFile(io.BytesIO(client.get(f'/api/runs/{run_id}/export', headers=agent).content))
                         .read('records.json'))
    assert records['candidates'] == []
    person = json.loads(zipfile.ZipFile(io.BytesIO(client.get(f'/api/runs/{run_id}/export').content)).read('records.json'))
    assert [c['id'] for c in person['candidates']] == [made['id']]


def test_parallel_agent_submissions_cannot_both_pass_the_jobs_budget(client, conn):
    from concurrent.futures import ThreadPoolExecutor
    first, second = add_observation(conn, tic=1), add_observation(conn, tic=2)
    _, agent = agent_in(conn, 'agent-par', budgets={'jobs': 1})

    def submit(oid):
        return client.post('/api/runs', headers=agent, json={'method': BLS, 'observation_ids': [str(oid)]}).status_code

    with ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(submit, [first, second]))
    assert codes == [201, 403]


def test_export_observations_do_not_change_with_a_new_index(client, conn, artifact_root):
    oid = add_observation(conn)
    run_id, _, _ = add_finished_job(conn, artifact_root, oid)
    before = client.get(f'/api/runs/{run_id}/export').content
    conn.execute("UPDATE observation SET last_seen_at = now() + interval '1 day' WHERE id = %s", [oid])
    assert client.get(f'/api/runs/{run_id}/export').content == before
    observations = json.loads(zipfile.ZipFile(io.BytesIO(before)).read('observations.json'))
    assert 'last_seen_at' not in observations[0] and 'first_indexed_at' not in observations[0]


def test_verify_export_reports_header_mismatch_drive_paths_false_redaction_and_bad_manifests(client, conn, artifact_root, tmp_path):
    oid = add_observation(conn)
    run_id, _, _ = add_finished_job(conn, artifact_root, oid)
    response = client.get(f'/api/runs/{run_id}/export')

    def check(data: bytes, *extra) -> tuple[int, dict]:
        path = tmp_path / f'{hashlib.sha256(data).hexdigest()[:12]}.zip'
        path.write_bytes(data)
        done = subprocess.run([sys.executable, str(VERIFY), str(path), *extra], check=False, capture_output=True, text=True)
        return done.returncode, json.loads(done.stdout)

    assert check(response.content, '--manifest-sha256', response.headers['x-export-manifest-sha256'])[0] == 0
    status, report = check(response.content, '--manifest-sha256', '0' * 64)
    assert status == 1 and any('expected manifest' in f for f in report['failures'])

    def rewritten(change) -> bytes:
        source = zipfile.ZipFile(io.BytesIO(response.content))
        manifest = json.loads(source.read('manifest.json'))
        extra = change(manifest)
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            for name in source.namelist():
                if name != 'manifest.json':
                    archive.writestr(name, source.read(name))
            for name, data in (extra or {}).items():
                archive.writestr(name, data)
            archive.writestr('manifest.json', json.dumps(manifest) if manifest is not None else '{not json')
        return out.getvalue()

    def drive(manifest):
        manifest['files'].append({'path': 'C:/x', 'bytes': 1, 'sha256': SHA, 'media_type': 'text/plain', 'content': 'readme'})
        return {'C:/x': b'x'}
    status, report = check(rewritten(drive))
    assert status == 1 and any('C:/x' in f and 'unsafe' in f for f in report['failures'])

    def redacted(manifest):
        manifest['files'].append({'path': 'artifacts/a/x.json', 'bytes': 1, 'sha256': hashlib.sha256(b'x').hexdigest(),
                                  'media_type': 'application/json', 'content': 'artifact',
                                  'artifact_id': '00000000-0000-4000-8000-000000000000', 'stored_sha256': SHA,
                                  'redacted': True})
        return {'artifacts/a/x.json': b'x'}
    status, report = check(rewritten(redacted))
    assert status == 1 and any('withholds nothing' in f for f in report['failures'])

    bad = io.BytesIO()
    with zipfile.ZipFile(bad, 'w') as archive:
        archive.writestr('manifest.json', '{not json')
    status, report = check(bad.getvalue())
    assert status == 1 and report['passed'] is False and 'not valid JSON' in report['failures'][0]
    status, report = check(b'not a zip file')
    assert status == 1 and 'not a readable ZIP' in report['failures'][0]
