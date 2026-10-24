"""The shared cohort register, experiments/cohorts.json, as the cohort guard reads it.

The API reads the register from a read-only mount. The file stays the only source.
An evaluation reservation protects its observations: the API refuses agent reads of them,
and each read or refused read writes an access event.
An observation matches a reservation when its target ID ("TIC:<n>") is a group or observation identity
of the reservation, or when "mast:TESS/product/<relative_path>" is an observation identity.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading

PROTECTED_ROLES = ('evaluation',)
TESS_PRODUCT_PREFIX = 'mast:TESS/product/'


class RegisterUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Register:
    sha256: str
    pairs: tuple  # (reservation_id, identity) for each protected identity


class CohortRegister:
    """Reads the register file again when its modification time or size changes. The cache holds only file content."""

    def __init__(self, path: Path | None, *, standalone_exploration=False):
        self.path = path
        self.standalone_exploration = standalone_exploration
        self._key = None
        self._value: Register | None = None
        self._lock = threading.Lock()

    def load(self) -> Register:
        if self.standalone_exploration:
            return Register('standalone-exploration', ())
        if self.path is None:
            raise RegisterUnavailable('no cohort register path is configured')
        try:
            stat = self.path.stat()
        except OSError as error:
            raise RegisterUnavailable(f'cohort register is unreadable: {error}') from error
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if key != self._key:
                data = self.path.read_bytes()
                try:
                    register = json.loads(data)
                    pairs = tuple((e['id'], ident) for e in register['entries'] if e['role'] in PROTECTED_ROLES
                                  for ident in [*e['observation_ids'], *e['group_ids']])
                except (ValueError, KeyError, TypeError) as error:
                    raise RegisterUnavailable(f'cohort register is invalid: {error}') from error
                self._value = Register(hashlib.sha256(data).hexdigest(), pairs)
                self._key = key
            return self._value

    def protected(self, conn, observation_ids=None) -> dict:
        """Map each protected observation ID to the ID of its reservation. Without IDs, check every observation."""
        register = self.load()
        if not register.pairs:
            return {}
        reservations, identities = [p[0] for p in register.pairs], [p[1] for p in register.pairs]
        where, args = '', [reservations, identities]
        if observation_ids is not None:
            where, args = 'WHERE o.id = ANY(%s)', [*args, list(observation_ids)]
        rows = conn.execute(f"""
            SELECT DISTINCT ON (o.id) o.id, p.reservation_id
            FROM observation o
            JOIN unnest(%s::text[], %s::text[]) AS p(reservation_id, identity)
              ON o.target_id = p.identity OR ('{TESS_PRODUCT_PREFIX}' || o.relative_path) = p.identity
            {where}
            ORDER BY o.id, p.reservation_id""", args).fetchall()
        return {r['id']: r['reservation_id'] for r in rows}
