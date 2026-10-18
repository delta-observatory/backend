"""Identity, permissions, and actor records (ROADMAP R8 and R4 access control)."""
import uuid

import psycopg
import pytest

from conftest import add_campaign, add_observation, token_for


def test_every_endpoint_except_health_needs_a_token(client):
    bare = {'Authorization': ''}
    assert client.get('/api/health', headers=bare).status_code == 200
    assert client.get('/api/openapi.json', headers=bare).status_code == 200
    for path in ('/api/methods', '/api/rules', '/api/datasets', '/api/observations', '/api/targets', '/api/runs',
                 '/api/runs/default', '/api/me', '/api/clients'):
        response = client.get(path, headers=bare)
        assert response.status_code == 401, path
        assert response.headers['www-authenticate'] == 'Bearer'
    assert client.get('/api/methods', headers={'Authorization': 'Bearer dlt_wrong'}).status_code == 401
    assert client.post('/api/runs', headers=bare, json={}).status_code == 401


def test_me_and_client_management(client, conn):
    assert client.get('/api/me').json()['name'] == 'richard'
    campaign = str(add_campaign(conn))
    assert client.post('/api/clients', json={'name': 'agent-a', 'kind': 'agent', 'permissions': ['read']}).status_code == 422
    created = client.post('/api/clients', json={'name': 'agent-a', 'kind': 'agent', 'campaign_id': campaign,
                                                'permissions': ['read', 'run.submit', 'record.write']})
    assert created.status_code == 201 and created.json()['token'].startswith('dlt_')
    token = created.json()['token']
    me = client.get('/api/me', headers={'Authorization': f'Bearer {token}'}).json()
    assert me['kind'] == 'agent' and me['permissions'] == ['read', 'record.write', 'run.submit']
    stored = conn.execute("SELECT token_sha256 FROM client WHERE name = 'agent-a'").fetchone()['token_sha256']
    assert token not in stored and len(stored) == 64
    assert client.post('/api/clients', json={'name': 'agent-a', 'kind': 'agent', 'campaign_id': campaign,
                                             'permissions': ['read']}).status_code == 409
    # Richard's decision: an agent cannot hold acquisition, identity reveal, or administration.
    for permission in ('acquire', 'identity.reveal', 'admin'):
        response = client.post('/api/clients', json={'name': f'agent-{permission.replace(".", "")}', 'kind': 'agent', 'permissions': ['read', permission]})
        assert response.status_code == 422, permission
    assert client.post('/api/clients', json={'name': 'x2', 'kind': 'person', 'permissions': ['fly']}).status_code == 422
    disabled = client.post(f"/api/clients/{created.json()['id']}/disable")
    assert disabled.status_code == 200 and disabled.json()['disabled_at']
    assert client.get('/api/me', headers={'Authorization': f'Bearer {token}'}).status_code == 401
    assert client.post(f"/api/clients/{client.get('/api/me').json()['id']}/disable").status_code == 409


def test_database_refuses_an_agent_with_a_forbidden_permission(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO client (name, kind, permissions, token_sha256, created_by) "
                     "VALUES ('agent-x', 'agent', '{read,admin}', repeat('a', 64), 'test')")


def test_permissions_separate_reading_submission_and_administration(client, conn):
    oid = add_observation(conn)
    reader = token_for(conn, 'reader', kind='service', permissions=['read'])
    agent = token_for(conn, 'agent-a')
    request = {'method': {'id': 'tess-bls', 'version': 1}, 'observation_ids': [str(oid)]}
    assert client.get('/api/runs', headers=reader).status_code == 200
    assert client.post('/api/runs/estimate', headers=reader, json=request).status_code == 200
    assert client.post('/api/runs', headers=reader, json=request).status_code == 403
    assert client.get('/api/clients', headers=agent).status_code == 403
    assert client.post('/api/clients', headers=agent, json={'name': 'agent-b', 'kind': 'agent', 'permissions': ['read']}).status_code == 403
    assert client.post('/api/datasets/tess-spoc-light-curves/index', headers=agent).status_code == 403
    nothing = token_for(conn, 'nothing', kind='service', permissions=[])
    assert client.get('/api/methods', headers=nothing).status_code == 403


def test_writes_record_their_actor(client, conn):
    oid = add_observation(conn)
    agent = token_for(conn, 'agent-a')
    request = {'method': {'id': 'tess-bls', 'version': 1}, 'observation_ids': [str(oid)]}
    run = client.post('/api/runs', headers=agent, json=request).json()
    assert run['requested_by'] == 'client:agent-a'
    agent_id = client.get('/api/me', headers=agent).json()['id']
    assert run['client_id'] == agent_id
    assert client.get('/api/runs').json()[0]['client_id'] == agent_id
    # A client cancels only the jobs of its own runs. Richard has "admin" and can cancel any job.
    # An agent of the same campaign sees the run but cannot cancel its jobs. Another campaign's agent does not see it.
    job_id = run['jobs'][0]['id']
    colleague = token_for(conn, 'agent-c', campaign_id=uuid.UUID(str(run['campaign_id'])))
    assert client.post(f'/api/jobs/{job_id}/cancel', headers=colleague).status_code == 403
    other = token_for(conn, 'agent-b')
    assert client.post(f'/api/jobs/{job_id}/cancel', headers=other).status_code == 404
    cancelled = client.post(f'/api/jobs/{job_id}/cancel', headers=agent).json()
    assert cancelled['cancelled_by'] == agent_id
    index = client.post('/api/datasets/tess-spoc-light-curves/index').json()
    assert conn.execute('SELECT requested_by FROM run WHERE id = %s', [index['run_id']]).fetchone()['requested_by'] == 'client:richard'
    assert client.post(f'/api/jobs/{uuid.uuid4()}/cancel').status_code == 404
