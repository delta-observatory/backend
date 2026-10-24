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

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.openapi.utils import get_openapi
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import __version__, auth, db, fold as fold_module, query as query_module, records, settings as settings_module
from .auth import Actor
from .gui import GuiReadOnly
from .contracts import Contracts, ValidationFailure, parameters_sha256
from .rules import BLS_GRID, CANDIDATE_STATUS, DEFAULT_RUN, FOLD_WINDOW, FREE_TEXT, PERIOD_AGREEMENT, RUN_COMPARISON
from .cohorts import CohortRegister
from . import exports as exports_module, policy as policy_module
from . import catalogue, signals, investigations, acquisition, agent_sessions
from .policy import Guard, Policy, policy_for, redact_error, redact_observation, redact_target

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


class RetryRequest(BaseModel):
    label: str | None = Field(default=None, max_length=200)


def create_app(config: settings_module.Settings | None = None) -> FastAPI:
    config = config or settings_module.load()
    contracts = Contracts(config.contracts_dir)
    pool = db.make_pool(config.database_url)
    guard = Guard(CohortRegister(config.cohorts_path, standalone_exploration=config.standalone_exploration))

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
    app.add_middleware(GuiReadOnly, enabled=bool(config.gui_read_client))
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

    def authenticate(request: Request, conn: Conn, authorization: str | None = Header(None, description='Bearer token of the client.')) -> Actor:
        """Authenticate an API token or the configured anonymous GUI reader."""
        if request.scope.get('gui_read') and authorization is None and config.gui_read_client:
            reader = auth.gui_reader(conn, config.gui_read_client)
            if reader is None:
                raise HTTPException(503, 'Anonymous GUI browsing is unavailable.')
            return reader
        token = authorization[7:].strip() if authorization and authorization[:7].lower() == 'bearer ' else None
        actor = auth.lookup(conn, token) if token else None
        if actor is None:
            raise HTTPException(401, 'a valid bearer token is required', headers={'WWW-Authenticate': 'Bearer'})
        return actor

    def need(permission: str):
        def check(actor: Annotated[Actor, Depends(authenticate)]) -> Actor:
            if not actor.has(permission):
                raise HTTPException(403, f'client {actor.name} lacks the permission "{permission}"')
            return actor
        return check

    Reader = Annotated[Actor, Depends(need('read'))]
    records_api = records.router(connection=connection, need=need, guard=guard, contracts=contracts)
    Submitter = Annotated[Actor, Depends(need('run.submit'))]
    Admin = Annotated[Actor, Depends(need('admin'))]
    RecordWriter = Annotated[Actor, Depends(need('record.write'))]
    Revealer = Annotated[Actor, Depends(need('identity.reveal'))]

    class ClientRequest(BaseModel):
        name: str = Field(pattern=r'^[a-z][a-z0-9-]{1,62}$')
        kind: str = Field(pattern=r'^(person|agent|service)$')
        permissions: list[str]
        note: str | None = Field(default=None, max_length=500)
        campaign_id: uuid.UUID | None = Field(default=None, description='The campaign of an agent. Every agent needs one.')

    class CampaignRequest(BaseModel):
        name: str = Field(pattern=r'^[a-z][a-z0-9-]{1,62}$')
        title: str = Field(min_length=1, max_length=200)
        question: str = Field(min_length=1, max_length=4000)
        blinding: str = Field(default='labels_identity', pattern=r'^(open|labels|labels_identity)$',
                              description='open, labels, or labels_identity. The default withholds labels and identity.')
        budgets: dict = Field(default_factory=dict, description='Limits by key: ' + ', '.join(policy_module.BUDGET_KEYS))

    class ApproveRequest(BaseModel):
        campaign_file_sha256: str = Field(pattern=r'^[0-9a-f]{64}$',
                                          description='SHA-256 of the campaign file that Richard approved.')

    class BudgetsRequest(BaseModel):
        budgets: dict

    class RevealRequest(BaseModel):
        reason: str = Field(min_length=1, max_length=2000)

    # Clients ------------------------------------------------------------

    @app.get('/api/me')
    def me(request: Request, actor: Annotated[Actor, Depends(authenticate)]):
        """The current client, its permissions, and whether the request uses anonymous GUI access."""
        return {**actor.describe(), 'anonymous': bool(request.scope.get('gui_read') and 'authorization' not in request.headers)}

    @app.get('/api/clients')
    def list_clients(conn: Conn, _: Admin):
        return conn.execute("""SELECT id, name, kind, permissions, campaign_id, note, created_by, created_at, disabled_at
                               FROM client ORDER BY created_at""").fetchall()

    @app.post('/api/clients', status_code=201)
    def create_client(request: ClientRequest, conn: Conn, actor: Admin):
        """Create a client. The response holds the token once. The API keeps only its SHA-256."""
        errors = auth.check_permissions(request.kind, request.permissions)
        if errors:
            raise ValidationFailure([{'loc': ['body', 'permissions'], 'msg': m} for m in errors])
        if request.kind == 'agent' and request.campaign_id is None:
            raise ValidationFailure([{'loc': ['body', 'campaign_id'], 'msg': 'an agent needs a campaign'}])
        if request.campaign_id is not None:
            campaign = conn.execute('SELECT closed_at FROM campaign WHERE id = %s', [request.campaign_id]).fetchone()
            if campaign is None:
                raise ValidationFailure([{'loc': ['body', 'campaign_id'], 'msg': 'campaign not found'}])
            if campaign['closed_at'] is not None:
                raise ValidationFailure([{'loc': ['body', 'campaign_id'], 'msg': 'the campaign is closed'}])
        if conn.execute('SELECT 1 FROM client WHERE name = %s', [request.name]).fetchone():
            raise HTTPException(409, f'a client named {request.name} exists')
        row, token = auth.create_client(conn, name=request.name, kind=request.kind, permissions=request.permissions,
                                        created_by=f'client:{actor.name}', note=request.note,
                                        campaign_id=request.campaign_id)
        return {**row, 'token': token}

    @app.post('/api/clients/{client_id}/disable')
    def disable_client(client_id: uuid.UUID, conn: Conn, actor: Admin):
        if client_id == actor.id:
            raise HTTPException(409, 'a client cannot disable itself')
        row = conn.execute("""UPDATE client SET disabled_at = coalesce(disabled_at, now()) WHERE id = %s
                               RETURNING id, name, disabled_at""", [client_id]).fetchone()
        if row is None:
            raise HTTPException(404, 'client not found')
        return row

    # Campaigns ----------------------------------------------------------

    def campaign_view(conn, row) -> dict:
        """A campaign with its budget state and its record counts. Waiting jobs count at the median seconds per job
        of their method."""
        rate = conn.execute("""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY (j.result -> 'resources' ->> 'elapsed_seconds')::float8) AS s
            FROM job j
            WHERE j.state = 'complete' AND jsonb_typeof(j.result -> 'resources' -> 'elapsed_seconds') = 'number'
              AND (j.document -> 'method' ->> 'id') IN (
                  SELECT r.method_id FROM run r WHERE r.campaign_id = %s)""", [row['id']]).fetchone()
        clients = conn.execute('SELECT id, name, kind, disabled_at FROM client WHERE campaign_id = %s ORDER BY name',
                               [row['id']]).fetchall()
        counts = conn.execute("""
            SELECT (SELECT count(*) FROM candidate WHERE campaign_id = %(c)s) AS candidates,
                   (SELECT count(*) FROM assessment a JOIN candidate c ON c.id = a.candidate_id
                     WHERE c.campaign_id = %(c)s) AS assessments,
                   (SELECT count(*) FROM decision d JOIN candidate c ON c.id = d.candidate_id
                     WHERE c.campaign_id = %(c)s) AS decisions,
                   (SELECT count(*) FROM note WHERE campaign_id = %(c)s) AS notes,
                   (SELECT coalesce(jsonb_object_agg(state, n), '{}'::jsonb) FROM
                       (SELECT state, count(*) AS n FROM agent_task WHERE campaign_id = %(c)s GROUP BY state) t) AS tasks
            """, {'c': row['id']}).fetchone()
        return {**row, 'budget_state': policy_module.budget_state(conn, row, rate and rate['s']), 'clients': clients,
                'records': counts}

    def campaign_row(conn, campaign_id, actor: Actor) -> dict:
        row = conn.execute('SELECT * FROM campaign WHERE id = %s', [campaign_id]).fetchone()
        # An agent sees only its own campaign. Another campaign is "not found" to it.
        if row is None or (actor.kind == 'agent' and actor.campaign_id != row['id']):
            raise HTTPException(404, 'campaign not found')
        return row

    def campaign_update(conn, campaign_id, sql: str, args: list, when: str, conflict: str) -> dict:
        """Update the campaign only in the state that `when` names. Otherwise answer 409."""
        row = conn.execute(f'UPDATE campaign SET {sql} WHERE id = %s AND closed_at IS NULL AND {when} RETURNING *',
                           [*args, campaign_id]).fetchone()
        if row is None:
            raise HTTPException(409, conflict)
        return row

    @app.get('/api/campaigns')
    def list_campaigns(conn: Conn, actor: Reader):
        """Campaigns, newest first. An agent sees only its own campaign."""
        if actor.kind == 'agent':
            rows = conn.execute('SELECT * FROM campaign WHERE id = %s', [actor.campaign_id]).fetchall()
        else:
            rows = conn.execute('SELECT * FROM campaign ORDER BY created_at DESC').fetchall()
        return {'items': [campaign_view(conn, r) for r in rows]}

    @app.get('/api/campaigns/{campaign_id}')
    def get_campaign(campaign_id: uuid.UUID, conn: Conn, actor: Reader):
        return campaign_view(conn, campaign_row(conn, campaign_id, actor))

    @app.post('/api/campaigns', status_code=201)
    def create_campaign(request: CampaignRequest, conn: Conn, actor: Admin):
        """Create a campaign. Agents of the campaign read observation data only after the approval."""
        errors = policy_module.check_budgets(request.budgets)
        if errors:
            raise ValidationFailure([{'loc': ['body', 'budgets'], 'msg': m} for m in errors])
        if conn.execute('SELECT 1 FROM campaign WHERE name = %s', [request.name]).fetchone():
            raise HTTPException(409, f'a campaign named {request.name} exists')
        row = conn.execute("""
            INSERT INTO campaign (name, title, question, blinding, budgets, created_by)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                           [request.name, request.title, request.question, request.blinding, Jsonb(request.budgets),
                            actor.id]).fetchone()
        return campaign_view(conn, row)

    @app.post('/api/campaigns/{campaign_id}/approve')
    def approve_campaign(campaign_id: uuid.UUID, request: ApproveRequest, conn: Conn, actor: Admin):
        """Record the approval of the campaign file. The approval starts the campaign."""
        campaign_row(conn, campaign_id, actor)
        row = campaign_update(conn, campaign_id,
                              'approved_by = %s, approved_at = now(), campaign_file_sha256 = %s',
                              [actor.id, request.campaign_file_sha256], 'approved_at IS NULL',
                              'the campaign is approved or closed')
        return campaign_view(conn, row)

    @app.post('/api/campaigns/{campaign_id}/budgets')
    def set_campaign_budgets(campaign_id: uuid.UUID, request: BudgetsRequest, conn: Conn, actor: Admin):
        """Replace the budgets of a campaign that is not closed."""
        errors = policy_module.check_budgets(request.budgets)
        if errors:
            raise ValidationFailure([{'loc': ['body', 'budgets'], 'msg': m} for m in errors])
        campaign_row(conn, campaign_id, actor)
        row = campaign_update(conn, campaign_id, 'budgets = %s', [Jsonb(request.budgets)], 'true',
                              'the campaign is closed')
        return campaign_view(conn, row)

    @app.post('/api/campaigns/{campaign_id}/fix-decisions')
    def fix_campaign_decisions(campaign_id: uuid.UUID, conn: Conn, actor: RecordWriter):
        """Fix the decisions of the campaign. After this, its agents submit no new runs, and the reveal can follow."""
        campaign_row(conn, campaign_id, actor)
        if actor.kind == 'agent':
            policy_for(conn, actor).require_active('fixing decisions')
        elif not actor.has('admin'):
            raise HTTPException(403, f'client {actor.name} is not an agent of the campaign and lacks "admin"')
        row = campaign_update(conn, campaign_id, 'decisions_fixed_by = %s, decisions_fixed_at = now()', [actor.id],
                              'approved_at IS NOT NULL AND decisions_fixed_at IS NULL',
                              'the campaign is not approved, is closed, or has fixed decisions')
        return campaign_view(conn, row)

    @app.post('/api/campaigns/{campaign_id}/reveal')
    def reveal_campaign(campaign_id: uuid.UUID, request: RevealRequest, conn: Conn, actor: Revealer):
        """Record the reveal. Blinding then ends for the agents of the campaign."""
        campaign_row(conn, campaign_id, actor)
        row = campaign_update(conn, campaign_id, 'revealed_by = %s, revealed_at = now(), reveal_reason = %s',
                              [actor.id, request.reason], 'decisions_fixed_at IS NOT NULL AND revealed_at IS NULL',
                              'the campaign has no fixed decisions, is closed, or is revealed')
        return campaign_view(conn, row)

    @app.post('/api/campaigns/{campaign_id}/close')
    def close_campaign(campaign_id: uuid.UUID, conn: Conn, actor: Admin):
        """Close the campaign. Its agents then read no observation data and write nothing."""
        campaign_row(conn, campaign_id, actor)
        row = campaign_update(conn, campaign_id, 'closed_by = %s, closed_at = now()', [actor.id], 'true',
                              'the campaign is closed')
        return campaign_view(conn, row)

    @app.get('/api/access-events')
    def list_access_events(conn: Conn, _: Admin,
                           campaign_id: uuid.UUID | None = None,
                           outcome: str | None = Query(None, pattern=r'^(allowed|refused)$'),
                           limit: int = Query(200, ge=1, le=2000)):
        """Reads and refused reads of protected observations, newest first."""
        where, args = [], []
        if campaign_id is not None:
            where.append('e.campaign_id = %s')
            args.append(campaign_id)
        if outcome is not None:
            where.append('e.outcome = %s')
            args.append(outcome)
        rows = conn.execute(f"""
            SELECT e.*, c.name AS client_name FROM access_event e JOIN client c ON c.id = e.client_id
            {'WHERE ' + ' AND '.join(where) if where else ''}
            ORDER BY e.id DESC LIMIT %s""", [*args, limit]).fetchall()
        return {'items': rows}

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

    @app.get('/api/methods', dependencies=[Depends(need('read'))])
    def list_methods():
        return [m.describe() for _, m in sorted(contracts.methods.items())]

    @app.get('/api/contracts/records', dependencies=[Depends(need('read'))])
    def record_schemas():
        return {name: contracts.expanded_record(name) for name in contracts.records}

    @app.get('/api/methods/{method_id}/{version}', dependencies=[Depends(need('read'))])
    def get_method(method_id: str, version: int):
        method = contracts.method(method_id, version)
        if method is None:
            raise HTTPException(404, f'unknown method {method_id} v{version}')
        return method.describe()

    @app.get('/api/rules', dependencies=[Depends(need('read'))])
    def list_rules():
        """Named rules that clients apply to stored values: status, default run, query grammar, fold, estimate,
        candidate status, run comparison, and the free text that blinded agents receive."""
        return [PERIOD_AGREEMENT.describe(), DEFAULT_RUN.describe(), query_module.describe(),
                FOLD_WINDOW.describe(), BLS_GRID.describe(), CANDIDATE_STATUS.describe(), RUN_COMPARISON.describe(),
                FREE_TEXT.describe()]

    # Datasets -----------------------------------------------------------

    @app.get('/api/datasets')
    def list_datasets(conn: Conn, actor: Reader):
        """Datasets with state, findings, and observation counts. Without identity, an agent gets path counts, not paths."""
        pol = policy_for(conn, actor)
        pol.require_active('a dataset list')
        rows = conn.execute("""
            SELECT d.id, d.state, d.findings, d.last_indexed_at,
                   COALESCE(jsonb_object_agg(o.state, o.n) FILTER (WHERE o.state IS NOT NULL), '{}'::jsonb)
                       AS observation_counts
            FROM dataset d
            LEFT JOIN (SELECT dataset_id, state, count(*) AS n FROM observation GROUP BY 1, 2) o
                   ON o.dataset_id = d.id
            GROUP BY d.id
            ORDER BY d.id""").fetchall()
        for row in rows:
            row['findings'] = policy_module.redact_findings(row['findings'], pol)
        return rows

    @app.post('/api/datasets/{dataset_id}/index', status_code=202)
    def index_dataset(dataset_id: str, conn: Conn, actor: Admin):
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
                _insert_run(conn, run_id, method, parameters, None, actor)
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

    def excluded_for(conn, pol: Policy) -> list:
        """Observations that a list hides from an agent: the protected ones."""
        return list(guard.protected(conn, pol)) if pol.agent else []

    @app.get('/api/observations')
    def list_observations(
        conn: Conn,
        actor: Reader,
        dataset_id: str | None = None,
        target_id: str | None = None,
        state: str | None = None,
        label_class: str | None = Query(None, alias='class',
                                        description='Label class: planet_host, no_known_planet, or eclipsing_binary.'),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        pol = policy_for(conn, actor)
        pol.require_active('an observation list')
        errors = []
        if label_class is not None and not pol.labels_visible:
            errors.append({'loc': ['query', 'class'], 'msg': 'the campaign withholds labels'})
        if target_id is not None and not pol.identity_visible:
            errors.append({'loc': ['query', 'target_id'], 'msg': 'the campaign withholds identity'})
        if errors:
            raise ValidationFailure(errors)
        excluded = excluded_for(conn, pol)
        where, args = (['NOT (id = ANY(%s))'], [excluded]) if excluded else ([], [])
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
            ORDER BY dataset_id, {'target_id, sector NULLS LAST, ' if pol.identity_visible else ''}id
            LIMIT %s OFFSET %s""", [*args, limit, offset]).fetchall()
        return {'total': total, 'limit': limit, 'offset': offset, 'withheld': pol.withheld(),
                'protected_excluded': len(excluded), 'items': [redact_observation(r, pol) for r in rows]}

    @app.get('/api/targets')
    def list_targets(
        conn: Conn,
        actor: Reader,
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
        pol = policy_for(conn, actor)
        pol.require_active('a target list')
        if run_id is not None:
            visible_run(conn, pol, run_id)
        labels_visible, identity_visible = pol.labels_visible, pol.identity_visible
        parsed = query_module.parse(q)
        errors = [{'loc': ['query', 'q'], 'msg': m} for m in parsed.errors]
        if not labels_visible:
            errors += [{'loc': ['query', 'q'], 'msg': f'field "{t.field}" needs known-object labels, which the campaign withholds'}
                       for t in parsed.terms if isinstance(t, query_module.Comparison) and t.field in query_module.LABEL_FIELDS]
            if label_class is not None:
                errors.append({'loc': ['query', 'class'], 'msg': 'the campaign withholds labels'})
        # A text token matches planet names (labels) and TIC identifiers (identity). Each blinding level removes one.
        text_columns = tuple(c for c, visible in (('planet_name', labels_visible), ('target_id', identity_visible)) if visible)
        if not text_columns and any(isinstance(t, query_module.Text) for t in parsed.terms):
            errors.append({'loc': ['query', 'q'], 'msg': 'the campaign withholds labels and identity, so a text search has nothing to match'})
        hidden = (set() if labels_visible else {'label_class', 'planet_name', 'reference_period_days', 'relative_period_error'}) | \
            (set() if identity_visible else {'target_id', 'ra_deg', 'dec_deg'})
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
        elif key in hidden:
            errors.append({'loc': ['query', 'sort'], 'msg': f'the campaign withholds "{key}"'})
        if errors:
            raise ValidationFailure(errors)
        excluded = excluded_for(conn, pol)
        where, args = ["o.state = 'ready'"], []
        if excluded:
            where.append('NOT (o.id = ANY(%s))'); args.append(excluded)
        if dataset_id is not None:
            where.append('o.dataset_id = %s'); args.append(dataset_id)
        if label_class is not None:
            where.append("o.labels ->> 'class' = %s"); args.append(label_class)
        if in_run:
            where.append('EXISTS (SELECT 1 FROM run_input ri WHERE ri.run_id = %s AND ri.observation_id = o.id)')
            args.append(run_id)
        q_where, q_args = query_module.to_sql(parsed, {a: a for a in TARGET_ATTRIBUTES}, text_columns or ('target_id',))
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
            ORDER BY {key} {direction} NULLS LAST, {'target_id, sector NULLS LAST, ' if identity_visible else ''}observation_id
            LIMIT %s OFFSET %s""", [*all_args, limit, offset]).fetchall()
        for row in rows:
            row['status'] = PERIOD_AGREEMENT.status(row['label_class'], row['depth_snr'], row['relative_period_error'],
                                                    labels_visible) if run_id is not None else None
            redact_target(row, pol)
        return {'total': total, 'limit': limit, 'offset': offset, 'run_id': run_id, 'grammar': query_module.GRAMMAR,
                'status_rule': PERIOD_AGREEMENT.id, 'labels_visible': labels_visible, 'units': TARGET_UNITS,
                'run_values': run_id is not None, 'withheld': pol.withheld(), 'protected_excluded': len(excluded),
                'note': None if run_id is not None else 'No run_id: search values, job states, and status are null. Name a run to get them.',
                'items': rows}

    @app.get('/api/observations/{observation_id}')
    def get_observation(observation_id: uuid.UUID, conn: Conn, actor: Reader):
        row = conn.execute(f'SELECT {OBSERVATION_COLUMNS}, header FROM observation WHERE id = %s',
                           [observation_id]).fetchone()
        if row is None:
            raise HTTPException(404, 'observation not found')
        pol = policy_for(conn, actor)
        guard.read(conn, pol, [observation_id], 'observation detail')
        redact_observation(row, pol)
        artifacts = conn.execute("""
            SELECT * FROM artifact WHERE observation_id = %s AND kind = 'light_curve'
            ORDER BY created_at DESC""", [observation_id]).fetchall()
        row['artifacts'] = [db.artifact(a) for a in artifacts]
        return row

    # Runs ---------------------------------------------------------------

    def visible_run(conn, pol: Policy, run_id) -> dict:
        """The run, or 404. An agent sees the runs of its campaign and the runs without a campaign."""
        row = conn.execute("""SELECT r.*, w.campaign_id AS writer_campaign_id FROM run r
                              LEFT JOIN client w ON w.id = r.client_id WHERE r.id = %s""", [run_id]).fetchone()
        if row is None or not pol.run_visible(row):
            raise HTTPException(404, 'run not found')
        return row

    def refuse_protected(conn, pol: Policy, ids: list, operation: str):
        """An agent's run request with a protected observation gives 422. Each refusal is recorded."""
        protected = guard.protected(conn, pol, ids)
        if protected:
            guard.refuse(conn, pol, protected, operation)
            raise ValidationFailure([{'loc': ['observation_ids', ids.index(str(o))],
                                      'msg': f'observation is reserved for evaluation ({r}). The refusal is recorded.'}
                                     for o, r in protected.items()])

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
        check_inputs(conn, ids)
        return method, parameters, ids

    def check_inputs(conn, ids: list[str]):
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

    def median_rate(conn, method) -> dict:
        """Median worker seconds per trial period and per job over the newest 200 complete jobs of the method."""
        return conn.execute("""
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds / NULLIF(trials, 0)) AS per_trial,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) AS per_job, count(*) AS jobs
            FROM (SELECT (result -> 'resources' ->> 'elapsed_seconds')::float8 AS seconds,
                         CASE WHEN jsonb_typeof(result -> 'diagnostics' -> 'grid_trials') = 'number'
                              THEN (result -> 'diagnostics' ->> 'grid_trials')::float8 END AS trials
                  FROM job
                  WHERE state = 'complete' AND document -> 'method' ->> 'id' = %s
                    AND (document -> 'method' ->> 'version')::int = %s
                    AND jsonb_typeof(result -> 'resources' -> 'elapsed_seconds') = 'number'
                  ORDER BY finished_at DESC NULLS LAST LIMIT 200) s""", [method.id, method.version]).fetchone()

    def workload(conn, method, parameters, ids) -> tuple[dict | None, str | None]:
        """The work estimate of a run request (rule bls-grid/1), or the reason that none exists."""
        if method.id != 'tess-bls':
            return None, f'no workload estimate for {method.id}'
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
        rate = median_rate(conn, method)
        per_trial = rate['per_trial'] if rate and rate['jobs'] else None
        return {
            'rule': BLS_GRID.id,
            'trials': {'min': min(known), 'max': max(known), 'total': sum(known)} if known else None,
            'limit': limit,
            'over_limit': sum(1 for i in items if i['over_limit']),
            'seconds_per_trial': {'median': per_trial, 'jobs': rate['jobs'],
                                  'source': 'median of elapsed_seconds / diagnostics.grid_trials over the newest '
                                            '200 complete jobs of the method'} if per_trial is not None else None,
            'worker_seconds': per_trial * sum(known) if per_trial is not None and known else None,
            'items': items,
        }, None

    def run_budget(conn, pol: Policy, method, n_jobs: int, worker_seconds: float | None) -> tuple[dict, list[str]]:
        """The campaign budgets after a run of n_jobs, and the budgets that it would exceed."""
        rate = median_rate(conn, method)
        state = policy_module.budget_state(conn, pol.campaign, rate['per_job'] if rate and rate['jobs'] else None)
        exceeded = []
        jobs, compute, storage = state['jobs'], state['compute_seconds'], state['storage_bytes']
        if jobs['limit'] is not None and jobs['used'] + n_jobs > jobs['limit']:
            exceeded.append(f"jobs: {jobs['used']} used + {n_jobs} new > {jobs['limit']}")
        if compute['limit'] is not None and compute['used'] + (worker_seconds or 0) > compute['limit']:
            exceeded.append(f"compute_seconds: {compute['used']:.0f} used and waiting + {worker_seconds or 0:.0f} estimated > {compute['limit']}")
        if storage['limit'] is not None and storage['used'] >= storage['limit']:
            exceeded.append(f"storage_bytes: {storage['used']} used ≥ {storage['limit']}")
        return state, exceeded

    @app.post('/api/runs/estimate')
    def estimate_run(request: RunRequest, conn: Conn, actor: Reader):
        """Check a run request and estimate its work without a write. A failed check gives the same 422 as POST /runs.
        For an agent, the response also gives the campaign budgets after the run."""
        pol = policy_for(conn, actor)
        pol.require_active('a run estimate')
        method, parameters, ids = prepare_run(request, conn)
        if pol.agent:
            refuse_protected(conn, pol, ids, 'run estimate')
        estimate, reason = workload(conn, method, parameters, ids)
        response = {'method': {'id': method.id, 'version': method.version}, 'parameters': parameters,
                    'observation_count': len(ids), 'estimate': estimate, 'estimate_reason': reason}
        if pol.agent:
            state, exceeded = run_budget(conn, pol, method, len(ids), estimate and estimate['worker_seconds'])
            response['budget'] = {'campaign': pol.campaign['name'], 'state': state, 'exceeded': exceeded}
        return response

    def submit_run(conn, actor: Actor, pol: Policy, method, parameters: dict, ids: list[str], label: str | None,
                   retry_of=None) -> dict:
        """Write a run and one queued job for each observation. Agents pass the campaign checks first.
        The budget check and the insert hold the campaign lock, so two parallel submissions cannot both pass a budget."""
        if pol.agent:
            pol.require_active('a run submission')
            # A refusal is recorded outside the insert transaction, so that it stays.
            refuse_protected(conn, pol, ids, 'run submission')
        run_id = str(uuid.uuid4())
        jobs = []
        for o in ids:
            job_id = str(uuid.uuid4())
            jobs.append((job_id, o, contracts.job_document(
                job_id=job_id, run_id=run_id, method=method, parameters=parameters, observation_ids=[o])))
        try:
            with conn.transaction():
                campaign_id = pol.campaign['id'] if pol.campaign is not None else None
                if pol.agent:
                    # The same lock as the observation budget of guard.read. Then read the campaign state again.
                    conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [str(pol.campaign['id'])])
                    current = Policy(actor, conn.execute('SELECT * FROM campaign WHERE id = %s',
                                                         [pol.campaign['id']]).fetchone())
                    current.require_active('a run submission')
                    if current.campaign['decisions_fixed_at'] is not None:
                        raise HTTPException(403, f'campaign {current.campaign["name"]} fixed its decisions. '
                                                 'It submits no new runs.')
                    estimate, _ = workload(conn, method, parameters, ids)
                    _, exceeded = run_budget(conn, current, method, len(ids), estimate and estimate['worker_seconds'])
                    if exceeded:
                        raise HTTPException(403, f'the run exceeds the budget of campaign {current.campaign["name"]}: '
                                            + '; '.join(exceeded))
                    campaign_id = current.campaign['id']
                _insert_run(conn, run_id, method, parameters, label, actor, campaign_id, retry_of)
                with conn.cursor() as cur:
                    cur.executemany('INSERT INTO run_input (run_id, observation_id) VALUES (%s, %s)',
                                    [(run_id, o) for o in ids])
                for job_id, o, document in jobs:
                    _insert_job(conn, job_id, run_id, o, document)
        except pg_errors.Error as error:
            if error.sqlstate == 'RC003':
                raise HTTPException(409, error.diag.message_primary) from error
            raise
        return _run_detail(conn, run_id, pol, excluded_for(conn, pol))

    @app.post('/api/runs', status_code=201)
    def create_run(request: RunRequest, conn: Conn, actor: Submitter):
        pol = policy_for(conn, actor)
        if pol.agent and pol.investigation:
            raise HTTPException(403, 'Execute the approved investigation revision through its execution endpoint.')
        pol.require_active('a run submission')
        method, parameters, ids = prepare_run(request, conn)
        return submit_run(conn, actor, pol, method, parameters, ids, request.label)

    @app.get('/api/runs')
    def list_runs(conn: Conn, actor: Reader, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                  observation_id: uuid.UUID | None = Query(None, description='Only the runs with a job for this observation.'),
                  dataset_id: str | None = Query(None, pattern=DATASET_PATTERN, description='Only runs with visible input observations from this dataset.')):
        """Runs, newest first, with job counts by state. An agent sees the runs of its campaign and the runs without
        a campaign, without the jobs of protected observations. For an agent, a protected observation_id gives no runs."""
        pol = policy_for(conn, actor)
        pol.require_active('a run list')
        scope, args = pol.run_scope('r')
        excluded = excluded_for(conn, pol)
        if dataset_id is not None:
            scope += ''' AND EXISTS (SELECT 1 FROM run_input ri JOIN observation o ON o.id = ri.observation_id
                                    WHERE ri.run_id = r.id AND o.dataset_id = %s
                                      AND NOT (o.id = ANY(%s::uuid[])))'''
            args = [*args, dataset_id, list(excluded)]
        if observation_id is not None:
            if observation_id in excluded:
                return []
            scope += ' AND EXISTS (SELECT 1 FROM job oj WHERE oj.run_id = r.id AND oj.observation_id = %s)'
            args = [*args, observation_id]
        ids = [r['id'] for r in conn.execute(f'SELECT id FROM run r WHERE {scope} ORDER BY created_at DESC, id LIMIT %s OFFSET %s',
                                             [*args, limit, offset]).fetchall()]
        return _run_summaries(conn, ids, pol, excluded)

    @app.get('/api/runs/default')
    def default_run(conn: Conn, actor: Reader, dataset_id: str | None = None, method_id: str | None = None,
                    method_version: int | None = None):
        """The run that a client shows when it names none (rule default-run/1), among the runs that the client sees."""
        pol = policy_for(conn, actor)
        pol.require_active('the default run')
        analysis = [key for key, m in contracts.methods.items() if m.kind == 'analysis'
                    and (method_id is None or key[0] == method_id) and (method_version is None or key[1] == method_version)]
        if not analysis:
            raise HTTPException(404, 'no analysis method matches')
        scope, scope_args = pol.run_scope('r')
        excluded = excluded_for(conn, pol)
        where, args = ['(r.method_id, r.method_version) IN (SELECT * FROM unnest(%s::text[], %s::int[]))', scope], \
            [[k[0] for k in analysis], [k[1] for k in analysis], *scope_args]
        if dataset_id is not None:
            where.append("""EXISTS (SELECT 1 FROM run_input ri JOIN observation o ON o.id = ri.observation_id
                                   WHERE ri.run_id = r.id AND o.dataset_id = %s)""")
            args.append(dataset_id)
        candidates = conn.execute(f"""
            SELECT r.id, r.created_at,
                   (SELECT count(*) FROM job j WHERE j.run_id = r.id AND j.state = 'complete'
                      AND NOT (j.observation_id = ANY(%s::uuid[]))) AS complete
            FROM run r WHERE {' AND '.join(where)}""", [excluded, *args]).fetchall()
        chosen = DEFAULT_RUN.choose(candidates)
        if chosen is None:
            raise HTTPException(404, 'no analysis run has a complete job')
        return {'rule': DEFAULT_RUN.id, 'run': _run_summaries(conn, [chosen['id']], pol, excluded)[0]}

    @app.get('/api/runs/compare')
    def compare_runs(conn: Conn, actor: Reader, run_a: uuid.UUID, run_b: uuid.UUID,
                     observation_id: uuid.UUID | None = Query(None, description='Only the row of this observation.'),
                     limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        """Stored measurements of two runs of one method side by side (rule run-comparison/1). The differences are
        display calculations, not stored results. Each listed observation counts as a read for an agent's budget."""
        pol = policy_for(conn, actor)
        pol.require_active('a run comparison')
        a, b = visible_run(conn, pol, run_a), visible_run(conn, pol, run_b)
        if a['method_id'] != b['method_id']:
            raise ValidationFailure([{'loc': ['query', 'run_b'],
                                      'msg': f'run_b uses {b["method_id"]}, run_a uses {a["method_id"]}. '
                                             'A comparison needs one method.'}])
        excluded = excluded_for(conn, pol)
        inputs = conn.execute("""
            SELECT DISTINCT observation_id FROM run_input
            WHERE run_id = ANY(%s) AND NOT (observation_id = ANY(%s::uuid[]))
            ORDER BY observation_id""", [[run_a, run_b], excluded]).fetchall()
        all_ids = [r['observation_id'] for r in inputs]
        if observation_id is not None:
            all_ids = [o for o in all_ids if o == observation_id]
        jobs = conn.execute("""SELECT id, run_id, observation_id, state, result FROM job
                               WHERE run_id = ANY(%s) AND observation_id = ANY(%s)""",
                            [[run_a, run_b], all_ids]).fetchall()
        side = {(j['run_id'], j['observation_id']): j for j in jobs}
        summary = {'observations': len(all_ids), 'in_both': 0, 'only_a': 0, 'only_b': 0, 'state_differs': 0}
        for o in all_ids:
            ja, jb = side.get((run_a, o)), side.get((run_b, o))
            if ja and jb:
                summary['in_both'] += 1
                summary['state_differs'] += ja['state'] != jb['state']
            elif ja:
                summary['only_a'] += 1
            elif jb:
                summary['only_b'] += 1
        page = all_ids[offset:offset + limit]
        guard.read(conn, pol, page, 'run comparison')
        page_jobs = [j['id'] for j in jobs if j['observation_id'] in set(page)]
        measured: dict = {}
        for m in conn.execute('SELECT * FROM measurement WHERE job_id = ANY(%s) ORDER BY id', [page_jobs]).fetchall():
            if m['role'] == 'reference_comparison' and not pol.labels_visible:
                continue
            measured.setdefault(m['job_id'], {}).setdefault(m['role'], {})[m['name']] = db.measurement(m)
        targets = {r['id']: r for r in conn.execute(
            'SELECT id, target_id, sector FROM observation WHERE id = ANY(%s)', [page]).fetchall()}

        def side_of(job):
            if job is None:
                return None
            result = job['result'] or {}
            return {'job_id': job['id'], 'state': job['state'], 'outcome': result.get('outcome'),
                    'error': redact_error(result.get('error'), pol), 'measurements': measured.get(job['id'], {})}

        rows = []
        for o in page:
            sa, sb = side_of(side.get((run_a, o))), side_of(side.get((run_b, o)))
            differences = {}
            for role in ('search', 'reference_comparison'):
                names = set((sa or {}).get('measurements', {}).get(role, {})) | set((sb or {}).get('measurements', {}).get(role, {}))
                for name in sorted(names):
                    d = RUN_COMPARISON.difference((sa or {}).get('measurements', {}).get(role, {}).get(name),
                                                  (sb or {}).get('measurements', {}).get(role, {}).get(name))
                    if d is not None:
                        differences.setdefault(role, {})[name] = d
            row = {'observation_id': o, 'target_id': targets[o]['target_id'] if pol.identity_visible else None,
                   'sector': targets[o]['sector'], 'a': sa, 'b': sb, 'differences': differences}
            rows.append(row)
        differences = parameter_differences(a['parameters'], b['parameters'])
        summaries = _run_summaries(conn, [run_a, run_b], pol, excluded)
        protected_excluded = conn.execute("""SELECT count(DISTINCT observation_id) AS n FROM run_input
                                              WHERE run_id = ANY(%s) AND observation_id = ANY(%s::uuid[])""",
                                          [[run_a, run_b], excluded]).fetchone()['n'] if excluded else 0
        return {'rule': RUN_COMPARISON.id, 'display_calculation': True, 'run_a': summaries[0], 'run_b': summaries[-1],
                'parameters_equal': not differences, 'parameter_differences': differences,
                'summary': summary, 'total': len(all_ids), 'limit': limit, 'offset': offset,
                'withheld': pol.withheld(), 'protected_excluded': protected_excluded, 'rows': rows}

    @app.get('/api/runs/{run_id}')
    def get_run(run_id: uuid.UUID, conn: Conn, actor: Reader):
        pol = policy_for(conn, actor)
        pol.require_active('a run')
        return _run_detail(conn, run_id, pol, excluded_for(conn, pol))

    def run_results(conn, pol: Policy, run_id, observation_id=None) -> dict:
        """Results of the run's jobs. For an agent, protected observations are left out, and each other observation
        counts toward the observation budget."""
        pol.require_active('run results')
        visible_run(conn, pol, run_id)
        only = 'AND j.observation_id = %s' if observation_id else ''
        extra = [observation_id] if observation_id else []
        jobs = conn.execute(f"""
            SELECT id, observation_id, state, result FROM job j WHERE run_id = %s {only}
            ORDER BY created_at, id""", [run_id, *extra]).fetchall()
        # An index job has no observation, but its light-curve artifacts each have one. The guard checks both.
        artifacts = conn.execute('SELECT a.* FROM artifact a WHERE a.job_id = ANY(%s) ORDER BY a.created_at, a.id',
                                 [[j['id'] for j in jobs]]).fetchall()
        excluded = 0
        if pol.agent and observation_id is None:
            protected = guard.protected(conn, pol, [o for o in {j['observation_id'] for j in jobs} |
                                                    {a['observation_id'] for a in artifacts} if o])
            excluded = len(protected)
            jobs = [j for j in jobs if j['observation_id'] not in protected]
            artifacts = [a for a in artifacts if a['observation_id'] not in protected]
        guard.read(conn, pol, [j['observation_id'] for j in jobs] + [a['observation_id'] for a in artifacts], 'run results')
        job_ids = [j['id'] for j in jobs]
        measurements = conn.execute('SELECT m.* FROM measurement m WHERE m.job_id = ANY(%s) ORDER BY m.id',
                                    [job_ids]).fetchall()
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
                'error': redact_error(result.get('error'), pol),
                'measurements': policy_module.redact_measurements(grouped, pol),
                'artifacts': [db.artifact(a) for a in artifacts if a['job_id'] == job['id']],
                'diagnostics': policy_module.redact_diagnostics(result.get('diagnostics'), pol),
                'resources': result.get('resources'),
                'software': result.get('software'),
            })
        return {'run_id': run_id, 'withheld': pol.withheld(), 'protected_excluded': excluded, 'jobs': items}

    @app.get('/api/runs/{run_id}/results')
    def get_run_results(run_id: uuid.UUID, conn: Conn, actor: Reader,
                        observation_id: uuid.UUID | None = Query(None, description='Only the job of this observation.')):
        """Results of the run's jobs. For an agent, protected observations are left out, and each other observation
        counts toward the observation budget."""
        return run_results(conn, policy_for(conn, actor), run_id, observation_id)

    @app.get('/api/runs/{run_id}/observations/{observation_id}/fold')
    def get_fold(
        run_id: uuid.UUID, observation_id: uuid.UUID, conn: Conn, actor: Reader,
        period: float | None = Query(None, gt=0, description='Fold period in days. Default: the stored BLS period.'),
        t0: float | None = Query(None, description='Event time, BTJD (TDB). Default: the stored BLS transit time.'),
        window_hours: float | None = Query(None, gt=0, description='Half width of the window. Default: rule fold-window/1.'),
        bin_hours: float | None = Query(None, gt=0, description='Bin width. Default: rule fold-window/1.'),
        t_min: float | None = Query(None, description='Use only times at or after this BTJD.'),
        t_max: float | None = Query(None, description='Use only times at or before this BTJD.'),
        points: bool = Query(False, description='Include the folded points.'),
    ):
        """Fold the stored flattened flux and bin it. A display calculation, not a stored result."""
        pol = policy_for(conn, actor)
        pol.require_active('a fold')
        visible_run(conn, pol, run_id)
        job = conn.execute('SELECT id, state FROM job WHERE run_id = %s AND observation_id = %s',
                           [run_id, observation_id]).fetchone()
        if job is None:
            raise HTTPException(404, 'the run has no job for this observation')
        guard.read(conn, pol, [observation_id], 'fold')
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

    def owned_run(conn, actor: Actor, pol: Policy, run_id) -> dict:
        """A run that the client can change: its own run, or any visible run with "admin"."""
        pol.require_active('a run change')
        run = visible_run(conn, pol, run_id)
        if run['client_id'] != actor.id and not actor.has('admin'):
            raise HTTPException(403, f'client {actor.name} can change only its own runs')
        return run

    @app.post('/api/runs/{run_id}/cancel')
    def cancel_run(run_id: uuid.UUID, conn: Conn, actor: Submitter):
        """Cancel every queued and running job of the run. A client cancels only its own runs, unless it has "admin".
        A running job stops at the worker's next lease extension. Its output is discarded. A running index job is not
        cancelled: an index job stops only at its end."""
        pol = policy_for(conn, actor)
        run = owned_run(conn, actor, pol, run_id)
        index = contracts.method(run['method_id'], run['method_version'])
        running_allowed = not (index and index.kind == 'index')
        with conn.transaction():
            rows = conn.execute("""
                WITH old AS (SELECT id, state FROM job WHERE run_id = %s AND state IN ('queued', 'running') FOR UPDATE)
                UPDATE job j SET state = 'cancelled', finished_at = now(), cancelled_by = %s, lease_expires_at = NULL
                FROM old WHERE j.id = old.id AND (old.state = 'queued' OR %s)
                RETURNING j.id, old.state AS previous_state""", [run_id, actor.id, running_allowed]).fetchall()
        summary = _run_summaries(conn, [run_id], pol, excluded_for(conn, pol))[0]
        return {'run_id': run_id, 'cancelled': len(rows), 'was_running': sum(r['previous_state'] == 'running' for r in rows),
                'job_states': summary['job_states'],
                'note': None if running_allowed else 'A running index job is not cancelled. It stops only at its end.'}

    @app.post('/api/runs/{run_id}/retry', status_code=201)
    def retry_run(run_id: uuid.UUID, conn: Conn, actor: Submitter, request: RetryRequest | None = None):
        """A new run with the same method version and parameters for the observations whose job in this run is failed
        or cancelled. Complete and inconclusive jobs stay, and the retry does not repeat them. The new run names this
        run in retry_of. A method does not resume partial work: each job of the retry starts again."""
        pol = policy_for(conn, actor)
        pol.require_active('a run retry')
        if pol.agent and pol.investigation:
            raise HTTPException(403, 'Execute an approved investigation revision to repeat its analysis.')
        run = visible_run(conn, pol, run_id)
        method = contracts.method(run['method_id'], run['method_version'])
        if method is None or method.kind != 'analysis':
            raise ValidationFailure([{'loc': ['path', 'run_id'],
                                      'msg': f'run {run_id} uses {run["method_id"]} v{run["method_version"]}, not an analysis method'}])
        parameters = run['parameters']
        if contracts.resolve_parameters(method, parameters) != parameters:
            raise HTTPException(409, 'the stored parameters differ from the current schema defaults. Submit a new run.')
        if run['campaign_id'] != (pol.campaign['id'] if pol.agent else None):
            raise HTTPException(409, 'a retry stays in the campaign of its run. A person retries runs without a campaign, '
                                     'and an agent retries the runs of its campaign.')
        label = request.label if request and request.label else f'Retry of {run_id}: failed and cancelled jobs'

        def open_failures() -> list[str]:
            # A failed or cancelled job that an earlier retry already repeats, in a job that is not failed or
            # cancelled, is not repeated again.
            return [str(r['observation_id']) for r in conn.execute("""
                SELECT j.observation_id FROM job j
                WHERE j.run_id = %(run)s AND j.state IN ('failed', 'cancelled')
                  AND NOT EXISTS (SELECT 1 FROM run r JOIN job k ON k.run_id = r.id
                                  WHERE r.retry_of = %(run)s AND k.observation_id = j.observation_id
                                    AND k.state NOT IN ('failed', 'cancelled'))
                ORDER BY j.observation_id""", {'run': run_id}).fetchall()]

        ids = open_failures()
        if not ids:
            raise HTTPException(409, f'run {run_id} has no failed or cancelled job that a retry does not already repeat')
        check_inputs(conn, ids)
        if pol.agent:
            refuse_protected(conn, pol, ids, 'run submission')
        with conn.transaction():
            # One retry of a run at a time: the second retry waits, then sees the first retry's jobs.
            conn.execute('SELECT id FROM run WHERE id = %s FOR UPDATE', [run_id])
            ids = open_failures()
            if not ids:
                raise HTTPException(409, f'run {run_id} has no failed or cancelled job that a retry does not already repeat')
            return submit_run(conn, actor, pol, method, parameters, ids, label, retry_of=run_id)

    # Jobs ---------------------------------------------------------------

    def job_row(conn, pol: Policy, job_id) -> dict:
        row = conn.execute("""SELECT j.*, r.campaign_id AS run_campaign_id, r.client_id AS run_client_id
                              FROM job j JOIN run r ON r.id = j.run_id WHERE j.id = %s""", [job_id]).fetchone()
        if row is None or not pol.run_visible({'campaign_id': row['run_campaign_id']}):
            raise HTTPException(404, 'job not found')
        return row

    @app.get('/api/jobs/{job_id}')
    def get_job(job_id: uuid.UUID, conn: Conn, actor: Reader):
        pol = policy_for(conn, actor)
        pol.require_active('a job')
        row = job_row(conn, pol, job_id)
        guard.read(conn, pol, [row['observation_id']], 'job')
        row.pop('run_campaign_id')
        row.pop('run_client_id')
        row['result'] = policy_module.redact_result_document(row['result'], pol)
        if row['result'] and row['result'].get('error'):
            row['result'] = {**row['result'], 'error': redact_error(row['result']['error'], pol)}
        return row

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel_job(job_id: uuid.UUID, conn: Conn, actor: Submitter):
        """Cancel a queued or running job. A client cancels only the jobs of its own runs, unless it has "admin".
        A running job stops at the worker's next lease extension. The worker discards its output."""
        pol = policy_for(conn, actor)
        pol.require_active('a job cancellation')
        owner = job_row(conn, pol, job_id)
        if owner['run_client_id'] != actor.id and not actor.has('admin'):
            raise HTTPException(403, f'client {actor.name} can cancel only the jobs of its own runs')
        index_methods = [m.id for m in contracts.methods.values() if m.kind == 'index']
        with conn.transaction():
            # The state check and the index check use the locked row, as in POST /runs/{id}/cancel.
            row = conn.execute("""
                WITH old AS (SELECT id, state, document -> 'method' ->> 'id' AS method_id FROM job
                             WHERE id = %s AND state IN ('queued', 'running') FOR UPDATE)
                UPDATE job j SET state = 'cancelled', finished_at = now(), cancelled_by = %s, lease_expires_at = NULL
                FROM old WHERE j.id = old.id AND (old.state = 'queued' OR NOT (old.method_id = ANY(%s)))
                RETURNING j.id, j.state, j.finished_at, j.cancelled_by, old.state = 'running' AS was_running""",
                               [job_id, actor.id, index_methods]).fetchone()
            if row is None:
                current = conn.execute("SELECT state, document -> 'method' ->> 'id' AS method_id FROM job WHERE id = %s",
                                       [job_id]).fetchone()
                if current['state'] == 'running' and current['method_id'] in index_methods:
                    raise HTTPException(409, 'a running index job is not cancelled. It stops only at its end.')
                raise HTTPException(409, f"job state is {current['state']}; only a queued or running job can be cancelled")
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

    def _artifact_row(conn, pol: Policy, artifact_id):
        row = conn.execute("""SELECT a.*, r.campaign_id AS run_campaign_id FROM artifact a
                              JOIN job j ON j.id = a.job_id JOIN run r ON r.id = j.run_id WHERE a.id = %s""",
                           [artifact_id]).fetchone()
        if row is None or not pol.run_visible({'campaign_id': row['run_campaign_id']}):
            raise HTTPException(404, 'artifact not found')
        return row

    @app.get('/api/artifacts/{artifact_id}')
    def get_artifact(artifact_id: uuid.UUID, conn: Conn, actor: Reader):
        pol = policy_for(conn, actor)
        pol.require_active('artifact metadata')
        row = _artifact_row(conn, pol, artifact_id)
        if pol.agent and row['observation_id']:
            protected = guard.protected(conn, pol, [row['observation_id']])
            if protected:
                guard.refuse(conn, pol, protected, 'artifact metadata')
                raise HTTPException(403, 'the artifact belongs to an observation that is reserved for evaluation. '
                                         'The refusal is recorded.')
        return db.artifact(row)

    @app.get('/api/artifacts/{artifact_id}/content')
    def get_artifact_content(artifact_id: uuid.UUID, request: Request, conn: Conn, actor: Reader):
        """The stored artifact bytes. When blinding withholds values, a JSON summary artifact comes back redacted
        and uncompressed."""
        pol = policy_for(conn, actor)
        pol.require_active('artifact content')
        row = _artifact_row(conn, pol, artifact_id)
        guard.read(conn, pol, [row['observation_id']], 'artifact content')
        if policy_module.needs_redaction(row['kind'], pol):
            document = policy_module.redact_json_artifact(row['kind'], read_artifact_json(row), pol)
            return JSONResponse(jsonable_encoder(document), headers={'Cache-Control': 'private, no-store'})
        target = artifact_path(row)
        etag = f'"{row["sha256"]}"'
        headers = {'ETag': etag, 'Cache-Control': 'private, max-age=31536000, immutable'}
        if row['content_encoding'] == 'gzip':
            headers['Content-Encoding'] = 'gzip'
        if request.headers.get('if-none-match') == etag:
            return Response(status_code=304, headers=headers)
        if not target.is_file():
            raise HTTPException(404, 'artifact file is missing')
        return FileResponse(target, media_type=row['media_type'], headers=headers)

    # Evidence exports ---------------------------------------------------

    def readable_artifacts(conn, pol: Policy, rows: list[dict], operation: str) -> tuple[list[dict], int]:
        """For an agent, leave out the artifacts of protected observations, and record the reads of the others
        (observation budget) before the export holds their bytes. Return the rows and the number of protected
        observations."""
        if not pol.agent:
            return rows, 0
        protected = guard.protected(conn, pol, [r['observation_id'] for r in rows if r['observation_id']])
        rows = [r for r in rows if r['observation_id'] not in protected]
        guard.read(conn, pol, [r['observation_id'] for r in rows], operation)
        return rows, len(protected)

    def export_artifact_files(conn, pol: Policy, rows: list[dict], limitations: list[str]) -> list:
        """The stored bytes of each artifact, or a redacted JSON document when blinding withholds values."""
        files = []
        for row in rows:
            name = row['path'].rsplit('/', 1)[-1]
            target = artifact_path(row)
            if not target.is_file():
                limitations.append(f'The file of artifact {row["id"]} is missing on the artifact volume. '
                                   'The export lists the artifact without its file.')
                continue
            if policy_module.needs_redaction(row['kind'], pol):
                document = policy_module.redact_json_artifact(row['kind'], read_artifact_json(row), pol)
                files.append(exports_module.ExportFile(
                    path=f'artifacts/{row["id"]}/{name.removesuffix(".gz")}', media_type='application/json',
                    content='artifact', data=exports_module.canonical_json(document), artifact_id=row['id'],
                    stored_sha256=row['sha256'], redacted=True))
            else:
                files.append(exports_module.ExportFile(
                    path=f'artifacts/{row["id"]}/{name}',
                    media_type='application/gzip' if row['content_encoding'] == 'gzip' else row['media_type'],
                    content='artifact', source=target, artifact_id=row['id'], stored_sha256=row['sha256']))
        return files

    def export_observations(conn, pol: Policy, ids: list) -> list[dict]:
        """Observation records without the index times, so that a repeated index does not change an export."""
        rows = conn.execute(f'SELECT {EXPORT_OBSERVATION_COLUMNS}, header FROM observation WHERE id = ANY(%s) ORDER BY id',
                            [list(ids)]).fetchall()
        return [redact_observation(r, pol) for r in rows]

    def export_measurements(conn, pol: Policy, rows: list[dict], targets: dict) -> list[dict]:
        out = []
        for m in rows:
            if m['role'] == 'reference_comparison' and not pol.labels_visible:
                continue
            item = db.measurement(m)
            target = targets.get(m['observation_id'])
            out.append({'measurement_id': m['id'], 'job_id': m['job_id'], 'observation_id': m['observation_id'],
                        'target_id': target if pol.identity_visible else None, **{k: v for k, v in item.items() if k != 'id'}})
        return out

    def export_jobs(conn, pol: Policy, job_ids: list) -> list[dict]:
        rows = conn.execute('SELECT * FROM job WHERE id = ANY(%s) ORDER BY created_at, id', [list(job_ids)]).fetchall()
        for row in rows:
            row['result'] = policy_module.redact_result_document(row['result'], pol)
            if row['result'] and row['result'].get('error'):
                row['result'] = {**row['result'], 'error': redact_error(row['result']['error'], pol)}
        return rows

    def export_limitations(pol: Policy, method_keys, protected_excluded: int) -> list[str]:
        out = []
        for key in sorted(set(method_keys)):
            method = contracts.method(*key)
            if method is not None:
                out.append(f'{method.id} v{method.version}: {method.info.get("scientific_status", "no scientific status")}')
        out.append('External files in the evidence are listed by URI, SHA-256, and size. The export does not include them. '
                   'An external file is a proposal, not platform evidence.')
        out.append('Measurements, model judgments, evidence statuses, and identity statuses are separate records. '
                   'A decision score is a judgment, not an uncertainty or a probability.')
        if pol.withheld():
            out.append(f'Blinding withheld these value groups from {pol.actor.name}: {", ".join(pol.withheld())}.')
        if protected_excluded:
            out.append(f'The cohort guard left out {protected_excluded} protected observation(s).')
        return out

    def export_response(subject_kind: str, subject_id, actor: Actor, pol: Policy, files: list, artifacts: str,
                        protected_excluded: int, limitations: list[str]):
        withheld = list(pol.withheld())
        if any(f.text_withheld for f in files):
            withheld.append('text')
        manifest = {
            'contract': 'export/1', 'subject': {'kind': subject_kind, 'id': str(subject_id)},
            'exported_by': {'name': actor.name, 'kind': actor.kind},
            'api': {'version': __version__, 'contracts_source': contracts.source},
            'artifacts': artifacts, 'withheld': withheld, 'protected_excluded': protected_excluded,
            'limitations': limitations,
        }
        files.append(exports_module.ExportFile(path='README.txt', media_type='text/plain; charset=utf-8', content='readme',
                                               data=exports_module.readme(manifest).encode()))
        spool, _, digest = exports_module.write_zip(files, manifest, contracts.export_manifest)
        filename = f'delta-{subject_kind}-{subject_id}.zip'
        return StreamingResponse(exports_module.chunks(spool), media_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="{filename}"', 'X-Export-Manifest-SHA256': digest,
            'Cache-Control': 'private, no-store'})

    ArtifactsMode = Query('none', pattern=r'^(none|all)$',
                          description='none: list artifacts with their SHA-256. all: include each artifact file.')

    @app.get('/api/runs/{run_id}/export')
    def export_run(run_id: uuid.UUID, conn: Conn, actor: Reader, artifacts: str = ArtifactsMode):
        """A ZIP file with the run, its observations, measurements, artifacts, and the research records that refer to it.
        manifest.json (contract export/1) lists each file with its size and SHA-256. The same request on the same
        records gives the same bytes. Blinding, the cohort guard, and the observation budget apply as in the views."""
        pol = policy_for(conn, actor)
        pol.require_active('an export')
        detail = _run_detail(conn, run_id, pol, excluded_for(conn, pol))
        results = run_results(conn, pol, run_id)
        job_ids = [j['job_id'] for j in results['jobs']]
        observation_ids = sorted({j['observation_id'] for j in results['jobs'] if j['observation_id']} |
                                 {a['observation_id'] for j in results['jobs'] for a in j['artifacts']
                                  if a['observation_id']}, key=str)
        observations = export_observations(conn, pol, observation_ids)
        targets = {o['id']: o['target_id'] for o in conn.execute(
            'SELECT id, target_id FROM observation WHERE id = ANY(%s)', [observation_ids]).fetchall()}
        measurement_rows = conn.execute('SELECT * FROM measurement WHERE job_id = ANY(%s) ORDER BY id', [job_ids]).fetchall()
        measurements = export_measurements(conn, pol, measurement_rows, targets)
        artifact_rows = conn.execute("""SELECT * FROM artifact WHERE job_id = ANY(%s)
                                           OR (observation_id = ANY(%s) AND kind = 'light_curve')
                                        ORDER BY id""", [job_ids, observation_ids]).fetchall()
        artifact_rows, _ = readable_artifacts(conn, pol, artifact_rows, 'export')
        method_key = (detail['method_id'], detail['method_version'])
        method = contracts.method(*method_key)
        limitations = export_limitations(pol, [method_key], results['protected_excluded'])
        records_view = records_api.helpers['records_about'](conn, actor, run_ids=[run_id], job_ids=job_ids,
                                                             measurement_ids=[m['id'] for m in measurement_rows],
                                                             artifact_ids=[a['id'] for a in artifact_rows])
        J = exports_module.json_file
        files = [
            J('run.json', 'run', {'run': detail, 'method': method.describe() if method else None,
                                  'jobs': export_jobs(conn, pol, job_ids)}, text_withheld=detail.get('text_withheld', False)),
            J('observations.json', 'observations', observations),
            J('measurements.json', 'measurements', measurements),
            exports_module.measurements_csv(measurements),
            J('artifacts.json', 'records', [db.artifact(a) for a in artifact_rows]),
            J('records.json', 'records', records_view, text_withheld='text' in records_view['withheld']),
            J('methods.json', 'methods', [method.describe()] if method else []),
        ]
        investigation_link = conn.execute('SELECT campaign_id FROM investigation_execution WHERE run_id=%s', [run_id]).fetchone()
        if investigation_link and actor.kind == 'person':
            cid = investigation_link['campaign_id']
            investigation = investigations.detail(conn, actor, cid)
            investigation['events'] = conn.execute('SELECT * FROM investigation_event WHERE campaign_id=%s ORDER BY id', [cid]).fetchall()
            investigation['sessions'] = conn.execute('SELECT id,document,created_at,updated_at FROM agent_session WHERE campaign_id=%s ORDER BY created_at,id', [cid]).fetchall()
            files.append(J('investigation.json', 'records', investigation))
        if artifacts == 'all':
            files += export_artifact_files(conn, pol, artifact_rows, limitations)
        return export_response('run', run_id, actor, pol, files, artifacts, results['protected_excluded'], limitations)

    @app.get('/api/candidates/{candidate_id}/export')
    def export_candidate(candidate_id: uuid.UUID, conn: Conn, actor: Reader, artifacts: str = ArtifactsMode):
        """A ZIP file with the candidate, its resolved evidence, assessments, decisions, notes, and the referenced runs,
        jobs, measurements, and artifacts. manifest.json (contract export/1) lists each file with its SHA-256."""
        pol = policy_for(conn, actor)
        pol.require_active('an export')
        detail = records_api.helpers['candidate_detail'](conn, actor, candidate_id)
        refs = detail['evidence']
        ids = lambda kind: [r['id'] for r in refs if r['kind'] == kind]  # noqa: E731
        job_ids = set(ids('job'))
        measurement_ids = ids('measurement')
        artifact_ids = ids('artifact')
        if measurement_ids:
            job_ids |= {r['job_id'] for r in conn.execute('SELECT job_id FROM measurement WHERE id = ANY(%s::bigint[])',
                                                          [measurement_ids]).fetchall()}
        if artifact_ids:
            job_ids |= {r['job_id'] for r in conn.execute('SELECT job_id FROM artifact WHERE id = ANY(%s::uuid[])',
                                                          [artifact_ids]).fetchall()}
        job_ids = sorted(job_ids, key=str)
        run_ids = set(ids('run')) | {r['run_id'] for r in conn.execute(
            'SELECT run_id FROM job WHERE id = ANY(%s::uuid[])', [job_ids]).fetchall()}
        runs = []
        method_keys = []
        for rid in sorted(run_ids, key=str):
            run = visible_run(conn, pol, rid)
            method_keys.append((run['method_id'], run['method_version']))
            runs.append({'run': {k: v for k, v in _run_detail(conn, rid, pol, excluded_for(conn, pol)).items() if k != 'jobs'},
                         'jobs': [j for j in export_jobs(conn, pol, job_ids) if j['run_id'] == rid]})
        observation_ids = sorted({uuid.UUID(str(o)) for o in detail['observation_ids']} |
                                 {r['observation_id'] for r in conn.execute(
                                     'SELECT observation_id FROM job WHERE id = ANY(%s::uuid[]) AND observation_id IS NOT NULL',
                                     [job_ids]).fetchall()}, key=str)
        targets = {o['id']: o['target_id'] for o in conn.execute(
            'SELECT id, target_id FROM observation WHERE id = ANY(%s)', [observation_ids]).fetchall()}
        measurement_rows = conn.execute("""SELECT * FROM measurement WHERE job_id = ANY(%s::uuid[]) OR id = ANY(%s::bigint[])
                                           ORDER BY id""", [job_ids, measurement_ids]).fetchall()
        artifact_rows = conn.execute("""SELECT * FROM artifact WHERE job_id = ANY(%s::uuid[]) OR id = ANY(%s::uuid[])
                                        ORDER BY id""", [job_ids, artifact_ids]).fetchall()
        artifact_rows, protected_artifacts = readable_artifacts(conn, pol, artifact_rows, 'export')
        limitations = export_limitations(pol, method_keys, protected_artifacts)
        J = exports_module.json_file
        measurements = export_measurements(conn, pol, measurement_rows, targets)
        files = [
            J('candidate.json', 'records', detail, text_withheld='text' in detail['withheld']),
            J('runs.json', 'run', runs, text_withheld=any(r['run'].get('text_withheld') for r in runs)),
            J('observations.json', 'observations', export_observations(conn, pol, observation_ids)),
            J('measurements.json', 'measurements', measurements),
            exports_module.measurements_csv(measurements),
            J('artifacts.json', 'records', [db.artifact(a) for a in artifact_rows]),
            J('methods.json', 'methods', [contracts.method(*k).describe() for k in sorted(set(method_keys))
                                          if contracts.method(*k)]),
        ]
        if artifacts == 'all':
            files += export_artifact_files(conn, pol, artifact_rows, limitations)
        return export_response('candidate', candidate_id, actor, pol, files, artifacts, protected_artifacts, limitations)

    # Research records: candidates, assessments, decisions, notes, and agent tasks.
    app.include_router(records_api, prefix='/api')
    app.include_router(agent_sessions.router(connection=connection, need=need, contracts=contracts), prefix='/api')
    app.include_router(acquisition.router(connection=connection, need=need, contracts=contracts), prefix='/api')
    app.include_router(investigations.router(connection=connection, need=need, contracts=contracts,
                                            check_inputs=check_inputs, submit_run=submit_run), prefix='/api')
    app.include_router(signals.router(connection=connection, need=need, guard=guard), prefix='/api')
    app.include_router(catalogue.router(need=need, datasets_root=config.datasets_root,
                                       experiments_root=config.experiments_root), prefix='/api')

    def openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        document = get_openapi(title=app.title, version=app.version, routes=app.routes)
        # Schemas remain authoritative in the vendored database contract. Both GUI forms and tools use this view.
        routes = {'/api/candidates':'candidate/1', '/api/assessments':'assessment/1', '/api/decisions':'decision/1',
                  '/api/notes':'note/1', '/api/tasks':'agent-task/1', '/api/acquisitions':'acquisition-request/1',
                  '/api/investigations':'investigation-plan/1',
                  '/api/investigations/{campaign_id}/sessions':'agent-session/1'}
        for path, name in routes.items():
            document['paths'][path]['post']['requestBody']['content']['application/json']['schema'] = contracts.expanded_record(name)
        schemas = document['components']['schemas']
        schemas['RevisionRequest']['properties']['document'] = contracts.expanded_record('investigation-plan/1')
        schemas['ReportRequest']['properties']['report'] = contracts.expanded_record('agent-task-report/1')
        app.openapi_schema = document
        return document
    app.openapi = openapi
    return app


