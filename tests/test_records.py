"""Research records: candidates, assessments, decisions, and notes (ROADMAP R8)."""
import psycopg
import pytest

from conftest import add_campaign, add_finished_job, add_observation, token_for, write_register

LABELS = {'class': 'planet_host', 'planets': [{'name': 'WASP-18 b', 'period_days': 0.94145}]}
HEADER = {'TICID': 100100827, 'OBJECT': 'TIC 100100827', 'RA_OBJ': 24.35, 'SECTOR': 2, 'TESSMAG': 8.8}
SHA = 'a' * 64
LAB_FILE = {'kind': 'external', 'uri': 'lab:runs/packet', 'sha256': SHA, 'bytes': 10}


def agent_in(conn, name, **campaign):
    campaign_id = add_campaign(conn, f'c-{name}', **campaign)
    return campaign_id, token_for(conn, name, campaign_id=campaign_id)


def measurement_ids(conn, job_id) -> dict:
    return {(r['role'], r['name']): r['id'] for r in conn.execute(
        'SELECT id, role, name FROM measurement WHERE job_id = %s', [job_id]).fetchall()}


def candidate(oid, evidence, **extra) -> dict:
    return {'contract': 'candidate/1', 'kind': 'transit_signal', 'tier': 'platform', 'summary': 'BLS signal.',
            'observation_ids': [str(oid)], 'evidence': evidence, **extra}


def decision(candidate_id, **extra) -> dict:
    return {'contract': 'decision/1', 'candidate_id': candidate_id, 'question': {'id': 'triage', 'version': 1},
            'mode': 'advisory', 'judge': 'model', 'model': {'kind': 'unavailable', 'reason': 'The runtime does not expose it.'},
            'packet': {'kind': 'sha256', 'sha256': SHA}, 'policy': {'kind': 'unavailable', 'reason': 'No policy is defined.'},
            'judged_evidence_status': 'rejected', 'action': 'discard', 'reason': 'The dip is broad.',
            'evidence': [LAB_FILE], **extra}


def judged(candidate_id, decision_id, **extra) -> dict:
    return {'contract': 'assessment/1', 'candidate_id': candidate_id, 'axis': 'evidence', 'status': 'rejected',
            'basis': 'model_judgment', 'decision_id': decision_id, 'reason': 'The triage decision rejects it.',
            'evidence': [{'kind': 'decision', 'id': decision_id}], **extra}


def identity(candidate_id, status='unmatched') -> dict:
    return {'contract': 'assessment/1', 'candidate_id': candidate_id, 'axis': 'identity', 'status': status,
            'basis': 'method', 'method': {'id': 'catalog-identity', 'version': 1}, 'reason': 'No catalog row agrees.',
            'evidence': [LAB_FILE]}


def platform_candidate(conn, artifact_root, *, labels=None, header=None, tic=100100827):
    """An observation with a finished tess-bls job. Return the observation, job, artifact, and measurement IDs."""
    oid = add_observation(conn, labels=labels, header=header, tic=tic)
    _, job_id, artifact_id = add_finished_job(conn, artifact_root, oid)
    return oid, job_id, artifact_id, measurement_ids(conn, job_id)


