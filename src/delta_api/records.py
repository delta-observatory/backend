"""Research records (ROADMAP R8): candidates, assessments, decisions, notes, and agent tasks.

The documents follow contracts/records/ in vendor/contracts/. The API checks each document against its schema,
adds the writer, the time, and the campaign, and writes typed rows. PostgreSQL keeps the record rules:
records do not change, the decision freeze, link consistency, and the campaign of each link.

Campaign rules for an agent:
- An agent writes and sees only the records of its own campaign. It writes only inside the approval.
- An agent writes judgments: decisions with judge "model", and evidence assessments with basis "model_judgment".
  Identity assessments come from worker methods or people.
- A record that refers to a value that blinding withholds, or to a protected observation, is hidden from the agent.
  A reference to a protected observation in a write is refused and recorded as an access event.
- Rule free-text/1: while blinding withholds values, an agent gets the free text of its own campaign's clients and the
  directed text of tasks. Other writers' free text is null, and the record has text_withheld true.
- An import route can give a record an import_key. A second record with the same key gives 409. Agents do not write keys.
"""
from contextlib import contextmanager
from typing import Annotated
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field
import psycopg

from . import db
from .auth import Actor
from .contracts import ValidationFailure
from .policy import Policy, policy_for, redact_observation

# Reference kind -> record_link column of the target.
REF_COLUMNS = {
    'campaign': 'ref_campaign', 'observation': 'ref_observation', 'run': 'ref_run', 'job': 'ref_job',
    'measurement': 'ref_measurement', 'artifact': 'ref_artifact', 'candidate': 'ref_candidate',
    'assessment': 'ref_assessment', 'decision': 'ref_decision', 'note': 'ref_note', 'agent_task': 'ref_agent_task',
}
# Record kinds that only the clients of their campaign see.
SCOPED = {'campaign', 'candidate', 'assessment', 'decision', 'note', 'agent_task'}
# Source record -> record_link column of the source.
SOURCE_COLUMNS = {'candidate': 'candidate_id', 'assessment': 'assessment_id', 'decision': 'decision_id',
                  'note': 'note_id', 'agent_task': 'agent_task_id'}

# The campaign, the observation, and the blinding facts of each referenced record.
REF_INFO = {
    'campaign': 'SELECT id AS campaign_id FROM campaign WHERE id = %s',
    'observation': 'SELECT NULL::uuid AS campaign_id, id AS observation_id FROM observation WHERE id = %s',
    'run': 'SELECT campaign_id FROM run WHERE id = %s',
    'job': 'SELECT r.campaign_id, j.observation_id FROM job j JOIN run r ON r.id = j.run_id WHERE j.id = %s',
    'measurement': """SELECT r.campaign_id, m.observation_id, m.role FROM measurement m JOIN job j ON j.id = m.job_id
                      JOIN run r ON r.id = j.run_id WHERE m.id = %s""",
    'artifact': """SELECT r.campaign_id, a.observation_id, a.sha256 FROM artifact a JOIN job j ON j.id = a.job_id
                   JOIN run r ON r.id = j.run_id WHERE a.id = %s""",
    'candidate': 'SELECT campaign_id FROM candidate WHERE id = %s',
    'assessment': 'SELECT c.campaign_id, a.axis FROM assessment a JOIN candidate c ON c.id = a.candidate_id WHERE a.id = %s',
    'decision': 'SELECT c.campaign_id FROM decision d JOIN candidate c ON c.id = d.candidate_id WHERE d.id = %s',
    'note': 'SELECT campaign_id FROM note WHERE id = %s',
    'agent_task': 'SELECT campaign_id FROM agent_task WHERE id = %s',
}

TASK_FAILED_REASON = 'The lease expired on every permitted attempt.'
# Platform records that a run owns. An agent sees those of its campaign and those without a campaign.
RUN_SCOPED = {'run', 'job', 'measurement', 'artifact'}
IMPORT_TABLES = {'candidate': 'candidate', 'assessment': 'assessment', 'decision': 'decision', 'note': 'note'}


@contextmanager
def record_rules():
    """Turn the database record rules into HTTP errors: RC001 and RC002 give 409, RC003 gives 422."""
    try:
        yield
    except pg_errors.UniqueViolation as error:
        raise HTTPException(409, error.diag.message_detail or error.diag.message_primary) from error
    except psycopg.Error as error:
        if error.sqlstate in ('RC001', 'RC002'):
            raise HTTPException(409, error.diag.message_primary) from error
        if error.sqlstate == 'RC003':
            raise HTTPException(422, error.diag.message_primary) from error
        raise


# ---------- Visibility ----------

def audience(campaign: dict) -> Policy:
    """The view of the agents of a campaign. A task that a person writes must be visible to them."""
    return Policy(Actor(uuid.UUID(int=0), 'campaign-agents', 'agent', frozenset(), campaign['id']), campaign)


class View:
    """What one client sees of the records: its campaign, the withheld values, and the protected observations."""

    def __init__(self, conn, pol: Policy, guard):
        pol.require_active('research records')
        self.pol = pol
        self.agent = pol.agent
        self.protected = list(guard.protected(conn, pol)) if pol.agent else []
        self.params = {'own': pol.actor.campaign_id, 'labels_hidden': not pol.labels_visible,
                       'protected': self.protected}

    def scope(self, campaign_expr: str) -> str:
        """SQL condition: the record is in the client's campaign. A person sees every campaign."""
        return f'{campaign_expr} = %(own)s' if self.agent else 'true'

    @staticmethod
    def _refers_hidden(source_column: str, id_expr: str) -> str:
        return f"""EXISTS (SELECT 1 FROM record_link hl
            LEFT JOIN measurement hm ON hm.id = hl.ref_measurement
            LEFT JOIN job hj ON hj.id = hl.ref_job
            LEFT JOIN artifact ha ON ha.id = hl.ref_artifact
            LEFT JOIN assessment hs ON hs.id = hl.ref_assessment
            WHERE hl.{source_column} = {id_expr}
              AND ((%(labels_hidden)s AND (hm.role = 'reference_comparison' OR hs.axis = 'identity'))
                   OR coalesce(hl.ref_observation, hm.observation_id, hj.observation_id, ha.observation_id)
                      = ANY(%(protected)s::uuid[])))"""

    def candidate_hidden(self, id_expr: str) -> str:
        """SQL condition: the candidate refers to a withheld value or a protected observation."""
        if not self.agent:
            return 'false'
        return f"""({self._refers_hidden('candidate_id', id_expr)}
            OR EXISTS (SELECT 1 FROM candidate_observation hco WHERE hco.candidate_id = {id_expr}
                       AND hco.observation_id = ANY(%(protected)s::uuid[])))"""

    def hidden(self, kind: str, alias: str) -> str:
        """SQL condition: the record with the table alias is hidden from the client."""
        if not self.agent:
            return 'false'
        if kind == 'candidate':
            return self.candidate_hidden(f'{alias}.id')
        own = self._refers_hidden(SOURCE_COLUMNS[kind], f'{alias}.id')
        if kind == 'assessment':
            return f"({own} OR {self.candidate_hidden(f'{alias}.candidate_id')} OR (%(labels_hidden)s AND {alias}.axis = 'identity'))"
        if kind == 'decision':
            return f"({own} OR {self.candidate_hidden(f'{alias}.candidate_id')})"
        return own

    def withheld(self) -> list[str]:
        return self.pol.withheld()

    def text_hidden(self, writer_campaign_id) -> bool:
        """Rule free-text/1: True when the client does not get the free text of this writer."""
        return not self.pol.text_visible(writer_campaign_id)

    def listed(self, items: list[dict]) -> list[str]:
        """The withheld groups of a response, with "text" when rule free-text/1 withheld text in an item."""
        return self.withheld() + (['text'] if any(i.get('text_withheld') for i in items) else [])


