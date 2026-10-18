"""Campaign policy for one request: blinding, the cohort guard, and budgets (ROADMAP R8).

A person or a service sees everything. Each read of a protected observation by a person writes an access event.
An agent works inside its campaign:
- The campaign must be approved and not closed before the agent reads observation data or writes.
- Blinding follows the campaign level until Richard records the reveal:
  open (nothing withheld), labels (known-object labels withheld), labels_identity (labels and catalog identity withheld).
- The cohort guard refuses each read of an observation that an evaluation reservation protects, and records the refusal.
- Budgets limit the distinct observations read, the jobs, the compute time, and the stored bytes of the campaign.
- An agent sees the runs of its campaign and the runs without a campaign. Another campaign's run is "not found".
- Rule free-text/1: while blinding withholds values, an agent receives free text from the clients of its own campaign
  and directed text (campaign title and question, task title and instructions). Other writers' free text is null.
  The API does not search text for withheld values. This handling does not guarantee scientific blinding.
"""
from dataclasses import dataclass

from fastapi import HTTPException

from .auth import Actor
from .cohorts import CohortRegister, RegisterUnavailable

BUDGET_KEYS = {
    'observations_read': 'distinct observations whose data the campaign read',
    'jobs': 'jobs in the runs of the campaign',
    'compute_seconds': 'worker seconds of finished jobs, plus estimates for waiting and new jobs',
    'storage_bytes': 'bytes of the artifacts of the campaign jobs',
}

# Observation header keywords that stay visible when identity is withheld. Other keywords, for example
# TICID, OBJECT, RA_OBJ, DEC_OBJ, CAMERA, and CCD, identify the star or its position.
# Stellar parameters stay, because vetting needs them. They narrow the identity but do not give it.
IDENTITY_SAFE_HEADER = {'SECTOR', 'TESSMAG', 'TEFF', 'LOGG', 'MH', 'RADIUS', 'CROWDSAP', 'FLFRCSAP', 'PDCVAR',
                        'PDCMETHD', 'CDPP0_5', 'CDPP1_0', 'CDPP2_0', 'PROCVER', 'DATA_REL', 'TIMEDEL', 'TIMESYS',
                        'TIMEUNIT', 'TIMEREF', 'BJDREFI', 'BJDREFF', 'TSTART', 'TSTOP', 'fits_checksums'}
IDENTITY_FIELDS = ('target_id', 'relative_path', 'sha256', 'ra_deg', 'dec_deg', 'provenance')
# Diagnostic keys of a result that compare the search with a known object.
LABEL_DIAGNOSTICS = ('reference_comparison', 'reference')


@dataclass(frozen=True)
class Policy:
    actor: Actor
    campaign: dict | None

    @property
    def agent(self) -> bool:
        return self.actor.kind == 'agent'

    @property
    def revealed(self) -> bool:
        return bool(self.campaign and self.campaign['revealed_at'])

    @property
    def labels_visible(self) -> bool:
        if not self.agent or self.revealed:
            return True
        return self.campaign['blinding'] == 'open'

    @property
    def identity_visible(self) -> bool:
        if not self.agent or self.revealed:
            return True
        return self.campaign['blinding'] in ('open', 'labels')

    def withheld(self) -> list[str]:
        out = []
        if not self.labels_visible:
            out.append('labels')
        if not self.identity_visible:
            out.append('identity')
        return out

    @property
    def text_rule(self) -> bool:
        """True when rule free-text/1 withholds other writers' free text from this client."""
        return self.agent and bool(self.withheld())

    def text_visible(self, writer_campaign_id) -> bool:
        """Free text from a writer whose client belongs to writer_campaign_id."""
        return not self.text_rule or (writer_campaign_id is not None and writer_campaign_id == self.campaign['id'])

    def run_visible(self, run: dict) -> bool:
        """An agent sees the runs of its campaign and the runs without a campaign."""
        return not self.agent or run['campaign_id'] is None or run['campaign_id'] == self.campaign['id']

    def run_scope(self, alias: str = 'r') -> tuple[str, list]:
        """SQL condition and arguments: the run with the table alias is visible to the client."""
        if not self.agent:
            return 'true', []
        return f'({alias}.campaign_id IS NULL OR {alias}.campaign_id = %s)', [self.campaign['id']]

    def require_active(self, operation: str):
        """An agent reads observation data and writes only inside an approved campaign that is not closed."""
        if not self.agent:
            return
        c = self.campaign
        if c['approved_at'] is None:
            raise HTTPException(403, f'campaign {c["name"]} is not approved: {operation} is not allowed yet')
        if c['closed_at'] is not None:
            raise HTTPException(403, f'campaign {c["name"]} is closed')


