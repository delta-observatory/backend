"""Client identity and permissions.

Each request carries "Authorization: Bearer <token>". The API stores only the SHA-256 of a token.
A token belongs to one client: a person, an agent, or a service. Each client has a set of permissions.
The API checks a permission for each operation and records the client of each write.
"""
from dataclasses import dataclass, field
import hashlib
import secrets
import uuid

PERMISSIONS = ('read', 'run.submit', 'record.write', 'acquire', 'identity.reveal', 'admin')
# Richard's decision: an agent never holds these permissions. The database enforces the same rule.
AGENT_FORBIDDEN = frozenset({'acquire', 'identity.reveal', 'admin'})
TOKEN_PREFIX = 'dlt_'


@dataclass(frozen=True)
class Actor:
    id: uuid.UUID
    name: str
    kind: str
    permissions: frozenset = field(default_factory=frozenset)

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def describe(self) -> dict:
        return {'id': self.id, 'name': self.name, 'kind': self.kind, 'permissions': sorted(self.permissions)}


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def check_permissions(kind: str, permissions: list[str]) -> list[str]:
    """Error messages for a permission set. An empty list means that the set is valid."""
    errors = [f'unknown permission "{p}"' for p in permissions if p not in PERMISSIONS]
    if kind == 'agent':
        errors += [f'an agent cannot hold "{p}"' for p in permissions if p in AGENT_FORBIDDEN]
    return errors


def create_client(conn, *, name: str, kind: str, permissions: list[str], created_by: str, note: str | None = None) -> tuple[dict, str]:
    """Insert a client. Return its row and its token. The token is not stored and cannot be read again."""
    token = new_token()
    row = conn.execute("""
        INSERT INTO client (name, kind, permissions, token_sha256, note, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id, name, kind, permissions, note, created_by, created_at, disabled_at""",
                       [name, kind, sorted(set(permissions)), token_sha256(token), note, created_by]).fetchone()
    return row, token


def lookup(conn, token: str) -> Actor | None:
    row = conn.execute("""
        SELECT id, name, kind, permissions FROM client
        WHERE token_sha256 = %s AND disabled_at IS NULL""", [token_sha256(token)]).fetchone()
    if row is None:
        return None
    return Actor(row['id'], row['name'], row['kind'], frozenset(row['permissions']))