def check_refs(conn, viewer: Policy, refs: list[dict], loc: list, campaign_id) -> tuple[list[dict], list]:
    """Check the references of a new record. Return the errors and the observations that the references touch.

    Each platform record must exist. A record of a campaign refers only to records of the same campaign or to
    records without a campaign. The viewer must see each referenced record.
    """
    errors, observations = [], []
    for i, ref in enumerate(refs):
        at = [*loc, i]
        kind = ref['kind']
        if kind == 'external':
            continue
        row = conn.execute(REF_INFO[kind], [ref['id']]).fetchone()
        if row is None:
            errors.append({'loc': at, 'msg': f'{kind} {ref["id"]} not found'})
            continue
        target_campaign = row['campaign_id']
        if viewer.agent and kind in SCOPED and target_campaign != viewer.actor.campaign_id:
            errors.append({'loc': at, 'msg': f'{kind} {ref["id"]} not found'})
            continue
        # An agent does not see the runs of another campaign. They are "not found", as in the run views.
        if viewer.actor.kind == 'agent' and viewer.actor.id.int != 0 and kind in RUN_SCOPED \
                and target_campaign is not None and target_campaign != viewer.actor.campaign_id:
            errors.append({'loc': at, 'msg': f'{kind} {ref["id"]} not found'})
            continue
        if campaign_id is not None and target_campaign is not None and target_campaign != campaign_id:
            errors.append({'loc': at, 'msg': f'{kind} {ref["id"]} belongs to another campaign'})
            continue
        if not viewer.labels_visible and (row.get('role') == 'reference_comparison' or row.get('axis') == 'identity'):
            errors.append({'loc': at, 'msg': f'{kind} {ref["id"]} holds known-object labels, which the campaign withholds'})
            continue
        if kind == 'artifact' and 'sha256' in ref and ref['sha256'] != row['sha256']:
            errors.append({'loc': [*at, 'sha256'], 'msg': f'the stored artifact has SHA-256 {row["sha256"]}'})
            continue
        if row.get('observation_id') is not None:
            observations.append((row['observation_id'], at))
    return errors, observations


def guard_writes(conn, guard, viewer: Policy, actor: Actor, observations: list, operation: str):
    """Refuse references to protected observations for an agent and for the audience of a campaign.
    A refusal of an agent's write is recorded as an access event."""
    if not viewer.agent or not observations:
        return
    protected = guard.protected(conn, viewer, [o for o, _ in observations])
    if not protected:
        return
    if actor.kind == 'agent':
        for oid, reservation in protected.items():
            conn.execute("""INSERT INTO access_event (client_id, campaign_id, observation_id, reservation_id, outcome, operation)
                            VALUES (%s, %s, %s, %s, 'refused', %s)""",
                         [actor.id, viewer.campaign['id'], oid, reservation, operation])
    raise ValidationFailure([{'loc': at, 'msg': f'observation is reserved for evaluation ({protected[o]}).'
                                                + (' The refusal is recorded.' if actor.kind == 'agent' else '')}
                             for o, at in observations if o in protected])


