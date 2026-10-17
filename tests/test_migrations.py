"""Migration 0004 applies to a database that already holds slice and campaign records."""
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / 'vendor' / 'migrations'


@pytest.fixture
def empty_database(database_url):
    """A separate, empty database on the test server."""
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute('DROP DATABASE IF EXISTS upgrade')
        admin.execute('CREATE DATABASE upgrade')
    url = database_url.replace('/delta?', '/upgrade?')
    yield url
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute('DROP DATABASE upgrade WITH (FORCE)')


def apply(conn, names):
    for name in names:
        conn.execute((MIGRATIONS / name).read_text())


def test_0004_upgrades_a_database_with_existing_records(empty_database):
    names = sorted(p.name for p in MIGRATIONS.glob('[0-9]*.sql'))
    assert names[-1] == '0004_research_records.sql'
    with psycopg.connect(empty_database, autocommit=True, row_factory=dict_row) as conn:
        apply(conn, names[:-1])
        person = conn.execute("""INSERT INTO client (name, kind, permissions, token_sha256, created_by)
                                 VALUES ('richard', 'person', '{admin}', repeat('a', 64), 'test') RETURNING id""").fetchone()['id']
        campaign = conn.execute("""INSERT INTO campaign (name, title, question, created_by, approved_by, approved_at,
                                                         campaign_file_sha256)
                                   VALUES ('old', 'Old', 'Q?', %s, %s, now(), repeat('c', 64)) RETURNING id""",
                                [person, person]).fetchone()['id']
        conn.execute("""INSERT INTO run (method_id, method_version, parameters, parameters_sha256, requested_by, campaign_id)
                        VALUES ('tess-bls', 1, '{}', repeat('0', 64), 'test', %s)""", [campaign])
        apply(conn, names[-1:])
        assert conn.execute('SELECT count(*) AS n FROM run WHERE campaign_id = %s', [campaign]).fetchone()['n'] == 1
        # An open campaign from before the migration takes records, and its close cancels its tasks.
        task = conn.execute("""INSERT INTO agent_task (campaign_id, kind, title, created_by)
                               VALUES (%s, 'vetting', 'Vet', %s) RETURNING id""", [campaign, person]).fetchone()['id']
        candidate = conn.execute("""INSERT INTO candidate (campaign_id, kind, tier, summary, created_by)
                                    VALUES (%s, 'transit_signal', 'lab', 'x', %s) RETURNING id""",
                                 [campaign, person]).fetchone()['id']
        status = conn.execute('SELECT * FROM candidate_status WHERE candidate_id = %s', [candidate]).fetchone()
        assert (status['evidence_status'], status['identity_status']) == ('unreviewed', 'unchecked')
        conn.execute('UPDATE campaign SET closed_by = %s, closed_at = now() WHERE id = %s', [person, campaign])
        assert conn.execute('SELECT state FROM agent_task WHERE id = %s', [task]).fetchone()['state'] == 'cancelled'
