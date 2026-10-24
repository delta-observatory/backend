"""Persistent investigation control. Canonical documents come from database contracts."""
from copy import deepcopy
from typing import Annotated
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .auth import Actor
from .contracts import LIMITS, ValidationFailure, parameters_sha256
from .policy import Policy, policy_for


class RevisionRequest(BaseModel):
    document: dict
    parent_id: uuid.UUID | None = None
    expected_head_id: uuid.UUID | None = None


class ApprovalRequest(BaseModel):
    revision_id: uuid.UUID
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ExecutionRequest(BaseModel):
    revision_id: uuid.UUID
    request_key: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9._:-]+$')


def load(conn, actor, campaign_id, *, lock=False):
    row = conn.execute('SELECT i.*, c.created_by, c.closed_at, c.decisions_fixed_at FROM investigation i '
                       'JOIN campaign c ON c.id = i.campaign_id WHERE i.campaign_id = %s'
                       + (' FOR UPDATE OF i' if lock else ''), [campaign_id]).fetchone()
    if row is None or (actor.kind == 'agent' and actor.campaign_id != campaign_id):
        raise HTTPException(404, 'Investigation not found.')
    return row


def require_owner(row, actor):
    if actor.kind != 'person' or (actor.id != row['created_by'] and not actor.has('admin')):
        raise HTTPException(403, 'Only the investigation owner can approve or control this investigation.')


def require_open(row):
    if row['closed_at'] is not None or row['decisions_fixed_at'] is not None:
        raise HTTPException(409, 'This campaign is closed or its scientific decisions are fixed.')


def append_event(conn, contracts, campaign_id, actor, kind, message, **references):
    document = contracts.record('investigation-event/1', {
        'contract': 'investigation-event/1', 'kind': kind, 'message': message,
        **{k: str(v) for k, v in references.items() if v is not None},
    })
    return conn.execute('INSERT INTO investigation_event (campaign_id, document, created_by) VALUES (%s, %s, %s) '
                        'RETURNING *', [campaign_id, Jsonb(document), actor.id]).fetchone()


def revision(conn, campaign_id, revision_id):
    row = conn.execute('SELECT * FROM investigation_revision WHERE campaign_id = %s AND id = %s',
                       [campaign_id, revision_id]).fetchone()
    if row is None:
        raise HTTPException(404, 'Plan revision not found.')
    return row


def selected_ids(document):
    excluded = {e['observation_id'] for e in document['exclusions']}
    return [o for o in document['observation_ids'] if o not in excluded]


def checked_plan(conn, contracts, document):
    document = deepcopy(document)
    if not isinstance(document, dict):
        raise HTTPException(422, 'A plan document is required.')
    reference = document.get('method', {})
    if not isinstance(reference, dict):
        raise HTTPException(422, 'The method reference must be an object.')
    method = contracts.method(reference.get('id'), reference.get('version'))
    if method is None or method.kind != 'analysis':
        raise HTTPException(422, 'Choose a supported analysis method.')
    document['parameters'] = contracts.resolve_parameters(method, document.get('parameters', {}))
    document = contracts.record('investigation-plan/1', document)
    ids = document['observation_ids']
    found = {str(r['id']) for r in conn.execute('SELECT id FROM observation WHERE id = ANY(%s::uuid[])', [ids])}
    if set(ids) != found:
        raise HTTPException(422, 'Every selected observation must exist in this workspace.')
    exclusions = [e['observation_id'] for e in document['exclusions']]
    if len(set(exclusions)) != len(exclusions) or not set(exclusions) <= set(ids):
        raise HTTPException(422, 'Exclusions must identify distinct observations in the selection.')
    if len(selected_ids(document)) > document['limits']['files']:
        raise HTTPException(422, 'The selection exceeds the file limit.')
    return document


def insert_revision(conn, contracts, campaign_id, actor, document, parent_id):
    number = conn.execute('SELECT coalesce(max(number), 0) + 1 AS n FROM investigation_revision WHERE campaign_id = %s',
                          [campaign_id]).fetchone()['n']
    result = conn.execute('INSERT INTO investigation_revision '
                          '(campaign_id, number, parent_id, document, sha256, created_by) VALUES (%s,%s,%s,%s,%s,%s) '
                          'RETURNING *', [campaign_id, number, parent_id, Jsonb(document),
                                          parameters_sha256(document), actor.id]).fetchone()
    conn.execute("UPDATE investigation SET head_revision_id=%s, state='draft', updated_at=now() WHERE campaign_id=%s",
                 [result['id'], campaign_id])
    append_event(conn, contracts, campaign_id, actor, 'plan_proposed', f'Plan revision {number} awaits approval.',
                 revision_id=result['id'])
    return result