EXPORT_OBSERVATION_COLUMNS = """id, dataset_id, product_type, relative_path, bytes, sha256, target_id, sector,
    cadence_seconds, time_start, time_stop, time_scale, time_reference, row_count, labels, state,
    findings, provenance"""
OBSERVATION_COLUMNS = EXPORT_OBSERVATION_COLUMNS + ', first_indexed_at, last_seen_at'


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


def parameter_differences(a, b, path: str = '') -> list[dict]:
    """Leaf values of two parameter documents that differ, by dotted path, in sorted order."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            out += parameter_differences(a.get(key), b.get(key), f'{path}.{key}' if path else key)
        return out
    return [] if a == b else [{'path': path, 'a': a, 'b': b}]


def _text_rule(row: dict, pol: Policy, field: str = 'label') -> dict:
    """Rule free-text/1 for one text field of a run. The writer is the client that created the run."""
    writer = row.pop('writer_campaign_id', None)
    row['text_withheld'] = False
    if row.get(field) is not None and not pol.text_visible(writer):
        row[field] = None
        row['text_withheld'] = True
    return row


def _run_summaries(conn, ids, pol: Policy, excluded=()):
    """Run summaries in the order of ids, with input counts and job states. The counts leave out excluded observations."""
    if not ids:
        return []
    excluded = list(excluded)
    rows = conn.execute("""
        SELECT r.id, r.method_id, r.method_version, r.label, r.requested_by, r.client_id, r.campaign_id, r.created_at,
               r.parameters_sha256, r.retry_of, w.campaign_id AS writer_campaign_id,
               (SELECT count(*) FROM run_input i WHERE i.run_id = r.id
                  AND NOT (i.observation_id = ANY(%(x)s::uuid[]))) AS input_count,
               (SELECT count(*) FROM run_input i WHERE i.run_id = r.id
                  AND i.observation_id = ANY(%(x)s::uuid[])) AS protected_excluded,
               COALESCE((SELECT jsonb_object_agg(state, n) FROM
                   (SELECT state, count(*) AS n FROM job j WHERE j.run_id = r.id
                      AND (j.observation_id IS NULL OR NOT (j.observation_id = ANY(%(x)s::uuid[]))) GROUP BY state) s),
                   '{}'::jsonb) AS job_states
        FROM run r LEFT JOIN client w ON w.id = r.client_id WHERE r.id = ANY(%(ids)s)""",
                        {'x': excluded, 'ids': list(ids)}).fetchall()
    by_id = {r['id']: _text_rule(r, pol) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _insert_run(conn, run_id, method, parameters, label, actor: Actor, campaign_id=None, retry_of=None):
    conn.execute("""
        INSERT INTO run (id, method_id, method_version, parameters, parameters_sha256, label, requested_by, client_id,
                         campaign_id, retry_of)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                 [run_id, method.id, method.version, Jsonb(parameters), parameters_sha256(parameters), label,
                  f'client:{actor.name}', actor.id, campaign_id, retry_of])


