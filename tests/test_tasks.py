"""Agent tasks: claims, leases, recovery, and ownership through PostgreSQL (ROADMAP R8)."""
from concurrent.futures import ThreadPoolExecutor
import threading

import psycopg
from psycopg.rows import dict_row
import pytest

from conftest import add_campaign, add_observation, token_for, write_register


def task(title, **extra) -> dict:
    return {'contract': 'agent-task/1', 'kind': 'vetting', 'title': title, **extra}


def report(outcome='complete', **extra) -> dict:
    return {'contract': 'agent-task-report/1', 'outcome': outcome, 'summary': 'Done.', **extra}


@pytest.fixture
def campaign(conn):
    return add_campaign(conn, 'tasks')


def agents(conn, campaign_id, *names):
    return [token_for(conn, name, campaign_id=campaign_id) for name in names]


def make(client, campaign_id, title, **extra) -> dict:
    response = client.post('/api/tasks', params={'campaign_id': str(campaign_id)}, json=task(title, **extra))
    assert response.status_code == 201, response.text
    return response.json()


def expire(conn, task_id):
    conn.execute("UPDATE agent_task SET lease_expires_at = now() - interval '1 second' WHERE id = %s", [task_id])


def test_claims_take_the_highest_priority_first(client, conn, campaign):
    a1, a2 = agents(conn, campaign, 'agent-1', 'agent-2')
    low, high, mid = (make(client, campaign, t, priority=p) for t, p in (('low', 10), ('high', 90), ('mid', 50)))
    assert [t['id'] for t in client.get('/api/tasks', headers=a1).json()['items']] == [high['id'], mid['id'], low['id']]
    first = client.post('/api/tasks/claim', headers=a1, json={}).json()['task']
    assert first['id'] == high['id'] and first['state'] == 'claimed' and first['attempts'] == 1
    assert first['claimed_by']['name'] == 'agent-1' and first['lease_id']
    assert client.post('/api/tasks/claim', headers=a2, json={}).json()['task']['id'] == mid['id']
    assert client.post('/api/tasks/claim', headers=a2, json={'kinds': ['search']}).json()['task'] is None
    assert client.post('/api/tasks/claim', headers=a1, json={'kinds': ['vetting']}).json()['task']['id'] == low['id']
    assert client.post('/api/tasks/claim', headers=a1, json={}).json()['task'] is None
    # The lease identity stays out of task reads. Only the claim response gives it.
    assert 'lease_id' not in client.get(f'/api/tasks/{high["id"]}', headers=a1).json()


def test_parallel_claims_never_share_a_task(client, conn, campaign):
    tokens = agents(conn, campaign, 'agent-1', 'agent-2', 'agent-3')
    created = {make(client, campaign, f'task {i}')['id'] for i in range(5)}
    start = threading.Barrier(12)

    def claim(i):
        start.wait()
        return client.post('/api/tasks/claim', headers=tokens[i % 3], json={}).json()['task']

    with ThreadPoolExecutor(12) as pool:
        claimed = [t for t in pool.map(claim, range(12)) if t is not None]
    assert len(claimed) == 5 and {t['id'] for t in claimed} == created


def test_a_claim_skips_a_task_that_another_transaction_locks(client, conn, database_url, campaign):
    (agent,) = agents(conn, campaign, 'agent-1')
    first = make(client, campaign, 'first', priority=90)
    second = make(client, campaign, 'second', priority=10)
    with psycopg.connect(database_url, row_factory=dict_row) as other:
        other.execute('SELECT id FROM agent_task WHERE id = %s FOR UPDATE', [first['id']])
        claimed = client.post('/api/tasks/claim', headers=agent, json={}).json()['task']
        assert claimed['id'] == second['id']
        other.rollback()
    assert client.post('/api/tasks/claim', headers=agent, json={}).json()['task']['id'] == first['id']


