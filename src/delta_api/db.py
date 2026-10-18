"""PostgreSQL connection pool and row serialization."""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def make_pool(database_url: str) -> ConnectionPool:
    return ConnectionPool(
        database_url,
        min_size=1,
        max_size=10,
        open=False,
        # Autocommit: each 'with conn.transaction()' block is a real transaction that commits
        # before the response. Without it, the block is a savepoint, and the commit waits for the
        # end of the request, so a client can read the old state just after a write.
        kwargs={'row_factory': dict_row, 'autocommit': True},
        name='delta-api',
    )


def measurement(row: dict) -> dict:
    """Serialize one measurement row with its ID, value, unit, and uncertainty. Records refer to the ID."""
    if row['uncertainty_kind'] == 'standard':
        uncertainty = {'kind': 'standard', 'value': row['uncertainty_value']}
    else:
        uncertainty = {'kind': 'unavailable', 'reason': row['uncertainty_reason']}
    value = row['value_boolean'] if row['value_boolean'] is not None else row['value_number']
    item = {
        'id': row['id'],
        'role': row['role'],
        'name': row['name'],
        'value': value,
        'unit': row['unit'],
        'uncertainty': uncertainty,
        'quality_flags': list(row['quality_flags']),
    }
    if row['time_scale'] is not None:
        item['time_scale'] = row['time_scale']
        item['time_reference'] = row['time_reference']
    return item


def artifact(row: dict) -> dict:
    return {
        'id': row['id'],
        'job_id': row['job_id'],
        'observation_id': row['observation_id'],
        'kind': row['kind'],
        'media_type': row['media_type'],
        'content_encoding': row['content_encoding'],
        'path': row['path'],
        'bytes': row['bytes'],
        'sha256': row['sha256'],
        'created_at': row['created_at'],
        'content_url': f"/api/artifacts/{row['id']}/content",
    }
