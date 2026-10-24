from conftest import token_for
from test_investigations import create, approve


def test_managed_session_consent_limits_scope_and_reconnect(client,conn):
    investigation=create(client,conn)
    assert approve(client,investigation).status_code==200
    cid=investigation['id'];revision=investigation['head_revision_id']
    document={'contract':'agent-session/1','provider':'codex','model':'selected-model','revision_id':revision,
              'provider_thread_id':None,'state':'idle','turns':0,'tokens':0,'consent':True}
    foreign=token_for(conn,'outside-agent')
    assert client.post(f'/api/investigations/{cid}/sessions',json=document,headers=foreign).status_code==404
    response=client.post(f'/api/investigations/{cid}/sessions',json=document)
    assert response.status_code==201,response.text
    saved=response.json();path='/api/agent-sessions/'+saved['session']['id'];agent={'Authorization':'Bearer '+saved['token']}
    oid=investigation['revisions'][0]['document']['observation_ids'][0]
    assert client.get('/api/observations/'+oid,headers=agent).status_code==403
    assert client.post(path+'/turn').json()['document']['turns']==1
    assert client.post(path+'/turn').status_code==409
    assert client.get('/api/observations/'+oid,headers=agent).status_code==200
    assert client.post(path+'/state',json={'state':'paused','tokens':100000}).status_code==200
    assert client.post(path+'/turn').status_code==409
    assert client.post(path+'/state',json={'tokens':0}).status_code==409
    rotated=client.post(path+'/reconnect').json()
    assert rotated['token']!=saved['token']
    assert client.get('/api/me',headers=agent).status_code==401
    assert client.post(path+'/events',json={'contract':'investigation-event/1','kind':'plan_approved','message':'No.'}).status_code==422
    assert client.post(path+'/events',json={'contract':'investigation-event/1','kind':'agent_message','message':'A scoped result explanation.'}).status_code==201


def test_public_exploration_requires_investigation_scope(client,conn,database_url,artifact_root):
    from fastapi.testclient import TestClient
    from conftest import ROOT,add_observation
    from delta_api.app import create_app
    from delta_api.settings import Settings
    config=Settings(database_url=database_url,artifact_root=artifact_root,contracts_dir=ROOT/'vendor/contracts',cors_origins=(),cohorts_path=None,standalone_exploration=True)
    with TestClient(create_app(config),headers=dict(client.headers)) as public:
        unscoped=token_for(conn,'unscoped')
        assert public.get('/api/observations',headers=unscoped).status_code==403
        investigation=create(public,conn)
        assert approve(public,investigation).status_code==200
        scoped=token_for(conn,'scoped',campaign_id=investigation['id'])
        selected=investigation['revisions'][0]['document']['observation_ids']
        outside=str(add_observation(conn,tic=123))
        items=public.get('/api/observations',headers=scoped).json()['items']
        assert {i['id'] for i in items}==set(selected)
        assert public.get('/api/observations/'+outside,headers=scoped).status_code==403


def test_canonical_forms_and_investigation_export(client,conn):
    import io,json,zipfile
    document=client.get('/api/openapi.json').json()
    schema=document['paths']['/api/investigations']['post']['requestBody']['content']['application/json']['schema']
    assert schema['properties']['parameters']['properties']['search']['properties']['minimum_period']['type']=='number'
    assert '$ref' not in json.dumps(client.get('/api/contracts/records').json())
    investigation=create(client,conn);assert approve(client,investigation).status_code==200
    response=client.post('/api/investigations/'+investigation['id']+'/execute',json={'revision_id':investigation['head_revision_id'],'request_key':'export-record'})
    assert response.status_code==201,response.text
    exported=client.get('/api/runs/'+response.json()['run_id']+'/export')
    assert exported.status_code==200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        record=json.loads(archive.read('investigation.json'))
        assert record['approved_revision_id']==investigation['head_revision_id']
        assert record['approvals']
        assert record['events']
        assert 'token_sha256' not in archive.read('investigation.json').decode()


def test_model_token_limit_counts_all_sessions_during_a_turn(client,conn):
    investigation=create(client,conn);assert approve(client,investigation).status_code==200
    cid=investigation['id'];oid=investigation['revisions'][0]['document']['observation_ids'][0]
    document={'contract':'agent-session/1','provider':'codex','model':'selected-model','revision_id':investigation['head_revision_id'],
              'provider_thread_id':None,'state':'idle','turns':0,'tokens':0,'consent':True}
    first=client.post(f'/api/investigations/{cid}/sessions',json=document).json()
    second=client.post(f'/api/investigations/{cid}/sessions',json=document).json()
    assert client.post('/api/agent-sessions/'+first['session']['id']+'/state',json={'tokens':90000}).status_code==200
    path='/api/agent-sessions/'+second['session']['id']
    assert client.post(path+'/turn').status_code==200
    assert client.post(path+'/state',json={'tokens':10000}).status_code==200
    assert client.get('/api/observations/'+oid,headers={'Authorization':'Bearer '+second['token']}).status_code==403
