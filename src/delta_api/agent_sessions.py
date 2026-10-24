"""Managed session records. This module never calls a model or agent host."""
from typing import Annotated
import hashlib
import secrets
import uuid
from fastapi import APIRouter, Body, Depends, HTTPException
from psycopg.types.json import Jsonb
from . import auth
from .auth import Actor
from .investigations import load, require_owner, require_open, revision, append_event


def router(*, connection, need, contracts):
    api = APIRouter(tags=['Agent sessions'])
    Conn = Annotated[object, Depends(connection)]
    Reader = Annotated[Actor, Depends(need('read'))]
    Owner = Annotated[Actor, Depends(need('record.write'))]

    def session(conn, actor, session_id, lock=False):
        row = conn.execute('SELECT * FROM agent_session WHERE id=%s' + (' FOR UPDATE' if lock else ''), [session_id]).fetchone()
        if row is None:
            raise HTTPException(404, 'Agent session not found.')
        investigation = load(conn, actor, row['campaign_id'], lock=lock)
        require_owner(investigation, actor)
        return row, investigation

    def write(conn, row, document):
        document = contracts.record('agent-session/1', document)
        return conn.execute('UPDATE agent_session SET document=%s,updated_at=now() WHERE id=%s RETURNING *',
                            [Jsonb(document), row['id']]).fetchone()

    @api.get('/investigations/{campaign_id}/sessions')
    def listing(campaign_id: uuid.UUID, conn: Conn, actor: Reader):
        row = load(conn, actor, campaign_id)
        require_owner(row, actor)
        return {'items': conn.execute('SELECT * FROM agent_session WHERE campaign_id=%s ORDER BY created_at DESC', [campaign_id]).fetchall()}

    @api.post('/investigations/{campaign_id}/sessions', status_code=201)
    def create(campaign_id: uuid.UUID, conn: Conn, actor: Owner, document: dict = Body(...)):
        with conn.transaction():
            row = load(conn, actor, campaign_id, lock=True)
            require_owner(row, actor)
            require_open(row)
            document = contracts.record('agent-session/1', document)
            if row['state'] != 'ready' or str(row['approved_revision_id']) != document['revision_id']:
                raise HTTPException(409, 'Connect an agent only to the current approved plan.')
            if document['turns'] or document['tokens'] or document['provider_thread_id'] is not None or document['state'] != 'idle':
                raise HTTPException(422, 'A new session starts idle with no provider thread or recorded usage.')
            client, token = auth.create_client(conn, name='managed-' + uuid.uuid4().hex, kind='agent',
                                               permissions=['read','run.submit','record.write'],
                                               created_by=f'client:{actor.name}', campaign_id=campaign_id)
            created = conn.execute('INSERT INTO agent_session(campaign_id,client_id,document,created_by) VALUES(%s,%s,%s,%s) RETURNING *',
                                    [campaign_id, client['id'], Jsonb(document), actor.id]).fetchone()
            append_event(conn, contracts, campaign_id, actor, 'agent_started',
                         'The owner consented to send permitted investigation evidence to the selected model provider.',
                         revision_id=row['approved_revision_id'], agent_session_id=created['id'])
            return {'session': created, 'token': token}

    @api.post('/agent-sessions/{session_id}/reconnect')
    def reconnect(session_id: uuid.UUID, conn: Conn, actor: Owner):
        with conn.transaction():
            row, investigation = session(conn, actor, session_id, lock=True)
            require_open(investigation)
            if row['document']['state'] == 'closed':
                raise HTTPException(409, 'A closed session cannot reconnect.')
            token = secrets.token_urlsafe(32)
            conn.execute('UPDATE client SET token_sha256=%s WHERE id=%s', [hashlib.sha256(token.encode()).hexdigest(), row['client_id']])
            document = {**row['document'], 'state': 'paused'}
            return {'session': write(conn, row, document), 'token': token}

    @api.post('/agent-sessions/{session_id}/turn')
    def reserve_turn(session_id: uuid.UUID, conn: Conn, actor: Owner):
        with conn.transaction():
            row, investigation = session(conn, actor, session_id, lock=True)
            require_open(investigation)
            if investigation['state'] != 'ready' or investigation['head_revision_id'] != investigation['approved_revision_id']:
                raise HTTPException(409, 'Agent work requires a ready investigation and its current approved plan.')
            document = row['document']
            if document['state'] in ('running', 'closed'):
                raise HTTPException(409, 'The session already has an active turn or is closed.')
            if conn.execute("SELECT 1 FROM agent_session WHERE campaign_id=%s AND document->>'state'='running'", [row['campaign_id']]).fetchone():
                raise HTTPException(409, 'Another agent turn is active in this investigation.')
            approved = revision(conn, row['campaign_id'], investigation['approved_revision_id'])
            totals = conn.execute("SELECT coalesce(sum((document->>'turns')::int),0) AS turns, "
                                  "coalesce(sum((document->>'tokens')::bigint),0) AS tokens FROM agent_session WHERE campaign_id=%s",
                                  [row['campaign_id']]).fetchone()
            limits = approved['document']['limits']
            if totals['turns'] >= limits['model_turns'] or totals['tokens'] >= limits['model_tokens']:
                raise HTTPException(409, 'The investigation reached its approved model-use limit.')
            return write(conn, row, {**document, 'revision_id':str(approved['id']), 'turns':document['turns']+1,'state':'running'})

    @api.post('/agent-sessions/{session_id}/state')
    def state(session_id: uuid.UUID, conn: Conn, actor: Owner, changes: dict = Body(...)):
        with conn.transaction():
            row, investigation = session(conn, actor, session_id, lock=True)
            if row['document']['state'] == 'closed':
                raise HTTPException(409, 'A closed session cannot change.')
            if set(changes) - {'state','provider_thread_id','tokens'}:
                raise HTTPException(422, 'Only connection state, provider thread, and measured tokens can change here.')
            document = contracts.record('agent-session/1', {**row['document'], **changes})
            if document['tokens'] < row['document']['tokens'] or document['state'] == 'running' and row['document']['state'] != 'running':
                raise HTTPException(409, 'Usage cannot decrease. Reserve a turn before entering the running state.')
            result = write(conn, row, document)
            if document['state'] == 'closed':
                conn.execute('UPDATE client SET disabled_at=now() WHERE id=%s', [row['client_id']])
            return result

    @api.post('/agent-sessions/{session_id}/events', status_code=201)
    def event(session_id: uuid.UUID, conn: Conn, actor: Owner, document: dict = Body(...)):
        row, investigation = session(conn, actor, session_id)
        document = contracts.record('investigation-event/1', document)
        if document['kind'] not in {'agent_started','agent_message','agent_paused','agent_completed','agent_error','instruction','limit'}:
            raise HTTPException(422, 'This endpoint accepts only managed agent events.')
        if set(document) - {'contract','kind','message'}:
            raise HTTPException(422, 'The host event accepts only its kind and message.')
        return append_event(conn, contracts, row['campaign_id'], actor, document['kind'], document['message'], agent_session_id=session_id)

    return api