def test_records_keep_measurements_judgments_and_statuses_apart(client, conn, artifact_root):
    oid, job_id, artifact_id, m = platform_candidate(conn, artifact_root, labels=LABELS, header=HEADER)
    sha = conn.execute('SELECT sha256 FROM artifact WHERE id = %s', [artifact_id]).fetchone()['sha256']
    campaign_id, agent = agent_in(conn, 'agent-a', blinding='labels_identity')
    evidence = [{'kind': 'job', 'id': str(job_id)}, {'kind': 'measurement', 'id': m[('search', 'period')]},
                {'kind': 'artifact', 'id': str(artifact_id), 'sha256': sha}]
    made = client.post('/api/candidates', headers=agent, json=candidate(oid, evidence, label='C001'))
    assert made.status_code == 201, made.text
    c = made.json()
    assert c['campaign_id'] == str(campaign_id) and c['actor']['kind'] == 'agent' and c['label'] == 'C001'
    assert c['evidence_status'] == {'status': 'unreviewed', 'assessment_id': None, 'basis': None}
    assert c['identity_status'] is None and c['withheld'] == ['labels', 'identity']
    assert 'period' not in c and 'value' not in c  # a candidate holds no measured values

    # An agent writes its judgment as a decision. The decision sets no status.
    judgment = client.post('/api/decisions', headers=agent, json=decision(c['id']))
    assert judgment.status_code == 201, judgment.text
    d = judgment.json()
    assert d['model_judgment'] and d['withheld_from_writer'] == ['labels', 'identity']
    assert d['model'] == {'kind': 'unavailable', 'reason': 'The runtime does not expose it.'}
    assert client.get(f'/api/candidates/{c["id"]}', headers=agent).json()['evidence_status']['status'] == 'unreviewed'
    # An assessment that cites the decision gives the evidence status.
    status = client.post('/api/assessments', headers=agent, json=judged(c['id'], d['id']))
    assert status.status_code == 201, status.text
    detail = client.get(f'/api/candidates/{c["id"]}', headers=agent).json()
    assert detail['evidence_status'] == {'status': 'rejected', 'assessment_id': status.json()['id'], 'basis': 'model_judgment'}

    # An agent writes model judgments only. Identity comparison is not an agent role.
    review = {**judged(c['id'], d['id']), 'basis': 'review'}
    review.pop('decision_id')
    assert client.post('/api/assessments', headers=agent, json=review).status_code == 403
    assert client.post('/api/assessments', headers=agent, json=identity(c['id'])).status_code == 403
    person_judge = {**decision(c['id']), 'judge': 'person'}
    person_judge.pop('model')
    assert client.post('/api/decisions', headers=agent, json=person_judge).status_code == 403

    # The detail resolves the measurement from the measurement table and redacts the observation.
    period = next(e for e in detail['evidence'] if e['kind'] == 'measurement')['record']
    assert period['name'] == 'period' and period['value'] == pytest.approx(0.9413849094310086) and period['unit'] == 'd'
    assert detail['observations'][0]['target_id'] is None and detail['observations'][0]['withheld'] == ['labels', 'identity']
    assert [a['axis'] for a in detail['assessments']] == ['evidence'] and len(detail['decisions']) == 1

    # A person records the identity comparison. Blinding hides it from the agent.
    assert client.post('/api/assessments', json=identity(c['id'])).status_code == 201
    mine = client.get(f'/api/candidates/{c["id"]}').json()
    assert mine['identity_status']['status'] == 'unmatched' and mine['evidence_status']['status'] == 'rejected'
    seen = client.get(f'/api/candidates/{c["id"]}', headers=agent).json()
    assert seen['identity_status'] is None and [a['axis'] for a in seen['assessments']] == ['evidence']
    assert client.get('/api/assessments', headers=agent, params={'axis': 'identity'}).status_code == 422
    assert client.get('/api/candidates', headers=agent, params={'identity_status': 'unmatched'}).status_code == 422
    assert client.get('/api/candidates', params={'identity_status': 'unmatched'}).json()['total'] == 1
    assert client.get('/api/assessments', params={'candidate_id': c['id']}).json()['total'] == 2
    # Records do not change.
    with pytest.raises(psycopg.Error) as error:
        conn.execute('UPDATE decision SET reason = %s WHERE id = %s', ['changed', d['id']])
    assert error.value.sqlstate == 'RC002'