def _insert_job(conn, job_id, run_id, observation_id, document):
    conn.execute('INSERT INTO job (id, run_id, observation_id, document) VALUES (%s, %s, %s, %s)',
                 [job_id, run_id, observation_id, Jsonb(document)])


def _run_detail(conn, run_id, pol: Policy, excluded=()):
    """One run with its inputs, jobs, and retries. For an agent: 404 for another campaign's run, and no protected
    observations."""
    excluded = list(excluded)
    run = conn.execute("""SELECT r.*, w.campaign_id AS writer_campaign_id FROM run r
                          LEFT JOIN client w ON w.id = r.client_id WHERE r.id = %s""", [run_id]).fetchone()
    if run is None or not pol.run_visible(run):
        raise HTTPException(404, 'run not found')
    _text_rule(run, pol)
    inputs = [r['observation_id'] for r in conn.execute(
        'SELECT observation_id FROM run_input WHERE run_id = %s ORDER BY observation_id', [run_id]).fetchall()]
    run['inputs'] = [o for o in inputs if o not in excluded]
    run['protected_excluded'] = len(inputs) - len(run['inputs'])
    jobs = conn.execute("""
        SELECT id, observation_id, state, attempts, max_attempts, available_at, claimed_by,
               lease_expires_at, created_at, started_at, finished_at, cancelled_by,
               result ->> 'outcome' AS outcome, result -> 'error' AS error
        FROM job WHERE run_id = %s ORDER BY created_at, id""", [run_id]).fetchall()
    run['jobs'] = [{**j, 'error': redact_error(j['error'], pol)} for j in jobs
                   if j['observation_id'] is None or j['observation_id'] not in excluded]
    scope, args = pol.run_scope('r')
    run['retries'] = [r['id'] for r in conn.execute(
        f'SELECT id FROM run r WHERE retry_of = %s AND {scope} ORDER BY created_at, id', [run_id, *args]).fetchall()]
    return run


app = create_app()
