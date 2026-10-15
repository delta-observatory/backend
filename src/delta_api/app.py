"""Stateless HTTP API for Delta Observatory.

The API reads and writes PostgreSQL records and reads artifact files.
It never calls a worker. Workers claim the jobs that the API inserts.
"""
from contextlib import asynccontextmanager
from typing import Annotated
import gzip
import json
import re
import uuid

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import __version__, db, fold as fold_module, query as query_module, settings as settings_module
from .contracts import Contracts, ValidationFailure, parameters_sha256
from .rules import BLS_GRID, DEFAULT_RUN, FOLD_WINDOW, PERIOD_AGREEMENT

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

    @app.get('/api/rules')
    def list_rules():
        """Named rules that clients apply to stored values: status, default run, query grammar, fold, estimate."""
        return [PERIOD_AGREEMENT.describe(), DEFAULT_RUN.describe(), query_module.describe(),
                FOLD_WINDOW.describe(), BLS_GRID.describe()]

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
        label_class: str | None = Query(None, alias='class',
                                        description='Label class: planet_host, no_known_planet, or eclipsing_binary.'),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        where, args = [], []
        for column, value in (('dataset_id', dataset_id), ('target_id', target_id), ('state', state)):
            if value is not None:
                where.append(f'{column} = %s')
                args.append(value)
        if label_class is not None:
            where.append("labels ->> 'class' = %s")
            args.append(label_class)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        total = conn.execute(f'SELECT count(*) AS n FROM observation {clause}', args).fetchone()['n']
        rows = conn.execute(f"""
            SELECT {OBSERVATION_COLUMNS}, {SKY_COLUMNS} FROM observation {clause}
            ORDER BY dataset_id, target_id, sector NULLS LAST, id
            LIMIT %s OFFSET %s""", [*args, limit, offset]).fetchall()
        return {'total': total, 'limit': limit, 'offset': offset, 'items': rows}

    @app.get('/api/targets')
    def list_targets(
        conn: Conn,
        run_id: uuid.UUID | None = Query(None, description='Run that supplies the measurements. Without it, measurement fields are null.'),
        dataset_id: str | None = None,
        q: str = Query('', description=f'Query in grammar {query_module.GRAMMAR}. GET /api/rules describes it.'),
        label_class: str | None = Query(None, alias='class', description='Label class: planet_host, no_known_planet, or eclipsing_binary.'),
        in_run: bool = Query(False, description='Only the observations that are inputs of the run.'),
        sort: str = Query('-depth_snr', description='A target attribute, with "-" for descending order. Null values sort last.'),
        limit: int = Query(1000, ge=1, le=5000),
        offset: int = Query(0, ge=0),
    ):
        """Ready observations with sky position, magnitude, labels, the run's search values, and a display status."""
        labels_visible = True  # A later blind-campaign policy sets this per client.
        parsed = query_module.parse(q)
        errors = [{'loc': ['query', 'q'], 'msg': m} for m in parsed.errors]
        if not labels_visible:
            errors += [{'loc': ['query', 'q'], 'msg': f'field "{t.field}" needs known-object labels'}
                       for t in parsed.terms if isinstance(t, query_module.Comparison) and t.field in query_module.LABEL_FIELDS]
        if run_id is None:
            # Without a run there are no search values and no status. A filter or sort on them is an error, not an empty result.
            errors += [{'loc': ['query', 'q'], 'msg': f'field "{t.field}" needs run_id'}
                       for t in parsed.terms if isinstance(t, query_module.Comparison) and t.field in query_module.RUN_FIELDS]
            if sort == '-depth_snr':
                sort = 'target_id'
            if in_run:
                errors.append({'loc': ['query', 'in_run'], 'msg': 'in_run needs run_id'})
        key = sort.removeprefix('-')
        if key not in TARGET_ATTRIBUTES:
            errors.append({'loc': ['query', 'sort'], 'msg': f'unknown attribute "{key}"'})
        elif run_id is None and key in RUN_ATTRIBUTES:
            errors.append({'loc': ['query', 'sort'], 'msg': f'attribute "{key}" needs run_id'})
        if errors:
            raise ValidationFailure(errors)
        where, args = ["o.state = 'ready'"], []
        if dataset_id is not None:
            where.append('o.dataset_id = %s'); args.append(dataset_id)
        if label_class is not None:
            where.append("o.labels ->> 'class' = %s"); args.append(label_class)
        if in_run:
            where.append('EXISTS (SELECT 1 FROM run_input ri WHERE ri.run_id = %s AND ri.observation_id = o.id)')
            args.append(run_id)
        q_where, q_args = query_module.to_sql(parsed, {a: a for a in TARGET_ATTRIBUTES})
        base = f"""
            WITH t AS (
                SELECT o.id AS observation_id, o.dataset_id, o.target_id, o.sector, o.cadence_seconds, {SKY_COLUMNS},
                       o.labels ->> 'class' AS label_class,
                       o.labels -> 'planets' -> 0 ->> 'name' AS planet_name,
                       {_json_number("o.labels -> 'planets' -> 0 -> 'period_days'")} AS reference_period_days,
                       j.id AS job_id, j.state AS job_state, j.result ->> 'outcome' AS outcome,
                       m.period_days, m.depth, m.depth_snr, m.relative_period_error
                FROM observation o
                LEFT JOIN job j ON j.observation_id = o.id AND j.run_id = %s
                LEFT JOIN LATERAL (
                    SELECT max(value_number) FILTER (WHERE role = 'search' AND name = 'period') AS period_days,
                           max(value_number) FILTER (WHERE role = 'search' AND name = 'depth') AS depth,
                           max(value_number) FILTER (WHERE role = 'search' AND name = 'depth_snr') AS depth_snr,
                           max(value_number) FILTER (WHERE role = 'reference_comparison'
                                                     AND name = 'relative_period_error') AS relative_period_error
                    FROM measurement WHERE job_id = j.id) m ON true
                WHERE {' AND '.join(where)})
            SELECT * FROM t {('WHERE ' + ' AND '.join(q_where)) if q_where else ''}"""
        all_args = [run_id, *args, *q_args]
        total = conn.execute(f'SELECT count(*) AS n FROM ({base}) c', all_args).fetchone()['n']
        direction = 'DESC' if sort.startswith('-') else 'ASC'
        rows = conn.execute(f"""{base}
            ORDER BY {key} {direction} NULLS LAST, target_id, sector NULLS LAST, observation_id
            LIMIT %s OFFSET %s""", [*all_args, limit, offset]).fetchall()
        for row in rows:
            row['status'] = PERIOD_AGREEMENT.status(row['label_class'], row['depth_snr'], row['relative_period_error'],
                                                    labels_visible) if run_id is not None else None
        return {'total': total, 'limit': limit, 'offset': offset, 'run_id': run_id, 'grammar': query_module.GRAMMAR,
                'status_rule': PERIOD_AGREEMENT.id, 'labels_visible': labels_visible, 'units': TARGET_UNITS,
                'run_values': run_id is not None,
                'note': None if run_id is not None else 'No run_id: search values, job states, and status are null. Name a run to get them.',
                'items': rows}

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

    def prepare_run(request: RunRequest, conn):
        """Check a run request as POST /runs does. Return the method, the full parameters, and the inputs."""
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
        return method, parameters, ids

    @app.post('/api/runs/estimate')
    def estimate_run(request: RunRequest, conn: Conn):
        """Check a run request and estimate its work without a write. A failed check gives the same 422 as POST /runs."""
        method, parameters, ids = prepare_run(request, conn)
        response = {'method': {'id': method.id, 'version': method.version}, 'parameters': parameters,
                    'observation_count': len(ids), 'estimate': None, 'estimate_reason': None}
        if method.id != 'tess-bls':
            response['estimate_reason'] = f'no workload estimate for {method.id}'
            return response
        search = parameters['search']
        spans = {str(r['id']): (r['time_stop'] - r['time_start']) if r['time_start'] is not None and r['time_stop'] is not None else None
                 for r in conn.execute('SELECT id, time_start, time_stop FROM observation WHERE id = ANY(%s)', [ids]).fetchall()}
        limit = search['maximum_grid_trials']
        items = []
        for o in ids:
            n = BLS_GRID.trials(spans[o], search['minimum_period'], search['maximum_period'],
                                search['duration_days'], search['frequency_factor']) if spans[o] else None
            items.append({'observation_id': o, 'baseline_days': spans[o], 'trials': n,
                          'over_limit': n is not None and n > limit})
        known = [i['trials'] for i in items if i['trials'] is not None]
        rate = conn.execute("""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds_per_trial) AS median, count(*) AS jobs
            FROM (SELECT (result -> 'resources' ->> 'elapsed_seconds')::float8
                         / NULLIF((result -> 'diagnostics' ->> 'grid_trials')::float8, 0) AS seconds_per_trial
                  FROM job
                  WHERE state = 'complete' AND document -> 'method' ->> 'id' = %s
                    AND (document -> 'method' ->> 'version')::int = %s
                    AND jsonb_typeof(result -> 'resources' -> 'elapsed_seconds') = 'number'
                    AND jsonb_typeof(result -> 'diagnostics' -> 'grid_trials') = 'number'
                  ORDER BY finished_at DESC NULLS LAST LIMIT 200) s""", [method.id, method.version]).fetchone()
        per_trial = rate['median'] if rate and rate['jobs'] else None
        response['estimate'] = {
            'rule': BLS_GRID.id,
            'trials': {'min': min(known), 'max': max(known), 'total': sum(known)} if known else None,
            'limit': limit,
            'over_limit': sum(1 for i in items if i['over_limit']),
            'seconds_per_trial': {'median': per_trial, 'jobs': rate['jobs'],
                                  'source': 'median of elapsed_seconds / diagnostics.grid_trials over the newest '
                                            '200 complete jobs of the method'} if per_trial is not None else None,
            'worker_seconds': per_trial * sum(known) if per_trial is not None and known else None,
            'items': items,
        }
        return response

    @app.post('/api/runs', status_code=201)
    def create_run(request: RunRequest, conn: Conn):
        method, parameters, ids = prepare_run(request, conn)
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
        ids = [r['id'] for r in conn.execute('SELECT id FROM run ORDER BY created_at DESC, id LIMIT %s OFFSET %s',
                                             [limit, offset]).fetchall()]
        return _run_summaries(conn, ids)

    @app.get('/api/runs/default')
    def default_run(conn: Conn, dataset_id: str | None = None, method_id: str | None = None,
                    method_version: int | None = None):
        """The run that a client shows when it names none (rule default-run/1)."""
        analysis = [key for key, m in contracts.methods.items() if m.kind == 'analysis'
                    and (method_id is None or key[0] == method_id) and (method_version is None or key[1] == method_version)]
        if not analysis:
            raise HTTPException(404, 'no analysis method matches')
        where, args = ['(r.method_id, r.method_version) IN (SELECT * FROM unnest(%s::text[], %s::int[]))'], \
            [[k[0] for k in analysis], [k[1] for k in analysis]]
        if dataset_id is not None:
            where.append("""EXISTS (SELECT 1 FROM run_input ri JOIN observation o ON o.id = ri.observation_id
                                   WHERE ri.run_id = r.id AND o.dataset_id = %s)""")
            args.append(dataset_id)
        candidates = conn.execute(f"""
            SELECT r.id, r.created_at,
                   (SELECT count(*) FROM job j WHERE j.run_id = r.id AND j.state = 'complete') AS complete
            FROM run r WHERE {' AND '.join(where)}""", args).fetchall()
        chosen = DEFAULT_RUN.choose(candidates)
        if chosen is None:
            raise HTTPException(404, 'no analysis run has a complete job')
        return {'rule': DEFAULT_RUN.id, 'run': _run_summaries(conn, [chosen['id']])[0]}

    @app.get('/api/runs/{run_id}')
    def get_run(run_id: uuid.UUID, conn: Conn):
        return _run_detail(conn, run_id)

    @app.get('/api/runs/{run_id}/results')
    def get_run_results(run_id: uuid.UUID, conn: Conn,
                        observation_id: uuid.UUID | None = Query(None, description='Only the job of this observation.')):
        if conn.execute('SELECT 1 FROM run WHERE id = %s', [run_id]).fetchone() is None:
            raise HTTPException(404, 'run not found')
        only = 'AND j.observation_id = %s' if observation_id else ''
        extra = [observation_id] if observation_id else []
        jobs = conn.execute(f"""
            SELECT id, observation_id, state, result FROM job j WHERE run_id = %s {only}
            ORDER BY created_at, id""", [run_id, *extra]).fetchall()
        measurements = conn.execute(f"""
            SELECT m.* FROM measurement m JOIN job j ON j.id = m.job_id
            WHERE j.run_id = %s {only} ORDER BY m.id""", [run_id, *extra]).fetchall()
        artifacts = conn.execute(f"""
            SELECT a.* FROM artifact a JOIN job j ON j.id = a.job_id
            WHERE j.run_id = %s {only} ORDER BY a.created_at, a.id""", [run_id, *extra]).fetchall()
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

    @app.get('/api/runs/{run_id}/observations/{observation_id}/fold')
    def get_fold(
        run_id: uuid.UUID, observation_id: uuid.UUID, conn: Conn,
        period: float | None = Query(None, gt=0, description='Fold period in days. Default: the stored BLS period.'),
        t0: float | None = Query(None, description='Event time, BTJD (TDB). Default: the stored BLS transit time.'),
        window_hours: float | None = Query(None, gt=0, description='Half width of the window. Default: rule fold-window/1.'),
        bin_hours: float | None = Query(None, gt=0, description='Bin width. Default: rule fold-window/1.'),
        t_min: float | None = Query(None, description='Use only times at or after this BTJD.'),
        t_max: float | None = Query(None, description='Use only times at or before this BTJD.'),
        points: bool = Query(False, description='Include the folded points.'),
    ):
        """Fold the stored flattened flux and bin it. A display calculation, not a stored result."""
        job = conn.execute('SELECT id, state FROM job WHERE run_id = %s AND observation_id = %s',
                           [run_id, observation_id]).fetchone()
        if job is None:
            raise HTTPException(404, 'the run has no job for this observation')
        rows = {a['kind']: a for a in conn.execute(
            "SELECT * FROM artifact WHERE job_id = %s AND kind IN ('prepared_series', 'search_summary')", [job['id']]).fetchall()}
        if set(rows) != {'prepared_series', 'search_summary'}:
            raise HTTPException(409, f"job state is {job['state']}; the fold needs its prepared series and search summary")
        best = read_artifact_json(rows['search_summary'])['best']
        columns = {c['name']: c['values'] for c in read_artifact_json(rows['prepared_series'])['columns']}
        stored = period is None and t0 is None
        p = period if period is not None else best['period']
        epoch = t0 if t0 is not None else best['transit_time']
        window = window_hours if window_hours is not None else FOLD_WINDOW.window_hours(best['duration'], p)
        width = bin_hours if bin_hours is not None else FOLD_WINDOW.bin_hours(window)
        time_range = None if t_min is None and t_max is None else \
            (t_min if t_min is not None else float('-inf'), t_max if t_max is not None else float('inf'))
        xs, ys = fold_module.fold(columns['time'], columns['flattened_flux'], epoch, p, window, time_range)
        result = {
            'run_id': run_id, 'observation_id': observation_id, 'job_id': job['id'], 'rule': FOLD_WINDOW.id,
            'display_calculation': True, 'stored_ephemeris': stored,
            'period_days': p, 't0': epoch, 'time_scale': 'tdb', 'time_reference': 'BJD 2457000.0',
            'window_hours': window, 'bin_hours': width, 'time_range': time_range and [t_min, t_max],
            'flux_column': 'flattened_flux', 'flux_unit': '1', 'n_points': len(xs),
            'bins': fold_module.bins(xs, ys, window, width),
            # The box model belongs to the stored BLS result only.
            'box_model': {'depth': best['depth'], 'duration_hours': best['duration'] * 24} if stored else None,
        }
        if points:
            result['points'] = {'x_hours': xs, 'flux': ys}
        return result

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

    def artifact_path(row):
        root = config.artifact_root.resolve()
        target = (root / row['path']).resolve()
        if not target.is_relative_to(root) or target == root:
            raise HTTPException(400, 'artifact path is outside the artifact root')
        return target

    def read_artifact_json(row):
        target = artifact_path(row)
        if not target.is_file():
            raise HTTPException(404, 'artifact file is missing')
        data = target.read_bytes()
        return json.loads(gzip.decompress(data) if row['content_encoding'] == 'gzip' else data)

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
        target = artifact_path(row)
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


