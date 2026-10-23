"""Read-only views of the dataset catalog and registered experiment descriptions.

These HTTP responses describe source documents. They are not scientific result
records, observation availability, or new experiment assessments.
"""
import hashlib
import json
from pathlib import Path
import re
from typing import Annotated, Callable

from fastapi import APIRouter, Depends, HTTPException

from .auth import Actor

IDENTIFIER = re.compile(r'^[a-z0-9][a-z0-9-]*$')
EXPERIMENT_ROW = re.compile(
    r'^\| \[([^\]]+)\]\(([a-z0-9][a-z0-9-]*)/README\.md\) \| ([^|]+) \| (.+) \|$', re.MULTILINE)
MAX_DOCUMENT_BYTES = 2_000_000


def _source(root: Path | None, relative: str) -> tuple[bytes, str]:
    if root is None:
        raise ValueError('The source directory is not configured.')
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('The document is outside its source directory.')
    with path.open('rb') as handle:
        data = handle.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError('The source document exceeds the supported size.')
    return data, hashlib.sha256(data).hexdigest()


def _json(root: Path | None, relative: str) -> tuple[dict, str]:
    data, digest = _source(root, relative)
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('The source document must contain an object.')
    return value, digest


def dataset_catalog(root: Path | None) -> dict:
    """Only catalog.json registers a dataset. Directory names do not establish registration."""
    try:
        catalog, digest = _json(root, 'catalog.json')
        entries = catalog['entries']
        if not isinstance(entries, list) or len(entries) > 2000:
            raise ValueError('The catalog entries are invalid or exceed the supported count.')
    except (OSError, ValueError, KeyError):
        return {'available': False, 'items': [], 'issues': ['The dataset catalog is unavailable.']}
    items, issues, seen = [], [], set()
    for entry in entries:
        identifier = entry.get('id') if isinstance(entry, dict) else None
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in seen:
            issues.append('The catalog contains an invalid or repeated dataset identifier.')
            continue
        seen.add(identifier)
        try:
            value, metadata_hash = _json(root, f'{identifier}/metadata.json')
            if value.get('id') != identifier:
                raise ValueError('The metadata identifier differs from the catalog identifier.')
            for key in ('name', 'kind', 'category', 'description', 'grain', 'coverage'):
                if not isinstance(value.get(key), str):
                    raise ValueError('The dataset description fields must contain text.')
            for key in ('bands', 'formats'):
                if not isinstance(value.get(key), list) or not all(isinstance(v, str) for v in value[key]):
                    raise ValueError('The dataset bands and formats must contain text lists.')
            for key, fields in (('access', ('provider', 'url', 'method')), ('acquisition', ('status', 'scope'))):
                if not isinstance(value.get(key), dict) or not all(value[key].get(f) is None or isinstance(value[key][f], str) for f in fields):
                    raise ValueError('The access and acquisition descriptions must contain text fields.')
            if value.get('documentation_reviewed_on') is not None and not isinstance(value['documentation_reviewed_on'], str):
                raise ValueError('The documentation review date must contain text.')
            item = {key: value.get(key) for key in ('name', 'kind', 'category', 'description', 'bands', 'formats',
                    'grain', 'coverage', 'timeframe', 'access', 'acquisition', 'related_dataset_ids', 'documentation_reviewed_on')}
            item.update(id=identifier, metadata_sha256=metadata_hash,
                        source_url=f'https://github.com/delta-observatory/datasets/blob/main/{identifier}/README.md',
                        document_url=f'/api/catalog/documents/datasets/{identifier}/README.md')
            items.append(item)
        except (OSError, ValueError):
            issues.append(f'The metadata for {identifier} is unavailable or invalid.')
    return {'available': True, 'items': items, 'issues': issues, 'source_sha256': digest,
            'documentation_reviewed_on': catalog.get('documentation_reviewed_on')}