def policy_for(conn, actor: Actor) -> Policy:
    if actor.campaign_id is None:
        return Policy(actor, None)
    campaign = conn.execute('SELECT * FROM campaign WHERE id = %s', [actor.campaign_id]).fetchone()
    return Policy(actor, campaign)


def usage(conn, campaign_id) -> dict:
    """The resources that a campaign used, in the units of BUDGET_KEYS."""
    row = conn.execute("""
        SELECT (SELECT count(*) FROM campaign_read WHERE campaign_id = %(c)s) AS observations_read,
               (SELECT count(*) FROM job j JOIN run r ON r.id = j.run_id WHERE r.campaign_id = %(c)s) AS jobs,
               (SELECT coalesce(sum((j.result -> 'resources' ->> 'elapsed_seconds')::float8), 0)
                  FROM job j JOIN run r ON r.id = j.run_id
                  WHERE r.campaign_id = %(c)s AND jsonb_typeof(j.result -> 'resources' -> 'elapsed_seconds') = 'number')
                   AS compute_seconds_finished,
               (SELECT count(*) FROM job j JOIN run r ON r.id = j.run_id
                  WHERE r.campaign_id = %(c)s AND j.state IN ('queued', 'running')) AS jobs_waiting,
               (SELECT coalesce(sum(a.bytes), 0) FROM artifact a JOIN job j ON j.id = a.job_id JOIN run r ON r.id = j.run_id
                  WHERE r.campaign_id = %(c)s) AS storage_bytes""", {'c': campaign_id}).fetchone()
    return dict(row)


def budget_state(conn, campaign: dict, seconds_per_job: float | None) -> dict:
    """Limits, use, and remaining amount for each budget key of the campaign."""
    used = usage(conn, campaign['id'])
    compute = used['compute_seconds_finished'] + (used['jobs_waiting'] * seconds_per_job if seconds_per_job else 0.0)
    values = {'observations_read': used['observations_read'], 'jobs': used['jobs'], 'compute_seconds': compute,
              'storage_bytes': used['storage_bytes']}
    out = {}
    for key, meaning in BUDGET_KEYS.items():
        limit = campaign['budgets'].get(key)
        out[key] = {'meaning': meaning, 'limit': limit, 'used': values[key],
                    'remaining': None if limit is None else max(0, limit - values[key])}
    return out


def check_budgets(budgets: dict) -> list[str]:
    errors = [f'unknown budget "{k}". Budgets: {", ".join(BUDGET_KEYS)}' for k in budgets if k not in BUDGET_KEYS]
    errors += [f'budget "{k}" must be a whole number ≥ 0' for k, v in budgets.items()
               if k in BUDGET_KEYS and (not isinstance(v, int) or isinstance(v, bool) or v < 0)]
    return errors


class Guard:
    """The cohort guard and the observation budget for the reads of one request."""

    def __init__(self, register: CohortRegister):
        self.register = register

    def protected(self, conn, policy: Policy, observation_ids=None) -> dict:
        """Protected observations among the IDs. An agent cannot work without the register. A person can."""
        try:
            return self.register.protected(conn, observation_ids)
        except RegisterUnavailable as error:
            if policy.agent:
                raise HTTPException(503, f'the cohort guard is unavailable: {error}') from error
            return {}

    def refuse(self, conn, policy: Policy, protected: dict, operation: str):
        """Record a refused read or write of each protected observation by an agent."""
        for oid, reservation in protected.items():
            conn.execute("""INSERT INTO access_event (client_id, campaign_id, observation_id, reservation_id, outcome, operation)
                            VALUES (%s, %s, %s, %s, 'refused', %s)""",
                         [policy.actor.id, policy.campaign and policy.campaign['id'], oid, reservation, operation])

    def read(self, conn, policy: Policy, observation_ids, operation: str):
        """Check and record a read of observation data. Refuses protected observations and reads past the budget."""
        ids = [i for i in dict.fromkeys(observation_ids) if i is not None]
        if not ids:
            return
        policy.require_active(operation)
        protected = self.protected(conn, policy, ids)
        if protected:
            outcome = 'refused' if policy.agent else 'allowed'
            for oid, reservation in protected.items():
                conn.execute("""INSERT INTO access_event (client_id, campaign_id, observation_id, reservation_id, outcome, operation)
                                VALUES (%s, %s, %s, %s, %s, %s)""",
                             [policy.actor.id, policy.campaign and policy.campaign['id'], oid, reservation, outcome, operation])
            if policy.agent:
                raise HTTPException(403, f'{len(protected)} observation(s) are reserved for evaluation '
                                         f'({", ".join(sorted(set(protected.values())))}). The refusal is recorded.')
        if not policy.agent:
            return
        campaign = policy.campaign
        limit = campaign['budgets'].get('observations_read')
        with conn.transaction():
            # One campaign at a time, so that two parallel reads cannot both pass the limit.
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [str(campaign['id'])])
            new = [r['o'] for r in conn.execute("""
                SELECT o FROM unnest(%s::uuid[]) AS o
                WHERE NOT EXISTS (SELECT 1 FROM campaign_read WHERE campaign_id = %s AND observation_id = o)""",
                [ids, campaign['id']]).fetchall()]
            if not new:
                return
            if limit is not None:
                used = conn.execute('SELECT count(*) AS n FROM campaign_read WHERE campaign_id = %s',
                                    [campaign['id']]).fetchone()['n']
                if used + len(new) > limit:
                    raise HTTPException(403, f'the observation budget of campaign {campaign["name"]} is {limit}. '
                                             f'{used} are read, and this request reads {len(new)} more.')
            for oid in new:
                conn.execute('INSERT INTO campaign_read (campaign_id, observation_id, client_id) VALUES (%s, %s, %s)',
                             [campaign['id'], oid, policy.actor.id])


