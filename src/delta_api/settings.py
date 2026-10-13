"""Settings from the environment. The API keeps no other state."""
from dataclasses import dataclass
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    database_url: str
    artifact_root: Path
    contracts_dir: Path
    cors_origins: tuple[str, ...]


def load() -> Settings:
    origins = os.environ.get('DELTA_CORS_ORIGINS', '')
    return Settings(
        database_url=os.environ.get('DATABASE_URL', ''),
        artifact_root=Path(os.environ.get('DELTA_ARTIFACT_ROOT', '/data/artifacts')),
        contracts_dir=Path(os.environ.get('DELTA_CONTRACTS_DIR', PACKAGE_ROOT / 'vendor' / 'contracts')),
        cors_origins=tuple(o.strip() for o in origins.split(',') if o.strip()),
    )
