"""Administration commands that run next to the API with DATABASE_URL.

Create the first client, which then creates other clients through POST /api/clients:
    python -m delta_api.admin create-client --name richard --kind person --permissions all
The command prints the token once. The database keeps only its SHA-256.
"""
import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

from .auth import PERMISSIONS, check_permissions, create_client


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m delta_api.admin')
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create-client', help='Create a client and print its token.')
    create.add_argument('--name', required=True)
    create.add_argument('--kind', required=True, choices=['person', 'agent', 'service'])
    create.add_argument('--permissions', required=True, help=f'Comma-separated, or "all". Permissions: {", ".join(PERMISSIONS)}.')
    create.add_argument('--note')
    sub.add_parser('list-clients', help='List clients without tokens.')
    args = parser.parse_args(argv)

    url = os.environ.get('DATABASE_URL')
    if not url:
        print('DATABASE_URL is required', file=sys.stderr)
        return 2
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        if args.command == 'list-clients':
            for row in conn.execute('SELECT id, name, kind, permissions, created_at, disabled_at FROM client ORDER BY name'):
                print(json.dumps(row, default=str))
            return 0
        permissions = list(PERMISSIONS) if args.permissions == 'all' else [p.strip() for p in args.permissions.split(',') if p.strip()]
        errors = check_permissions(args.kind, permissions)
        if errors:
            print('; '.join(errors), file=sys.stderr)
            return 2
        row, token = create_client(conn, name=args.name, kind=args.kind, permissions=permissions,
                                   created_by='admin-command', note=args.note)
        print(json.dumps({'client': row, 'token': token}, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