def _json_number(expression: str) -> str:
    """A JSON value as double precision, or null when it is not a number."""
    return f"CASE WHEN jsonb_typeof({expression}) = 'number' THEN ({expression})::text::float8 END"


# Sky position (ICRS, from the SPOC header keywords RA_OBJ and DEC_OBJ) and TESS magnitude.
SKY_COLUMNS = ', '.join(f"{_json_number(f'header -> {key!r}')} AS {name}"
                        for key, name in (('RA_OBJ', 'ra_deg'), ('DEC_OBJ', 'dec_deg'), ('TESSMAG', 'tmag')))

TARGET_UNITS = {'cadence_seconds': 's', 'ra_deg': 'deg', 'dec_deg': 'deg', 'tmag': 'mag', 'reference_period_days': 'd', 'period_days': 'd',
                'depth': '1', 'depth_snr': '1', 'relative_period_error': '1'}
RUN_ATTRIBUTES = {'job_state', 'outcome', 'period_days', 'depth', 'depth_snr', 'relative_period_error'}
TARGET_ATTRIBUTES = ('observation_id', 'dataset_id', 'target_id', 'sector', 'cadence_seconds', 'ra_deg', 'dec_deg', 'tmag', 'label_class',
                     'planet_name', 'reference_period_days', 'job_state', 'outcome', 'period_days', 'depth',
                     'depth_snr', 'relative_period_error')


def _run_summaries(conn, ids):
    """Run summaries in the order of ids, with input counts and job states."""
    if not ids:
        return []
    rows = conn.execute("""
        SELECT r.id, r.method_id, r.method_version, r.label, r.requested_by, r.created_at,
               r.parameters_sha256,
               (SELECT count(*) FROM run_input i WHERE i.run_id = r.id) AS input_count,
               COALESCE((SELECT jsonb_object_agg(state, n) FROM
                   (SELECT state, count(*) AS n FROM job j WHERE j.run_id = r.id GROUP BY state) s),
                   '{}'::jsonb) AS job_states
        FROM run r WHERE r.id = ANY(%s)""", [ids]).fetchall()
    by_id = {r['id']: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


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