def insert_links(conn, source: str, source_id, role: str, refs: list[dict]):
    column = SOURCE_COLUMNS[source]
    for position, ref in enumerate(refs):
        if ref['kind'] == 'external':
            conn.execute(f"""INSERT INTO record_link (role, position, {column}, external_uri, external_sha256, external_bytes,
                                                      external_description)
                             VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                         [role, position, source_id, ref['uri'], ref['sha256'], ref['bytes'], ref.get('description')])
        else:
            conn.execute(f'INSERT INTO record_link (role, position, {column}, {REF_COLUMNS[ref["kind"]]}) VALUES (%s, %s, %s, %s)',
                         [role, position, source_id, ref['id']])


def links_of(conn, source: str, ids: list) -> dict:
    """References of each source record, by source ID and role, in the written order."""
    if not ids:
        return {}
    column = SOURCE_COLUMNS[source]
    rows = conn.execute(f"""
        SELECT l.*, a.sha256 AS artifact_sha256 FROM record_link l LEFT JOIN artifact a ON a.id = l.ref_artifact
        WHERE l.{column} = ANY(%s) ORDER BY l.role, l.position""", [ids]).fetchall()
    out: dict = {}
    for link in rows:
        out.setdefault(link[column], {}).setdefault(link['role'], []).append(ref_of(link))
    return out


def ref_of(link: dict) -> dict:
    if link['external_uri'] is not None:
        ref = {'kind': 'external', 'uri': link['external_uri'], 'sha256': link['external_sha256'],
               'bytes': link['external_bytes']}
        if link['external_description'] is not None:
            ref['description'] = link['external_description']
        return ref
    for kind, column in REF_COLUMNS.items():
        if link[column] is not None:
            ref = {'kind': kind, 'id': link[column]}
            if kind == 'artifact':
                ref['sha256'] = link['artifact_sha256']
            return ref
    raise AssertionError('a link without a target')


def actor_of(row: dict) -> dict:
    return {'id': row['created_by'], 'name': row['actor_name'], 'kind': row['actor_kind']}


# ---------- Serialization ----------

CANDIDATE_SELECT = """
    SELECT c.*, s.evidence_status, s.evidence_assessment_id, s.evidence_basis,
           identity_s.identity_status, identity_s.identity_assessment_id, identity_s.identity_basis,
           w.name AS actor_name, w.kind AS actor_kind, w.campaign_id AS actor_campaign_id,
           ARRAY(SELECT observation_id FROM candidate_observation co WHERE co.candidate_id = c.id
                 ORDER BY observation_id) AS observation_ids
    FROM candidate c JOIN candidate_status stored ON stored.candidate_id = c.id
    LEFT JOIN LATERAL (
        SELECT stored.evidence_status, stored.evidence_assessment_id, stored.evidence_basis
        WHERE NOT EXISTS (SELECT 1 FROM assessment status_source
                          WHERE status_source.id = stored.evidence_assessment_id AND {evidence_hidden})
    ) s ON true
    LEFT JOIN LATERAL (
        SELECT stored.identity_status, stored.identity_assessment_id, stored.identity_basis
        WHERE NOT EXISTS (SELECT 1 FROM assessment status_source
                          WHERE status_source.id = stored.identity_assessment_id AND {identity_hidden})
    ) identity_s ON true
    JOIN client w ON w.id = c.created_by"""
ASSESSMENT_SELECT = """
    SELECT a.*, c.campaign_id, w.name AS actor_name, w.kind AS actor_kind, w.campaign_id AS actor_campaign_id
    FROM assessment a JOIN candidate c ON c.id = a.candidate_id JOIN client w ON w.id = a.created_by"""
DECISION_SELECT = """
    SELECT d.*, c.campaign_id, w.name AS actor_name, w.kind AS actor_kind, w.campaign_id AS actor_campaign_id
    FROM decision d JOIN candidate c ON c.id = d.candidate_id JOIN client w ON w.id = d.created_by"""
NOTE_SELECT = """
    SELECT n.*, w.name AS actor_name, w.kind AS actor_kind, w.campaign_id AS actor_campaign_id
    FROM note n JOIN client w ON w.id = n.created_by"""
TASK_SELECT = """
    SELECT t.*, w.name AS actor_name, w.kind AS actor_kind, h.name AS claimed_by_name,
           h.campaign_id AS claimed_by_campaign_id, k.campaign_id AS cancelled_by_campaign_id,
           (t.state = 'claimed' AND t.lease_expires_at < now()) AS lease_expired
    FROM agent_task t JOIN client w ON w.id = t.created_by LEFT JOIN client h ON h.id = t.claimed_by
    LEFT JOIN client k ON k.id = t.cancelled_by"""


def candidate_select(view: View) -> str:
    """Mask a status whose source assessment is hidden, including in list filters and counts."""
    hidden = view.hidden('assessment', 'status_source')
    return CANDIDATE_SELECT.format(evidence_hidden=hidden, identity_hidden=hidden)


def masked(refs: list[dict], hidden: bool) -> list[dict]:
    """External references without their URI and description, when rule free-text/1 withholds the writer's text."""
    if not hidden:
        return refs
    return [{**r, 'uri': None, **({'description': None} if 'description' in r else {})} if r['kind'] == 'external' else r
            for r in refs]


def text(value, hidden: bool):
    return None if hidden else value


def candidate_out(row: dict, links: dict, view: View) -> dict:
    labels_visible = view.pol.labels_visible
    hidden = view.text_hidden(row['actor_campaign_id'])
    return {
        'contract': 'candidate/1', 'id': row['id'], 'campaign_id': row['campaign_id'], 'label': text(row['label'], hidden),
        'kind': row['kind'], 'tier': row['tier'], 'summary': text(row['summary'], hidden),
        'observation_ids': row['observation_ids'],
        'evidence': masked(links.get(row['id'], {}).get('evidence', []), hidden),
        'import_key': row['import_key'], 'text_withheld': hidden,
        'status_rule': 'candidate-status/1',
        'evidence_status': {'status': row['evidence_status'], 'assessment_id': row['evidence_assessment_id'],
                            'basis': row['evidence_basis']} if row['evidence_status'] is not None else None,
        # Identity comparison uses known-object labels.
        'identity_status': {'status': row['identity_status'], 'assessment_id': row['identity_assessment_id'],
                            'basis': row['identity_basis']} if labels_visible and row['identity_status'] is not None else None,
        'actor': actor_of(row), 'created_at': row['created_at'], 'withheld': view.withheld(),
    }


def assessment_out(row: dict, links: dict, view: View) -> dict:
    hidden = view.text_hidden(row['actor_campaign_id'])
    return {
        'contract': 'assessment/1', 'id': row['id'], 'candidate_id': row['candidate_id'], 'campaign_id': row['campaign_id'],
        'axis': row['axis'], 'status': row['status'], 'basis': row['basis'],
        'method': {'id': row['method_id'], 'version': row['method_version']} if row['method_id'] else None,
        'decision_id': row['decision_id'], 'reason': text(row['reason'], hidden), 'quality_flags': list(row['quality_flags']),
        'evidence': masked(links.get(row['id'], {}).get('evidence', []), hidden), 'supersedes': row['supersedes'],
        'import_key': row['import_key'], 'text_withheld': hidden, 'actor': actor_of(row), 'created_at': row['created_at'],
    }


def decision_out(row: dict, links: dict, view: View) -> dict:
    hidden = view.text_hidden(row['actor_campaign_id'])
    if row['judge'] == 'person':
        model = None
    elif row['model_reason'] is not None:
        model = {'kind': 'unavailable', 'reason': row['model_reason']}
    else:
        model = {'kind': 'identified', 'provider': row['model_provider'], 'id': row['model_id']}
    return {
        'contract': 'decision/1', 'id': row['id'], 'candidate_id': row['candidate_id'], 'campaign_id': row['campaign_id'],
        'assessment_id': row['assessment_id'],
        'question': {'id': row['question_id'], 'version': row['question_version']},
        'mode': row['mode'], 'judge': row['judge'], 'model_judgment': row['judge'] == 'model', 'model': model,
        'packet': ({'kind': 'sha256', 'sha256': row['packet_sha256']} if row['packet_sha256']
                   else {'kind': 'unavailable', 'reason': row['packet_reason']}),
        'policy': ({'kind': 'identified', 'id': row['policy_id'], 'version': row['policy_version']} if row['policy_id']
                   else {'kind': 'unavailable', 'reason': row['policy_reason']}),
        'judged_evidence_status': row['judged_evidence_status'], 'action': row['action'],
        'reason': text(row['reason'], hidden), 'evidence': masked(links.get(row['id'], {}).get('evidence', []), hidden),
        'response': text(row['response'], hidden), 'scores': row['scores'],
        'withheld_from_writer': list(row['withheld']), 'supersedes': row['supersedes'],
        'import_key': row['import_key'], 'text_withheld': hidden, 'actor': actor_of(row), 'created_at': row['created_at'],
    }


def note_out(row: dict, links: dict, view: View) -> dict:
    hidden = view.text_hidden(row['actor_campaign_id'])
    return {'contract': 'note/1', 'id': row['id'], 'campaign_id': row['campaign_id'], 'body': text(row['body'], hidden),
            'subjects': masked(links.get(row['id'], {}).get('subject', []), hidden), 'import_key': row['import_key'],
            'text_withheld': hidden, 'actor': actor_of(row), 'created_at': row['created_at']}


def task_out(row: dict, links: dict, view: View) -> dict:
    task_links = links.get(row['id'], {})
    # The title, instructions, and inputs are directed text: the API gives them to the agents of the campaign.
    # The report summary and outputs come from the claimant. A cancellation reason comes from the client that cancelled.
    report_hidden = row['claimed_by'] is not None and view.text_hidden(row['claimed_by_campaign_id'])
    if row['state'] == 'cancelled':
        reason_hidden = row['cancelled_by'] is not None and view.text_hidden(row['cancelled_by_campaign_id'])
    else:
        reason_hidden = report_hidden and row['reason'] != TASK_FAILED_REASON
    hidden = (report_hidden and (row['summary'] is not None or bool(task_links.get('output')))) or \
        (reason_hidden and row['reason'] is not None)
    return {
        'contract': 'agent-task/1', 'id': row['id'], 'campaign_id': row['campaign_id'], 'kind': row['kind'],
        'title': row['title'], 'instructions': row['instructions'], 'priority': row['priority'],
        'lease_seconds': row['lease_seconds'], 'max_attempts': row['max_attempts'], 'state': row['state'],
        'attempts': row['attempts'],
        'claimed_by': {'id': row['claimed_by'], 'name': row['claimed_by_name']} if row['claimed_by'] else None,
        'claimed_at': row['claimed_at'], 'lease_expires_at': row['lease_expires_at'], 'lease_expired': row['lease_expired'],
        'inputs': task_links.get('input', []), 'outputs': masked(task_links.get('output', []), report_hidden),
        'summary': text(row['summary'], report_hidden), 'reason': text(row['reason'], reason_hidden),
        'cancelled_by': row['cancelled_by'], 'text_withheld': hidden,
        'actor': actor_of(row), 'created_at': row['created_at'], 'finished_at': row['finished_at'],
    }


# ---------- Routes ----------

class ClaimRequest(BaseModel):
    kinds: list[str] | None = Field(default=None, description='Claim only tasks of these kinds.')


class LeaseRequest(BaseModel):
    lease_id: uuid.UUID = Field(description='The lease_id of the claim.')


class ReportRequest(LeaseRequest):
    report: dict = Field(description='An agent-task-report/1 document.')


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


def router(*, connection, need, guard, contracts) -> APIRouter:
    api = APIRouter()
    Conn = Annotated[object, Depends(connection)]
    Reader = Annotated[Actor, Depends(need('read'))]
    RecordWriter = Annotated[Actor, Depends(need('record.write'))]
    Document = Annotated[dict, Body(description='The record document. contracts/records/ defines it.')]
    Page = {'limit': Query(100, ge=1, le=1000), 'offset': Query(0, ge=0)}

    def write_campaign(conn, actor: Actor, pol: Policy, campaign_id, operation: str, *, required=False):
        """The campaign of a new record. An agent writes only in its own campaign, inside the approval."""
        if pol.agent:
            if campaign_id is not None and campaign_id != actor.campaign_id:
                raise HTTPException(404, 'campaign not found')
            pol.require_active(operation)
            return pol.campaign
        if campaign_id is None:
            if required:
                raise ValidationFailure([{'loc': ['query', 'campaign_id'], 'msg': 'campaign_id is required'}])
            return None
        row = conn.execute('SELECT * FROM campaign WHERE id = %s', [campaign_id]).fetchone()
        if row is None:
            raise HTTPException(404, 'campaign not found')
        return row

    def read_scope(view: View, campaign_id) -> tuple[str, dict]:
        """A campaign filter for a list. An agent lists only its own campaign."""
        if campaign_id is None:
            return '', {}
        if view.agent and campaign_id != view.pol.actor.campaign_id:
            raise HTTPException(404, 'campaign not found')
        return ' AND {alias}.campaign_id = %(campaign)s', {'campaign': campaign_id}

    def one(conn, view: View, kind: str, select: str, alias: str, record_id, campaign_expr: str) -> dict:
        row = conn.execute(f"""{select} WHERE {alias}.id = %(id)s AND {view.scope(campaign_expr)}
                               AND NOT {view.hidden(kind, alias)}""", {**view.params, 'id': record_id}).fetchone()
        if row is None:
            raise HTTPException(404, f'{kind.replace("_", " ")} not found')
        return row

    def page(conn, view: View, kind: str, select: str, alias: str, campaign_expr: str, where: list[str], args: dict,
             limit: int, offset: int, order: str) -> tuple[int, list]:
        clause = ' AND '.join([view.scope(campaign_expr), f'NOT {view.hidden(kind, alias)}', *where])
        params = {**view.params, **args, 'limit': limit, 'offset': offset}
        total = conn.execute(f'SELECT count(*) AS n FROM ({select} WHERE {clause}) x', params).fetchone()['n']
        rows = conn.execute(f'{select} WHERE {clause} ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s', params).fetchall()
        return total, rows

    def visible_candidate(conn, view: View, candidate_id, loc: list) -> dict:
        row = conn.execute(f"""SELECT * FROM candidate c WHERE c.id = %(id)s AND {view.scope('c.campaign_id')}
                               AND NOT {view.hidden('candidate', 'c')}""", {**view.params, 'id': candidate_id}).fetchone()
        if row is None:
            raise ValidationFailure([{'loc': loc, 'msg': f'candidate {candidate_id} not found'}])
        return row

    def checked(conn, actor, viewer: Policy, campaign_id, groups: list[tuple[list, list]], operation: str):
        """Check groups of (references, loc). Raise one 422 with every error."""
        errors, observations = [], []
        for refs, loc in groups:
            e, o = check_refs(conn, viewer, refs, loc, campaign_id)
            errors += e
            observations += o
        if errors:
            raise ValidationFailure(errors)
        guard_writes(conn, guard, viewer, actor, observations, operation)

    def import_key(conn, actor: Actor, kind: str, doc: dict):
        """An import key is for import routes. A second record with the same key gives 409 with the existing ID."""
        key = doc.get('import_key')
        if key is None:
            return None
        if actor.kind == 'agent':
            raise HTTPException(403, 'agents do not write import keys')
        existing = conn.execute(f'SELECT id FROM {IMPORT_TABLES[kind]} WHERE import_key = %s', [key]).fetchone()
        if existing is not None:
            raise HTTPException(409, {'msg': f'a {kind} with import key {key} exists', 'existing_id': str(existing['id'])})
        return key

    # Candidates --------------------------------------------------------

    @api.post('/candidates', status_code=201)
    def create_candidate(document: Document, conn: Conn, actor: RecordWriter,
                         campaign_id: uuid.UUID | None = Query(None, description='The campaign. An agent writes in its own campaign.')):
        """Write a candidate/1 record. Its evidence status starts as unreviewed and its identity status as unchecked."""
        doc = contracts.record('candidate/1', document)
        pol = policy_for(conn, actor)
        key = import_key(conn, actor, 'candidate', doc)
        campaign = write_campaign(conn, actor, pol, campaign_id, 'a candidate')
        cid = campaign and campaign['id']
        found = {r['id'] for r in conn.execute('SELECT id FROM observation WHERE id = ANY(%s::uuid[])',
                                               [doc['observation_ids']]).fetchall()}
        errors = [{'loc': ['body', 'observation_ids', i], 'msg': 'observation not found'}
                  for i, o in enumerate(doc['observation_ids']) if uuid.UUID(o) not in found]
        if errors:
            raise ValidationFailure(errors)
        observations = [(uuid.UUID(o), ['body', 'observation_ids', i]) for i, o in enumerate(doc['observation_ids'])]
        errors, more = check_refs(conn, pol, doc['evidence'], ['body', 'evidence'], cid)
        if errors:
            raise ValidationFailure(errors)
        guard_writes(conn, guard, pol, actor, observations + more, 'candidate write')
        with record_rules(), conn.transaction():
            new_id = conn.execute("""INSERT INTO candidate (campaign_id, label, kind, tier, summary, import_key, created_by)
                                     VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                                  [cid, doc.get('label'), doc['kind'], doc['tier'], doc['summary'], key,
                                   actor.id]).fetchone()['id']
            with conn.cursor() as cur:
                cur.executemany('INSERT INTO candidate_observation VALUES (%s, %s)', [(new_id, o) for o in doc['observation_ids']])
            insert_links(conn, 'candidate', new_id, 'evidence', doc['evidence'])
        return candidate_record(conn, View(conn, pol, guard), new_id)

    @api.get('/candidates')
    def list_candidates(conn: Conn, actor: Reader, campaign_id: uuid.UUID | None = None,
                        kind: str | None = Query(None, pattern=r'^(transit_signal|moving_object)$'),
                        tier: str | None = Query(None, pattern=r'^(platform|lab)$'),
                        evidence_status: str | None = Query(None, pattern=r'^(unreviewed|supported|ambiguous|rejected)$'),
                        identity_status: str | None = Query(None, pattern=r'^(unchecked|matched|unmatched|ambiguous)$'),
                        observation_id: uuid.UUID | None = None,
                        limit: int = Page['limit'], offset: int = Page['offset']):
        """Candidates with their current statuses (rule candidate-status/1), newest first.
        An agent sees the candidates of its campaign that refer to no withheld value and no protected observation."""
        view = View(conn, policy_for(conn, actor), guard)
        if identity_status is not None and not view.pol.labels_visible:
            raise ValidationFailure([{'loc': ['query', 'identity_status'], 'msg': 'the campaign withholds known-object labels'}])
        scope, args = read_scope(view, campaign_id)
        where = [scope.format(alias='c').removeprefix(' AND ')] if scope else []
        for column, value in (('c.kind', kind), ('c.tier', tier), ('s.evidence_status', evidence_status),
                              ('identity_s.identity_status', identity_status)):
            if value is not None:
                key = column.split('.')[1]
                where.append(f'{column} = %({key})s')
                args[key] = value
        if observation_id is not None:
            where.append('EXISTS (SELECT 1 FROM candidate_observation f WHERE f.candidate_id = c.id AND f.observation_id = %(obs)s)')
            args['obs'] = observation_id
        total, rows = page(conn, view, 'candidate', candidate_select(view), 'c', 'c.campaign_id', where, args, limit, offset,
                           'c.created_at DESC, c.id')
        links = links_of(conn, 'candidate', [r['id'] for r in rows])
        items = [candidate_out(r, links, view) for r in rows]
        return {'total': total, 'limit': limit, 'offset': offset, 'withheld': view.listed(items), 'items': items}

    @api.get('/candidates/{candidate_id}')
    def get_candidate(candidate_id: uuid.UUID, conn: Conn, actor: Reader):
        """One candidate with its observations, its resolved evidence, its assessments, decisions, and notes.
        The measurement values and observations count as observation reads for an agent's budget."""
        pol = policy_for(conn, actor)
        out = candidate_record(conn, View(conn, pol, guard), candidate_id)
        resolved, touched = resolve(conn, out['evidence'])
        guard.read(conn, pol, [uuid.UUID(str(o)) for o in [*out['observation_ids'], *touched]], 'candidate detail')
        observations = conn.execute("""SELECT id, dataset_id, target_id, relative_path, sha256, sector, cadence_seconds,
                                              time_start, time_stop, time_scale, time_reference, state
                                       FROM observation WHERE id = ANY(%s) ORDER BY id""", [out['observation_ids']]).fetchall()
        out['observations'] = [redact_observation(o, pol) for o in observations]
        out['evidence'] = [{**ref, 'record': resolved.get((ref['kind'], str(ref.get('id'))))} for ref in out['evidence']]
        out['assessments'] = list_assessments(conn, actor, candidate_id=candidate_id, campaign_id=None, axis=None,
                                              limit=1000, offset=0)['items']
        out['decisions'] = list_decisions(conn, actor, candidate_id=candidate_id, campaign_id=None, action=None,
                                          limit=1000, offset=0)['items']
        out['notes'] = list_notes(conn, actor, campaign_id=None, subject_kind='candidate', subject_id=str(candidate_id),
                                  limit=1000, offset=0)['items']
        out['withheld'] = view_withheld = list(out['withheld'])
        if any(r.get('text_withheld') for r in [out, *out['assessments'], *out['decisions'], *out['notes']]) \
                and 'text' not in view_withheld:
            view_withheld.append('text')
        return out

    def candidate_record(conn, view: View, candidate_id) -> dict:
        row = one(conn, view, 'candidate', candidate_select(view), 'c', candidate_id, 'c.campaign_id')
        return candidate_out(row, links_of(conn, 'candidate', [row['id']]), view)

    def resolve(conn, refs: list[dict]) -> tuple[dict, list]:
        """Measurement values, artifact metadata, and job states of the references, and their observations."""
        resolved, touched = {}, []
        ids = lambda kind: [str(r['id']) for r in refs if r['kind'] == kind]  # noqa: E731
        for m in conn.execute('SELECT * FROM measurement WHERE id = ANY(%s::bigint[])', [ids('measurement')]).fetchall():
            resolved[('measurement', str(m['id']))] = {'id': m['id'], 'job_id': m['job_id'], 'observation_id': m['observation_id'],
                                                  **db.measurement(m)}
            touched.append(m['observation_id'])
        for a in conn.execute('SELECT * FROM artifact WHERE id = ANY(%s::uuid[])', [ids('artifact')]).fetchall():
            resolved[('artifact', str(a['id']))] = db.artifact(a)
            touched.append(a['observation_id'])
        for j in conn.execute("""SELECT id, run_id, observation_id, state, document -> 'method' AS method FROM job
                                 WHERE id = ANY(%s::uuid[])""", [ids('job')]).fetchall():
            resolved[('job', str(j['id']))] = j
            touched.append(j['observation_id'])
        touched += ids('observation')
        return resolved, touched

    # Assessments --------------------------------------------------------

    @api.post('/assessments', status_code=201)
    def create_assessment(document: Document, conn: Conn, actor: RecordWriter):
        """Write an assessment/1 record. The newest assessment on an axis gives the current status of the candidate.
        An agent writes evidence assessments with basis model_judgment only. Identity assessments come from worker
        methods or people."""
        doc = contracts.record('assessment/1', document)
        pol = policy_for(conn, actor)
        if pol.agent:
            if doc['axis'] == 'identity':
                raise HTTPException(403, 'identity comparison is a worker method, not an agent role')
            if doc['basis'] != 'model_judgment':
                raise HTTPException(403, 'an agent writes its judgment as a decision and cites it with basis model_judgment')
            pol.require_active('an assessment')
        key = import_key(conn, actor, 'assessment', doc)
        view = View(conn, pol, guard)
        candidate = visible_candidate(conn, view, doc['candidate_id'], ['body', 'candidate_id'])
        cid = candidate['campaign_id']
        groups = [([{'kind': kind, 'id': doc[key]}], ['body', key])
                  for key, kind in (('decision_id', 'decision'), ('supersedes', 'assessment')) if doc.get(key)]
        checked(conn, actor, pol, cid, groups + [(doc.get('evidence', []), ['body', 'evidence'])], 'assessment write')
        with record_rules(), conn.transaction():
            new_id = conn.execute("""
                INSERT INTO assessment (candidate_id, axis, status, basis, method_id, method_version, decision_id, reason,
                                        quality_flags, supersedes, import_key, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [doc['candidate_id'], doc['axis'], doc['status'], doc['basis'], (doc.get('method') or {}).get('id'),
                 (doc.get('method') or {}).get('version'), doc.get('decision_id'), doc['reason'],
                 doc.get('quality_flags', []), doc.get('supersedes'), key, actor.id]).fetchone()['id']
            insert_links(conn, 'assessment', new_id, 'evidence', doc.get('evidence', []))
        return get_assessment(new_id, conn, actor)

    @api.get('/assessments')
    def list_assessments(conn: Conn, actor: Reader, candidate_id: uuid.UUID | None = None,
                         campaign_id: uuid.UUID | None = None,
                         axis: str | None = Query(None, pattern=r'^(evidence|identity)$'),
                         limit: int = Page['limit'], offset: int = Page['offset']):
        """Assessments, newest first. A campaign that withholds labels hides identity assessments from its agents."""
        view = View(conn, policy_for(conn, actor), guard)
        if axis == 'identity' and not view.pol.labels_visible:
            raise ValidationFailure([{'loc': ['query', 'axis'], 'msg': 'the campaign withholds known-object labels'}])
        scope, args = read_scope(view, campaign_id)
        where = [scope.format(alias='c').removeprefix(' AND ')] if scope else []
        if candidate_id is not None:
            where.append('a.candidate_id = %(candidate)s')
            args['candidate'] = candidate_id
        if axis is not None:
            where.append('a.axis = %(axis)s')
            args['axis'] = axis
        total, rows = page(conn, view, 'assessment', ASSESSMENT_SELECT, 'a', 'c.campaign_id', where, args, limit, offset,
                           'a.seq DESC')
        links = links_of(conn, 'assessment', [r['id'] for r in rows])
        items = [assessment_out(r, links, view) for r in rows]
        return {'total': total, 'limit': limit, 'offset': offset, 'withheld': view.listed(items), 'items': items}

    @api.get('/assessments/{assessment_id}')
    def get_assessment(assessment_id: uuid.UUID, conn: Conn, actor: Reader):
        view = View(conn, policy_for(conn, actor), guard)
        row = one(conn, view, 'assessment', ASSESSMENT_SELECT, 'a', assessment_id, 'c.campaign_id')
        return assessment_out(row, links_of(conn, 'assessment', [row['id']]), view)

    # Decisions ----------------------------------------------------------

    @api.post('/decisions', status_code=201)
    def create_decision(document: Document, conn: Conn, actor: RecordWriter):
        """Write a decision/1 record: one advisory judgment about a candidate. A decision sets no status.
        An agent writes model judgments only. The record keeps the values that blinding withheld from the writer."""
        doc = contracts.record('decision/1', document)
        pol = policy_for(conn, actor)
        if pol.agent:
            if doc['judge'] != 'model':
                raise HTTPException(403, 'an agent writes model judgments: judge must be "model"')
            pol.require_active('a decision')
        key = import_key(conn, actor, 'decision', doc)
        view = View(conn, pol, guard)
        candidate = visible_candidate(conn, view, doc['candidate_id'], ['body', 'candidate_id'])
        groups = [([{'kind': kind, 'id': doc[key]}], ['body', key])
                  for key, kind in (('assessment_id', 'assessment'), ('supersedes', 'decision')) if doc.get(key)]
        checked(conn, actor, pol, candidate['campaign_id'], groups + [(doc['evidence'], ['body', 'evidence'])],
                'decision write')
        model, packet, policy = doc.get('model') or {}, doc['packet'], doc['policy']
        with record_rules(), conn.transaction():
            new_id = conn.execute("""
                INSERT INTO decision (candidate_id, assessment_id, question_id, question_version, mode, judge,
                                      model_provider, model_id, model_reason, packet_sha256, packet_reason,
                                      policy_id, policy_version, policy_reason, judged_evidence_status, action, reason,
                                      response, scores, withheld, supersedes, import_key, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id""",
                [doc['candidate_id'], doc.get('assessment_id'), doc['question']['id'], doc['question']['version'],
                 doc['mode'], doc['judge'], model.get('provider'), model.get('id'), model.get('reason'),
                 packet.get('sha256'), packet.get('reason'), policy.get('id'), policy.get('version'), policy.get('reason'),
                 doc['judged_evidence_status'], doc['action'], doc['reason'],
                 Jsonb(doc['response']) if 'response' in doc else None, Jsonb(doc['scores']) if 'scores' in doc else None,
                 pol.withheld(), doc.get('supersedes'), key, actor.id]).fetchone()['id']
            insert_links(conn, 'decision', new_id, 'evidence', doc['evidence'])
        return get_decision(new_id, conn, actor)

    @api.get('/decisions')
    def list_decisions(conn: Conn, actor: Reader, candidate_id: uuid.UUID | None = None,
                       campaign_id: uuid.UUID | None = None,
                       action: str | None = Query(None, pattern=r'^[a-z_]+$'),
                       limit: int = Page['limit'], offset: int = Page['offset']):
        """Decisions, newest first."""
        view = View(conn, policy_for(conn, actor), guard)
        scope, args = read_scope(view, campaign_id)
        where = [scope.format(alias='c').removeprefix(' AND ')] if scope else []
        for column, key, value in (('d.candidate_id', 'candidate', candidate_id), ('d.action', 'action', action)):
            if value is not None:
                where.append(f'{column} = %({key})s')
                args[key] = value
        total, rows = page(conn, view, 'decision', DECISION_SELECT, 'd', 'c.campaign_id', where, args, limit, offset,
                           'd.created_at DESC, d.id')
        links = links_of(conn, 'decision', [r['id'] for r in rows])
        items = [decision_out(r, links, view) for r in rows]
        return {'total': total, 'limit': limit, 'offset': offset, 'withheld': view.listed(items), 'items': items}

    @api.get('/decisions/{decision_id}')
    def get_decision(decision_id: uuid.UUID, conn: Conn, actor: Reader):
        view = View(conn, policy_for(conn, actor), guard)
        row = one(conn, view, 'decision', DECISION_SELECT, 'd', decision_id, 'c.campaign_id')
        return decision_out(row, links_of(conn, 'decision', [row['id']]), view)

    # Notes --------------------------------------------------------------

    @api.post('/notes', status_code=201)
    def create_note(document: Document, conn: Conn, actor: RecordWriter):
        """Write a note/1 record about one or more records. The note takes the campaign of its subjects.
        An agent writes notes in its own campaign until the campaign closes."""
        doc = contracts.record('note/1', document)
        pol = policy_for(conn, actor)
        if pol.agent:
            pol.require_active('a note')
        key = import_key(conn, actor, 'note', doc)
        subjects = doc['subjects']
        campaigns = {r['c'] for r in conn.execute(
            'SELECT record_campaign(k, i) AS c FROM unnest(%s::text[], %s::text[]) AS s (k, i)',
            [[s['kind'] for s in subjects if s['kind'] != 'external'],
             [str(s['id']) for s in subjects if s['kind'] != 'external']]).fetchall()} - {None}
        if len(campaigns) > 1:
            raise ValidationFailure([{'loc': ['body', 'subjects'], 'msg': 'the subjects belong to more than one campaign'}])
        cid = pol.campaign['id'] if pol.agent else next(iter(campaigns), None)
        checked(conn, actor, pol, cid, [(subjects, ['body', 'subjects'])], 'note write')
        with record_rules(), conn.transaction():
            new_id = conn.execute('INSERT INTO note (campaign_id, body, import_key, created_by) VALUES (%s, %s, %s, %s) RETURNING id',
                                  [cid, doc['body'], key, actor.id]).fetchone()['id']
            insert_links(conn, 'note', new_id, 'subject', subjects)
        return get_note(new_id, conn, actor)

    @api.get('/notes')
    def list_notes(conn: Conn, actor: Reader, campaign_id: uuid.UUID | None = None,
                   subject_kind: str | None = Query(None, pattern='^(' + '|'.join(REF_COLUMNS) + ')$'),
                   subject_id: str | None = Query(None, max_length=64),
                   limit: int = Page['limit'], offset: int = Page['offset']):
        """Notes, newest first. subject_kind and subject_id select the notes about one record."""
        view = View(conn, policy_for(conn, actor), guard)
        scope, args = read_scope(view, campaign_id)
        where = [scope.format(alias='n').removeprefix(' AND ')] if scope else []
        if (subject_kind is None) != (subject_id is None):
            raise ValidationFailure([{'loc': ['query', 'subject_id'], 'msg': 'subject_kind and subject_id go together'}])
        if subject_kind is not None:
            where.append(f'EXISTS (SELECT 1 FROM record_link f WHERE f.note_id = n.id '
                         f'AND f.{REF_COLUMNS[subject_kind]}::text = %(subject)s)')
            args['subject'] = str(subject_id)
        total, rows = page(conn, view, 'note', NOTE_SELECT, 'n', 'n.campaign_id', where, args, limit, offset,
                           'n.created_at DESC, n.id')
        links = links_of(conn, 'note', [r['id'] for r in rows])
        items = [note_out(r, links, view) for r in rows]
        return {'total': total, 'limit': limit, 'offset': offset, 'withheld': view.listed(items), 'items': items}

    @api.get('/notes/{note_id}')
    def get_note(note_id: uuid.UUID, conn: Conn, actor: Reader):
        view = View(conn, policy_for(conn, actor), guard)
        row = one(conn, view, 'note', NOTE_SELECT, 'n', note_id, 'n.campaign_id')
        return note_out(row, links_of(conn, 'note', [row['id']]), view)

    # Agent tasks --------------------------------------------------------

    @api.post('/tasks', status_code=201)
    def create_task(document: Document, conn: Conn, actor: RecordWriter,
                    campaign_id: uuid.UUID | None = Query(None, description='The campaign. An agent writes in its own campaign.')):
        """Write an agent-task/1 record in the queued state. Its inputs must be visible to the agents of the campaign."""
        doc = contracts.record('agent-task/1', document)
        pol = policy_for(conn, actor)
        campaign = write_campaign(conn, actor, pol, campaign_id, 'a task', required=True)
        viewer = pol if pol.agent else audience(campaign)
        checked(conn, actor, viewer, campaign['id'], [(doc['inputs'], ['body', 'inputs'])], 'task write')
        with record_rules(), conn.transaction():
            new_id = conn.execute("""
                INSERT INTO agent_task (campaign_id, kind, title, instructions, priority, lease_seconds, max_attempts, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [campaign['id'], doc['kind'], doc['title'], doc['instructions'], doc['priority'], doc['lease_seconds'],
                 doc['max_attempts'], actor.id]).fetchone()['id']
            insert_links(conn, 'agent_task', new_id, 'input', doc['inputs'])
        return get_task(new_id, conn, actor)

    @api.get('/tasks')
    def list_tasks(conn: Conn, actor: Reader, campaign_id: uuid.UUID | None = None,
                   state: str | None = Query(None, pattern=r'^(queued|claimed|complete|failed|cancelled)$'),
                   kind: str | None = Query(None, pattern=r'^[a-z][a-z0-9_]{0,62}$'),
                   limit: int = Page['limit'], offset: int = Page['offset']):
        """Agent tasks, in claim order: highest priority first, then oldest."""
        view = View(conn, policy_for(conn, actor), guard)
        scope, args = read_scope(view, campaign_id)
        where = [scope.format(alias='t').removeprefix(' AND ')] if scope else []
        for column, value in (('state', state), ('kind', kind)):
            if value is not None:
                where.append(f't.{column} = %({column})s')
                args[column] = value
        total, rows = page(conn, view, 'agent_task', TASK_SELECT, 't', 't.campaign_id', where, args, limit, offset,
                           't.priority DESC, t.created_at, t.id')
        links = links_of(conn, 'agent_task', [r['id'] for r in rows])
        items = [task_out(r, links, view) for r in rows]
        return {'total': total, 'limit': limit, 'offset': offset, 'withheld': view.listed(items), 'items': items}

    @api.get('/tasks/{task_id}')
    def get_task(task_id: uuid.UUID, conn: Conn, actor: Reader):
        view = View(conn, policy_for(conn, actor), guard)
        return task_record(conn, view, task_id)

    def task_record(conn, view: View, task_id) -> dict:
        row = one(conn, view, 'agent_task', TASK_SELECT, 't', task_id, 't.campaign_id')
        return task_out(row, links_of(conn, 'agent_task', [row['id']]), view)

    @api.post('/tasks/claim')
    def claim_task(request: ClaimRequest, conn: Conn, actor: RecordWriter,
                   campaign_id: uuid.UUID | None = Query(None, description='The campaign. An agent claims in its own campaign.')):
        """Claim the next task of the campaign for one lease: a queued task, or a claimed task with an expired lease and
        attempts left. The highest priority goes first, then the oldest task. Parallel claims never return the same task.
        A task whose lease expired on its last attempt becomes failed. "task" is null when no task is available."""
        pol = policy_for(conn, actor)
        campaign = write_campaign(conn, actor, pol, campaign_id, 'a task claim', required=True)
        view = View(conn, pol, guard)
        params = {**view.params, 'actor': actor.id, 'campaign': campaign['id'], 'kinds': request.kinds,
                  'reason': TASK_FAILED_REASON}
        task = None
        with conn.transaction():
            failed = conn.execute(f"""
                UPDATE agent_task t SET state = 'failed', finished_at = now(), reason = %(reason)s, lease_expires_at = NULL
                WHERE campaign_id = %(campaign)s AND state = 'claimed' AND lease_expires_at < now() AND attempts >= max_attempts
                  AND NOT {view.hidden('agent_task', 't')}
                RETURNING id""", params).fetchall()
            row = conn.execute(f"""
                UPDATE agent_task SET state = 'claimed', attempts = attempts + 1, claimed_by = %(actor)s, claimed_at = now(),
                       lease_id = gen_random_uuid(), lease_expires_at = now() + make_interval(secs => lease_seconds)
                WHERE id = (
                    SELECT id FROM agent_task t
                    WHERE campaign_id = %(campaign)s
                      AND NOT {view.hidden('agent_task', 't')}
                      AND (state = 'queued' OR (state = 'claimed' AND lease_expires_at < now() AND attempts < max_attempts))
                      AND (%(kinds)s::text[] IS NULL OR kind = ANY(%(kinds)s::text[]))
                    ORDER BY priority DESC, created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1)
                RETURNING id, lease_id""", params).fetchone()
            if row is not None:
                # Use the same visibility snapshot and finish the response before committing the claim.
                task = {**task_record(conn, view, row['id']), 'lease_id': row['lease_id']}
        return {'task': task, 'failed_on_expiry': [r['id'] for r in failed]}

    def lease_update(conn, task_id, actor: Actor, lease_id, assignments: str, args: list) -> dict:
        """Change a claimed task for the holder of its current lease. Otherwise explain the refusal."""
        row = conn.execute(f"""UPDATE agent_task SET {assignments}
                               WHERE id = %s AND state = 'claimed' AND claimed_by = %s AND lease_id = %s RETURNING *""",
                           [*args, task_id, actor.id, lease_id]).fetchone()
        if row is not None:
            return row
        task = conn.execute('SELECT campaign_id, state, claimed_by, attempts FROM agent_task WHERE id = %s', [task_id]).fetchone()
        if task is None or (actor.kind == 'agent' and task['campaign_id'] != actor.campaign_id):
            raise HTTPException(404, 'agent task not found')
        if task['state'] != 'claimed':
            raise HTTPException(409, f'the task is {task["state"]}. The lease ended.')
        if task['claimed_by'] != actor.id:
            raise HTTPException(403, f'client {actor.name} does not hold the lease of the task')
        raise HTTPException(409, f'the lease ended. The task was claimed again (attempt {task["attempts"]}).')

    def lease_scope(conn, actor: Actor, operation: str):
        pol = policy_for(conn, actor)
        if pol.agent:
            pol.require_active(operation)

    @api.post('/tasks/{task_id}/heartbeat')
    def heartbeat_task(task_id: uuid.UUID, request: LeaseRequest, conn: Conn, actor: RecordWriter):
        """Extend the lease by lease_seconds from now. Only the holder of the current lease can extend it.
        A lease that expired, but that no other claim took, can still be extended."""
        lease_scope(conn, actor, 'a lease extension')
        with conn.transaction():
            lease_update(conn, task_id, actor, request.lease_id,
                         'lease_expires_at = now() + make_interval(secs => lease_seconds)', [])
        return get_task(task_id, conn, actor)

    @api.post('/tasks/{task_id}/release')
    def release_task(task_id: uuid.UUID, request: LeaseRequest, conn: Conn, actor: RecordWriter):
        """Return the claimed task to the queue without a used attempt."""
        lease_scope(conn, actor, 'a task release')
        with record_rules(), conn.transaction():
            lease_update(conn, task_id, actor, request.lease_id,
                         """state = 'queued', claimed_by = NULL, claimed_at = NULL, lease_id = NULL, lease_expires_at = NULL,
                            attempts = GREATEST(attempts - 1, 0)""", [])
        return get_task(task_id, conn, actor)

    @api.post('/tasks/{task_id}/report')
    def report_task(task_id: uuid.UUID, request: ReportRequest, conn: Conn, actor: RecordWriter):
        """End the claimed task with an agent-task-report/1 document: complete or failed. Only the holder of the
        current lease can end it."""
        doc = contracts.record('agent-task-report/1', request.report)
        lease_scope(conn, actor, 'a task report')
        task = conn.execute('SELECT campaign_id FROM agent_task WHERE id = %s', [task_id]).fetchone()
        if task is None or (actor.kind == 'agent' and task['campaign_id'] != actor.campaign_id):
            raise HTTPException(404, 'agent task not found')
        pol = policy_for(conn, actor)
        checked(conn, actor, pol, task['campaign_id'], [(doc['outputs'], ['body', 'report', 'outputs'])], 'task report')
        with record_rules(), conn.transaction():
            lease_update(conn, task_id, actor, request.lease_id,
                         'state = %s, finished_at = now(), summary = %s, reason = %s, lease_expires_at = NULL',
                         [doc['outcome'], doc['summary'], doc.get('failure_reason')])
            insert_links(conn, 'agent_task', task_id, 'output', doc['outputs'])
        return get_task(task_id, conn, actor)

    @api.post('/tasks/{task_id}/cancel')
    def cancel_task(task_id: uuid.UUID, request: CancelRequest, conn: Conn, actor: RecordWriter):
        """Cancel a queued or claimed task. A client cancels only the tasks that it wrote, unless it has "admin"."""
        lease_scope(conn, actor, 'a task cancellation')
        task = conn.execute('SELECT campaign_id, created_by FROM agent_task WHERE id = %s', [task_id]).fetchone()
        if task is None or (actor.kind == 'agent' and task['campaign_id'] != actor.campaign_id):
            raise HTTPException(404, 'agent task not found')
        if task['created_by'] != actor.id and not actor.has('admin'):
            raise HTTPException(403, f'client {actor.name} can cancel only the tasks that it wrote')
        with record_rules(), conn.transaction():
            row = conn.execute("""UPDATE agent_task SET state = 'cancelled', finished_at = now(), cancelled_by = %s,
                                         reason = %s, lease_expires_at = NULL
                                  WHERE id = %s AND state IN ('queued', 'claimed') RETURNING state""",
                               [actor.id, request.reason, task_id]).fetchone()
            if row is None:
                state = conn.execute('SELECT state FROM agent_task WHERE id = %s', [task_id]).fetchone()['state']
                raise HTTPException(409, f'the task is {state}. Only a queued or claimed task can be cancelled.')
        return get_task(task_id, conn, actor)

    def records_about(conn, actor: Actor, *, run_ids=(), job_ids=(), measurement_ids=(), artifact_ids=()) -> dict:
        """The visible candidates whose records refer to the run, its jobs, measurements, or artifacts, each with its
        detail: resolved evidence, assessments, decisions, notes, and actors. For an evidence export."""
        view = View(conn, policy_for(conn, actor), guard)
        rows = conn.execute(f"""
            SELECT c.id FROM candidate c
            WHERE {view.scope('c.campaign_id')} AND NOT {view.hidden('candidate', 'c')}
              AND EXISTS (
                SELECT 1 FROM record_link l
                LEFT JOIN assessment la ON la.id = l.assessment_id LEFT JOIN decision ld ON ld.id = l.decision_id
                WHERE (l.candidate_id = c.id OR la.candidate_id = c.id OR ld.candidate_id = c.id)
                  AND (l.ref_run = ANY(%(runs)s::uuid[]) OR l.ref_job = ANY(%(jobs)s::uuid[])
                       OR l.ref_measurement = ANY(%(measurements)s::bigint[]) OR l.ref_artifact = ANY(%(artifacts)s::uuid[])))
            ORDER BY c.created_at, c.id""", {**view.params, 'runs': list(run_ids), 'jobs': list(job_ids),
                                             'measurements': list(measurement_ids), 'artifacts': list(artifact_ids)}).fetchall()
        items = [get_candidate(r['id'], conn, actor) for r in rows]
        return {'rule': 'candidate-status/1', 'withheld': view.listed(items), 'candidates': items}

    api.helpers = {'candidate_detail': lambda conn, actor, candidate_id: get_candidate(candidate_id, conn, actor),
                   'records_about': records_about}
    return api