def test_the_newest_assessment_gives_the_status_and_corrections_stay_on_their_candidate(client, conn, artifact_root):
    oid, job_id, _, _ = platform_candidate(conn, artifact_root)
    campaign_id = add_campaign(conn, 'review')
    evidence = [{'kind': 'job', 'id': str(job_id)}]
    first = client.post('/api/candidates', params={'campaign_id': str(campaign_id)}, json=candidate(oid, evidence)).json()
    second = client.post('/api/candidates', params={'campaign_id': str(campaign_id)}, json=candidate(oid, evidence)).json()
    review = {'contract': 'assessment/1', 'candidate_id': first['id'], 'axis': 'evidence', 'status': 'supported',
              'basis': 'review', 'reason': 'Clear dips.', 'evidence': evidence}
    one = client.post('/api/assessments', json=review).json()
    two = client.post('/api/assessments', json={**review, 'status': 'ambiguous', 'supersedes': one['id']})
    assert two.status_code == 201
    assert client.get(f'/api/candidates/{first["id"]}').json()['evidence_status']['status'] == 'ambiguous'
    # A correction refers to an assessment of the same candidate and axis.
    wrong = client.post('/api/assessments', json={**review, 'candidate_id': second['id'], 'supersedes': one['id']})
    assert wrong.status_code == 422 and 'same candidate' in wrong.json()['detail']
    unreviewed = {'contract': 'assessment/1', 'candidate_id': first['id'], 'axis': 'evidence', 'status': 'supported',
                  'basis': 'review', 'reason': 'No evidence.'}
    assert client.post('/api/assessments', json=unreviewed).status_code == 422
    # A model-judgment assessment cites a decision about the same candidate.
    d = client.post('/api/decisions', json={**decision(second['id']), 'evidence': evidence}).json()
    other = client.post('/api/assessments', json=judged(first['id'], d['id']))
    assert other.status_code == 422 and 'same candidate' in other.json()['detail']


def test_the_decision_freeze_and_the_campaign_lifecycle(client, conn, artifact_root):
    oid, job_id, _, _ = platform_candidate(conn, artifact_root, labels=LABELS, header=HEADER)
    evidence = [{'kind': 'job', 'id': str(job_id)}]
    campaign_id = add_campaign(conn, 'life', blinding='labels_identity', approved=False)
    agent = token_for(conn, 'agent-l', campaign_id=campaign_id)
    # Before the approval, an agent writes nothing, and the database refuses a person's candidate in the campaign.
    assert client.post('/api/candidates', headers=agent, json=candidate(oid, evidence)).status_code == 403
    early = client.post('/api/candidates', params={'campaign_id': str(campaign_id)}, json=candidate(oid, evidence))
    assert early.status_code == 409 and 'not approved' in early.json()['detail']
    assert client.post(f'/api/campaigns/{campaign_id}/approve', json={'campaign_file_sha256': 'b' * 64}).status_code == 200

    c = client.post('/api/candidates', headers=agent, json=candidate(oid, evidence)).json()
    d = client.post('/api/decisions', headers=agent, json=decision(c['id'])).json()
    assert client.post(f'/api/campaigns/{campaign_id}/fix-decisions', headers=agent).status_code == 200

    # After the decisions are fixed: no candidate, decision, or evidence assessment, from anyone.
    for path, body, headers in (('/api/candidates', candidate(oid, evidence), agent),
                                ('/api/decisions', decision(c['id']), agent),
                                ('/api/assessments', judged(c['id'], d['id']), agent),
                                ('/api/decisions', decision(c['id']), {}),
                                ('/api/assessments', judged(c['id'], d['id']), {})):
        refused = client.post(path, headers=headers, json=body)
        assert refused.status_code == 409 and 'fixed its decisions' in refused.json()['detail'], (path, refused.text)
    # Notes and the identity comparison continue.
    note = {'contract': 'note/1', 'body': 'Decisions are fixed.', 'subjects': [{'kind': 'candidate', 'id': c['id']}]}
    assert client.post('/api/notes', headers=agent, json=note).status_code == 201
    assert client.post('/api/assessments', json=identity(c['id'], 'matched')).status_code == 201
    assert client.get(f'/api/candidates/{c["id"]}', headers=agent).json()['identity_status'] is None

    # The reveal ends blinding: the agent sees the identity status.
    assert client.post(f'/api/campaigns/{campaign_id}/reveal', json={'reason': 'Decisions fixed.'}).status_code == 200
    assert client.get(f'/api/candidates/{c["id"]}', headers=agent).json()['identity_status']['status'] == 'matched'

    # After the close, the agent writes nothing. A person still writes notes, but no records of the campaign.
    assert client.post(f'/api/campaigns/{campaign_id}/close').status_code == 200
    assert client.post('/api/notes', headers=agent, json=note).status_code == 403
    assert client.post('/api/notes', json=note).status_code == 201
    closed = client.post('/api/assessments', json=identity(c['id']))
    assert closed.status_code == 409 and 'closed' in closed.json()['detail']
    counts = client.get(f'/api/campaigns/{campaign_id}').json()['records']
    assert counts == {'candidates': 1, 'assessments': 1, 'decisions': 1, 'notes': 2, 'tasks': {}}

    # The database keeps the freeze for every writer, not only the API.
    owner = conn.execute("SELECT id FROM client WHERE name = 'richard'").fetchone()['id']
    with pytest.raises(psycopg.Error) as error:
        conn.execute("""INSERT INTO decision (candidate_id, question_id, question_version, mode, judge, packet_reason,
                            policy_reason, judged_evidence_status, action, reason, created_by)
                        VALUES (%s, 'triage', 1, 'advisory', 'person', 'none', 'none', 'supported', 'no_action', 'x', %s)""",
                     [c['id'], owner])
    assert error.value.sqlstate == 'RC001'
    with pytest.raises(psycopg.Error) as error:
        conn.execute('DELETE FROM note')
    assert error.value.sqlstate == 'RC002'