def detail(conn, actor, campaign_id):
    row = load(conn, actor, campaign_id)
    campaign = conn.execute('SELECT * FROM campaign WHERE id=%s', [campaign_id]).fetchone()
    revisions = conn.execute('SELECT * FROM investigation_revision WHERE campaign_id=%s ORDER BY number',
                             [campaign_id]).fetchall()
    executions = conn.execute('SELECT e.*, r.label, jsonb_object_agg(s.state, s.n) FILTER (WHERE s.state IS NOT NULL) AS job_states '
                              'FROM investigation_execution e JOIN run r ON r.id=e.run_id '
                              'LEFT JOIN LATERAL (SELECT state,count(*) n FROM job WHERE run_id=r.id GROUP BY state) s ON true '
                              'WHERE e.campaign_id=%s GROUP BY e.campaign_id,e.request_key,r.label ORDER BY e.created_at',
                              [campaign_id]).fetchall()
    return {**row, 'id': row['campaign_id'], 'campaign': campaign, 'revisions': revisions, 'executions': executions,
            'approvals': conn.execute('SELECT * FROM investigation_approval WHERE campaign_id=%s ORDER BY id',
                                      [campaign_id]).fetchall()}


def router(*, connection, need, contracts, check_inputs, submit_run):
    api = APIRouter(tags=['Investigations'])
    Conn = Annotated[object, Depends(connection)]
    Reader = Annotated[Actor, Depends(need('read'))]
    Writer = Annotated[Actor, Depends(need('record.write'))]
    Submitter = Annotated[Actor, Depends(need('run.submit'))]

    @api.get('/investigations/schema')
    def plan_schema(_: Reader):
        return {'schema': contracts.records['investigation-plan/1'][0],
                'method': contracts.method('tess-bls', 1).describe()}

    @api.get('/investigations')
    def list_investigations(conn: Conn, actor: Reader, limit: int = Query(100, ge=1, le=500)):
        where = 'WHERE i.campaign_id=%s' if actor.kind == 'agent' else ''
        args = [actor.campaign_id] if actor.kind == 'agent' else []
        rows = conn.execute('SELECT i.*, c.title, c.question, c.created_at, c.created_by, c.blinding, '
                            '(SELECT count(*) FROM investigation_execution e WHERE e.campaign_id=i.campaign_id) AS runs '
                            'FROM investigation i JOIN campaign c ON c.id=i.campaign_id '
                            f'{where} ORDER BY i.updated_at DESC LIMIT %s', [*args, limit]).fetchall()
        return {'items': rows}

    @api.post('/investigations', status_code=201)
    def create_investigation(conn: Conn, actor: Writer, document: dict = Body(...)):
        if actor.kind != 'person':
            raise HTTPException(403, 'Only a person can create an investigation.')
        document = checked_plan(conn, contracts, document)
        with conn.transaction():
            campaign = conn.execute('INSERT INTO campaign(name,title,question,blinding,created_by) VALUES(%s,%s,%s,%s,%s) '
                                    'RETURNING id', ['investigation-' + uuid.uuid4().hex, document['title'],
                                                     document['question'], document['blinding'], actor.id]).fetchone()
            cid = campaign['id']
            conn.execute('INSERT INTO investigation(campaign_id) VALUES(%s)', [cid])
            append_event(conn, contracts, cid, actor, 'created', 'The owner created an exploratory investigation.')
            insert_revision(conn, contracts, cid, actor, document, None)
        return detail(conn, actor, cid)

    @api.get('/investigations/{campaign_id}')
    def get_investigation(campaign_id: uuid.UUID, conn: Conn, actor: Reader):
        return detail(conn, actor, campaign_id)

    @api.post('/investigations/{campaign_id}/revisions', status_code=201)
    def propose_plan(campaign_id: uuid.UUID, request: RevisionRequest, conn: Conn, actor: Writer):
        with conn.transaction():
            row = load(conn, actor, campaign_id, lock=True)
            require_open(row)
            if actor.kind != 'agent':
                require_owner(row, actor)
            if request.expected_head_id != row['head_revision_id']:
                raise HTTPException(409, 'The plan changed. Reload the investigation before editing it.')
            if request.parent_id is None:
                raise HTTPException(422, 'A revision must identify its parent.')
            revision(conn, campaign_id, request.parent_id)
            document = checked_plan(conn, contracts, request.document)
            if actor.kind == 'agent':
                # Agents can propose settings and exclusions. Expanding input scope requires the owner.
                approved = revision(conn, campaign_id, row['approved_revision_id']) if row['approved_revision_id'] else None
                if approved is None or not set(document['observation_ids']) <= set(approved['document']['observation_ids']):
                    raise HTTPException(403, 'Only the owner can expand the observation selection.')
                if document['blinding'] != approved['document']['blinding']:
                    raise HTTPException(403, 'Only the owner can propose a different blinding level.')
            return insert_revision(conn, contracts, campaign_id, actor, document, request.parent_id)

    @api.post('/investigations/{campaign_id}/approve')
    def approve_plan(campaign_id: uuid.UUID, request: ApprovalRequest, conn: Conn, actor: Writer):
        with conn.transaction():
            row = load(conn, actor, campaign_id, lock=True)
            require_owner(row, actor)
            require_open(row)
            plan = revision(conn, campaign_id, request.revision_id)
            if request.revision_id != row['head_revision_id'] or request.sha256 != plan['sha256']:
                raise HTTPException(409, 'Approval must match the current plan revision and its checksum.')
            if row['approved_revision_id'] == request.revision_id and row['state'] == 'ready':
                return detail(conn, actor, campaign_id)
            document = checked_plan(conn, contracts, plan['document'])
            ids = selected_ids(document)
            if ids:
                check_inputs(conn, ids)
            current = conn.execute('SELECT blinding,approved_at FROM campaign WHERE id=%s', [campaign_id]).fetchone()
            if current['approved_at'] and current['blinding'] != document['blinding']:
                raise HTTPException(409, 'An approved campaign keeps its blinding level. Create a separate investigation to change it.')
            conn.execute('INSERT INTO investigation_approval(campaign_id,revision_id,sha256,approved_by) VALUES(%s,%s,%s,%s)',
                         [campaign_id, plan['id'], plan['sha256'], actor.id])
            budgets = {'observations_read': len(ids), 'jobs': document['limits']['jobs'],
                       'compute_seconds': document['limits']['compute_seconds']}
            conn.execute('UPDATE campaign SET title=%s,question=%s,blinding=%s,budgets=%s, '
                         'approved_by=coalesce(approved_by,%s),approved_at=coalesce(approved_at,now()),campaign_file_sha256=%s WHERE id=%s',
                         [document['title'], document['question'], document['blinding'], Jsonb(budgets), actor.id, plan['sha256'], campaign_id])
            conn.execute("UPDATE investigation SET approved_revision_id=%s,state='ready',updated_at=now() WHERE campaign_id=%s",
                         [plan['id'], campaign_id])
            append_event(conn, contracts, campaign_id, actor, 'plan_approved', 'The owner approved this exact plan revision.',
                         revision_id=plan['id'])
        return detail(conn, actor, campaign_id)

    def control(campaign_id, conn, actor, state):
        with conn.transaction():
            row = load(conn, actor, campaign_id, lock=True)
            require_owner(row, actor)
            require_open(row)
            if state == 'ready' and (row['head_revision_id'] != row['approved_revision_id'] or not row['approved_revision_id']):
                raise HTTPException(409, 'Approve the current revision before resuming.')
            conn.execute('UPDATE investigation SET state=%s,updated_at=now() WHERE campaign_id=%s', [state, campaign_id])
            cancelled = []
            if state == 'stopped':
                cancelled = conn.execute("UPDATE job SET state='cancelled',finished_at=now(),cancelled_by=%s,lease_expires_at=NULL "
                                         "WHERE run_id IN (SELECT run_id FROM investigation_execution WHERE campaign_id=%s) "
                                         "AND state IN ('queued','running') RETURNING id", [actor.id, campaign_id]).fetchall()
            kind = {'ready': 'resumed', 'paused': 'paused', 'stopped': 'stopped'}[state]
            messages = {'ready': 'New operations can use the approved revision.',
                        'paused': 'New operations are paused. Active scientific jobs can finish.',
                        'stopped': f'New operations stopped. {len(cancelled)} active or queued jobs were cancelled.'}
            append_event(conn, contracts, campaign_id, actor, kind, messages[state])
        return detail(conn, actor, campaign_id)

    @api.post('/investigations/{campaign_id}/pause')
    def pause(campaign_id: uuid.UUID, conn: Conn, actor: Writer):
        return control(campaign_id, conn, actor, 'paused')

    @api.post('/investigations/{campaign_id}/resume')
    def resume(campaign_id: uuid.UUID, conn: Conn, actor: Writer):
        return control(campaign_id, conn, actor, 'ready')

    @api.post('/investigations/{campaign_id}/stop')
    def stop(campaign_id: uuid.UUID, conn: Conn, actor: Writer):
        return control(campaign_id, conn, actor, 'stopped')

    @api.post('/investigations/{campaign_id}/execute', status_code=201)
    def execute(campaign_id: uuid.UUID, request: ExecutionRequest, conn: Conn, actor: Submitter):
        with conn.transaction():
            row = load(conn, actor, campaign_id, lock=True)
            if actor.kind != 'agent':
                require_owner(row, actor)
            existing = conn.execute('SELECT * FROM investigation_execution WHERE campaign_id=%s AND request_key=%s',
                                    [campaign_id, request.request_key]).fetchone()
            if existing:
                if existing['revision_id'] != request.revision_id:
                    raise HTTPException(409, 'The request key belongs to a different plan revision.')
                return {'run_id': existing['run_id'], 'revision_id': existing['revision_id'], 'repeated': True}
            require_open(row)
            if row['state'] != 'ready' or row['approved_revision_id'] != request.revision_id or row['head_revision_id'] != request.revision_id:
                raise HTTPException(409, 'Execution requires the current approved revision and a ready investigation.')
            plan = revision(conn, campaign_id, request.revision_id)['document']
            ids = selected_ids(plan)
            if not ids:
                raise HTTPException(422, 'Select at least one usable observation before execution.')
            check_inputs(conn, ids)
            used = conn.execute('SELECT count(*) AS jobs, coalesce(sum(CASE WHEN j.state IN (\'queued\',\'running\') '
                                "THEN (j.document->'limits'->>'maximum_seconds')::float8 "
                                "ELSE coalesce((j.result->'resources'->>'elapsed_seconds')::float8,0) END),0) AS seconds "
                                'FROM job j JOIN investigation_execution e ON e.run_id=j.run_id WHERE e.campaign_id=%s',
                                [campaign_id]).fetchone()
            maximum_seconds = LIMITS[plan['method']['id']]['maximum_seconds'] * len(ids)
            if used['jobs'] + len(ids) > plan['limits']['jobs'] or used['seconds'] + maximum_seconds > plan['limits']['compute_seconds']:
                raise HTTPException(409, 'This execution exceeds the approved job or computing limit.')
            campaign = conn.execute('SELECT * FROM campaign WHERE id=%s', [campaign_id]).fetchone()
            pol = policy_for(conn, actor) if actor.kind == 'agent' else Policy(actor, campaign)
            method = contracts.method(plan['method']['id'], plan['method']['version'])
            run = submit_run(conn, actor, pol, method, plan['parameters'], ids, plan['title'])
            conn.execute('INSERT INTO investigation_execution(campaign_id,request_key,revision_id,run_id,created_by) VALUES(%s,%s,%s,%s,%s)',
                         [campaign_id, request.request_key, request.revision_id, run['id'], actor.id])
            append_event(conn, contracts, campaign_id, actor, 'run_submitted', 'The approved analysis entered the worker queue.',
                         revision_id=request.revision_id, run_id=run['id'])
            conn.execute('UPDATE investigation SET updated_at=now() WHERE campaign_id=%s', [campaign_id])
            return {'run_id': run['id'], 'revision_id': request.revision_id, 'repeated': False}

    @api.get('/investigations/{campaign_id}/events')
    def events(campaign_id: uuid.UUID, conn: Conn, actor: Reader, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
        load(conn, actor, campaign_id)
        return {'items': conn.execute('SELECT e.*, c.name AS writer FROM investigation_event e JOIN client c ON c.id=e.created_by '
                                      'WHERE e.campaign_id=%s AND e.id>%s ORDER BY e.id LIMIT %s',
                                      [campaign_id, after, limit]).fetchall()}

    @api.post('/investigations/{campaign_id}/instructions', status_code=201)
    def instruction(campaign_id: uuid.UUID, conn: Conn, actor: Writer, message: str = Body(..., embed=True, min_length=1, max_length=4000)):
        row = load(conn, actor, campaign_id)
        require_owner(row, actor)
        require_open(row)
        return append_event(conn, contracts, campaign_id, actor, 'instruction', message)

    return api
