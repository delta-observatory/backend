"""Plan approval and permission boundaries on a real database."""
import copy
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from conftest import add_observation, token_for

EXAMPLE = Path(__file__).parents[1] / 'vendor/contracts/examples/records/investigation-plan-bounded.valid.json'


def create(client, conn, **changes):
    plan = json.loads(EXAMPLE.read_text())
    plan['observation_ids'] = [str(add_observation(conn))]
    plan.update(changes)
    response = client.post('/api/investigations', json=plan)
    assert response.status_code == 201, response.text
    return response.json()


def approve(client, investigation, headers=None):
    head = investigation['revisions'][-1]
    response = client.post(f"/api/investigations/{investigation['id']}/approve",
                           json={'revision_id': head['id'], 'sha256': head['sha256']}, headers=headers)
    return response


def test_exact_approval_and_parallel_idempotence(client, conn):
    investigation = create(client, conn)
    path = f"/api/investigations/{investigation['id']}"
    head = investigation['revisions'][-1]
    request = {'revision_id': head['id'], 'request_key': 'one-operation'}
    assert client.post(path + '/execute', json=request).status_code == 409
    assert client.post(path + '/approve', json={'revision_id': head['id'], 'sha256': '0' * 64}).status_code == 409
    assert approve(client, investigation).status_code == 200
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda _: client.post(path + '/execute', json=request), range(2)))
    assert [r.status_code for r in replies] == [201, 201], [r.text for r in replies]
    assert replies[0].json()['run_id'] == replies[1].json()['run_id']
    assert conn.execute('SELECT count(*) AS n FROM job').fetchone()['n'] == 1
    saved = client.get(path).json()
    assert saved['approved_revision_id'] == head['id']
    assert len(saved['executions']) == 1


def test_agent_scope_revision_pause_and_stop(client, conn):
    investigation = create(client, conn)
    path = f"/api/investigations/{investigation['id']}"
    head = investigation['revisions'][-1]
    outside = str(add_observation(conn, tic=99))
    agent = token_for(conn, 'assistant', campaign_id=investigation['id'])
    assert approve(client, investigation, agent).status_code == 403
    assert approve(client, investigation).status_code == 200
    assert client.get('/api/observations/' + outside, headers=agent).status_code == 403
    assert client.get('/api/observations/' + head['document']['observation_ids'][0], headers=agent).status_code == 200
    assert client.post('/api/runs', json={'method': head['document']['method'], 'parameters': {},
                                        'observation_ids': head['document']['observation_ids']}, headers=agent).status_code == 403
    request = {'revision_id': head['id'], 'request_key': 'agent-operation'}
    assert client.post(path + '/execute', json=request, headers=agent).status_code == 201
    assert client.post(path + '/pause').status_code == 200
    request['request_key'] = 'after-pause'
    assert client.post(path + '/execute', json=request, headers=agent).status_code == 409
    assert conn.execute('SELECT state FROM job').fetchone()['state'] == 'queued'
    assert client.post(path + '/stop').status_code == 200
    assert conn.execute('SELECT state FROM job').fetchone()['state'] == 'cancelled'
    assert client.post(path + '/resume').status_code == 200
    changed = copy.deepcopy(head['document'])
    changed['parameters']['search']['maximum_period'] = 8
    revision = {'document': changed, 'parent_id': head['id'], 'expected_head_id': head['id']}
    proposal = client.post(path + '/revisions', json=revision, headers=agent)
    assert proposal.status_code == 201, proposal.text
    assert client.post(path + '/revisions', json=revision).status_code == 409
    assert client.post(path + '/execute', json=request, headers=agent).status_code == 409
    assert client.post(path + '/resume').status_code == 409
    revised = client.get(path).json()
    assert approve(client, revised).status_code == 200
    changed['observation_ids'].append(outside)
    revision.update(document=changed, parent_id=proposal.json()['id'], expected_head_id=proposal.json()['id'])
    assert client.post(path + '/revisions', json=revision, headers=agent).status_code == 403


def test_exclusion_and_budget(client, conn):
    investigation = create(client, conn)
    head = investigation['revisions'][-1]
    document = head['document']
    document['limits']['jobs'] = 1
    path = f"/api/investigations/{investigation['id']}"
    response = client.post(path + '/revisions', json={'document': document, 'parent_id': head['id'], 'expected_head_id': head['id']})
    assert response.status_code == 201
    investigation = client.get(path).json()
    assert approve(client, investigation).status_code == 200
    request = {'revision_id': response.json()['id'], 'request_key': 'first'}
    assert client.post(path + '/execute', json=request).status_code == 201
    request['request_key'] = 'second'
    assert client.post(path + '/execute', json=request).status_code == 409
    document['exclusions'] = [{'observation_id': document['observation_ids'][0], 'reason': 'Engineering exclusion.'}]
    response = client.post(path + '/revisions', json={'document': document, 'parent_id': response.json()['id'],
                                                    'expected_head_id': response.json()['id']})
    assert response.status_code == 201
    assert approve(client, client.get(path).json()).status_code == 200
    request['revision_id'] = response.json()['id']
    assert client.post(path + '/execute', json=request).status_code == 422