def test_an_expired_lease_goes_to_the_next_claim_and_the_last_attempt_fails(client, conn, campaign):
    a1, a2 = agents(conn, campaign, 'agent-1', 'agent-2')
    made = make(client, campaign, 'retry', max_attempts=2)
    held = client.post('/api/tasks/claim', headers=a1, json={}).json()['task']
    # An unexpired lease is not available to another claim.
    assert client.post('/api/tasks/claim', headers=a2, json={}).json()['task'] is None
    expire(conn, made['id'])
    assert client.get(f'/api/tasks/{made["id"]}', headers=a1).json()['lease_expired'] is True
    taken = client.post('/api/tasks/claim', headers=a2, json={}).json()['task']
    assert taken['id'] == made['id'] and taken['attempts'] == 2 and taken['lease_id'] != held['lease_id']
    # The earlier holder lost the lease.
    stale = client.post(f'/api/tasks/{made["id"]}/heartbeat', headers=a1, json={'lease_id': held['lease_id']})
    assert stale.status_code == 403 and 'does not hold the lease' in stale.json()['detail']
    late = client.post(f'/api/tasks/{made["id"]}/report', headers=a1, json={'lease_id': held['lease_id'], 'report': report()})
    assert late.status_code == 403
    renewed = client.post(f'/api/tasks/{made["id"]}/heartbeat', headers=a2, json={'lease_id': taken['lease_id']})
    assert renewed.status_code == 200 and renewed.json()['lease_expired'] is False
    # The last lease expires. The next claim records the task as failed.
    expire(conn, made['id'])
    after = client.post('/api/tasks/claim', headers=a1, json={}).json()
    assert after['task'] is None and after['failed_on_expiry'] == [made['id']]
    failed = client.get(f'/api/tasks/{made["id"]}', headers=a1).json()
    assert failed['state'] == 'failed' and failed['reason'] == 'The lease expired on every permitted attempt.'


def test_a_new_claim_by_the_same_client_ends_its_old_lease(client, conn, campaign):
    (agent,) = agents(conn, campaign, 'agent-1')
    made = make(client, campaign, 'same client')
    old = client.post('/api/tasks/claim', headers=agent, json={}).json()['task']
    expire(conn, made['id'])
    new = client.post('/api/tasks/claim', headers=agent, json={}).json()['task']
    assert new['id'] == old['id'] and new['attempts'] == 2
    refused = client.post(f'/api/tasks/{made["id"]}/heartbeat', headers=agent, json={'lease_id': old['lease_id']})
    assert refused.status_code == 409 and 'claimed again (attempt 2)' in refused.json()['detail']


def test_only_the_lease_holder_ends_a_task(client, conn, campaign):
    a1, a2 = agents(conn, campaign, 'agent-1', 'agent-2')
    oid = add_observation(conn)
    made = make(client, campaign, 'own', inputs=[{'kind': 'observation', 'id': str(oid)}])
    assert made['inputs'] == [{'kind': 'observation', 'id': str(oid)}]
    held = client.post('/api/tasks/claim', headers=a1, json={}).json()['task']
    lease = {'lease_id': held['lease_id']}
    assert client.post(f'/api/tasks/{made["id"]}/report', headers=a2, json={**lease, 'report': report()}).status_code == 403
    assert client.post(f'/api/tasks/{made["id"]}/heartbeat', json=lease).status_code == 403  # a person, not the holder
    bad = client.post(f'/api/tasks/{made["id"]}/report', headers=a1, json={**lease, 'report': report('failed')})
    assert bad.status_code == 422 and bad.json()['detail'][0]['msg'] == "'failure_reason' is a required property"
    note = client.post('/api/notes', headers=a1, json={'contract': 'note/1', 'body': 'Vetted.',
                                                       'subjects': [{'kind': 'agent_task', 'id': made['id']}]}).json()
    done = client.post(f'/api/tasks/{made["id"]}/report', headers=a1,
                       json={**lease, 'report': report(outputs=[{'kind': 'note', 'id': note['id']}])})
    assert done.status_code == 200, done.text
    assert done.json()['state'] == 'complete' and done.json()['outputs'] == [{'kind': 'note', 'id': note['id']}]
    after = client.post(f'/api/tasks/{made["id"]}/heartbeat', headers=a1, json=lease)
    assert after.status_code == 409 and 'complete' in after.json()['detail']
    with pytest.raises(psycopg.Error) as error:
        conn.execute("UPDATE agent_task SET summary = 'changed' WHERE id = %s", [made['id']])
    assert error.value.sqlstate == 'RC002'


def test_release_and_cancel(client, conn, campaign):
    a1, a2 = agents(conn, campaign, 'agent-1', 'agent-2')
    made = make(client, campaign, 'release')
    held = client.post('/api/tasks/claim', headers=a1, json={}).json()['task']
    released = client.post(f'/api/tasks/{made["id"]}/release', headers=a1, json={'lease_id': held['lease_id']}).json()
    assert released['state'] == 'queued' and released['attempts'] == 0 and released['claimed_by'] is None
    # A client cancels only the tasks that it wrote, unless it has "admin".
    assert client.post(f'/api/tasks/{made["id"]}/cancel', headers=a2, json={'reason': 'not mine'}).status_code == 403
    own = client.post('/api/tasks', headers=a2, json=task('agent task')).json()
    assert own['actor']['name'] == 'agent-2'
    assert client.post(f'/api/tasks/{own["id"]}/cancel', headers=a2, json={'reason': 'Duplicate.'}).json()['state'] == 'cancelled'
    cancelled = client.post(f'/api/tasks/{made["id"]}/cancel', json={'reason': 'Out of scope.'})
    assert cancelled.status_code == 200 and cancelled.json()['reason'] == 'Out of scope.'
    assert client.post(f'/api/tasks/{made["id"]}/cancel', json={'reason': 'again'}).status_code == 409