def experiment_catalog(root: Path | None) -> dict:
    """Use the registered index wording. Do not infer a scientific outcome from execution status."""
    try:
        data, digest = _source(root, 'README.md')
        rows = EXPERIMENT_ROW.findall(data.decode('utf-8'))
    except (OSError, ValueError):
        return {'available': False, 'items': [], 'issues': ['The experiment index is unavailable.']}
    items, issues, seen = [], [], set()
    for title, identifier, status, result in rows:
        if identifier in seen:
            issues.append(f'The experiment index repeats {identifier}.')
            continue
        seen.add(identifier)
        try:
            manifest, manifest_hash = _json(root, f'{identifier}/experiment.json')
            if manifest.get('experiment_id') != identifier:
                raise ValueError('The experiment identifier differs from the index identifier.')
            datasets = manifest['datasets']
            if not isinstance(datasets, list) or not all(isinstance(d, dict) and isinstance(d.get('dataset_id'), str) for d in datasets):
                raise ValueError('The experiment dataset references are invalid.')
            methods = manifest.get('detection_methods', [])
            if not isinstance(methods, list) or not all(isinstance(m, dict) and isinstance(m.get('method_id'), str)
                    and (m.get('scope') is None or isinstance(m['scope'], str)) for m in methods):
                raise ValueError('The experiment method references are invalid.')
            if not all(d.get('role') is None or isinstance(d['role'], str) for d in datasets):
                raise ValueError('The experiment dataset roles are invalid.')
            items.append({'id': identifier, 'title': title, 'status': status.strip(), 'result': result.strip(),
                          'datasets': [{'dataset_id': d['dataset_id'], 'role': d.get('role')} for d in datasets],
                          'methods': [{'method_id': m.get('method_id'), 'scope': m.get('scope')} for m in methods],
                          'manifest_sha256': manifest_hash,
                          'source_url': f'https://github.com/delta-observatory/experiments/blob/main/{identifier}/README.md',
                          'document_url': f'/api/catalog/documents/experiments/{identifier}/README.md'})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            issues.append(f'The manifest for {identifier} is unavailable or invalid.')
    if not rows:
        return {'available': False, 'items': [], 'issues': ['The experiment index contains no supported registration rows.'], 'source_sha256': digest}
    return {'available': True, 'items': items, 'issues': issues, 'source_sha256': digest}


def router(*, need: Callable, datasets_root: Path | None, experiments_root: Path | None) -> APIRouter:
    api = APIRouter(tags=['Catalog'])
    Reader = Annotated[Actor, Depends(need('read'))]

    def permitted(actor: Actor):
        # Repository text can disclose identities and observations outside an agent's campaign.
        # Existing campaign-scoped observation and result endpoints remain available to agents.
        if actor.kind == 'agent':
            raise HTTPException(403, 'Repository documents are outside an agent campaign. Use the campaign-scoped observation and result endpoints.')

    @api.get('/catalog')
    def read_catalog(actor: Reader):
        """Dataset descriptions and registered experiments. Catalog inclusion does not establish local observations.

        Source status and result text retain the experiment index wording. Missing source directories are explicit.
        Unscoped repository text is unavailable to agent clients because it can disclose protected identities.
        """
        permitted(actor)
        return {'datasets': dataset_catalog(datasets_root), 'experiments': experiment_catalog(experiments_root)}

    @api.get('/catalog/documents/{collection}/{identifier}/{name}')
    def read_document(collection: str, identifier: str, name: str, actor: Reader):
        """Read the registered entry's README as plain text with its size and checksum. No arbitrary file paths."""
        permitted(actor)
        if collection not in ('datasets', 'experiments') or name != 'README.md' or not IDENTIFIER.fullmatch(identifier):
            raise HTTPException(404, 'The catalog document does not exist.')
        root = datasets_root if collection == 'datasets' else experiments_root
        catalog = dataset_catalog(root) if collection == 'datasets' else experiment_catalog(root)
        if not catalog['available']:
            raise HTTPException(503, 'The catalog source is unavailable.')
        if identifier not in {item['id'] for item in catalog['items']}:
            raise HTTPException(404, 'The entry is not registered in the available catalog.')
        try:
            data, digest = _source(root, f'{identifier}/{name}')
            text = data.decode('utf-8')
        except (OSError, ValueError):
            raise HTTPException(503, 'The catalog document is unavailable.') from None
        return {'name': name, 'text': text, 'sha256': digest, 'bytes': len(data),
                'source_url': f'https://github.com/delta-observatory/{collection}/blob/main/{identifier}/{name}'}

    return api