def test_campaign_isolation(client, conn, artifact_root):
    oid, job_id, _, _ = platform_candidate(conn, artifact_root)
    evidence = [{'kind': 'job', 'id': str(job_id)}]
    campaign_a, agent_a = agent_in(conn, 'agent-a')
    campaign_b, agent_b = agent_in(conn, 'agent-b')
    cb = client.post('/api/candidates', headers=agent_b, json=candidate(oid, evidence)).json()
    ca = client.post('/api/candidates', headers=agent_a, json=candidate(oid, evidence)).json()

    assert client.get(f'/api/candidates/{cb["id"]}', headers=agent_a).status_code == 404
    assert [i['id'] for i in client.get('/api/candidates', headers=agent_a).json()['items']] == [ca['id']]
    assert client.get('/api/candidates', headers=agent_a, params={'campaign_id': str(campaign_b)}).status_code == 404
    assert client.get('/api/tasks', headers=agent_a, params={'campaign_id': str(campaign_b)}).status_code == 404
    note = {'contract': 'note/1', 'body': 'x', 'subjects': [{'kind': 'candidate', 'id': cb['id']}]}
    assert client.post('/api/notes', headers=agent_a, json=note).status_code == 422
    assert client.post('/api/decisions', headers=agent_a, json=decision(cb['id'])).status_code == 422
    cross = candidate(oid, [*evidence, {'kind': 'candidate', 'id': cb['id']}])
    assert client.post('/api/candidates', headers=agent_a, json=cross).status_code == 422
    # A person also keeps the records of one campaign apart.
    refused = client.post('/api/candidates', params={'campaign_id': str(campaign_a)}, json=cross)
    assert refused.status_code == 422 and 'another campaign' in refused.json()['detail'][0]['msg']
    both = {'contract': 'note/1', 'body': 'x', 'subjects': [{'kind': 'candidate', 'id': cb['id']},
                                                            {'kind': 'candidate', 'id': ca['id']}]}
    assert client.post('/api/notes', json=both).status_code == 422
    # The person's note takes the campaign of its subject, and the agents of that campaign see it.
    made = client.post('/api/notes', json={**note, 'body': 'Check the second sector.'}).json()
    assert made['campaign_id'] == str(campaign_b)
    assert [n['id'] for n in client.get('/api/notes', headers=agent_b).json()['items']] == [made['id']]
    assert client.get('/api/notes', headers=agent_a).json()['items'] == []
    about = client.get('/api/notes', params={'subject_kind': 'candidate', 'subject_id': cb['id']}).json()
    assert about['total'] == 1
    # A note without a campaign is not visible to any agent.
    loose = client.post('/api/notes', json={'contract': 'note/1', 'body': 'General.',
                                            'subjects': [{'kind': 'observation', 'id': str(oid)}]}).json()
    assert loose['campaign_id'] is None
    assert client.get(f'/api/notes/{loose["id"]}', headers=agent_a).status_code == 404

    # The database refuses a link between campaigns from any writer.
    owner = conn.execute("SELECT id FROM client WHERE name = 'richard'").fetchone()['id']
    with pytest.raises(psycopg.Error) as error:
        with conn.transaction():
            new = conn.execute("""INSERT INTO candidate (campaign_id, kind, tier, summary, created_by)
                                  VALUES (%s, 'transit_signal', 'lab', 'x', %s) RETURNING id""",
                               [campaign_a, owner]).fetchone()['id']
            conn.execute("INSERT INTO record_link (role, position, candidate_id, ref_candidate) VALUES ('evidence', 0, %s, %s)",
                         [new, cb['id']])
    assert error.value.sqlstate == 'RC003'


