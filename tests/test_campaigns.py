"""Campaign protections: blinding, the cohort guard, budgets, and the campaign lifecycle (ROADMAP R8)."""
import uuid

from psycopg.types.json import Jsonb

from conftest import add_campaign, add_finished_job, add_job, add_observation, add_run, token_for, write_register

LABELS = {'class': 'planet_host', 'planets': [{'name': 'WASP-18 b', 'period_days': 0.94145}]}
HEADER = {'TICID': 100100827, 'OBJECT': 'TIC 100100827', 'RA_OBJ': 24.35, 'SECTOR': 2, 'TESSMAG': 8.8}
BLS = {'id': 'tess-bls', 'version': 1}


def agent_in(conn, name, **campaign):
    campaign_id = add_campaign(conn, f'c-{name}', **campaign)
    return campaign_id, token_for(conn, name, campaign_id=campaign_id)


def test_blinding_levels_withhold_labels_and_identity(client, conn, artifact_root):
    oid = add_observation(conn, labels=LABELS, header=HEADER)
    run_id, job_id, _ = add_finished_job(conn, artifact_root, oid)
    expected = {'open': ([], 'TIC:100100827', 'planet_host'),
                'labels': (['labels'], 'TIC:100100827', None),
                'labels_identity': (['labels', 'identity'], None, None)}
    for level, (withheld, target, label_class) in expected.items():
        _, agent = agent_in(conn, f'agent-{level.replace("_", "-")}', blinding=level)
        listed = client.get('/api/observations', headers=agent).json()
        assert listed['withheld'] == withheld
        item = listed['items'][0]
        assert item['target_id'] == target and item['labels'].get('class') == label_class
        detail = client.get(f'/api/observations/{oid}', headers=agent).json()
        assert ('TICID' in detail['header']) == (target is not None) and detail['header']['SECTOR'] == 2
        targets = client.get('/api/targets', headers=agent, params={'sort': 'sector'}).json()
        assert targets['items'][0]['label_class'] == label_class and targets['items'][0]['target_id'] == target
        results = client.get(f'/api/runs/{run_id}/results', headers=agent).json()
        assert ('reference_comparison' in results['jobs'][0]['measurements']) == ('labels' not in withheld)
        job = client.get(f'/api/jobs/{job_id}', headers=agent).json()
        assert all(m['role'] != 'reference_comparison' for m in job['result']['measurements']) or 'labels' not in withheld
    # A blind agent cannot filter or search by what the campaign withholds.
    _, blind = agent_in(conn, 'agent-blind', blinding='labels_identity')
    assert client.get('/api/observations', headers=blind, params={'class': 'planet_host'}).status_code == 422
    assert client.get('/api/observations', headers=blind, params={'target_id': 'TIC:100100827'}).status_code == 422
    assert client.get('/api/targets', headers=blind, params={'q': 'wasp'}).status_code == 422
    assert client.get('/api/targets', headers=blind, params={'q': 'pref<1'}).status_code == 422
    assert client.get('/api/targets', headers=blind, params={'sort': 'ra_deg'}).status_code == 422
    # A person sees everything.
    assert client.get('/api/observations').json()['items'][0]['target_id'] == 'TIC:100100827'


def test_the_reveal_follows_fixed_decisions_and_ends_blinding(client, conn):
    oid = add_observation(conn, labels=LABELS, header=HEADER)
    campaign_id, agent = agent_in(conn, 'agent-r', blinding='labels_identity')
    assert client.get(f'/api/observations/{oid}', headers=agent).json()['labels'] == {}
    # The reveal needs fixed decisions, and an agent cannot record it.
    assert client.post(f'/api/campaigns/{campaign_id}/reveal', json={'reason': 'early'}).status_code == 409
    assert client.post(f'/api/campaigns/{campaign_id}/fix-decisions', headers=agent).status_code == 200
    assert client.post(f'/api/campaigns/{campaign_id}/reveal', headers=agent, json={'reason': 'x'}).status_code == 403
    # After the decisions are fixed, the agent submits no new runs.
    run = client.post('/api/runs', headers=agent, json={'method': BLS, 'observation_ids': [str(oid)]})
    assert run.status_code == 403 and 'fixed its decisions' in run.json()['detail']
    revealed = client.post(f'/api/campaigns/{campaign_id}/reveal', json={'reason': 'decisions fixed in record X'})
    assert revealed.status_code == 200 and revealed.json()['reveal_reason'] == 'decisions fixed in record X'
    detail = client.get(f'/api/observations/{oid}', headers=agent).json()
    assert detail['labels']['class'] == 'planet_host' and detail['target_id'] == 'TIC:100100827'
    assert 'withheld' not in detail


