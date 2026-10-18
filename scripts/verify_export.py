"""Check a Delta Observatory evidence export (contract export/1).

The check reads manifest.json from the ZIP file and compares each listed file with its size and SHA-256.
It reports files that the manifest does not list, unsafe paths, and artifact files whose SHA-256 differs from the stored
SHA-256 without the redacted mark, and a redacted mark in an export that withholds nothing. It uses only the Python
standard library. With --schema, it also checks the manifest against the export/1 schema (this needs the jsonschema
package). With --manifest-sha256, it compares the manifest with the X-Export-Manifest-SHA256 header of the response.

Usage: python scripts/verify_export.py export.zip [--schema vendor/contracts/exports/manifest/v1.schema.json]
                                                  [--manifest-sha256 <hex>]
The report is JSON on standard output, also for an unreadable file or manifest. The exit status is 1 when a check fails.
"""
import argparse
import hashlib
import json
from pathlib import PurePosixPath
import re
import sys
import zipfile

CHUNK = 1024 * 1024


def unsafe(path) -> bool:
    if not isinstance(path, str) or not path:
        return True
    parts = PurePosixPath(path).parts
    return path.startswith('/') or '\\' in path or '..' in parts or bool(re.match(r'^[A-Za-z]:', path))


def failed(path: str, message: str) -> dict:
    return {'file': path, 'passed': False, 'failures': [message], 'files_checked': 0}


def verify(path: str, schema_path: str | None = None, manifest_sha256: str | None = None) -> dict:
    try:
        return _verify(path, schema_path, manifest_sha256)
    except (OSError, zipfile.BadZipFile) as error:
        return failed(path, f'the file is not a readable ZIP file: {error}')


def _verify(path, schema_path, expected_manifest_sha256) -> dict:
    failures, checked = [], 0
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            failures.append('the archive holds a file name more than once')
        if 'manifest.json' not in names:
            return {'file': path, 'passed': False, 'failures': ['manifest.json is missing'], 'files_checked': 0}
        manifest_bytes = archive.read('manifest.json')
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if expected_manifest_sha256 and expected_manifest_sha256.lower() != manifest_sha256:
            failures.append('manifest.json: SHA-256 differs from the expected manifest SHA-256')
        try:
            manifest = json.loads(manifest_bytes)
        except ValueError as error:
            return {**failed(path, f'manifest.json is not valid JSON: {error}'), 'manifest_sha256': manifest_sha256}
        if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), list) \
                or not all(isinstance(e, dict) for e in manifest['files']):
            return {**failed(path, 'manifest.json is not an object with a list of file entries'),
                    'manifest_sha256': manifest_sha256}
        if manifest.get('contract') != 'export/1':
            failures.append(f'manifest contract is {manifest.get("contract")!r}, not "export/1"')
        if schema_path:
            from jsonschema import Draft202012Validator, FormatChecker
            with open(schema_path, encoding='utf-8') as stream:
                validator = Draft202012Validator(json.load(stream), format_checker=FormatChecker())
            failures += [f'manifest: {e.message}' for e in validator.iter_errors(manifest)]
        listed = set()
        withheld = manifest.get('withheld') if isinstance(manifest.get('withheld'), list) else []
        for entry in manifest['files']:
            name = entry.get('path', '')
            if not isinstance(name, str):
                failures.append(f'a file entry has a path that is not text: {name!r}')
                continue
            listed.add(name)
            if unsafe(name):
                failures.append(f'{name}: unsafe path')
                continue
            if name not in names:
                failures.append(f'{name}: listed but absent')
                continue
            digest, size = hashlib.sha256(), 0
            with archive.open(name) as stream:
                while block := stream.read(CHUNK):
                    digest.update(block)
                    size += len(block)
            checked += 1
            if size != entry.get('bytes'):
                failures.append(f'{name}: {size} bytes, manifest gives {entry.get("bytes")}')
            if digest.hexdigest() != entry.get('sha256'):
                failures.append(f'{name}: SHA-256 differs from the manifest')
            stored = entry.get('stored_sha256')
            if entry.get('content') == 'artifact' and stored != entry.get('sha256') and not entry.get('redacted'):
                failures.append(f'{name}: SHA-256 differs from the stored artifact, and the file is not marked redacted')
            if entry.get('redacted') and not withheld:
                failures.append(f'{name}: marked redacted, but the export withholds nothing')
        for name in names:
            if name != 'manifest.json' and name not in listed:
                failures.append(f'{name}: in the archive but not in the manifest')
            if unsafe(name):
                failures.append(f'{name}: unsafe path in the archive')
    return {'file': path, 'passed': not failures, 'failures': failures, 'files_checked': checked,
            'manifest_sha256': manifest_sha256, 'subject': manifest.get('subject'),
            'withheld': manifest.get('withheld'), 'artifacts': manifest.get('artifacts')}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Check a Delta Observatory evidence export.')
    parser.add_argument('export')
    parser.add_argument('--schema', help='path of contracts/exports/manifest/v1.schema.json')
    parser.add_argument('--manifest-sha256', help='the X-Export-Manifest-SHA256 header of the export response')
    args = parser.parse_args(argv)
    report = verify(args.export, args.schema, args.manifest_sha256)
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