def test_tasks_follow_the_campaign(client, conn):
    waiting = add_campaign(conn, 'waiting', approved=False)
    (early,) = agents(conn, waiting, 'agent-w')
    assert client.post('/api/tasks', headers=early, json=task('too early')).status_code == 403
    prepared = make(client, waiting, 'prepared before the approval')
    assert client.post('/api/tasks/claim', headers=early, json={}).status_code == 403

    blind = add_campaign(conn, 'blind', blinding='labels')
    (agent,) = agents(conn, blind, 'agent-b')
    other = add_campaign(conn, 'other')
    assert client.get(f'/api/tasks/{prepared["id"]}', headers=agent).status_code == 404
    assert client.post('/api/tasks/claim', headers=agent, params={'campaign_id': str(other)}, json={}).status_code == 404
    # A person cannot give the campaign's agents a task with a withheld input.
    oid = add_observation(conn)
    lab = {'kind': 'external', 'uri': 'lab:x', 'sha256': 'a' * 64, 'bytes': 1}
    c = client.post('/api/candidates', params={'campaign_id': str(blind)},
                    json={'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'lab', 'summary': 'x',
                          'observation_ids': [str(oid)], 'evidence': [lab]}).json()
    match = client.post('/api/assessments', json={'contract': 'assessment/1', 'candidate_id': c['id'], 'axis': 'identity',
                                                  'status': 'matched', 'basis': 'review', 'reason': 'Known planet.',
                                                  'evidence': [lab]}).json()
    withheld = client.post('/api/tasks', params={'campaign_id': str(blind)},
                           json=task('compare', inputs=[{'kind': 'assessment', 'id': match['id']}]))
    assert withheld.status_code == 422 and 'withholds' in withheld.json()['detail'][0]['msg']
    assert client.post('/api/tasks', json=task('no campaign')).status_code == 422

    # The close cancels waiting and claimed tasks. A closed campaign takes no new task.
    queued, claimed = make(client, blind, 'queued'), make(client, blind, 'claimed', priority=99)
    assert client.post('/api/tasks/claim', headers=agent, json={}).json()['task']['id'] == claimed['id']
    assert client.post(f'/api/campaigns/{blind}/close').status_code == 200
    for t in (queued, claimed):
        row = client.get(f'/api/tasks/{t["id"]}').json()
        assert row['state'] == 'cancelled' and row['reason'] == 'The campaign closed.'
    assert client.post('/api/tasks/claim', headers=agent, json={}).status_code == 403
    late = client.post('/api/tasks', params={'campaign_id': str(blind)}, json=task('late'))
    assert late.status_code == 409 and 'closed' in late.json()['detail']
    assert client.get(f'/api/campaigns/{blind}').json()['records']['tasks'] == {'cancelled': 2}


@pytest.mark.parametrize('initial_state', ['queued', 'expired', 'exhausted'])
def test_claims_skip_hidden_tasks_without_changing_them(client, conn, campaign, cohorts_path, initial_state):
    (agent,) = agents(conn, campaign, 'agent-protected')
    oid = add_observation(conn, tic=111)
    hidden = make(client, campaign, 'protected', priority=99,
                  max_attempts=1 if initial_state == 'exhausted' else 3,
                  inputs=[{'kind': 'observation', 'id': str(oid)}])
    if initial_state != 'queued':
        assert client.post('/api/tasks/claim', headers=agent, json={}).json()['task']['id'] == hidden['id']
        expire(conn, hidden['id'])
    before = conn.execute('SELECT * FROM agent_task WHERE id = %s', [hidden['id']]).fetchone()
    write_register(cohorts_path, [('eval-task', 'evaluation', [], ['TIC:111'])])
    visible = make(client, campaign, 'visible', priority=10)
    assert [t['id'] for t in client.get('/api/tasks', headers=agent).json()['items']] == [visible['id']]
    claimed = client.post('/api/tasks/claim', headers=agent, json={})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()['task']['id'] == visible['id']
    assert claimed.json()['failed_on_expiry'] == []
    empty = client.post('/api/tasks/claim', headers=agent, json={})
    assert empty.status_code == 200 and empty.json() == {'task': None, 'failed_on_expiry': []}
    assert conn.execute('SELECT * FROM agent_task WHERE id = %s', [hidden['id']]).fetchone() == before