def test_cohort_guard_refuses_agents_and_records_each_access(client, conn, artifact_root, cohorts_path):
    protected = add_observation(conn, tic=111)
    free = add_observation(conn, tic=222)
    run_id, job_id, artifact_id = add_finished_job(conn, artifact_root, protected)
    add_job(conn, run_id, free)
    write_register(cohorts_path, [('eval-a', 'evaluation', [], ['TIC:111']), ('dev-b', 'development', [], ['TIC:222'])])
    campaign_id, agent = agent_in(conn, 'agent-g', blinding='open')

    listed = client.get('/api/observations', headers=agent).json()
    assert [i['id'] for i in listed['items']] == [str(free)] and listed['protected_excluded'] == 1
    assert client.get('/api/targets', headers=agent).json()['protected_excluded'] == 1
    for path in (f'/api/observations/{protected}', f'/api/jobs/{job_id}', f'/api/artifacts/{artifact_id}',
                 f'/api/artifacts/{artifact_id}/content', f'/api/runs/{run_id}/observations/{protected}/fold',
                 f'/api/runs/{run_id}/results?observation_id={protected}'):
        assert client.get(path, headers=agent).status_code == 403, path
    results = client.get(f'/api/runs/{run_id}/results', headers=agent).json()
    assert [j['observation_id'] for j in results['jobs']] == [str(free)] and results['protected_excluded'] == 1
    submitted = client.post('/api/runs', headers=agent, json={'method': BLS, 'observation_ids': [str(free), str(protected)]})
    assert submitted.status_code == 422 and submitted.json()['detail'][0]['loc'] == ['observation_ids', 1]

    # A person reads the protected observation. The read is recorded as allowed.
    assert client.get(f'/api/observations/{protected}').status_code == 200
    events = client.get('/api/access-events').json()['items']
    assert {e['outcome'] for e in events if e['client_name'] == 'agent-g'} == {'refused'}
    assert {e['reservation_id'] for e in events} == {'eval-a'}
    assert any(e['operation'] == 'run submission' for e in events)
    assert [e['outcome'] for e in events if e['client_name'] == 'richard'] == ['allowed']
    assert client.get('/api/access-events', headers=agent).status_code == 403
    assert client.get('/api/access-events', params={'campaign_id': str(campaign_id), 'outcome': 'allowed'}).json()['items'] == []

    # Without the register, an agent cannot work. A person can.
    cohorts_path.unlink()
    assert client.get('/api/observations', headers=agent).status_code == 503
    assert client.get(f'/api/observations/{free}', headers=agent).status_code == 503
    assert client.get(f'/api/observations/{free}').status_code == 200


def test_budgets_stop_reads_and_runs(client, conn):
    first, second, third = (add_observation(conn, tic=t) for t in (1, 2, 3))
    campaign_id, agent = agent_in(conn, 'agent-b', budgets={'observations_read': 2, 'jobs': 2, 'compute_seconds': 100})
    assert client.get(f'/api/observations/{first}', headers=agent).status_code == 200
    assert client.get(f'/api/observations/{first}', headers=agent).status_code == 200
    assert client.get(f'/api/observations/{second}', headers=agent).status_code == 200
    refused = client.get(f'/api/observations/{third}', headers=agent)
    assert refused.status_code == 403 and 'observation budget' in refused.json()['detail']
    state = client.get(f'/api/campaigns/{campaign_id}', headers=agent).json()['budget_state']
    assert state['observations_read'] == {**state['observations_read'], 'used': 2, 'remaining': 0}

    request = {'method': BLS, 'observation_ids': [str(first), str(second), str(third)]}
    estimate = client.post('/api/runs/estimate', headers=agent, json=request).json()
    assert estimate['budget']['exceeded'] and estimate['budget']['exceeded'][0].startswith('jobs:')
    assert client.post('/api/runs', headers=agent, json=request).status_code == 403
    created = client.post('/api/runs', headers=agent, json={'method': BLS, 'observation_ids': [str(first)]})
    assert created.status_code == 201 and created.json()['campaign_id'] == str(campaign_id)

    # Finished work counts toward compute. 150 s of worker time passes the 100 s limit.
    conn.execute("""UPDATE job SET state = 'complete', finished_at = now(),
                        result = %s WHERE run_id = %s""",
                 [Jsonb({'contract': 'result/1', 'outcome': 'complete', 'measurements': [], 'artifacts': [],
                         'resources': {'elapsed_seconds': 150.0}}), created.json()['id']])
    over = client.post('/api/runs', headers=agent, json={'method': BLS, 'observation_ids': [str(second)]})
    assert over.status_code == 403 and 'compute_seconds' in over.json()['detail']
    # A person has no campaign budget.
    assert client.post('/api/runs', json=request).status_code == 201


