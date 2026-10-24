import json
from pathlib import Path
from psycopg.types.json import Jsonb
from conftest import token_for
from delta_api.contracts import parameters_sha256

ROOT = Path(__file__).parents[1] / 'vendor/contracts/examples/records'

def test_person_approves_exact_bounded_manifest_and_retries(client, conn):
    request = json.loads((ROOT / 'acquisition-request-bounded.valid.json').read_text())
    agent = token_for(conn, 'acquisition-agent', permissions=('read',))
    assert client.post('/api/acquisitions', json=request, headers=agent).status_code == 403
    response = client.post('/api/acquisitions', json=request)
    assert response.status_code == 201, response.text
    path = '/api/acquisitions/' + response.json()['id']
    assert client.post(path + '/approve', json={'sha256':'0'*64}).status_code == 409
    manifest = json.loads((ROOT / 'acquisition-manifest-bounded.valid.json').read_text())
    digest = parameters_sha256(manifest)
    conn.execute("UPDATE acquisition SET state='preview',manifest=%s,manifest_sha256=%s WHERE id=%s", [Jsonb(manifest),digest,response.json()['id']])
    assert client.post(path + '/approve', json={'sha256':'0'*64}).status_code == 409
    assert client.post(path + '/approve', json={'sha256':digest}).json()['state'] == 'queued'
    assert client.post(path + '/cancel').json()['state'] == 'cancelled'
    repeated = client.post(path + '/retry').json()
    assert repeated['state'] == 'queued'
    assert repeated['manifest_sha256'] == digest
    assert client.get(path, headers=agent).status_code == 404
    assert client.get('/api/acquisitions', headers=agent).json()['items'] == []
