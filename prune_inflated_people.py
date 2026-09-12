#!/usr/bin/env python3
"""
Delete person groups that only exist because synced faces were counted twice.

Sidecars before schema v4 copied face_search embeddings verbatim, so every
synced face had a byte-identical twin at distance 0. Immich's recognition
counts k-NN matches against minFaces to decide whether a cluster is "core", and
a twin is a match — so each real face voted twice, and a person appearing in
two synced photos cleared a threshold of 3 and became a person who should not
exist.

Schema v4 stops new copies doing this and strips the embeddings from existing
ones, but it cannot undo a recognition run that already happened. This script
is that remediation: it finds person groups whose *original* (non-copied) faces
number fewer than minFaces and deletes them, leaving their faces unassigned —
where minFaces would have left them had the twins never existed.

A group is judged on original faces only. A face is a copy when its asset is a
target in _face_sync_asset_map; everything else is original. Named people are
never touched, on the assumption that a name means a human decided the person
was real regardless of how it was clustered.

Usage:
  python3 prune_inflated_people.py --min-faces 3          # dry run
  python3 prune_inflated_people.py --min-faces 3 --apply
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

# Load .env before importing from src: src.config builds its settings singleton
# at import time. Import must stay side-effect-free enough to be testable, so a
# missing .env is only fatal in main().
ENV_FILE = Path(__file__).parent / ".env"

if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key and value:
            os.environ.setdefault(key.strip(), value.strip())

os.environ.setdefault("SYNC_INTERVAL_SECONDS", "9999")

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)

from src.db import close_pool, init_pool, transaction


# One definition of "inflated", used by both the preview and the delete, so the
# two can never disagree about which groups are in scope.
INFLATED_GROUPS_CTE = """
WITH face_class AS (
    SELECT af."personGroupId" AS grp,
           (m.target_asset_id IS NOT NULL) AS is_copy
    FROM asset_face af
    LEFT JOIN _face_sync_asset_map m ON m.target_asset_id = af."assetId"
    WHERE af."deletedAt" IS NULL AND af."personGroupId" IS NOT NULL
), per_group AS (
    SELECT grp,
           count(*) AS total_faces,
           count(*) FILTER (WHERE NOT is_copy) AS original_faces
    FROM face_class
    GROUP BY grp
), inflated AS (
    SELECT g.grp, g.total_faces, g.original_faces
    FROM per_group g
    WHERE g.original_faces < $1
      -- A name means a human judged this person real; leave it alone.
      AND NOT EXISTS (
          SELECT 1 FROM person p
          WHERE p."personGroupId" = g.grp AND p.name <> ''
      )
)
"""


async def preview(conn, min_faces: int) -> dict:
    row = await conn.fetchrow(
        INFLATED_GROUPS_CTE + """
        SELECT count(*) AS groups,
               coalesce(sum(total_faces), 0) AS faces_unassigned,
               coalesce(sum(original_faces), 0) AS original_faces,
               (SELECT count(*) FROM person p JOIN inflated i ON i.grp = p."personGroupId")
                   AS person_rows
        FROM inflated
        """,
        min_faces,
    )
    return dict(row)


async def prune(conn, min_faces: int) -> int:
    """Delete the inflated person groups themselves.

    Deleting person_group rather than person makes the effect immediate and
    total: person cascades from person_group (ON DELETE CASCADE) and
    asset_face."personGroupId" is ON DELETE SET NULL, so the faces are
    unassigned in the same statement. Deleting only the person rows would leave
    a person-less group behind until Immich's own deleteEmptyGroups happened to
    run, with the faces still pointing at it in the meantime.

    Both audit triggers still fire -- person_group_delete_audit at depth 0 and
    person_delete_audit at depth 1 -- so clients learn the people are gone.

    Returns the number of groups deleted.
    """
    result = await conn.execute(
        INFLATED_GROUPS_CTE + """
        DELETE FROM person_group pg
        USING inflated i
        WHERE pg.id = i.grp
        """,
        min_faces,
    )
    return int(result.rsplit(" ", 1)[-1])


async def main(min_faces: int, apply: bool) -> None:
    if not ENV_FILE.exists():
        print("Error: .env not found. Copy env.example to .env and fill in your values.")
        sys.exit(1)

    from src.config import settings
    print(f"Connecting to {settings.db_hostname}:{settings.db_port}/{settings.db_database_name}")
    await init_pool()

    try:
        async with transaction() as conn:
            stats = await preview(conn, min_faces)

            print(f"\nPerson groups with fewer than {min_faces} original faces:")
            print(f"  groups:                 {stats['groups']}")
            print(f"  person rows:            {stats['person_rows']}")
            print(f"  faces they hold:        {stats['faces_unassigned']}")
            print(f"  of which original:      {stats['original_faces']}")
            print("\nNamed people are excluded.")

            if stats["groups"] == 0:
                print("\nNothing to prune.")
                return

            if not apply:
                print("\n[DRY RUN] Nothing was changed. Re-run with --apply to delete.")
                return

            deleted = await prune(conn, min_faces)
            print(f"\nDeleted {deleted} person group(s).")
            print("Their faces are unassigned again, as minFaces would have left them.")
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Delete person groups that only exist because synced faces were double-counted.",
    )
    parser.add_argument(
        "--min-faces", type=int, default=3,
        help="Match your Immich facial recognition minFaces setting (default: 3)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually delete. Without this the script only reports.",
    )
    args = parser.parse_args()
    asyncio.run(main(min_faces=args.min_faces, apply=args.apply))