def test_evidence_references_keep_their_integrity(client, conn, artifact_root, cohorts_path):
    oid, job_id, artifact_id, m = platform_candidate(conn, artifact_root, tic=111)
    campaign_id, agent = agent_in(conn, 'agent-i', blinding='open')
    bad = [({'kind': 'measurement', 'id': 999999}, ['body', 'evidence', 0], 'not found'),
           ({'kind': 'job', 'id': '00000000-0000-4000-8000-000000000000'}, ['body', 'evidence', 0], 'not found'),
           ({'kind': 'artifact', 'id': str(artifact_id), 'sha256': 'f' * 64}, ['body', 'evidence', 0, 'sha256'], 'stored artifact')]
    for ref, loc, message in bad:
        response = client.post('/api/candidates', headers=agent, json=candidate(oid, [ref, {'kind': 'job', 'id': str(job_id)}]))
        assert response.status_code == 422, response.text
        assert response.json()['detail'][0]['loc'] == loc and message in response.json()['detail'][0]['msg']
    # The schema refuses an external file without a checksum, a platform candidate without platform evidence,
    # an unknown contract version, and an unknown observation.
    no_sum = {'kind': 'external', 'uri': 'lab:x', 'bytes': 1}
    assert client.post('/api/candidates', headers=agent, json=candidate(oid, [no_sum], tier='lab')).status_code == 422
    assert client.post('/api/candidates', headers=agent, json=candidate(oid, [LAB_FILE])).status_code == 422
    version = client.post('/api/candidates', headers=agent, json={**candidate(oid, [LAB_FILE], tier='lab'), 'contract': 'candidate/2'})
    assert version.status_code == 422 and version.json()['detail'][0]['loc'] == ['body', 'contract']
    unknown = candidate('00000000-0000-4000-8000-000000000001', [LAB_FILE], tier='lab')
    assert client.post('/api/candidates', headers=agent, json=unknown).json()['detail'][0]['loc'] == ['body', 'observation_ids', 0]
    lab = client.post('/api/candidates', headers=agent, json=candidate(oid, [LAB_FILE], tier='lab'))
    assert lab.status_code == 201 and lab.json()['evidence'] == [LAB_FILE]

    # A link and the observations of a candidate are written with the candidate, not later.
    with pytest.raises(psycopg.Error) as error:
        conn.execute("INSERT INTO record_link (role, position, candidate_id, ref_job) VALUES ('evidence', 9, %s, %s)",
                     [lab.json()['id'], job_id])
    assert error.value.sqlstate == 'RC003'
    with pytest.raises(psycopg.Error) as error:
        conn.execute('INSERT INTO candidate_observation VALUES (%s, %s)', [lab.json()['id'], add_observation(conn, tic=5)])
    assert error.value.sqlstate == 'RC003'

    # An evaluation reservation protects the observation. The agent's reference is refused and recorded.
    write_register(cohorts_path, [('eval-a', 'evaluation', [], ['TIC:111'])])
    refused = client.post('/api/candidates', headers=agent,
                          json=candidate(oid, [{'kind': 'measurement', 'id': m[('search', 'depth')]}]))
    assert refused.status_code == 422 and 'reserved for evaluation' in refused.json()['detail'][0]['msg']
    events = client.get('/api/access-events').json()['items']
    assert [(e['operation'], e['outcome']) for e in events] == [('candidate write', 'refused')]
    # The earlier candidate now refers to a protected observation, so the agent no longer sees it.
    assert client.get('/api/candidates', headers=agent).json()['total'] == 0
    assert client.get(f'/api/candidates/{lab.json()["id"]}', headers=agent).status_code == 404
    assert client.get(f'/api/candidates/{lab.json()["id"]}').status_code == 200


