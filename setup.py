#!/usr/bin/env python3
"""Interactive setup wizard for immich-shared-library sidecar.

Runs on the host (not in Docker) — needs filesystem access for symlinks
and the Immich web API is typically exposed on the host.

Usage:
    python3 setup.py
"""

import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path



# ── Helpers ──────────────────────────────────────────────────────────────────

def prompt(question: str, default: str = "") -> str:
    """Prompt the user for input with an optional default."""
    if default:
        answer = input(f"{question} [{default}]: ").strip()
        return answer or default
    while True:
        answer = input(f"{question}: ").strip()
        if answer:
            return answer
        print("  A value is required.")


def prompt_secret(question: str, default: str = "") -> str:
    """Prompt for a secret value (masked input)."""
    if default:
        answer = getpass.getpass(f"{question} [{default}]: ").strip()
        return answer or default
    while True:
        answer = getpass.getpass(f"{question}: ").strip()
        if answer:
            return answer
        print("  A value is required.")


def prompt_yes_no(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def pick_from_list(items: list[dict], label_fn, detail_fn=None) -> dict:
    """Display a numbered list and let the user pick one."""
    for i, item in enumerate(items, 1):
        label = label_fn(item)
        if detail_fn:
            detail = detail_fn(item)
            print(f"  {i}. {label}  ({detail})")
        else:
            print(f"  {i}. {label}")
    while True:
        choice = input("Enter number: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(items):
                return items[idx]
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(items)}.")


def user_label(user: dict) -> str:
    name = user.get("name", "")
    email = user.get("email", "")
    return f"{name} <{email}>" if email else name


def user_detail(user: dict) -> str:
    return user["id"]


# ── Immich API ───────────────────────────────────────────────────────────────

class ImmichClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"x-api-key": api_key, "Accept": "application/json"}

    def _request(self, path: str, data: dict | None = None, method: str | None = None) -> dict | list:
        """Make an HTTP request. Returns parsed JSON.

        GET when data is None, POST when data is provided (override with method param).
        """
        url = f"{self.base_url}{path}"
        if data is not None:
            body = json.dumps(data).encode()
            req = urllib.request.Request(url, data=body, headers={**self.headers, "Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url, headers=self.headers)
        if method:
            req.method = method
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def ping(self) -> bool:
        try:
            self._request("/api/server/ping")
            return True
        except (urllib.error.URLError, OSError):
            return False

    def get_users(self) -> list[dict]:
        return self._request("/api/users")

    def get_libraries(self) -> list[dict]:
        return self._request("/api/libraries")

    def create_library(self, owner_id: str, name: str, import_paths: list[str]) -> dict:
        """Create a library with exclusion pattern **/* so Immich won't scan it."""
        return self._request("/api/libraries", {
            "ownerId": owner_id,
            "name": name,
            "importPaths": import_paths,
            "exclusionPatterns": ["**/*"],
        })



# ── Docker-compose detection ─────────────────────────────────────────────────

def resolve_env_vars(value: str, env: dict[str, str]) -> str:
    """Resolve ${VAR} and $VAR references in a string using the given env dict."""
    def replacer(m):
        var_name = m.group(1) or m.group(2)
        return env.get(var_name, m.group(0))
    return re.sub(r'\$\{(\w+)\}|\$(\w+)', replacer, value)


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple .env file into a dict (no interpolation, handles quoting)."""
    env = {}
    if not path.is_file():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        env[key] = value
    return env


def load_config_yaml(path: Path) -> list[dict]:
    """Parse config.yaml and return the list of job dicts.

    Uses line-based parsing — no YAML library needed on the host.
    Returns an empty list if the file doesn't exist or can't be parsed.
    """
    if not path.is_file():
        return []
    try:
        jobs = []
        current_job = None
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- name:"):
                if current_job is not None:
                    jobs.append(current_job)
                current_job = {"name": stripped.split(":", 1)[1].strip().strip('"').strip("'")}
            elif current_job is not None and ":" in stripped and not stripped.startswith("-"):
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if value and not value.startswith("#"):
                    current_job[key] = value
        if current_job is not None:
            jobs.append(current_job)
        return jobs
    except Exception:
        return []


def extract_volumes_from_compose(compose_path: Path) -> list[str] | None:
    """Extract volume mount strings from the immich-server service in a docker-compose.yml.

    Uses line-based parsing — no YAML library needed. Looks for the immich-server
    service's volumes section and extracts lines like "- host:container".
    Returns None if parsing fails.
    """
    if not compose_path.is_file():
        return None

    try:
        lines = compose_path.read_text().splitlines()
    except Exception:
        return None

    # State machine: find immich-server service, then its volumes block
    in_server = False
    in_volumes = False
    server_indent = 0
    volumes_indent = 0
    volumes = []

    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        # Skip blank lines and comments
        if not stripped or stripped.startswith("#"):
            continue

        # Look for the immich-server service name
        if stripped in ("immich-server:", "immich_server:", "server:") and not in_server:
            in_server = True
            server_indent = indent
            continue

        # If we're inside the server service
        if in_server:
            # Exited the service block (same or less indent)
            if indent <= server_indent and not stripped.startswith("-"):
                in_server = False
                in_volumes = False
                continue

            if stripped == "volumes:":
                in_volumes = True
                volumes_indent = indent
                continue

            if in_volumes:
                # Exited the volumes block
                if indent <= volumes_indent and not stripped.startswith("-"):
                    in_volumes = False
                    continue
                # Volume line: "- host:container" or "- host:container:ro"
                if stripped.startswith("- "):
                    volumes.append(stripped[2:].strip())

    return volumes if volumes else None


def detect_paths_from_compose(compose_path: Path) -> dict[str, str] | None:
    """Try to extract volume mount paths from an Immich docker-compose.yml.

    Returns a dict with keys: upload_host, upload_container, ext_lib_host, ext_lib_container.
    Host paths are resolved to absolute paths relative to the compose file's directory.
    Returns None if detection fails.
    """
    volumes = extract_volumes_from_compose(compose_path)
    if not volumes:
        return None

    compose_dir = compose_path.parent

    # Load adjacent .env for variable resolution
    env = load_env_file(compose_dir / ".env")
    # Also load system environment as fallback
    for k, v in os.environ.items():
        env.setdefault(k, v)

    result = {}
    for vol in volumes:
        parts = vol.split(":")
        if len(parts) >= 2:
            host_path = resolve_env_vars(parts[0].strip(), env)
            container_path = parts[1].strip()

            # Resolve host path relative to the compose file's directory
            # (docker compose resolves relative paths from its own directory)
            host_path = str((compose_dir / host_path).resolve())

            # Detect upload location
            if "/usr/src/app/upload" in container_path or container_path.rstrip("/") == "/data":
                result["upload_host"] = host_path
                result["upload_container"] = container_path.rstrip("/")

            # Detect external library
            if "external" in container_path.lower() or "external" in host_path.lower():
                result["ext_lib_host"] = host_path
                result["ext_lib_container"] = container_path.rstrip("/")

    if "upload_host" in result and "ext_lib_host" in result:
        return result
    return None


# ── Wizard steps ─────────────────────────────────────────────────────────────

def step_connect(existing: dict[str, str]) -> tuple[ImmichClient, str]:
    """Step 1: Connect to Immich and verify access. Returns (client, api_key)."""
    print("\n═══ Step 1: Connect to Immich ═══\n")

    base_url = prompt("Immich server URL", "http://localhost:2283")
    api_key_default = existing.get("IMMICH_API_KEY", "")
    if api_key_default:
        api_key = prompt_secret("Admin API key", api_key_default)
    else:
        api_key = prompt_secret("Admin API key")

    client = ImmichClient(base_url, api_key)

    print("  Connecting...")
    if not client.ping():
        print(f"  Error: Cannot reach Immich at {base_url}")
        print("  Check that the URL is correct and Immich is running.")
        sys.exit(1)
    print("  Connected to Immich.")

    # Verify we can list users (requires admin)
    try:
        users = client.get_users()
        if not users:
            print("  Error: No users found. Is this an admin API key?")
            sys.exit(1)
        print(f"  Verified admin access ({len(users)} users found).")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print("  Error: This API key doesn't have admin access.")
            print("  The setup wizard needs an admin key to list users and create libraries.")
        else:
            print(f"  Error: HTTP {e.code} — {e.reason}")
        sys.exit(1)

    return client, api_key


def select_target_user(users: list[dict], source_user: dict) -> dict:
    """Pick the target user (receives synced copies), excluding the source user."""
    candidates = [u for u in users if u["id"] != source_user["id"]]
    if len(candidates) == 1:
        print(f"  Target user: {user_label(candidates[0])}")
        return candidates[0]
    print("\nSelect the target user (receives synced copies):\n")
    return pick_from_list(candidates, user_label, user_detail)


def step_detect_paths(existing: dict[str, str]) -> dict[str, str]:
    """Step 2: Detect or ask for paths."""
    print("\n═══ Step 2: Configure paths ═══\n")

    # If we already have paths from a previous run, offer to reuse them
    existing_upload = existing.get("UPLOAD_LOCATION", "")
    existing_ext = existing.get("EXTERNAL_LIBRARY_DIR", "")
    if existing_upload and existing_ext:
        print(f"  Current paths:")
        print(f"    Upload location:  {existing_upload}")
        print(f"    External library: {existing_ext}")
        if prompt_yes_no("\n  Keep these paths?"):
            paths = {
                "upload_host": existing_upload,
                "upload_container": existing.get("UPLOAD_LOCATION_MOUNT", "/usr/src/app/upload"),
                "ext_lib_host": existing_ext,
                "ext_lib_container": existing.get("EXTERNAL_LIBRARY_MOUNT", "/external_library"),
            }
            paths["upload_host"] = str(Path(paths["upload_host"]).expanduser().resolve())
            paths["ext_lib_host"] = str(Path(paths["ext_lib_host"]).expanduser().resolve())
            return paths

    compose_default = "../immich-app/docker-compose.yml"
    compose_path_str = prompt(
        "Path to Immich's docker-compose.yml (or 'skip' to enter paths manually)",
        compose_default,
    )

    paths = None
    if compose_path_str.lower() != "skip":
        compose_path = Path(compose_path_str).expanduser().resolve()
        paths = detect_paths_from_compose(compose_path)
        if paths:
            print(f"\n  Auto-detected from {compose_path}:")
            print(f"    Upload location: {paths['upload_host']} -> {paths['upload_container']}")
            print(f"    External library: {paths['ext_lib_host']} -> {paths['ext_lib_container']}")
            if not prompt_yes_no("\n  Use these paths?"):
                paths = None

    if paths is None:
        print("\n  Enter paths manually:\n")
        upload_host = prompt("Host path to Immich upload/data directory",
                             existing_upload or "../immich-app/library")
        ext_lib_host = prompt("Host path to external library directory",
                              existing_ext or "../immich-app/external_library")
        upload_container = prompt("Container mount for upload directory",
                                  existing.get("UPLOAD_LOCATION_MOUNT", "/usr/src/app/upload"))
        ext_lib_container = prompt("Container mount for external library",
                                   existing.get("EXTERNAL_LIBRARY_MOUNT", "/external_library"))
        paths = {
            "upload_host": upload_host,
            "upload_container": upload_container,
            "ext_lib_host": ext_lib_host,
            "ext_lib_container": ext_lib_container,
        }

    # Resolve to absolute paths
    paths["upload_host"] = str(Path(paths["upload_host"]).expanduser().resolve())
    paths["ext_lib_host"] = str(Path(paths["ext_lib_host"]).expanduser().resolve())

    return paths


def find_or_create_library(
    client: ImmichClient, target_user: dict, import_path: str, default_name: str
) -> dict:
    """Check for an existing library matching the import path, or create a new one.

    Returns the library dict (with at least an 'id' key).
    """
    libraries = client.get_libraries()
    # Find libraries owned by the target user whose import paths overlap
    matches = []
    for lib in libraries:
        if lib.get("ownerId") != target_user["id"]:
            continue
        lib_paths = lib.get("importPaths", [])
        if import_path in lib_paths:
            matches.append(lib)

    if matches:
        print(f"\n  Found existing library matching import path {import_path}:")
        for lib in matches:
            print(f"    - {lib.get('name', '(unnamed)')}  ({lib['id']})")
        if len(matches) == 1:
            if prompt_yes_no("  Use this library?"):
                return matches[0]
        else:
            if prompt_yes_no("  Use one of these libraries?"):
                return pick_from_list(
                    matches,
                    lambda l: l.get("name", "(unnamed)"),
                    lambda l: l["id"],
                )

    lib_name = prompt("Library name", default_name)
    print(f"\n  Creating external library for {user_label(target_user)}...")
    print(f"    Import path: {import_path}")
    print(f"    Exclusion: **/*")

    library = client.create_library(target_user["id"], lib_name, [import_path])
    print(f"  Library created: {library['id']}")
    return library


def configure_external_library_job(
    client: ImmichClient, users: list[dict], paths: dict,
    existing_job: dict | None = None,
) -> dict:
    """Configure an external library sync job. Returns a job config dict."""
    print("\n── External Library Sync ──\n")

    print("Select the source user (whose external library to sync from):\n")
    source_user = pick_from_list(users, user_label, user_detail)

    target_user = select_target_user(users, source_user)

    ext_lib_host = paths["ext_lib_host"]
    ext_lib_container = paths["ext_lib_container"]

    print(f"\n  External library host path: {ext_lib_host}")

    # Show directories in the external library
    if os.path.isdir(ext_lib_host):
        dirs = sorted(
            e for e in os.listdir(ext_lib_host)
            if os.path.isdir(os.path.join(ext_lib_host, e))
        )
        if dirs:
            print(f"\n  Directories in {ext_lib_host}:")
            for d in dirs:
                full = os.path.join(ext_lib_host, d)
                suffix = " -> " + os.readlink(full) if os.path.islink(full) else ""
                print(f"    {d}{suffix}")
        print()

    source_path = prompt(
        "Source path within external library (e.g. 'donncha/photos')"
    )
    target_path = prompt(
        "Target symlink path within external library (e.g. 'jacinta/photos')",
    )

    # Determine symlink source and target
    source_real = os.path.join(ext_lib_host, source_path)
    target_link = os.path.join(ext_lib_host, target_path)

    if not os.path.exists(source_real):
        print(f"\n  Warning: source path {source_real} does not exist.")
        if not prompt_yes_no("  Continue anyway?", default=False):
            sys.exit(1)

    # Create symlink
    print(f"\n  Symlink: {target_link} -> {source_real}")
    if os.path.exists(target_link) or os.path.islink(target_link):
        print(f"  (already exists)")
    else:
        if prompt_yes_no("  Create this symlink?"):
            os.makedirs(os.path.dirname(target_link), exist_ok=True)
            os.symlink(source_real, target_link)
            print("  Symlink created.")
        else:
            print("  Skipped symlink creation (create it manually before running the sidecar).")

    # Library import path: parent directory of the symlink
    target_parent = os.path.dirname(target_path)
    if target_parent:
        import_path = f"{ext_lib_container}/{target_parent}"
    else:
        import_path = ext_lib_container

    source_name = source_user.get("name", "source")
    library = find_or_create_library(
        client, target_user, import_path, f"Shared from {source_name}"
    )

    # Compute path prefixes (as seen inside the Immich container)
    shared_prefix = f"{ext_lib_container}/{source_path.rstrip('/')}/"
    target_prefix = f"{ext_lib_container}/{target_path.rstrip('/')}/"

    return {
        "type": "external",
        "source_user_id": source_user["id"],
        "target_user_id": target_user["id"],
        "target_library_id": library["id"],
        "source_path_prefix": shared_prefix,
        "target_path_prefix": target_prefix,
    }


def configure_upload_sync_job(
    client: ImmichClient, users: list[dict], paths: dict,
    exclude_library_ids: list[str] | None = None,
) -> dict:
    """Configure an internal library (upload) sync job. Returns a job config dict."""
    print("\n── Internal Library Sync ──\n")

    print("Select the source user (whose internal library to sync):\n")
    source_user = pick_from_list(users, user_label, user_detail)

    target_user = select_target_user(users, source_user)

    ext_lib_host = paths["ext_lib_host"]
    ext_lib_container = paths["ext_lib_container"]
    upload_host = paths["upload_host"]
    upload_container = paths["upload_container"]

    source_name = source_user.get("name", "source").lower().replace(" ", "_")
    target_path_default = f"{target_user.get('name', 'target').lower().replace(' ', '_')}_library/{source_name}"
    target_path = prompt("Target symlink path within external library", target_path_default)

    source_library_host = os.path.join(upload_host, "library", source_user["id"])
    target_link = os.path.join(ext_lib_host, target_path)

    source_library_container = f"{upload_container}/library/{source_user['id']}"

    print(f"\n  Symlink: {target_link} -> {source_library_host}")
    print(f"  (In-container: {ext_lib_container}/{target_path} -> {source_library_container})")

    if not os.path.exists(source_library_host):
        print(f"\n  Warning: source library path {source_library_host} does not exist.")
        print("  This is normal if the user hasn't uploaded any photos yet.")

    if os.path.exists(target_link) or os.path.islink(target_link):
        print(f"  (symlink already exists)")
    else:
        if prompt_yes_no("  Create this symlink?"):
            os.makedirs(os.path.dirname(target_link), exist_ok=True)
            os.symlink(source_library_host, target_link)
            print("  Symlink created.")
        else:
            print("  Skipped symlink creation (create it manually before running the sidecar).")

    # Library import path: parent directory of the symlink
    target_parent = os.path.dirname(target_path)
    if target_parent:
        import_path = f"{ext_lib_container}/{target_parent}"
    else:
        import_path = ext_lib_container

    # Find existing libraries with **/* exclusion (not scanning for new files)
    exclude_ids = set(exclude_library_ids or [])
    libraries = client.get_libraries()
    candidates = [
        lib for lib in libraries
        if lib.get("ownerId") == target_user["id"]
        and "**/*" in lib.get("exclusionPatterns", [])
        and lib["id"] not in exclude_ids
    ]

    library = None
    if candidates:
        print(f"\n  Found existing library/libraries with **/* exclusion:")
        for lib in candidates:
            paths_str = ", ".join(lib.get("importPaths", []))
            print(f"    - {lib.get('name', '(unnamed)')}  ({lib['id']})")
            if paths_str:
                print(f"      import paths: {paths_str}")
        if len(candidates) == 1:
            if prompt_yes_no("  Use this library?"):
                library = candidates[0]
        else:
            if prompt_yes_no("  Use one of these libraries?"):
                library = pick_from_list(
                    candidates,
                    lambda l: l.get("name", "(unnamed)"),
                    lambda l: l["id"],
                )

    if library is None:
        source_display = source_user.get("name", "source")
        lib_name = prompt("Library name", f"Internal library from {source_display}")
        print(f"\n  Creating external library for {user_label(target_user)}...")
        print(f"    Import path: {import_path}")
        print(f"    Exclusion: **/*")
        library = client.create_library(target_user["id"], lib_name, [import_path])
        print(f"  Library created: {library['id']}")

    # Source path prefix: internal library path in the container
    source_prefix = f"{upload_container}/library/{source_user['id']}/"
    # Target path prefix in the container
    target_upload_prefix = f"{ext_lib_container}/{target_path.rstrip('/')}/"

    return {
        "type": "upload",
        "source_user_id": source_user["id"],
        "target_user_id": target_user["id"],
        "target_library_id": library["id"],
        "source_path_prefix": source_prefix,
        "target_path_prefix": target_upload_prefix,
    }


def step_configure_jobs(
    client: ImmichClient, users: list[dict], paths: dict,
    existing_jobs: list[dict],
) -> list[dict]:
    """Step 3: Configure sync jobs in a loop."""
    print("\n═══ Step 3: Configure sync jobs ═══\n")

    if existing_jobs:
        print("  Existing jobs:")
        for j in existing_jobs:
            album_str = f", album={j.get('album_id', '')}" if j.get("album_id") else ""
            print(f"    - {j.get('name', '?')}: {j.get('source_user_id', '?')[:8]}... -> {j.get('target_user_id', '?')[:8]}...{album_str}")
        if prompt_yes_no("\n  Keep existing jobs and add more?"):
            jobs = list(existing_jobs)
        else:
            jobs = []
    else:
        jobs = []

    # Collect library IDs already used (to avoid reusing them)
    used_library_ids = [j.get("target_library_id", "") for j in jobs]

    while True:
        print(f"\n  Jobs configured so far: {len(jobs)}")
        if jobs:
            for j in jobs:
                print(f"    - {j.get('name', '?')}")

        if not jobs:
            print("\n  What type of sync job do you want to add?")
        else:
            print("\n  Add another sync job?")
            if not prompt_yes_no("  Add another job?", default=False):
                break
            print()

        print("  1. External Library Sync — sync from a user's external library")
        print("  2. Internal Library Sync — sync from a user's app/web uploads")
        while True:
            choice = input("\nEnter number: ").strip()
            if choice in ("1", "2"):
                break
            print("  Please enter 1 or 2.")

        if choice == "1":
            job = configure_external_library_job(client, users, paths)
        else:
            job = configure_upload_sync_job(client, users, paths, used_library_ids)

        # Give the job a name
        default_name = f"job-{len(jobs) + 1}"
        if job["type"] == "external":
            default_name = f"external-{len(jobs) + 1}"
        elif job["type"] == "upload":
            default_name = f"upload-{len(jobs) + 1}"
        job["name"] = prompt("Job name", default_name)

        # Per-job album assignment
        album_id = step_album_for_job(job["name"])
        if album_id:
            job["album_id"] = album_id

        jobs.append(job)
        used_library_ids.append(job.get("target_library_id", ""))

    if not jobs:
        print("\n  Error: At least one sync job is required.")
        sys.exit(1)

    return jobs


def step_album_for_job(job_name: str) -> str | None:
    """Optional: assign an album to a specific job."""
    if not prompt_yes_no(f"  Add synced assets to an album for job '{job_name}'?", default=False):
        return None

    print("\n  Create an album in the target user's Immich UI, then paste the ID from the URL.")
    print("  Example: http://immich:2283/albums/<album-id-here>\n")
    return prompt("Album ID (UUID)")


def step_database(existing: dict[str, str]) -> str:
    """Step 4: Database settings."""
    print("\n═══ Step 4: Database settings ═══\n")
    print("The sidecar connects to Immich's PostgreSQL database.")
    print("The hostname is resolved automatically inside Docker (immich_postgres).\n")
    db_password = prompt_secret("Database password", existing.get("DB_PASSWORD", "postgres"))
    return db_password


def generate_config_yaml(jobs: list[dict]) -> str:
    """Generate config.yaml content from a list of job dicts.

    Uses string formatting (no pyyaml needed on host).
    """
    lines = [
        "# Sync job configuration for immich-shared-library",
        "",
        "sync_jobs:",
    ]

    for job in jobs:
        lines.append(f'  - name: "{job["name"]}"')
        lines.append(f'    source_user_id: "{job["source_user_id"]}"')
        lines.append(f'    target_user_id: "{job["target_user_id"]}"')
        lines.append(f'    target_library_id: "{job["target_library_id"]}"')
        lines.append(f'    source_path_prefix: "{job["source_path_prefix"]}"')
        lines.append(f'    target_path_prefix: "{job["target_path_prefix"]}"')
        if job.get("album_id"):
            lines.append(f'    album_id: "{job["album_id"]}"')
        lines.append("")

    return "\n".join(lines)


def generate_env(
    *,
    api_key: str,
    paths: dict[str, str],
    db_password: str,
) -> str:
    """Generate .env file content (infrastructure only)."""
    lines = []

    lines.append("# Database (same credentials as your Immich .env)")
    lines.append("DB_HOSTNAME=immich_postgres")
    lines.append(f"DB_PASSWORD={db_password}")
    lines.append("")

    lines.append("# Paths to Immich's data directories on the host")
    lines.append(f"UPLOAD_LOCATION={paths['upload_host']}")
    lines.append(f"EXTERNAL_LIBRARY_DIR={paths['ext_lib_host']}")
    lines.append("")

    # Only include container mounts if non-default
    if paths["upload_container"] != "/usr/src/app/upload":
        lines.append(f"UPLOAD_LOCATION_MOUNT={paths['upload_container']}")
    if paths["ext_lib_container"] != "/external_library":
        lines.append(f"EXTERNAL_LIBRARY_MOUNT={paths['ext_lib_container']}")
    if paths["upload_container"] != "/usr/src/app/upload" or paths["ext_lib_container"] != "/external_library":
        lines.append("")

    lines.append("# Immich API")
    lines.append(f"IMMICH_API_KEY={api_key}")
    lines.append("")

    lines.append("# SYNC_INTERVAL_SECONDS=60")
    lines.append("# LOG_LEVEL=INFO")
    lines.append("")

    return "\n".join(lines)


def enable_config_volume_mount() -> None:
    """Uncomment the config.yaml volume mount in docker-compose.yml if present."""
    compose_path = Path("docker-compose.yml")
    if not compose_path.is_file():
        return

    content = compose_path.read_text()
    old_line = "      # - ./config.yaml:/app/config.yaml:ro"
    new_line = "      - ./config.yaml:/app/config.yaml:ro"

    if old_line in content:
        content = content.replace(old_line, new_line)
        compose_path.write_text(content)
        print("  Enabled config.yaml volume mount in docker-compose.yml")
    elif new_line in content:
        pass  # Already enabled


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("╔══════════════════════════════════════════════╗")
    print("║  immich-shared-library — Setup Wizard        ║")
    print("╚══════════════════════════════════════════════╝")

    # Load existing .env if present
    existing = load_env_file(Path(".env"))
    if existing:
        print("\n  Found existing .env — values will be used as defaults.")

    # Load existing config.yaml if present
    existing_jobs = load_config_yaml(Path("config.yaml"))
    if existing_jobs:
        print(f"  Found existing config.yaml — {len(existing_jobs)} job(s) configured.")

    # Step 1: Connect to Immich
    client, api_key = step_connect(existing)
    users = client.get_users()

    # Step 2: Detect paths
    paths = step_detect_paths(existing)

    # Step 3: Configure sync jobs
    jobs = step_configure_jobs(client, users, paths, existing_jobs)

    # Step 4: Database
    db_password = step_database(existing)

    # Generate config.yaml
    yaml_content = generate_config_yaml(jobs)

    # Generate .env (infrastructure only)
    env_content = generate_env(
        api_key=api_key,
        paths=paths,
        db_password=db_password,
    )

    print("\n═══ Summary ═══\n")
    print("── config.yaml ──")
    print(yaml_content)
    print("── .env ──")
    print(env_content)

    # Write config.yaml
    yaml_path = Path("config.yaml")
    if yaml_path.exists():
        print(f"\nWarning: {yaml_path} already exists.")
        if not prompt_yes_no("Overwrite?", default=False):
            alt = Path("config.yaml.generated")
            alt.write_text(yaml_content)
            print(f"  Saved to {alt} instead.")
        else:
            yaml_path.write_text(yaml_content)
            print(f"  Saved to {yaml_path}")
    else:
        yaml_path.write_text(yaml_content)
        print(f"  Saved to {yaml_path}")

    # Write .env
    env_path = Path(".env")
    if env_path.exists():
        print(f"\nWarning: {env_path} already exists.")
        if not prompt_yes_no("Overwrite?", default=False):
            alt = Path(".env.generated")
            alt.write_text(env_content)
            print(f"  Saved to {alt} instead.")
            print(f"  Review and rename: mv {alt} .env")
        else:
            env_path.write_text(env_content)
            print(f"  Saved to {env_path}")
    else:
        env_path.write_text(env_content)
        print(f"  Saved to {env_path}")

    # Enable config volume mount in docker-compose.yml
    enable_config_volume_mount()

    print_next_steps()


def print_next_steps():
    print("\n═══ Next steps ═══\n")
    print("  1. Review config.yaml and .env")
    print("  2. Start the sidecar:")
    print("       docker compose up -d")
    print("  3. Check logs:")
    print("       docker compose logs -f")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        sys.exit(1)
