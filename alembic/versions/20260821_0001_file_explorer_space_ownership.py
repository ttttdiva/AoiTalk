"""Add Space ownership to Files bookmarks and launchers.

The pre-task-14 schema stores both collections under ``user_id``.  This
forward migration deliberately keeps those rows intact and creates a separate
Space-owned clone only when the target can be resolved to one active project
Space *and* the row's user is that Space owner.  A shared clone therefore has
``space_id`` set and ``user_id`` NULL; unresolved/private/member rows remain
user-owned (``space_id`` NULL, ``user_id`` non-NULL).

Bookmark folders require a little more work than a direct target: an eligible
folder ancestor is cloned once for each descendant Space using a deterministic
UUID, and cloned children are remapped to that same-Space ancestor.  Existing
personal hierarchy rows are never rewritten.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260821_0001"
down_revision = "20260818_0006"
branch_labels = None
depends_on = None


_PROJECT_UUID_RE = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
_PROJECT_TARGET_RE = rf"^_projects/project_{_PROJECT_UUID_RE}(/|$)"
_RECORD_TARGET_RE = rf"^aoitalk-record-table:{_PROJECT_UUID_RE}:{_PROJECT_UUID_RE}$"


_BOOKMARK_CANDIDATES_SQL = f"""
INSERT INTO _file_explorer_bookmark_space_candidates (row_id, space_id)
SELECT b.id, p.space_id
FROM file_explorer_bookmarks AS b
JOIN projects AS p
  ON p.id = CASE
      WHEN b.path ~* '{_PROJECT_TARGET_RE}'
      THEN substring(b.path FROM '^_projects/project_({_PROJECT_UUID_RE})(/|$)')::uuid
      ELSE NULL
    END
JOIN spaces AS s ON s.id = p.space_id AND s.owner_id = b.user_id
WHERE b.user_id IS NOT NULL
  AND b.space_id IS NULL
  AND b.kind = 'bookmark'
  AND p.space_id IS NOT NULL
  AND p.deleted_at IS NULL
GROUP BY b.id, p.space_id
HAVING count(*) = 1
UNION
SELECT b.id, p.space_id
FROM file_explorer_bookmarks AS b
JOIN record_tables AS rt
  ON rt.id = CASE
      WHEN b.path ~* '{_RECORD_TARGET_RE}'
      THEN split_part(b.path, ':', 3)::uuid
      ELSE NULL
    END
JOIN projects AS p ON p.id = rt.project_id
JOIN spaces AS s ON s.id = p.space_id AND s.owner_id = b.user_id
WHERE b.user_id IS NOT NULL
  AND b.space_id IS NULL
  AND b.kind = 'bookmark'
  AND rt.deleted_at IS NULL
  AND p.space_id IS NOT NULL
  AND p.deleted_at IS NULL
  AND CASE
      WHEN b.path ~* '{_RECORD_TARGET_RE}'
      THEN split_part(b.path, ':', 2)::uuid
      ELSE NULL
    END = p.id
GROUP BY b.id, p.space_id
HAVING count(*) = 1
ON CONFLICT (row_id, space_id) DO NOTHING
"""


_BOOKMARK_ANCESTOR_CANDIDATES_SQL = """
WITH RECURSIVE walk(source_id, source_user_id, space_id, ancestor_id) AS (
    SELECT c.row_id, b.user_id, c.space_id, b.parent_id
    FROM _file_explorer_bookmark_space_candidates AS c
    JOIN file_explorer_bookmarks AS b ON b.id = c.row_id
    WHERE b.parent_id IS NOT NULL
    UNION
    SELECT w.source_id, w.source_user_id, w.space_id, parent.parent_id
    FROM walk AS w
    JOIN file_explorer_bookmarks AS parent
      ON parent.id = w.ancestor_id
     AND parent.user_id = w.source_user_id
    WHERE parent.parent_id IS NOT NULL
)
INSERT INTO _file_explorer_bookmark_space_candidates (row_id, space_id)
SELECT DISTINCT walk.ancestor_id, walk.space_id
FROM walk
JOIN file_explorer_bookmarks AS ancestor
  ON ancestor.id = walk.ancestor_id
 AND ancestor.user_id = walk.source_user_id
WHERE ancestor.kind = 'folder'
ON CONFLICT (row_id, space_id) DO NOTHING
"""


_LAUNCHER_CANDIDATES_SQL = f"""
INSERT INTO _file_explorer_launcher_space_candidates (row_id, space_id)
SELECT l.id, p.space_id
FROM file_explorer_launchers AS l
JOIN projects AS p
  ON p.id = CASE
      WHEN l.path ~* '{_PROJECT_TARGET_RE}'
      THEN substring(l.path FROM '^_projects/project_({_PROJECT_UUID_RE})(/|$)')::uuid
      ELSE NULL
    END
JOIN spaces AS s ON s.id = p.space_id AND s.owner_id = l.user_id
WHERE l.user_id IS NOT NULL
  AND l.space_id IS NULL
  AND p.space_id IS NOT NULL
  AND p.deleted_at IS NULL
