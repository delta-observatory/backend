"""Evidence exports: deterministic ZIP files with a manifest (contract export/1).

The API builds the content from the same views that its endpoints return, so blinding, the cohort guard, and the
observation budget apply. This module writes the files. Each file has a fixed date, fixed attributes, and a sorted
place in the archive. The manifest holds no request time, so the same request on the same records gives the same bytes.
"""
from dataclasses import dataclass
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import zipfile

from fastapi.encoders import jsonable_encoder

FIXED_DATE = (1980, 1, 1, 0, 0, 0)
SPOOL_BYTES = 32 * 1024 * 1024
CHUNK = 1024 * 1024
CSV_COLUMNS = ('measurement_id', 'job_id', 'observation_id', 'target_id', 'role', 'name', 'value', 'unit',
               'uncertainty_kind', 'uncertainty_value', 'uncertainty_reason', 'time_scale', 'time_reference',
               'quality_flags')


class ManifestInvalid(Exception):
    pass


@dataclass
class ExportFile:
    path: str
    media_type: str
    content: str
    data: bytes | None = None
    source: Path | None = None
    artifact_id: object = None
    stored_sha256: str | None = None
    redacted: bool = False
    # True when rule free-text/1 withheld text in this file.
    text_withheld: bool = False


def canonical_json(value) -> bytes:
    return (json.dumps(jsonable_encoder(value), sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
            + '\n').encode()


def json_file(path: str, content: str, value, *, text_withheld: bool = False) -> ExportFile:
    return ExportFile(path=path, media_type='application/json', content=content, data=canonical_json(value),
                      text_withheld=text_withheld)


def measurements_csv(measurements: list[dict]) -> ExportFile:
    """measurements.csv: one row for each measurement, in the order of measurements.json."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(CSV_COLUMNS)
    for m in measurements:
        u = m['uncertainty']
        writer.writerow([m['measurement_id'], m['job_id'], m['observation_id'], m['target_id'] or '', m['role'],
                         m['name'], json.dumps(m['value']), m['unit'], u['kind'],
                         '' if u.get('value') is None else repr(u['value']), u.get('reason') or '',
                         m.get('time_scale') or '', m.get('time_reference') or '', ' '.join(m['quality_flags'])])
    return ExportFile(path='measurements.csv', media_type='text/csv; charset=utf-8', content='measurements',
                      data=out.getvalue().encode())


def readme(manifest: dict) -> str:
    subject = manifest['subject']
    lines = [
        f'Delta Observatory evidence export: {subject["kind"]} {subject["id"]}',
        '',
        f'Exported by client {manifest["exported_by"]["name"]} ({manifest["exported_by"]["kind"]}).',
        f'API {manifest["api"]["version"]}, contracts from database commit {manifest["api"]["contracts_source"]}.',
        'manifest.json follows contract export/1. It lists every other file with its size and SHA-256.',
        'The same request on the same stored records gives the same bytes. The manifest holds no request time.',
        '',
        'Files:',
        '  run.json or runs.json   run parameters, method version, jobs, result documents, and software versions',
        '  candidate.json          candidate with resolved evidence, assessments, decisions, notes, and actors',
        '  records.json            research records that refer to the run',
        '  observations.json       observation identifiers, file SHA-256, time conventions, and header values',
        '  measurements.json/.csv  stored measurements with units, uncertainty, and quality flags',
        '  artifacts.json          artifact identifiers, sizes, and SHA-256',
        '  methods.json            method descriptions, parameter schemas, and scientific status',
        '  artifacts/<id>/<file>   stored artifact bytes, when artifacts=all',
        '',
        'Measurements come from worker methods. Model judgments are decision records. Evidence status and identity',
        'status come from assessment records. These stay separate. A decision score is not an uncertainty.',
        f'Artifacts: {manifest["artifacts"]}. Withheld: {", ".join(manifest["withheld"]) or "nothing"}.',
        '',
        'Limitations:',
        *[f'  - {item}' for item in manifest['limitations']],
        '',
        'Check the export with backend/scripts/verify_export.py <file.zip>.',
    ]
    return '\n'.join(lines) + '\n'


def _info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=FIXED_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def write_zip(files: list[ExportFile], manifest: dict, validator) -> tuple[tempfile.SpooledTemporaryFile, bytes, str]:
    """Write the files in path order and then manifest.json. Return the archive, the manifest bytes, and its SHA-256."""
    paths = [f.path for f in files]
    if len(paths) != len(set(paths)) or 'manifest.json' in paths:
        raise ValueError('export file paths must be unique and must not be manifest.json')
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)
    entries = []
    with zipfile.ZipFile(spool, 'w') as archive:
        for item in sorted(files, key=lambda f: f.path):
            digest, size = hashlib.sha256(), 0
            with archive.open(_info(item.path), 'w') as out:
                if item.data is not None:
                    out.write(item.data)
                    digest.update(item.data)
                    size = len(item.data)
                else:
                    with item.source.open('rb') as stream:
                        while block := stream.read(CHUNK):
                            out.write(block)
                            digest.update(block)
                            size += len(block)
            entry = {'path': item.path, 'bytes': size, 'sha256': digest.hexdigest(), 'media_type': item.media_type,
                     'content': item.content}
            if item.content == 'artifact':
                entry['artifact_id'] = str(item.artifact_id)
                entry['stored_sha256'] = item.stored_sha256
                if item.redacted:
                    entry['redacted'] = True
            entries.append(entry)
        document = {**manifest, 'files': entries}
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
        if errors:
            raise ManifestInvalid(f'the export manifest breaks export/1: {errors[0].message}')
        manifest_bytes = canonical_json(document)
        archive.writestr(_info('manifest.json'), manifest_bytes)
    spool.seek(0)
    return spool, manifest_bytes, hashlib.sha256(manifest_bytes).hexdigest()


def chunks(spool):
    try:
        while block := spool.read(CHUNK):
            yield block
    finally:
        spool.close()
