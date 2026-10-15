"""Test fixtures with a real, temporary PostgreSQL server (pgserver).

The fixture applies the vendored migrations, so the tests check the API
against the same schema that the database repository releases.
"""
from pathlib import Path
import gzip
import hashlib
import json
import uuid

import pgserver
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest
from fastapi.testclient import TestClient

from delta_api.app import create_app
from delta_api.auth import create_client
from delta_api.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / 'vendor' / 'migrations'
TABLES = 'measurement, artifact, job, run_input, run, observation, dataset, client'


@pytest.fixture(scope='session')
def database_url(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp('pg'), cleanup_mode='stop')
    base = server.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute('CREATE DATABASE delta')
    url = base.replace('/postgres?', '/delta?')
    with psycopg.connect(url, autocommit=True) as conn:
        for path in sorted(MIGRATIONS.glob('[0-9]*.sql')):
            conn.execute(path.read_text())
    yield url
    server.cleanup()


@pytest.fixture
def conn(database_url):
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as connection:
        connection.execute(f'TRUNCATE {TABLES} CASCADE')
        yield connection


@pytest.fixture
def artifact_root(tmp_path):
    root = tmp_path / 'artifacts'
    root.mkdir()
    return root


@pytest.fixture
def client(database_url, conn, artifact_root):
    settings = Settings(database_url=database_url, artifact_root=artifact_root,
                        contracts_dir=ROOT / 'vendor' / 'contracts', cors_origins=())
    _, token = create_client(conn, name='richard', kind='person', permissions=['read', 'run.submit', 'record.write',
                             'acquire', 'identity.reveal', 'admin'], created_by='test')
    with TestClient(create_app(settings), headers={'Authorization': f'Bearer {token}'}) as test_client:
        yield test_client


def token_for(conn, name, kind='agent', permissions=('read', 'run.submit', 'record.write')):
    """Create a client and return headers with its token."""
    _, token = create_client(conn, name=name, kind=kind, permissions=list(permissions), created_by='test')
    return {'Authorization': f'Bearer {token}'}


def add_observation(conn, *, state='ready', tic=100100827, sector=2, labels=None, header=None, time_start=1354.1, time_stop=1381.5):
    """Insert a dataset (if absent) and one observation row. Return its id."""
    conn.execute("INSERT INTO dataset (id, state) VALUES ('tess-spoc-light-curves', 'ready') "
                 'ON CONFLICT DO NOTHING')
    oid = uuid.uuid4()
    digest = hashlib.sha256(oid.bytes).hexdigest()
    conn.execute("""
        INSERT INTO observation (id, dataset_id, product_type, relative_path, bytes, sha256, target_id,
            sector, cadence_seconds, time_start, time_stop, time_scale, time_reference, row_count, header, labels, state)
        VALUES (%s, 'tess-spoc-light-curves', 'tess_spoc_light_curve', %s, 2000000, %s, %s, %s, 120,
            %s, %s, 'tdb', 'BJD 2457000.0', 18000, %s, %s, %s)""",
                 [oid, f'{oid}.fits', digest, f'TIC:{tic}', sector, time_start, time_stop,
                  Jsonb(header or {}), Jsonb(labels or {}), state])
    return oid


