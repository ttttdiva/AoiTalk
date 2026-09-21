"""Repair ambiguous Knowledge date backfills without guessing host timezones.

Revision ID: 20260908_0003
Revises: 20260908_0002
"""
from datetime import date, datetime, time, timezone
import re

from alembic import op
import sqlalchemy as sa

revision = "20260908_0003"
down_revision = "20260908_0002"
branch_labels = None
depends_on = None

# Freeze the date policy here: old migrations must not import the live runtime
# or change behavior when ingestion evolves.
_DATE_KEYS = (
    "date", "document_date", "documentDate", "published_at", "publishedAt",
    "published", "created_at", "createdAt", "created", "updated_at",
    "updatedAt", "updated", "lastmod", "last_modified",
)
_GROWI_KEYS = ("updated_at", "updatedAt", "updated", "last_updated_at", "lastUpdatedAt")


def _parse(value, *, require_offset=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, time.min)
        else:
            raw = str(value).strip()
            if re.fullmatch(r"\d{8}", raw):
                parsed = datetime.strptime(raw, "%Y%m%d")
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("/", "-"))
        if parsed.utcoffset() is None:
            if require_offset:
                return None
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _recover(metadata, path):
    metadata = metadata if isinstance(metadata, dict) else {}
    for mapping in (metadata, metadata.get("metadata"), metadata.get("meta")):
        if isinstance(mapping, dict):
            for key in _DATE_KEYS:
                parsed = _parse(mapping.get(key))
                if parsed is not None:
                    return parsed, "frontmatter"
    basename = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    match = re.search(r"(?<!\d)(?:(\d{4})[-_.](\d{1,2})[-_.](\d{1,2})|(\d{4})(\d{2})(\d{2}))(?!\d)", basename)
    if match:
        indexes = (1, 2, 3) if match.group(1) else (4, 5, 6)
        try:
            return datetime(*(int(match.group(i)) for i in indexes), tzinfo=timezone.utc), "filename"
        except ValueError:
            pass
    growi = metadata.get("growi")
    if isinstance(growi, dict):
        for key in _GROWI_KEYS:
            parsed = _parse(growi.get(key), require_offset=True)
            if parsed is not None:
                return parsed, "growi"
    return None, "unknown"


def upgrade():
    documents = sa.table(
        "knowledge_documents",
        sa.column("id", sa.Uuid()), sa.column("path", sa.Text()),
        sa.column("frontmatter_json", sa.JSON()),
        sa.column("document_date", sa.DateTime(timezone=True)),
        sa.column("document_date_source", sa.String(32)),
    )
    bind = op.get_bind()
    # A legacy naive mtime and a subsequently synchronized UTC mtime cannot be
    # distinguished from this column pair. Recover independent evidence only;
    # source sync restores a proven fallback. Existing semantic dates survive.
    rows = bind.execute(sa.select(
        documents.c.id, documents.c.path, documents.c.frontmatter_json,
    ).where(sa.or_(
        documents.c.document_date_source.is_(None),
        documents.c.document_date_source.in_(("modified_at", "unknown")),
    )).execution_options(stream_results=True))
    try:
        while batch := rows.fetchmany(250):
            for row in batch:
                value, origin = _recover(row.frontmatter_json, row.path)
                bind.execute(documents.update().where(documents.c.id == row.id).values(
                    document_date=value, document_date_source=origin,
                ))
    finally:
        rows.close()


def downgrade():
    # This is a data correction, not a schema change. Deliberately do not
    # fabricate the ambiguous UTC dates again when moving the revision back.
    pass
