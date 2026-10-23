"""Review queue from stored BLS searches. Ordering is not a detection decision."""
from typing import Annotated, Callable, Literal
import uuid

from fastapi import APIRouter, Depends, Query

from .auth import Actor
from .contracts import ValidationFailure
from .policy import policy_for
from .records import View

RULE = 'signal-review/1'


def router(*, connection: Callable, need: Callable, guard) -> APIRouter:
    api = APIRouter(tags=['Exploration'])
    Conn = Annotated[object, Depends(connection)]
    Reader = Annotated[Actor, Depends(need('read'))]

    @api.get('/signals')
    def signals(conn: Conn, actor: Reader, dataset_id: str | None = None, run_id: uuid.UUID | None = None,
                sample: Literal['all', 'unconfirmed', 'candidates'] = 'all', q: str = Query('', max_length=200),
                sort: Literal['snr', 'period', 'depth', 'recent'] = 'snr',
                limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        """One completed BLS search per ready observation, ordered for human review.

        Without run_id, use the newest completed job in a visible run. Order by stored depth S/N by default.
        Unconfirmed excludes labels planet_host and eclipsing_binary. Missing labels do not establish a new object.
        No significance threshold, probability, or scientific assessment follows from this order.
        """
        pol = policy_for(conn, actor)
        pol.require_active('a signal review queue')
        errors = []
        if sample == 'unconfirmed' and not pol.labels_visible:
            errors.append({'loc': ['query', 'sample'], 'msg': 'the campaign withholds known-object labels'})
        if q and not pol.identity_visible:
            errors.append({'loc': ['query', 'q'], 'msg': 'the campaign withholds target identity'})
        if errors:
            raise ValidationFailure(errors)
        view = View(conn, pol, guard)
        scope, scope_args = pol.run_scope('r')
        params = {**view.params, 'run_id': run_id, 'dataset': dataset_id, 'query': '%' + q.strip() + '%',
                  'scope_campaign': scope_args[0] if scope_args else None, 'limit': limit, 'offset': offset}
        scope = scope.replace('%s', '%(scope_campaign)s')
        candidate = f"""SELECT c.id FROM candidate c JOIN candidate_observation co ON co.candidate_id = c.id
                        WHERE co.observation_id = o.id AND {view.scope('c.campaign_id')}
                          AND NOT {view.hidden('candidate', 'c')}"""
        where = ["o.state = 'ready'", 'NOT (o.id = ANY(%(protected)s::uuid[]))']
        if dataset_id is not None:
            where.append('o.dataset_id = %(dataset)s')
        if q:
            where.append('o.target_id ILIKE %(query)s')
        if sample == 'unconfirmed':
            where.append("coalesce(o.labels ->> 'class', '') NOT IN ('planet_host', 'eclipsing_binary')")
        if sample == 'candidates':
            where.append(f'EXISTS ({candidate})')
        base = f"""SELECT o.id AS observation_id, o.dataset_id, o.target_id, o.sector, o.cadence_seconds,
                    o.labels ->> 'class' AS label_class, o.labels -> 'planets' -> 0 ->> 'name' AS planet_name,
                    chosen.id AS job_id, chosen.run_id, chosen.label AS run_label,
                    chosen.campaign_id AS run_campaign_id, chosen.writer_campaign_id,
                    chosen.method_id, chosen.method_version, chosen.finished_at,
                    m.period_days, m.depth, m.depth_snr, m.duration_days, m.observed_transit_count,
                    ARRAY({candidate} ORDER BY c.created_at, c.id) AS candidate_ids
                FROM observation o
                JOIN LATERAL (
                    SELECT j.id, j.run_id, j.finished_at, r.label, r.campaign_id, r.method_id, r.method_version,
                           w.campaign_id AS writer_campaign_id
                    FROM job j JOIN run r ON r.id = j.run_id LEFT JOIN client w ON w.id = r.client_id
                    WHERE j.observation_id = o.id AND j.state = 'complete' AND j.result ->> 'outcome' = 'complete'
                      AND r.method_id = 'tess-bls' AND {scope}
                      {'AND r.id = %(run_id)s' if run_id is not None else ''}
                    ORDER BY j.finished_at DESC NULLS LAST, r.created_at DESC, j.id DESC LIMIT 1
                ) chosen ON true
                LEFT JOIN LATERAL (
                    SELECT max(value_number) FILTER (WHERE name = 'period') AS period_days,
                           max(value_number) FILTER (WHERE name = 'depth') AS depth,
                           max(value_number) FILTER (WHERE name = 'depth_snr') AS depth_snr,
                           max(value_number) FILTER (WHERE name = 'duration') AS duration_days,
                           max(value_number) FILTER (WHERE name = 'observed_transit_count') AS observed_transit_count
                    FROM measurement WHERE job_id = chosen.id AND role = 'search'
                ) m ON true
                WHERE {' AND '.join(where)}"""
        total = conn.execute(f'SELECT count(*) AS n FROM ({base}) s', params).fetchone()['n']
        order = {'snr': 'depth_snr DESC', 'period': 'period_days ASC', 'depth': 'depth DESC', 'recent': 'finished_at DESC'}[sort]
        rows = conn.execute(f'{base} ORDER BY {order} NULLS LAST, observation_id LIMIT %(limit)s OFFSET %(offset)s', params).fetchall()
        # Search values are evidence reads, so campaign budgets and access records apply.
        guard.read(conn, pol, [r['observation_id'] for r in rows], 'signal review queue')
        for row in rows:
            if not pol.identity_visible:
                row['target_id'] = None
            if not pol.labels_visible:
                row['label_class'] = row['planet_name'] = None
            row['text_withheld'] = not pol.text_visible(row.pop('writer_campaign_id'))
            if row['text_withheld']:
                row['run_label'] = None
            row.pop('run_campaign_id')
        return {'rule': RULE, 'total': total, 'limit': limit, 'offset': offset, 'items': rows,
                'withheld': pol.withheld(), 'protected_excluded': len(view.protected),
                'selection': 'The newest completed visible tess-bls job for each ready observation, or the selected run.',
                'ranking': 'Stored depth S/N orders the default review queue. It is not a probability or a validated candidate threshold.',
                'units': {'period_days': 'd', 'duration_days': 'd', 'depth': '1', 'depth_snr': '1', 'observed_transit_count': '1'}}
    return api