def add_finished_job(conn, artifact_root, observation_id, *, payload=b'{"format":"columns/1"}'):
    """Insert a run, a complete job, measurements, and one gzip artifact file."""
    run_id, job_id = uuid.uuid4(), uuid.uuid4()
    document = {'contract': 'job/1', 'job_id': str(job_id), 'run_id': str(run_id),
                'method': {'id': 'tess-bls', 'version': 1}, 'parameters': {},
                'inputs': [{'observation_id': str(observation_id)}],
                'limits': {'maximum_seconds': 600, 'maximum_memory_mib': 2048}}
    result = {'contract': 'result/1', 'job_id': str(job_id), 'outcome': 'complete', 'measurements': [],
              'artifacts': [], 'diagnostics': {'grid_trials': 35678},
              'software': {'worker_version': 'test', 'python': '3.12', 'packages': {}}}
    conn.execute("INSERT INTO run (id, method_id, method_version, parameters, parameters_sha256, requested_by) "
                 "VALUES (%s, 'tess-bls', 1, '{}', %s, 'test')", [run_id, '0' * 64])
    conn.execute('INSERT INTO run_input VALUES (%s, %s)', [run_id, observation_id])
    conn.execute("""INSERT INTO job (id, run_id, observation_id, document, state, attempts, result, finished_at)
                    VALUES (%s, %s, %s, %s, 'complete', 1, %s, now())""",
                 [job_id, run_id, observation_id, Jsonb(document), Jsonb(result)])
    conn.execute("""INSERT INTO measurement (job_id, observation_id, role, name, value_number, unit,
                        uncertainty_kind, uncertainty_reason)
                    VALUES (%s, %s, 'search', 'period', 0.9413849094310086, 'd', 'unavailable', 'grid value')""",
                 [job_id, observation_id])
    conn.execute("""INSERT INTO measurement (job_id, observation_id, role, name, value_number, unit,
                        time_scale, time_reference, uncertainty_kind, uncertainty_reason)
                    VALUES (%s, %s, 'search', 'transit_time', 1354.4595, 'd', 'tdb', 'BJD 2457000.0',
                        'unavailable', 'grid value')""", [job_id, observation_id])
    conn.execute("""INSERT INTO measurement (job_id, observation_id, role, name, value_number, unit,
                        uncertainty_kind, uncertainty_value, quality_flags)
                    VALUES (%s, %s, 'search', 'depth', 0.00947, '1', 'standard', 1.45e-05,
                        '{trend_removal_can_reduce_depth}')""", [job_id, observation_id])
    conn.execute("""INSERT INTO measurement (job_id, observation_id, role, name, value_boolean, unit,
                        uncertainty_kind, uncertainty_reason)
                    VALUES (%s, %s, 'reference_comparison', 'recovered', true, '1', 'unavailable', 'decision')""",
                 [job_id, observation_id])
    path = f'jobs/{job_id}/1/periodogram.json.gz'
    compressed = gzip.compress(payload)
    (artifact_root / path).parent.mkdir(parents=True)
    (artifact_root / path).write_bytes(compressed)
    artifact_id = conn.execute("""
        INSERT INTO artifact (job_id, observation_id, kind, media_type, content_encoding, path, bytes, sha256)
        VALUES (%s, %s, 'periodogram', 'application/vnd.delta.columns+json', 'gzip', %s, %s, %s)
        RETURNING id""", [job_id, observation_id, path, len(compressed),
                          hashlib.sha256(compressed).hexdigest()]).fetchone()['id']
    return run_id, job_id, artifact_id


def add_run(conn, *, label=None, created_at='2026-10-13T00:00:00Z'):
    """Insert an analysis run with no inputs. Return its id."""
    run_id = uuid.uuid4()
    conn.execute("INSERT INTO run (id, method_id, method_version, parameters, parameters_sha256, label, requested_by, created_at) "
                 "VALUES (%s, 'tess-bls', 1, '{}', %s, %s, 'test', %s)", [run_id, '0' * 64, label, created_at])
    return run_id


def add_job(conn, run_id, observation_id, *, state='complete', measurements=(), result=None, artifacts=()):
    """Insert one job with search measurements (role, name, value) and artifact files (kind, json document)."""
    job_id = uuid.uuid4()
    document = {'contract': 'job/1', 'job_id': str(job_id), 'run_id': str(run_id),
                'method': {'id': 'tess-bls', 'version': 1}, 'parameters': {},
                'inputs': [{'observation_id': str(observation_id)}],
                'limits': {'maximum_seconds': 600, 'maximum_memory_mib': 2048}}
    finished = state in ('complete', 'inconclusive', 'failed')
    result = result if result is not None else ({'contract': 'result/1', 'job_id': str(job_id), 'outcome': state,
                                                 'measurements': [], 'artifacts': []} if finished else None)
    conn.execute('INSERT INTO run_input VALUES (%s, %s) ON CONFLICT DO NOTHING', [run_id, observation_id])
    conn.execute("""INSERT INTO job (id, run_id, observation_id, document, state, attempts, result, finished_at,
                        lease_expires_at)
                    VALUES (%s, %s, %s, %s, %s, 1, %s, CASE WHEN %s THEN now() END,
                            CASE WHEN %s = 'running' THEN now() + interval '1 minute' END)""",
                 [job_id, run_id, observation_id, Jsonb(document), state, Jsonb(result) if result else None,
                  finished, state])
    for role, name, value in measurements:
        conn.execute("""INSERT INTO measurement (job_id, observation_id, role, name, value_number, unit,
                            uncertainty_kind, uncertainty_reason)
                        VALUES (%s, %s, %s, %s, %s, '1', 'unavailable', 'test')""",
                     [job_id, observation_id, role, name, value])
    return job_id
