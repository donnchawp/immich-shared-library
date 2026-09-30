"""Load .env into the environment before src.config is imported.

src.config builds its `settings` singleton at import time, so every repo-root
script has to populate os.environ *first*. This module deliberately imports
nothing from src: importing it must not drag in the very module it is
preparing the environment for.

A missing .env is not fatal here, only in each script's main(). Importing a
script must stay side-effect-free enough for the test suite to reach the
functions in it — .env is gitignored, so an import-time sys.exit would make
every test of those files pass only on a machine that happens to have one.
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"


def load_env(env_file: Path = ENV_FILE) -> bool:
    """Populate os.environ from `env_file`. Returns whether the file existed.

    setdefault, not assignment: a value already exported into the environment
    (docker-compose, `make test`, a one-off override) outranks the file.
    """
    if not env_file.is_file():
        return False
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key and value:
            os.environ.setdefault(key.strip(), value.strip())
    return True


def bootstrap(env_file: Path = ENV_FILE) -> bool:
    """load_env plus the settings every one-shot script wants.

    SYNC_INTERVAL_SECONDS is a sentinel: these scripts do one pass and exit,
    so the loop interval must never make them wait.
    """
    found = load_env(env_file)
    os.environ.setdefault("SYNC_INTERVAL_SECONDS", "9999")
    return found
