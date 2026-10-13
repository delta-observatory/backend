"""Stateless HTTP API for Delta Observatory.

The API reads and writes PostgreSQL records and reads artifact files.
It never calls a worker. Workers claim the jobs that the API inserts.
"""
from contextlib import asynccontextmanager
from typing import Annotated
import re
import uuid

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import __version__, db, settings as settings_module
from .contracts import Contracts, ValidationFailure, parameters_sha256

DATASET_PATTERN = r'^[a-z0-9][a-z0-9-]*$'
INDEX_METHOD = ('tess-spoc-index', 1)


class MethodRef(BaseModel):
    id: str
    version: int = Field(ge=1)


class RunRequest(BaseModel):
    method: MethodRef
    parameters: dict = Field(default_factory=dict)
    observation_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)
    label: str | None = Field(default=None, max_length=200)


def create_app(config: settings_module.Settings | None = None) -> FastAPI:
    config = config or settings_module.load()
    contracts = Contracts(config.contracts_dir)
    pool = db.make_pool(config.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open(wait=False)
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(
        title='Delta Observatory API',
        version=__version__,
        docs_url='/api/docs',
        openapi_url='/api/openapi.json',
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = config
    app.state.contracts = contracts
    app.state.pool = pool
    if config.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(config.cors_origins),
                           allow_methods=['GET', 'POST'], allow_headers=['*'])

    @app.exception_handler(ValidationFailure)
    async def validation_failure(request: Request, exc: ValidationFailure):
        return JSONResponse(status_code=422, content={'detail': exc.errors})

    def connection():
        with pool.connection() as conn:
            yield conn

    Conn = Annotated[object, Depends(connection)]

    # Health and methods ---------------------------------------------------

    @app.get('/api/health')
    def health():
        try:
            with pool.connection(timeout=2) as conn:
                conn.execute('SELECT 1')
        except Exception as error:  # noqa: BLE001 - any failure means unavailable
            return JSONResponse(status_code=503, content={
                'status': 'unavailable', 'database': 'unavailable', 'error': type(error).__name__})
        return {'status': 'ok', 'database': 'ok', 'version': __version__, 'contracts': contracts.source}

    @app.get('/api/methods')
    def list_methods():
        return [m.describe() for _, m in sorted(contracts.methods.items())]

    @app.get('/api/methods/{method_id}/{version}')
    def get_method(method_id: str, version: int):
        method = contracts.method(method_id, version)
        if method is None:
            raise HTTPException(404, f'unknown method {method_id} v{version}')
        return method.describe()

    # Datasets -----------------------------------------------------------

    @app.get('/api/datasets')
    def list_datasets(conn: Conn):
        rows = conn.execute("""
            SELECT d.id, d.state, d.findings, d.last_indexed_at,
                   COALESCE(jsonb_object_agg(o.state, o.n) FILTER (WHERE o.state IS NOT NULL), '{}'::jsonb)
                       AS observation_counts
            FROM dataset d
            LEFT JOIN (SELECT dataset_id, state, count(*) AS n FROM observation GROUP BY 1, 2) o
                   ON o.dataset_id = d.id
            GROUP BY d.id
            ORDER BY d.id""").fetchall()
        return rows

    @app.post('/api/datasets/{dataset_id}/index', status_code=202)
    def index_dataset(dataset_id: str, conn: Conn):
        if not re.fullmatch(DATASET_PATTERN, dataset_id):
            raise HTTPException(422, [{'loc': ['path', 'dataset_id'], 'msg': 'invalid dataset id'}])
        method = contracts.method(*INDEX_METHOD)
        parameters = contracts.resolve_parameters(method, {'dataset_id': dataset_id})
        run_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
        document = contracts.job_document(job_id=job_id, run_id=run_id, method=method,
                                          parameters=parameters, observation_ids=[])
        try:
            with conn.transaction():
                conn.execute('INSERT INTO dataset (id) VALUES (%s) ON CONFLICT (id) DO NOTHING', [dataset_id])
                _insert_run(conn, run_id, method, parameters, None)
                _insert_job(conn, job_id, run_id, None, document)
        except pg_errors.UniqueViolation:
            active = conn.execute("""
                SELECT id, state FROM job
                WHERE document -> 'method' ->> 'id' = 'tess-spoc-index'
                  AND document -> 'parameters' ->> 'dataset_id' = %s
                  AND state IN ('queued', 'running')""", [dataset_id]).fetchone()
            raise HTTPException(409, {'msg': 'an index job for this dataset is queued or running',
                                      'job': jsonable_encoder(active)})
        return {'run_id': run_id, 'job_id': job_id}

    # Observations -------------------------------------------------------

    @app.get('/api/observations')
    def list_observations(
        conn: Conn,
        dataset_id: str | None = None,
        target_id: str | None = None,
        state: str | None = None,
        label: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        where, args = [], []
        for column, value in (('dataset_id', dataset_id), ('target_id', target_id), ('state', state)):
            if value is not None:
                where.append(f'{column} = %s')
                args.append(value)
        if label is not None:
            where.append("labels ->> 'label' = %s")
            args.append(label)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        total = conn.execute(f'SELECT count(*) AS n FROM observation {clause}', args).fetchone()['n']
        rows = conn.execute(f"""
            SELECT {OBSERVATION_COLUMNS} FROM observation {clause}
            ORDER BY dataset_id, target_id, sector NULLS LAST, id
            LIMIT %s OFFSET %s""", [*args, limit, offset]).fetchall()
        return {'total': total, 'limit': limit, 'offset': offset, 'items': rows}

    @app.get('/api/observations/{observation_id}')
    def get_observation(observation_id: uuid.UUID, conn: Conn):
        row = conn.execute(f'SELECT {OBSERVATION_COLUMNS}, header FROM observation WHERE id = %s',
                           [observation_id]).fetchone()
        if row is None:
            raise HTTPException(404, 'observation not found')
        artifacts = conn.execute("""
            SELECT * FROM artifact WHERE observation_id = %s AND kind = 'light_curve'
            ORDER BY created_at DESC""", [observation_id]).fetchall()
        row['artifacts'] = [db.artifact(a) for a in artifacts]
        return row

    # Runs ---------------------------------------------------------------

    @app.post('/api/runs', status_code=201)
    def create_run(request: RunRequest, conn: Conn):
        method = contracts.method(request.method.id, request.method.version)
        if method is None:
            raise ValidationFailure([{'loc': ['method'], 'msg':
                                      f'unknown method {request.method.id} v{request.method.version}'}])
        if method.kind != 'analysis':
            raise ValidationFailure([{'loc': ['method', 'id'], 'msg':
                                      f'{method.id} is a {method.kind or "non-analysis"} method'}])
        parameters = contracts.resolve_parameters(method, request.parameters)
        ids = [str(o) for o in request.observation_ids]
        errors = [{'loc': ['observation_ids', i], 'msg': 'duplicate observation id'}
                  for i, o in enumerate(ids) if o in ids[:i]]
        states = {str(r['id']): r['state'] for r in conn.execute(
            'SELECT id, state FROM observation WHERE id = ANY(%s)', [ids]).fetchall()}
        for i, o in enumerate(ids):
            if o not in states:
                errors.append({'loc': ['observation_ids', i], 'msg': 'observation not found'})
            elif states[o] != 'ready':
                errors.append({'loc': ['observation_ids', i], 'msg': f'observation state is {states[o]}, not ready'})
        if errors:
            raise ValidationFailure(errors)
        run_id = str(uuid.uuid4())
        jobs = []
        for o in ids:
            job_id = str(uuid.uuid4())
            jobs.append((job_id, o, contracts.job_document(
                job_id=job_id, run_id=run_id, method=method, parameters=parameters, observation_ids=[o])))
        with conn.transaction():
            _insert_run(conn, run_id, method, parameters, request.label)
            with conn.cursor() as cur:
                cur.executemany('INSERT INTO run_input (run_id, observation_id) VALUES (%s, %s)',
                                [(run_id, o) for o in ids])
            for job_id, o, document in jobs:
                _insert_job(conn, job_id, run_id, o, document)
        return _run_detail(conn, run_id)

    @app.get('/api/runs')
    def list_runs(conn: Conn, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        rows = conn.execute("""
            SELECT r.id, r.method_id, r.method_version, r.label, r.requested_by, r.created_at,
                   r.parameters_sha256,
                   (SELECT count(*) FROM run_input i WHERE i.run_id = r.id) AS input_count,
                   COALESCE((SELECT jsonb_object_agg(state, n) FROM
                       (SELECT state, count(*) AS n FROM job j WHERE j.run_id = r.id GROUP BY state) s),
                       '{}'::jsonb) AS job_states
            FROM run r ORDER BY r.created_at DESC, r.id LIMIT %s OFFSET %s""", [limit, offset]).fetchall()
        return rows

    @app.get('/api/runs/{run_id}')
    def get_run(run_id: uuid.UUID, conn: Conn):
        return _run_detail(conn, run_id)

    @app.get('/api/runs/{run_id}/results')
    def get_run_results(run_id: uuid.UUID, conn: Conn):
        if conn.execute('SELECT 1 FROM run WHERE id = %s', [run_id]).fetchone() is None:
            raise HTTPException(404, 'run not found')
        jobs = conn.execute("""
            SELECT id, observation_id, state, result FROM job WHERE run_id = %s
            ORDER BY created_at, id""", [run_id]).fetchall()
        measurements = conn.execute("""
            SELECT m.* FROM measurement m JOIN job j ON j.id = m.job_id
            WHERE j.run_id = %s ORDER BY m.id""", [run_id]).fetchall()
        artifacts = conn.execute("""
            SELECT a.* FROM artifact a JOIN job j ON j.id = a.job_id
            WHERE j.run_id = %s ORDER BY a.created_at, a.id""", [run_id]).fetchall()
        items = []
        for job in jobs:
            result = job['result'] or {}
            grouped: dict[str, list] = {'search': [], 'reference_comparison': []}
            for m in measurements:
                if m['job_id'] == job['id']:
                    grouped.setdefault(m['role'], []).append(db.measurement(m))
            items.append({
                'job_id': job['id'],
                'observation_id': job['observation_id'],
                'state': job['state'],
                'outcome': result.get('outcome'),
                'error': result.get('error'),
                'measurements': grouped,
                'artifacts': [db.artifact(a) for a in artifacts if a['job_id'] == job['id']],
                'diagnostics': result.get('diagnostics'),
                'resources': result.get('resources'),
                'software': result.get('software'),
            })
        return {'run_id': run_id, 'jobs': items}

    # Jobs ---------------------------------------------------------------

    @app.get('/api/jobs/{job_id}')
    def get_job(job_id: uuid.UUID, conn: Conn):
        row = conn.execute('SELECT * FROM job WHERE id = %s', [job_id]).fetchone()
        if row is None:
            raise HTTPException(404, 'job not found')
        return row

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel_job(job_id: uuid.UUID, conn: Conn):
        with conn.transaction():
            row = conn.execute("""
                UPDATE job SET state = 'cancelled', finished_at = now()
                WHERE id = %s AND state = 'queued'
                RETURNING id, state, finished_at""", [job_id]).fetchone()
            if row is None:
                current = conn.execute('SELECT state FROM job WHERE id = %s', [job_id]).fetchone()
                if current is None:
                    raise HTTPException(404, 'job not found')
                raise HTTPException(409, f"job state is {current['state']}; only a queued job can be cancelled")
        return row

    # Artifacts ----------------------------------------------------------

    def _artifact_row(conn, artifact_id):
        row = conn.execute('SELECT * FROM artifact WHERE id = %s', [artifact_id]).fetchone()
        if row is None:
            raise HTTPException(404, 'artifact not found')
        return row

    @app.get('/api/artifacts/{artifact_id}')
    def get_artifact(artifact_id: uuid.UUID, conn: Conn):
        return db.artifact(_artifact_row(conn, artifact_id))

    @app.get('/api/artifacts/{artifact_id}/content')
    def get_artifact_content(artifact_id: uuid.UUID, request: Request, conn: Conn):
        row = _artifact_row(conn, artifact_id)
        root = config.artifact_root.resolve()
        target = (root / row['path']).resolve()
        if not target.is_relative_to(root) or target == root:
            raise HTTPException(400, 'artifact path is outside the artifact root')
        etag = f'"{row["sha256"]}"'
        headers = {'ETag': etag, 'Cache-Control': 'public, max-age=31536000, immutable'}
        if row['content_encoding'] == 'gzip':
            headers['Content-Encoding'] = 'gzip'
        if request.headers.get('if-none-match') == etag:
            return Response(status_code=304, headers=headers)
        if not target.is_file():
            raise HTTPException(404, 'artifact file is missing')
        return FileResponse(target, media_type=row['media_type'], headers=headers)

    return app


OBSERVATION_COLUMNS = """id, dataset_id, product_type, relative_path, bytes, sha256, target_id, sector,
    cadence_seconds, time_start, time_stop, time_scale, time_reference, row_count, labels, state,
    findings, first_indexed_at, last_seen_at"""


def _insert_run(conn, run_id, method, parameters, label):
    conn.execute("""
        INSERT INTO run (id, method_id, method_version, parameters, parameters_sha256, label, requested_by)
        VALUES (%s, %s, %s, %s, %s, %s, 'api')""",
                 [run_id, method.id, method.version, Jsonb(parameters), parameters_sha256(parameters), label])


def _insert_job(conn, job_id, run_id, observation_id, document):
    conn.execute('INSERT INTO job (id, run_id, observation_id, document) VALUES (%s, %s, %s, %s)',
                 [job_id, run_id, observation_id, Jsonb(document)])


def _run_detail(conn, run_id):
    run = conn.execute('SELECT * FROM run WHERE id = %s', [run_id]).fetchone()
    if run is None:
        raise HTTPException(404, 'run not found')
    run['inputs'] = [r['observation_id'] for r in conn.execute(
        'SELECT observation_id FROM run_input WHERE run_id = %s ORDER BY observation_id', [run_id]).fetchall()]
    run['jobs'] = conn.execute("""
        SELECT id, observation_id, state, attempts, max_attempts, available_at, claimed_by,
               lease_expires_at, created_at, started_at, finished_at,
               result ->> 'outcome' AS outcome, result -> 'error' AS error
        FROM job WHERE run_id = %s ORDER BY created_at, id""", [run_id]).fetchall()
    return run


app = create_app()
