"""Stored-search selection, ranking, candidate visibility, and campaign safeguards."""
from conftest import add_campaign, add_job, add_observation, add_run, token_for, write_register


def test_queue_uses_newest_complete_search_and_preserves_measurements(client, conn):
    first = add_observation(conn, tic=111)
    second = add_observation(conn, tic=222, labels={'class': 'planet_host', 'planets': [{'name': 'Known b'}]})
    pending = add_observation(conn, tic=333)
    older, newer, failed = add_run(conn, label='Older'), add_run(conn, label='Newer'), add_run(conn)
    add_job(conn, older, first, measurements=[('search', 'period', 2), ('search', 'depth_snr', 90)])
    job = add_job(conn, newer, first, measurements=[('search', 'period', 3), ('search', 'depth_snr', 8), ('search', 'depth', .002), ('search', 'observed_transit_count', 7)])
    add_job(conn, failed, first, state='failed')
    add_job(conn, older, second, measurements=[('search', 'period', 1), ('search', 'depth_snr', 12)])
    add_job(conn, newer, pending, state='queued')
    response = client.get('/api/signals')
    assert response.status_code == 200, response.text
    page = response.json()
    assert page['total'] == 2 and page['rule'] == 'signal-review/1'
    assert [r['observation_id'] for r in page['items']] == [str(second), str(first)]
    row = page['items'][1]
    assert row['job_id'] == str(job) and row['run_id'] == str(newer)
    assert row['period_days'] == 3 and row['depth_snr'] == 8 and row['observed_transit_count'] == 7
    assert client.get('/api/signals?sample=unconfirmed').json()['total'] == 1
    assert client.get('/api/signals?q=TIC:111').json()['items'][0]['job_id'] == str(job)
    assert client.get(f'/api/signals?run_id={older}').json()['items'][0]['depth_snr'] == 90
    assert len(client.get('/api/signals?limit=1&offset=1').json()['items']) == 1
    assert client.get('/api/signals?dataset_id=missing').json()['total'] == 0
    assert client.get('/api/signals?sort=sql').status_code == 422


def test_queue_enforces_campaign_blinding_cohorts_and_budget(client, conn, cohorts_path):
    free, protected = add_observation(conn, tic=111, labels={'class': 'planet_host'}), add_observation(conn, tic=222)
    run = add_run(conn, label='Secret name')
    add_job(conn, run, free, measurements=[('search', 'depth_snr', 8)])
    add_job(conn, run, protected, measurements=[('search', 'depth_snr', 99)])
    write_register(cohorts_path, [('eval', 'evaluation', [], ['TIC:222'])])
    campaign = add_campaign(conn, 'blind-queue', blinding='labels_identity', budgets={'observations_read': 1})
    agent = token_for(conn, 'queue-agent', campaign_id=campaign)
    row = client.get('/api/signals', headers=agent).json()['items'][0]
    assert row['observation_id'] == str(free)
    assert row['target_id'] is None and row['label_class'] is None and row['run_label'] is None
    assert client.get('/api/signals?sample=unconfirmed', headers=agent).status_code == 422
    assert client.get('/api/signals?q=111', headers=agent).status_code == 422
    assert conn.execute('SELECT count(*) AS n FROM campaign_read WHERE campaign_id=%s', [campaign]).fetchone()['n'] == 1
    another = add_observation(conn, tic=333)
    add_job(conn, run, another, measurements=[('search', 'depth_snr', 20)])
    assert client.get('/api/signals', headers=agent).status_code == 403


def test_candidate_filter_uses_visible_records(client, conn):
    oid = add_observation(conn)
    run = add_run(conn)
    job = add_job(conn, run, oid, measurements=[('search', 'depth_snr', 10)])
    candidate = client.post('/api/candidates', json={'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'platform',
        'summary': 'Unreviewed signal.', 'observation_ids': [str(oid)], 'evidence': [{'kind': 'job', 'id': str(job)}]})
    assert candidate.status_code == 201, candidate.text
    page = client.get('/api/signals?sample=candidates').json()
    assert page['total'] == 1 and page['items'][0]['candidate_ids'] == [candidate.json()['id']]
    assert client.get('/api/signals?sample=candidates', headers=token_for(conn, 'other-campaign')).json()['total'] == 0
    assert client.get('/api/signals', headers={'Authorization': ''}).status_code == 401
    assert client.get('/api/signals', headers=token_for(conn, 'no-read-signals', kind='service', permissions=())).status_code == 403


def test_newer_foreign_campaign_result_does_not_replace_visible_result(client, conn):
    oid = add_observation(conn)
    own_campaign = add_campaign(conn, 'queue-own')
    other_campaign = add_campaign(conn, 'queue-other')
    agent = token_for(conn, 'queue-scope', campaign_id=own_campaign)
    visible = add_run(conn)
    first = add_job(conn, visible, oid, measurements=[('search', 'depth_snr', 5)])
    foreign = add_run(conn)
    conn.execute('UPDATE run SET campaign_id=%s WHERE id=%s', [other_campaign, foreign])
    add_job(conn, foreign, oid, measurements=[('search', 'depth_snr', 999)])
    page = client.get('/api/signals', headers=agent).json()
    assert page['total'] == 1 and page['items'][0]['job_id'] == str(first)
    assert client.get(f'/api/signals?run_id={foreign}', headers=agent).json()['total'] == 0
