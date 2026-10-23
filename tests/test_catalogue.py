"""Catalog provenance, confinement, permissions, and dataset run selection."""
import hashlib
import json
from dataclasses import replace

from fastapi.testclient import TestClient

from delta_api.app import create_app
from delta_api.catalogue import dataset_catalog, experiment_catalog
from conftest import add_observation, add_run, add_job, token_for, write_register


def sources(root):
    datasets, experiments = root / 'datasets', root / 'experiments'
    (datasets / 'sample').mkdir(parents=True)
    (datasets / 'catalog.json').write_text(json.dumps({'entries': [{'id': 'sample'}]}))
    (datasets / 'sample' / 'metadata.json').write_text(json.dumps({'id': 'sample', 'name': 'Sample', 'kind': 'survey', 'category': 'images', 'description': 'Sample images.', 'grain': 'One image.', 'coverage': 'Two targets.', 'bands': [], 'formats': ['FITS'], 'access': {}, 'acquisition': {'scope': 'Two files only.'}}))
    (datasets / 'sample' / 'README.md').write_text('# Sample\nNo complete download.\n')
    (experiments / 'trial').mkdir(parents=True)
    (experiments / 'README.md').write_text('| [Trial](trial/README.md) | Closed, diagnostic only | Failed calibration. Independent evaluation remains open. |\n')
    (experiments / 'trial' / 'experiment.json').write_text(json.dumps({'experiment_id': 'trial', 'datasets': [{'dataset_id': 'sample', 'role': 'reference'}], 'detection_methods': [{'method_id': 'test', 'scope': 'Diagnostic only.'}]}))
    (experiments / 'trial' / 'README.md').write_text('# Trial\n<script>alert(1)</script>\n')
    return datasets, experiments


def test_registration_and_exact_source_wording(tmp_path):
    datasets, experiments = sources(tmp_path)
    (datasets / 'unregistered').mkdir()
    (experiments / 'unregistered').mkdir()
    catalog = dataset_catalog(datasets)
    assert [d['id'] for d in catalog['items']] == ['sample']
    assert catalog['items'][0]['acquisition']['scope'] == 'Two files only.'
    assert catalog['source_sha256'] == hashlib.sha256((datasets / 'catalog.json').read_bytes()).hexdigest()
    exp = experiment_catalog(experiments)['items'][0]
    assert exp['status'] == 'Closed, diagnostic only'
    assert exp['result'] == 'Failed calibration. Independent evaluation remains open.'
    assert exp['datasets'] == [{'dataset_id': 'sample', 'role': 'reference'}]
    assert exp['methods'][0]['scope'] == 'Diagnostic only.'


def test_missing_empty_invalid_and_escaped_sources(tmp_path):
    assert not dataset_catalog(None)['available']
    assert not experiment_catalog(tmp_path)['available']
    datasets, experiments = sources(tmp_path)
    (datasets / 'catalog.json').write_text('{"entries": []}')
    assert dataset_catalog(datasets) == {'available': True, 'items': [], 'issues': [], 'source_sha256': hashlib.sha256(b'{"entries": []}').hexdigest(), 'documentation_reviewed_on': None}
    (datasets / 'catalog.json').write_text(json.dumps({'entries': [{'id': '../secret'}, {'id': 'sample'}]}))
    (datasets / 'sample' / 'metadata.json').write_text('{"id":"different"}')
    result = dataset_catalog(datasets)
    assert result['available'] and not result['items'] and len(result['issues']) == 2
    (datasets / 'sample' / 'metadata.json').unlink()
    (datasets / 'sample' / 'metadata.json').symlink_to(experiments / 'trial' / 'experiment.json')
    assert not dataset_catalog(datasets)['items']
    (experiments / 'trial' / 'experiment.json').write_text('{"experiment_id":"trial","datasets":null}')
    assert experiment_catalog(experiments)['issues']


def test_catalog_auth_and_document_confinement(client, conn, tmp_path):
    datasets, experiments = sources(tmp_path)
    config = replace(client.app.state.settings, datasets_root=datasets, experiments_root=experiments)
    with TestClient(create_app(config), headers=client.headers) as browser:
        assert browser.get('/api/catalog').status_code == 200
        doc = browser.get('/api/catalog/documents/experiments/trial/README.md').json()
        assert doc['text'] == '# Trial\n<script>alert(1)</script>\n'
        assert doc['sha256'] == hashlib.sha256(doc['text'].encode()).hexdigest()
        assert doc['bytes'] == len(doc['text'].encode())
        for path in ('datasets/unregistered/README.md', 'datasets/sample/metadata.json', 'raw/sample/README.md'):
            assert browser.get('/api/catalog/documents/' + path).status_code == 404
        (datasets / 'sample' / 'README.md').unlink()
        (datasets / 'sample' / 'README.md').symlink_to(experiments / 'trial' / 'README.md')
        assert browser.get('/api/catalog/documents/datasets/sample/README.md').status_code == 503
        assert browser.get('/api/catalog', headers={'Authorization': ''}).status_code == 401
        agent = token_for(conn, 'catalog-agent')
        assert browser.get('/api/catalog', headers=agent).status_code == 403
        assert browser.get('/api/catalog/documents/experiments/trial/README.md', headers=agent).status_code == 403
        assert browser.get('/api/catalog', headers=token_for(conn, 'no-read', kind='service', permissions=())).status_code == 403


def test_runs_filter_uses_visible_inputs(client, conn, cohorts_path):
    visible, protected = add_observation(conn), add_observation(conn, tic=222)
    first, second, empty = add_run(conn), add_run(conn), add_run(conn)
    add_job(conn, first, visible)
    add_job(conn, second, protected)
    path = '/api/runs?dataset_id=tess-spoc-light-curves'
    assert {r['id'] for r in client.get(path).json()} == {str(first), str(second)}
    assert client.get('/api/runs?dataset_id=other-dataset').json() == []
    write_register(cohorts_path, [('hidden', 'evaluation', [], ['TIC:222'])])
    agent = token_for(conn, 'dataset-agent')
    assert {r['id'] for r in client.get(path, headers=agent).json()} == {str(first)}
    assert client.get(path + '&limit=1&offset=1').json()[0]['id'] in {str(first), str(second)}
    assert client.get('/api/runs?dataset_id=../bad').status_code == 422