def test_labels_blinding_hides_records_that_use_labels(client, conn, artifact_root):
    oid, job_id, _, m = platform_candidate(conn, artifact_root, labels=LABELS, header=HEADER)
    campaign_id, agent = agent_in(conn, 'agent-b', blinding='labels')
    comparison = {'kind': 'measurement', 'id': m[('reference_comparison', 'recovered')]}
    refused = client.post('/api/candidates', headers=agent, json=candidate(oid, [{'kind': 'job', 'id': str(job_id)}, comparison]))
    assert refused.status_code == 422 and 'withholds' in refused.json()['detail'][0]['msg']
    # A person's candidate that cites a label comparison is hidden from the agent until the reveal.
    mine = client.post('/api/candidates', params={'campaign_id': str(campaign_id)},
                       json=candidate(oid, [{'kind': 'job', 'id': str(job_id)}, comparison])).json()
    plain = client.post('/api/candidates', headers=agent, json=candidate(oid, [{'kind': 'job', 'id': str(job_id)}])).json()
    listed = client.get('/api/candidates', headers=agent).json()
    assert [i['id'] for i in listed['items']] == [plain['id']] and listed['withheld'] == ['labels']
    assert client.get(f'/api/candidates/{mine["id"]}', headers=agent).status_code == 404
    detail = client.get(f'/api/candidates/{plain["id"]}', headers=agent).json()
    assert detail['observations'][0]['target_id'] == 'TIC:100100827' and detail['identity_status'] is None
    client.post(f'/api/campaigns/{campaign_id}/fix-decisions', headers=agent)
    client.post(f'/api/campaigns/{campaign_id}/reveal', json={'reason': 'Decisions fixed.'})
    assert client.get('/api/candidates', headers=agent).json()['total'] == 2
    assert client.get(f'/api/candidates/{plain["id"]}', headers=agent).json()['identity_status']['status'] == 'unchecked'


def test_record_permissions(client, conn, artifact_root):
    oid, job_id, _, _ = platform_candidate(conn, artifact_root)
    body = candidate(oid, [{'kind': 'job', 'id': str(job_id)}])
    reader = token_for(conn, 'reader', kind='service', permissions=['read'])
    campaign_id = add_campaign(conn, 'perm')
    no_write = token_for(conn, 'agent-r', permissions=['read', 'run.submit'], campaign_id=campaign_id)
    for headers in (reader, no_write):
        assert client.post('/api/candidates', headers=headers, json=body).status_code == 403
        assert client.post('/api/notes', headers=headers, json={'contract': 'note/1', 'body': 'x',
                                                               'subjects': [{'kind': 'observation', 'id': str(oid)}]}).status_code == 403
        assert client.post('/api/tasks/claim', headers=headers, json={}).status_code == 403
    assert client.get('/api/candidates', headers=reader).status_code == 200
    assert client.get('/api/candidates', headers={'Authorization': ''}).status_code == 401
    # The candidate detail counts as observation reads in the agent's budget.
    budget_campaign = add_campaign(conn, 'budget', budgets={'observations_read': 0})
    tight = token_for(conn, 'agent-t', campaign_id=budget_campaign)
    made = client.post('/api/candidates', headers=tight, json=body)
    assert made.status_code == 201
    refused = client.get(f'/api/candidates/{made.json()["id"]}', headers=tight)
    assert refused.status_code == 403 and 'observation budget' in refused.json()['detail']


