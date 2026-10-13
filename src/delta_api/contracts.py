"""Read the vendored contract schemas. Check job documents and method parameters.

The schemas in vendor/contracts/ are the only source of job, result, and
method-parameter shapes. This module does not define those shapes.
"""
from dataclasses import dataclass
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

# Resource limits that the API writes into each job document.
LIMITS = {
    'tess-bls': {'maximum_seconds': 600, 'maximum_memory_mib': 2048},
    'tess-spoc-index': {'maximum_seconds': 1800, 'maximum_memory_mib': 1536},
}


@dataclass(frozen=True)
class Method:
    id: str
    version: int
    schema: dict

    @property
    def info(self) -> dict:
        return self.schema.get('x-method', {})

    @property
    def kind(self) -> str:
        return self.info.get('kind', '')

    @property
    def defaults(self) -> dict:
        return defaults(self.schema)

    def describe(self) -> dict:
        return {
            'id': self.id,
            'version': self.version,
            **self.info,
            'title': self.schema.get('title'),
            'description': self.schema.get('description'),
            'constraints': self.schema.get('x-constraints', []),
            'parameters_schema': self.schema,
            'defaults': self.defaults,
        }


class ValidationFailure(Exception):
    """Field errors in a request. Each error has 'loc' and 'msg'."""

    def __init__(self, errors: list[dict]):
        super().__init__(errors)
        self.errors = errors


def defaults(schema: dict) -> Any:
    """Build a document from the 'default' keywords of a schema."""
    if schema.get('type') == 'object' and 'properties' in schema:
        built = {}
        for name, sub in schema['properties'].items():
            value = defaults(sub)
            if value is not None:
                built[name] = value
        return built
    return copy.deepcopy(schema.get('default'))


def deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def parameters_sha256(parameters: dict) -> str:
    text = json.dumps(parameters, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode()).hexdigest()


def _errors(validator: Draft202012Validator, document: Any, prefix: tuple) -> list[dict]:
    found = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    return [{'loc': [*prefix, *e.absolute_path], 'msg': e.message} for e in found]


def check_constraints(method: Method, parameters: dict) -> list[dict]:
    """Check the x-constraints rules that JSON Schema cannot express."""
    errors = []
    if method.id == 'tess-bls':
        search = parameters['search']
        if not search['minimum_period'] < search['maximum_period']:
            errors.append({'loc': ['parameters', 'search', 'maximum_period'],
                           'msg': 'search.minimum_period must be less than search.maximum_period'})
        if not max(search['duration_days']) < search['minimum_period']:
            errors.append({'loc': ['parameters', 'search', 'duration_days'],
                           'msg': 'max(search.duration_days) must be less than search.minimum_period'})
    return errors


class Contracts:
    def __init__(self, root: Path):
        self.root = root
        self.source = (root / 'SOURCE').read_text().strip() if (root / 'SOURCE').exists() else 'unknown'
        self.job_validator = self._validator(root / 'job' / 'v1.schema.json')
        self.methods: dict[tuple[str, int], Method] = {}
        self._validators: dict[tuple[str, int], Draft202012Validator] = {}
        for path in sorted((root / 'methods').glob('*/v*.schema.json')):
            version = int(path.name.removeprefix('v').split('.')[0])
            schema = json.loads(path.read_text())
            Draft202012Validator.check_schema(schema)
            key = (path.parent.name, version)
            self.methods[key] = Method(path.parent.name, version, schema)
            self._validators[key] = Draft202012Validator(schema, format_checker=FormatChecker())

    @staticmethod
    def _validator(path: Path) -> Draft202012Validator:
        schema = json.loads(path.read_text())
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())

    def method(self, method_id: str, version: int) -> Method | None:
        return self.methods.get((method_id, version))

    def resolve_parameters(self, method: Method, partial: dict) -> dict:
        """Apply defaults, then check the schema and the x-constraints rules."""
        parameters = deep_merge(method.defaults, partial)
        errors = _errors(self._validators[(method.id, method.version)], parameters, ('parameters',))
        if errors:
            raise ValidationFailure(errors)
        errors = check_constraints(method, parameters)
        if errors:
            raise ValidationFailure(errors)
        return parameters

    def job_document(self, *, job_id: str, run_id: str, method: Method, parameters: dict,
                     observation_ids: list[str]) -> dict:
        document = {
            'contract': 'job/1',
            'job_id': job_id,
            'run_id': run_id,
            'method': {'id': method.id, 'version': method.version},
            'parameters': parameters,
            'inputs': [{'observation_id': o} for o in observation_ids],
            'limits': dict(LIMITS[method.id]),
        }
        errors = _errors(self.job_validator, document, ('job',))
        if errors:
            raise ValidationFailure(errors)
        return document