GROUP BY l.id, p.space_id
HAVING count(*) = 1
UNION
SELECT l.id, p.space_id
FROM file_explorer_launchers AS l
JOIN record_tables AS rt
  ON rt.id = CASE
      WHEN l.path ~* '{_RECORD_TARGET_RE}'
      THEN split_part(l.path, ':', 3)::uuid
      ELSE NULL
    END
JOIN projects AS p ON p.id = rt.project_id
JOIN spaces AS s ON s.id = p.space_id AND s.owner_id = l.user_id
WHERE l.user_id IS NOT NULL
  AND l.space_id IS NULL
  AND rt.deleted_at IS NULL
  AND p.space_id IS NOT NULL
  AND p.deleted_at IS NULL
  AND CASE
      WHEN l.path ~* '{_RECORD_TARGET_RE}'
      THEN split_part(l.path, ':', 2)::uuid
      ELSE NULL
    END = p.id
GROUP BY l.id, p.space_id
HAVING count(*) = 1
ON CONFLICT (row_id, space_id) DO NOTHING
"""


_BOOKMARK_CLONE_SQL = """
CREATE TEMP TABLE _file_explorer_bookmark_clone_map (
    source_id uuid NOT NULL,
    space_id uuid NOT NULL,
    clone_id uuid NOT NULL,
    PRIMARY KEY (source_id, space_id),
    UNIQUE (clone_id)
) ON COMMIT DROP;

INSERT INTO _file_explorer_bookmark_clone_map (source_id, space_id, clone_id)
SELECT row_id,
       space_id,
       md5(row_id::text || ':space:' || space_id::text)::uuid
FROM _file_explorer_bookmark_space_candidates;

INSERT INTO file_explorer_bookmarks (
    id, user_id, space_id, name, path, icon, kind, parent_id,
    sort_order, created_at, updated_at
)
SELECT m.clone_id,
       NULL,
       m.space_id,
       source.name,
       CASE WHEN source.kind = 'folder'
            THEN 'aoitalk-bookmark-folder:' || m.clone_id::text
            ELSE source.path
       END,
       source.icon,
       source.kind,
       NULL,
       source.sort_order,
       source.created_at,
       source.updated_at
FROM _file_explorer_bookmark_clone_map AS m
JOIN file_explorer_bookmarks AS source ON source.id = m.source_id
ON CONFLICT (id) DO NOTHING;

-- A shared child only points at the clone of its original parent in the same
-- Space.  If the parent was not eligible (for example a member-owned folder),
-- the clone remains a root instead of retaining a cross-owner link.
UPDATE file_explorer_bookmarks AS clone
SET parent_id = parent_clone.clone_id
FROM _file_explorer_bookmark_clone_map AS child_map
JOIN file_explorer_bookmarks AS source
  ON source.id = child_map.source_id
JOIN _file_explorer_bookmark_clone_map AS parent_clone
  ON parent_clone.source_id = source.parent_id
 AND parent_clone.space_id = child_map.space_id
WHERE clone.id = child_map.clone_id
  AND source.parent_id IS NOT NULL;