@pytest.mark.parametrize('axis,hidden_source', [('evidence', 'labels'), ('evidence', 'evaluation'), ('identity', 'evaluation')])
def test_candidate_status_does_not_expose_a_hidden_assessment(client, conn, artifact_root, cohorts_path, axis, hidden_source):
    oid, job_id, _, m = platform_candidate(conn, artifact_root, tic=111)
    campaign_id, agent = agent_in(conn, 'status-reader', blinding='labels_identity' if hidden_source == 'labels' else 'open')
    c = client.post('/api/candidates', headers=agent,
                    json=candidate(oid, [{'kind': 'job', 'id': str(job_id)}])).json()
    if hidden_source == 'labels':
        evidence = [{'kind': 'measurement', 'id': m[('reference_comparison', 'recovered')]}]
    else:
        protected = add_observation(conn, tic=222)
        evidence = [{'kind': 'observation', 'id': str(protected)}]
        write_register(cohorts_path, [('eval-status', 'evaluation', [], ['TIC:222'])])
    status_value = 'supported' if axis == 'evidence' else 'matched'
    status_key = f'{axis}_status'
    assessment = client.post('/api/assessments', json={
        'contract': 'assessment/1', 'candidate_id': c['id'], 'axis': axis, 'status': status_value,
        'basis': 'review', 'reason': 'The cited evidence supports the signal.', 'evidence': evidence})
    assert assessment.status_code == 201, assessment.text
    aid = assessment.json()['id']
    assert client.get(f'/api/assessments/{aid}', headers=agent).status_code == 404
    detail = client.get(f'/api/candidates/{c["id"]}', headers=agent)
    assert detail.status_code == 200
    assert detail.json()[status_key] is None
    assert detail.json()['assessments'] == []
    listed = client.get('/api/candidates', headers=agent).json()
    assert listed['total'] == 1 and listed['items'][0][status_key] is None
    # Neither the matching filter nor any alternative status reveals the withheld value.
    statuses = ('supported', 'ambiguous', 'rejected', 'unreviewed') if axis == 'evidence' else ('matched', 'unmatched', 'ambiguous', 'unchecked')
    for status in statuses:
        filtered = client.get('/api/candidates', headers=agent, params={status_key: status}).json()
        assert filtered['total'] == 0 and filtered['items'] == []
    assert client.get(f'/api/candidates/{c["id"]}').json()[status_key]['status'] == status_value
    if hidden_source == 'labels':
        assert client.post(f'/api/campaigns/{campaign_id}/fix-decisions', headers=agent).status_code == 200
        assert client.post(f'/api/campaigns/{campaign_id}/reveal', json={'reason': 'Review complete.'}).status_code == 200
        revealed = client.get('/api/candidates', headers=agent, params={'evidence_status': 'supported'}).json()
        assert revealed['total'] == 1
        assert revealed['items'][0]['evidence_status']['assessment_id'] == aid


def test_all_research_record_reads_require_an_active_agent_campaign(client, conn, artifact_root):
    campaign_id, agent = agent_in(conn, 'record-reader', approved=False)
    task = client.post('/api/tasks', params={'campaign_id': str(campaign_id)}, json={
        'contract': 'agent-task/1', 'kind': 'review', 'title': 'Review the evidence.'})
    assert task.status_code == 201
    collections = ('candidates', 'assessments', 'decisions', 'notes', 'tasks')
    for collection in collections:
        assert client.get(f'/api/{collection}', headers=agent).status_code == 403
        assert client.get(f'/api/{collection}/{task.json()["id"]}', headers=agent).status_code == 403
    assert client.post(f'/api/campaigns/{campaign_id}/approve', json={'campaign_file_sha256': SHA}).status_code == 200
    oid, job_id, _, _ = platform_candidate(conn, artifact_root)
    c = client.post('/api/candidates', headers=agent,
                    json=candidate(oid, [{'kind': 'job', 'id': str(job_id)}])).json()
    d = client.post('/api/decisions', headers=agent, json=decision(c['id'])).json()
    a = client.post('/api/assessments', headers=agent, json=judged(c['id'], d['id'])).json()
    n = client.post('/api/notes', headers=agent, json={
        'contract': 'note/1', 'body': 'Review complete.', 'subjects': [{'kind': 'candidate', 'id': c['id']}]}).json()
    records = dict(zip(collections, (c, a, d, n, task.json())))
    for collection, record in records.items():
        assert client.get(f'/api/{collection}', headers=agent).status_code == 200
        assert client.get(f'/api/{collection}/{record["id"]}', headers=agent).status_code == 200
    assert client.post(f'/api/campaigns/{campaign_id}/close').status_code == 200
    for collection, record in records.items():
        assert client.get(f'/api/{collection}', headers=agent).status_code == 403
        assert client.get(f'/api/{collection}/{record["id"]}', headers=agent).status_code == 403
        assert client.get(f'/api/{collection}/{record["id"]}').status_code == 200
