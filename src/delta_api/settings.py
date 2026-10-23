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
    # The shared cohort register, mounted read-only. The cohort guard reads it.
    cohorts_path: Path | None = None
    # Optional read-only repository mounts for the dataset browser.
    datasets_root: Path | None = None
    experiments_root: Path | None = None
    # Optional service client with exactly read permission for /gui-api/.
    gui_read_client: str | None = None


def load() -> Settings:
    origins = os.environ.get('DELTA_CORS_ORIGINS', '')
    return Settings(
        database_url=os.environ.get('DATABASE_URL', ''),
        artifact_root=Path(os.environ.get('DELTA_ARTIFACT_ROOT', '/data/artifacts')),
        contracts_dir=Path(os.environ.get('DELTA_CONTRACTS_DIR', PACKAGE_ROOT / 'vendor' / 'contracts')),
        cors_origins=tuple(o.strip() for o in origins.split(',') if o.strip()),
        cohorts_path=Path(os.environ.get('DELTA_COHORTS_PATH', '/data/experiments/cohorts.json')),
        datasets_root=Path(os.environ.get('DELTA_DATASETS_ROOT', '/data/datasets')),
        experiments_root=Path(os.environ.get('DELTA_EXPERIMENTS_ROOT', '/data/experiments')),
        gui_read_client=os.environ.get('DELTA_GUI_READ_CLIENT') or None,
    )
