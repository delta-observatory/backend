"""Archive requests are database records. Workers contact the archive."""
from typing import Annotated
import uuid
from fastapi import APIRouter, Body, Depends, HTTPException
from .auth import Actor
from psycopg.types.json import Jsonb


def router(*, connection, need, contracts):
    api = APIRouter(tags=['Acquisition'])
    Conn = Annotated[object, Depends(connection)]
    Reader = Annotated[Actor, Depends(need('read'))]
    Acquirer = Annotated[Actor, Depends(need('acquire'))]

    def person(actor):
        if actor.kind != 'person':
            raise HTTPException(403, 'Only a person can request or control archive acquisition.')

    def get(conn, actor, acquisition_id, lock=False):
        row = conn.execute('SELECT * FROM acquisition WHERE id=%s' + (' FOR UPDATE' if lock else ''), [acquisition_id]).fetchone()
        if row is None or actor.kind == 'agent':
            raise HTTPException(404, 'Acquisition not found.')
        return row

    def owner(row, actor):
        person(actor)
        if row['created_by'] != actor.id and not actor.has('admin'):
            raise HTTPException(403, 'Only the acquisition owner can change this request.')

    @api.get('/acquisitions')
    def listing(conn: Conn, actor: Reader):
        if actor.kind == 'agent':
            return {'items': []}
        return {'items': conn.execute('SELECT * FROM acquisition ORDER BY created_at DESC LIMIT 100').fetchall()}

    @api.get('/acquisitions/{acquisition_id}')
    def retrieve(acquisition_id: uuid.UUID, conn: Conn, actor: Reader):
        return get(conn, actor, acquisition_id)

    @api.post('/acquisitions', status_code=201)
    def create(conn: Conn, actor: Acquirer, document: dict = Body(...)):
        person(actor)
        document = contracts.record('acquisition-request/1', document)
        return conn.execute('INSERT INTO acquisition(request,created_by) VALUES(%s,%s) RETURNING *',
                            [Jsonb(document), actor.id]).fetchone()

    @api.post('/acquisitions/{acquisition_id}/approve')
    def approve(acquisition_id: uuid.UUID, conn: Conn, actor: Acquirer, sha256: str = Body(..., embed=True)):
        with conn.transaction():
            row = get(conn, actor, acquisition_id, lock=True)
            owner(row, actor)
            if row['state'] != 'preview' or sha256 != row['manifest_sha256']:
                raise HTTPException(409, 'Approval must match the waiting manifest and its checksum.')
            manifest = contracts.record('acquisition-manifest/1', row['manifest'])
            if len(manifest['products']) > row['request']['maximum_files'] or sum(p['bytes'] for p in manifest['products']) > row['request']['maximum_bytes']:
                raise HTTPException(409, 'The manifest exceeds the requested file or byte limit.')
            return conn.execute("UPDATE acquisition SET state='queued',approved_by=%s,approved_at=now(),updated_at=now() WHERE id=%s RETURNING *",
                                [actor.id, acquisition_id]).fetchone()

    @api.post('/acquisitions/{acquisition_id}/cancel')
    def cancel(acquisition_id: uuid.UUID, conn: Conn, actor: Acquirer):
        with conn.transaction():
            row = get(conn, actor, acquisition_id, lock=True)
            owner(row, actor)
            if row['state'] == 'complete':
                raise HTTPException(409, 'The acquisition is complete.')
            return conn.execute("UPDATE acquisition SET state='cancelled',updated_at=now() WHERE id=%s RETURNING *", [acquisition_id]).fetchone()

    @api.post('/acquisitions/{acquisition_id}/retry')
    def retry(acquisition_id: uuid.UUID, conn: Conn, actor: Acquirer):
        with conn.transaction():
            row = get(conn, actor, acquisition_id, lock=True)
            owner(row, actor)
            if row['state'] not in ('failed', 'cancelled'):
                raise HTTPException(409, 'Only a failed or cancelled acquisition can repeat.')
            state = 'queued' if row['approved_at'] else ('preview' if row['manifest'] else 'querying')
            return conn.execute('UPDATE acquisition SET state=%s,error=NULL,updated_at=now() WHERE id=%s RETURNING *',
                                [state, acquisition_id]).fetchone()
    return api