"""


def _prepare_bookmark_clones() -> None:
    """Create owner-only bookmark clones and remap same-Space ancestors."""

    op.execute(
        sa.text(
            """
            CREATE TEMP TABLE _file_explorer_bookmark_space_candidates (
                row_id uuid NOT NULL,
                space_id uuid NOT NULL,
                PRIMARY KEY (row_id, space_id)
            ) ON COMMIT DROP
            """
        )
    )
    op.execute(sa.text(_BOOKMARK_CANDIDATES_SQL))
    op.execute(sa.text(_BOOKMARK_ANCESTOR_CANDIDATES_SQL))
    op.execute(sa.text(_BOOKMARK_CLONE_SQL))


def _prepare_launcher_clones() -> None:
    """Create owner-only launcher clones for project/record targets."""

    op.execute(
        sa.text(
            """
            CREATE TEMP TABLE _file_explorer_launcher_space_candidates (
                row_id uuid NOT NULL,
                space_id uuid NOT NULL,
                PRIMARY KEY (row_id, space_id)
            ) ON COMMIT DROP
            """
        )
    )
    op.execute(sa.text(_LAUNCHER_CANDIDATES_SQL))
    op.execute(
        sa.text(
            """
            CREATE TEMP TABLE _file_explorer_launcher_clone_map (
                source_id uuid NOT NULL,
                space_id uuid NOT NULL,
                clone_id uuid NOT NULL,
                PRIMARY KEY (source_id, space_id),
                UNIQUE (clone_id)
            ) ON COMMIT DROP;

            INSERT INTO _file_explorer_launcher_clone_map
                (source_id, space_id, clone_id)
            SELECT row_id,
                   space_id,
                   md5(row_id::text || ':space:' || space_id::text)::uuid
            FROM _file_explorer_launcher_space_candidates;

            INSERT INTO file_explorer_launchers (
                id, user_id, space_id, name, path, icon,
                sort_order, created_at, updated_at
            )
            SELECT m.clone_id,
                   NULL,
                   m.space_id,
                   source.name,
                   source.path,
                   source.icon,
                   source.sort_order,
                   source.created_at,
                   source.updated_at
            FROM _file_explorer_launcher_clone_map AS m
            JOIN file_explorer_launchers AS source
              ON source.id = m.source_id
            ON CONFLICT (id) DO NOTHING;
            """
        )
    )


def upgrade() -> None:
    """Add nullable Space ownership and preserve legacy rows by cloning."""

    op.add_column(
        "file_explorer_bookmarks",
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "file_explorer_launchers",
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # The historical schema made user_id NOT NULL.  Make it nullable before
    # inserting Space-owned clones, whose explicit ownership is represented by
    # ``space_id`` and ``user_id IS NULL``.
    op.alter_column(
        "file_explorer_bookmarks",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "file_explorer_launchers",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_file_explorer_bookmarks_space_id",
        "file_explorer_bookmarks",
        "spaces",
        ["space_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_file_explorer_launchers_space_id",
        "file_explorer_launchers",
        "spaces",
        ["space_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # The temporary tables and clone INSERTs run before the XOR/Space unique
    # constraints are installed.  Existing rows remain user-owned and are
    # never updated in place.
    _prepare_bookmark_clones()
    _prepare_launcher_clones()

    op.create_check_constraint(
        "ck_file_explorer_bookmarks_owner_xor",
        "file_explorer_bookmarks",
        "(space_id IS NULL) <> (user_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_file_explorer_launchers_owner_xor",
        "file_explorer_launchers",
        "(space_id IS NULL) <> (user_id IS NULL)",
    )
    op.create_unique_constraint(
        "unique_file_explorer_bookmark_space_path",
        "file_explorer_bookmarks",
        ["space_id", "path"],
    )
    op.create_unique_constraint(
        "unique_file_explorer_launcher_space_path",
        "file_explorer_launchers",
        ["space_id", "path"],
    )

    op.create_index(
        "ix_file_explorer_bookmarks_space_id",
        "file_explorer_bookmarks",
        ["space_id"],
    )
    op.create_index(
        "ix_file_explorer_bookmarks_space_sort",
        "file_explorer_bookmarks",
        ["space_id", "sort_order"],
    )
    op.create_index(
        "ix_file_explorer_bookmarks_space_parent_sort",
        "file_explorer_bookmarks",
        ["space_id", "parent_id", "sort_order"],
    )
    op.create_index(
        "ix_file_explorer_launchers_space_id",
        "file_explorer_launchers",
        ["space_id"],
    )
    op.create_index(
        "ix_file_explorer_launchers_space_sort",
        "file_explorer_launchers",
        ["space_id", "sort_order"],
    )


def downgrade() -> None:
    """Remove Space-owned clones and restore the pre-task-14 schema."""

    # Originals were never updated, so deleting only shared clones restores
    # every pre-migration user row without an ownership or parent rewrite.
    op.execute(
        sa.text(
            """
            UPDATE file_explorer_bookmarks AS personal
            SET parent_id = NULL
            WHERE personal.space_id IS NULL
              AND personal.parent_id IN (
                  SELECT shared.id
                  FROM file_explorer_bookmarks AS shared
                  WHERE shared.space_id IS NOT NULL
              )
            """
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM file_explorer_bookmarks WHERE space_id IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM file_explorer_launchers WHERE space_id IS NOT NULL"
        )
    )

    op.drop_index(
        "ix_file_explorer_bookmarks_space_parent_sort",
        table_name="file_explorer_bookmarks",
    )
    op.drop_index(
        "ix_file_explorer_bookmarks_space_sort",
        table_name="file_explorer_bookmarks",
    )
    op.drop_index(
        "ix_file_explorer_bookmarks_space_id",
        table_name="file_explorer_bookmarks",
    )
    op.drop_index(
        "ix_file_explorer_launchers_space_sort",
        table_name="file_explorer_launchers",
    )
    op.drop_index(
        "ix_file_explorer_launchers_space_id",
        table_name="file_explorer_launchers",
    )
    op.drop_constraint(
        "unique_file_explorer_bookmark_space_path",
        "file_explorer_bookmarks",
        type_="unique",
    )
    op.drop_constraint(
        "unique_file_explorer_launcher_space_path",
        "file_explorer_launchers",
        type_="unique",
    )
    op.drop_constraint(
        "ck_file_explorer_bookmarks_owner_xor",
        "file_explorer_bookmarks",
        type_="check",
    )
    op.drop_constraint(
        "ck_file_explorer_launchers_owner_xor",
        "file_explorer_launchers",
        type_="check",
    )
    op.drop_constraint(
        "fk_file_explorer_bookmarks_space_id",
        "file_explorer_bookmarks",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_file_explorer_launchers_space_id",
        "file_explorer_launchers",
        type_="foreignkey",
    )
    op.drop_column("file_explorer_bookmarks", "space_id")
    op.drop_column("file_explorer_launchers", "space_id")
    op.alter_column(
        "file_explorer_bookmarks",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "file_explorer_launchers",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