def test_campaign_lifecycle_and_visibility(client, conn):
    oid = add_observation(conn)
    bad = client.post('/api/campaigns', json={'name': 'p1', 'title': 'T', 'question': 'Q?', 'budgets': {'jobs': -1, 'gpus': 1}})
    assert bad.status_code == 422 and len(bad.json()['detail']) == 2
    created = client.post('/api/campaigns', json={'name': 'p1', 'title': 'T', 'question': 'Q?', 'budgets': {'jobs': 5}})
    assert created.status_code == 201 and created.json()['blinding'] == 'labels_identity'
    campaign_id = created.json()['id']
    assert client.post('/api/campaigns', json={'name': 'p1', 'title': 'T', 'question': 'Q?'}).status_code == 409
    agent = token_for(conn, 'agent-p1', campaign_id=uuid.UUID(campaign_id))

    # Before the approval, the agent reads its campaign but no observation data.
    assert client.get(f'/api/campaigns/{campaign_id}', headers=agent).status_code == 200
    assert client.get('/api/observations', headers=agent).status_code == 403
    assert client.get(f'/api/observations/{oid}', headers=agent).status_code == 403
    assert client.post(f'/api/campaigns/{campaign_id}/fix-decisions', headers=agent).status_code == 403
    assert client.post(f'/api/campaigns/{campaign_id}/approve', headers=agent, json={'campaign_file_sha256': 'a' * 64}).status_code == 403
    approved = client.post(f'/api/campaigns/{campaign_id}/approve', json={'campaign_file_sha256': 'a' * 64})
    assert approved.status_code == 200 and approved.json()['approved_at']
    assert client.post(f'/api/campaigns/{campaign_id}/approve', json={'campaign_file_sha256': 'b' * 64}).status_code == 409
    assert client.get('/api/observations', headers=agent).status_code == 200
    assert client.post(f'/api/campaigns/{campaign_id}/budgets', headers=agent, json={'budgets': {'jobs': 99}}).status_code == 403
    assert client.post(f'/api/campaigns/{campaign_id}/budgets', json={'budgets': {'jobs': 9}}).json()['budgets'] == {'jobs': 9}

    # An agent sees only its own campaign.
    other = add_campaign(conn, 'other')
    assert [c['name'] for c in client.get('/api/campaigns', headers=agent).json()['items']] == ['p1']
    assert client.get(f'/api/campaigns/{other}', headers=agent).status_code == 404
    assert client.post(f'/api/campaigns/{other}/fix-decisions', headers=agent).status_code == 404
    assert {c['name'] for c in client.get('/api/campaigns').json()['items']} >= {'p1', 'other'}

    closed = client.post(f'/api/campaigns/{campaign_id}/close')
    assert closed.status_code == 200 and closed.json()['closed_at']
    assert client.get('/api/observations', headers=agent).status_code == 403
    assert client.post('/api/runs', headers=agent, json={'method': BLS, 'observation_ids': [str(oid)]}).status_code == 403
    assert client.post('/api/clients', json={'name': 'agent-late', 'kind': 'agent', 'permissions': ['read'],
                                             'campaign_id': campaign_id}).status_code == 422


def test_run_lists_record_the_campaign(client, conn):
    add_run(conn)
    campaign_id, agent = agent_in(conn, 'agent-l')
    oid = add_observation(conn)
    client.post('/api/runs', headers=agent, json={'method': BLS, 'observation_ids': [str(oid)]})
    runs = client.get('/api/runs').json()
    assert sorted(str(r['campaign_id']) for r in runs) == sorted(['None', str(campaign_id)])