# ---------- Redaction for blinding ----------

def redact_observation(row: dict, policy: Policy) -> dict:
    if not policy.labels_visible and 'labels' in row:
        row['labels'] = {}
    if not policy.identity_visible:
        for key in IDENTITY_FIELDS:
            if key in row:
                row[key] = None
        if 'header' in row:
            row['header'] = {k: v for k, v in row['header'].items() if k in IDENTITY_SAFE_HEADER}
    if policy.withheld():
        row['withheld'] = policy.withheld()
    return row


def redact_error(error, policy: Policy):
    """A job error. Without identity, the message and traceback can name files, so only the kind stays."""
    if policy.identity_visible or not isinstance(error, dict):
        return error
    return {**error, 'message': None, **({'traceback': None} if 'traceback' in error else {}), 'withheld': ['identity']}


def redact_findings(findings, policy: Policy):
    """Dataset findings. Without identity, file paths become a count."""
    if policy.identity_visible or not isinstance(findings, list):
        return findings
    out = []
    for finding in findings:
        if isinstance(finding, dict) and ('path' in finding or 'paths' in finding):
            count = (1 if 'path' in finding else 0) + len(finding.get('paths') or [])
            finding = {k: v for k, v in finding.items() if k not in ('path', 'paths')}
            finding['path_count'] = count
            finding['withheld'] = ['identity']
        out.append(finding)
    return out


def redact_target(row: dict, policy: Policy) -> dict:
    if not policy.labels_visible:
        for key in ('label_class', 'planet_name', 'reference_period_days', 'relative_period_error', 'status'):
            row[key] = None
    if not policy.identity_visible:
        for key in IDENTITY_FIELDS:
            if key in row:
                row[key] = None
    return row


def redact_measurements(grouped: dict, policy: Policy) -> dict:
    if not policy.labels_visible:
        grouped = {role: items for role, items in grouped.items() if role != 'reference_comparison'}
    return grouped


def redact_result_document(result: dict | None, policy: Policy) -> dict | None:
    """A result/1 document: comparisons with known objects use labels."""
    if result is None or policy.labels_visible:
        return result
    result = dict(result)
    result['measurements'] = [m for m in result.get('measurements', []) if m.get('role') != 'reference_comparison']
    if isinstance(result.get('diagnostics'), dict):
        result['diagnostics'] = {k: v for k, v in result['diagnostics'].items() if k not in LABEL_DIAGNOSTICS}
    return result


def redact_diagnostics(diagnostics, policy: Policy):
    if policy.labels_visible or not isinstance(diagnostics, dict):
        return diagnostics
    return {k: v for k, v in diagnostics.items() if k not in LABEL_DIAGNOSTICS}


def redact_json_artifact(kind: str, document, policy: Policy):
    """search_summary and input_checks can hold label and identity values."""
    if not isinstance(document, dict):
        return document
    out = dict(document)
    if not policy.labels_visible:
        for key in LABEL_DIAGNOSTICS:
            out.pop(key, None)
    if not policy.identity_visible:
        for key in ('source_sha256', 'source_path', 'object', 'ticid'):
            out.pop(key, None)
    return out


def needs_redaction(kind: str, policy: Policy) -> bool:
    return kind in ('search_summary', 'input_checks') and bool(policy.withheld())

