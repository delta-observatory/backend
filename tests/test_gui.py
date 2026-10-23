"""Anonymous GUI reads preserve token authentication and campaign scope on the API."""
from dataclasses import replace

from fastapi.testclient import TestClient

from conftest import add_campaign, add_observation, token_for
from delta_api.app import create_app


def public_app(client, conn):
    token_for(conn, 'gui-browser', kind='service', permissions=['read'])
    return TestClient(create_app(replace(client.app.state.settings, gui_read_client='gui-browser')))


def test_guest_reads_and_all_write_paths_remain_protected(client, conn):
    oid = add_observation(conn)
    with public_app(client, conn) as guest:
        who = guest.get('/gui-api/me')
        assert who.status_code == 200
        assert who.json()['anonymous'] is True and who.json()['permissions'] == ['read']
        assert who.json()['name'] == 'gui-browser'
        for path in ('/datasets', '/observations', '/methods', '/runs', '/candidates', '/signals'):
            assert guest.get('/gui-api' + path).status_code == 200, path
            assert guest.get('/api' + path).status_code == 401, path
        request = {'method': {'id': 'tess-bls', 'version': 1}, 'observation_ids': [str(oid)]}
        assert guest.post('/gui-api/runs/estimate', json=request).status_code == 200
        for path in ('/runs', '/candidates', '/notes', '/clients', '/jobs/example/cancel', '/datasets/example/index'):
            assert guest.post('/gui-api' + path, json={}).status_code == 403, path
        assert guest.delete('/gui-api/candidates/example').status_code == 403
        assert guest.get('/gui-api/clients').status_code == 403
        assert guest.post('/api/runs', json=request).status_code == 401
        # A GUI request cannot turn an authenticated writer into an anonymous writer.
        assert guest.post('/gui-api/runs', json=request, headers=dict(client.headers)).status_code == 403
        # The original authenticated API still submits runs.
        assert guest.post('/api/runs', json=request, headers=dict(client.headers)).status_code == 201


def test_bad_credentials_never_fall_back_to_guest_and_agents_keep_blinding(client, conn):
    add_observation(conn, tic=123, labels={'class': 'planet_host'})
    campaign = add_campaign(conn, blinding='labels_identity')
    agent = token_for(conn, 'blind-browser', campaign_id=campaign)
    with public_app(client, conn) as guest:
        for value in ('', 'Bearer invalid', 'Basic invalid'):
            assert guest.get('/gui-api/me', headers={'Authorization': value}).status_code == 401
        who = guest.get('/gui-api/me', headers=agent).json()
        assert who['kind'] == 'agent' and who['anonymous'] is False
        observation = guest.get('/gui-api/observations', headers=agent).json()['items'][0]
        assert observation['target_id'] is None and observation['labels'] == {}


def test_guest_access_is_opt_in_and_fails_closed_if_service_changes(client, conn):
    assert client.get('/gui-api/me').status_code == 404
    with public_app(client, conn) as guest:
        conn.execute("UPDATE client SET permissions=ARRAY['read','run.submit'] WHERE name='gui-browser'")
        assert guest.get('/gui-api/me').status_code == 503
        conn.execute("UPDATE client SET permissions=ARRAY['read'], disabled_at=now() WHERE name='gui-browser'")
        assert guest.get('/gui-api/me').status_code == 503
