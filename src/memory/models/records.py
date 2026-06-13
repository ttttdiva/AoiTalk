"""プロジェクトスコープの柔軟レコードテーブル系モデル。"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    String,
    Text,
    Integer,
    DateTime,
    Float,
    JSON,
    ForeignKey,
    Boolean,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base, _encrypted_text_property, _encrypted_json_property


class RecordTable(Base):
    """Project-scoped flexible table definition."""

    __tablename__ = "record_tables"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    description = Column(Text)
    icon = Column(String(64))
    sort_order = Column(Float, default=0)
    schema_version = Column(Integer, default=1)
    memory_policy = Column(String(32), default="manual")
    default_sensitivity = Column(String(32), default="normal")
    table_metadata = Column(JSON, default=dict)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True, index=True)

    project = relationship("Project", back_populates="record_tables")
    fields = relationship(
        "RecordField",
        back_populates="table",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    rows = relationship(
        "RecordRow",
        back_populates="table",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    views = relationship(
        "RecordView",
        back_populates="table",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_record_tables_project_sort", "project_id", "sort_order"),
    )


class RecordField(Base):
    """Column definition for a flexible record table."""

    __tablename__ = "record_fields"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    table_id = Column(
        UUID(as_uuid=True),
        ForeignKey("record_tables.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column("field_key", String(120), nullable=False)
    label = Column(String(200), nullable=False)
    field_type = Column(String(32), nullable=False)
    options = Column(JSON, default=dict)
    required = Column(Boolean, default=False)
    unique_value = Column(Boolean, default=False)
    sort_order = Column(Float, default=0)
    is_title = Column(Boolean, default=False)
    is_due = Column(Boolean, default=False)
    sensitivity = Column(String(32), default="normal")
    field_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True, index=True)

    table = relationship("RecordTable", back_populates="fields")

    __table_args__ = (
        UniqueConstraint("table_id", "field_key", name="unique_record_field_key"),
        Index("ix_record_fields_table_sort", "table_id", "sort_order"),
    )


class RecordRow(Base):
    """JSON-backed row for a flexible record table."""

    __tablename__ = "record_rows"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    table_id = Column(
        UUID(as_uuid=True),
        ForeignKey("record_tables.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    _values = Column("values", JSON, default=dict)
    values = _encrypted_json_property("_values", "record_rows.values")
    _title = Column("title", Text)
    title = _encrypted_text_property("_title", "record_rows.title")
    status = Column(String(64))
    due_at = Column(DateTime, nullable=True, index=True)
    _search_text = Column("search_text", Text)
    search_text = _encrypted_text_property("_search_text", "record_rows.search_text")
    sensitivity = Column(String(32), default="normal")
    row_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True, index=True)

    table = relationship("RecordTable", back_populates="rows")
    attachments = relationship(
        "RecordAttachment",
        back_populates="row",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_record_rows_table_updated", "table_id", "updated_at"),
        Index("ix_record_rows_project_table", "project_id", "table_id"),
    )


class RecordView(Base):
    """Saved view settings for a record table."""

    __tablename__ = "record_views"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    table_id = Column(
        UUID(as_uuid=True),
        ForeignKey("record_tables.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    view_type = Column(String(32), default="grid")
    config = Column(JSON, default=dict)
    sort_order = Column(Float, default=0)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    table = relationship("RecordTable", back_populates="views")


class RecordAttachment(Base):
    """File attachment linked to a record row."""

    __tablename__ = "record_attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    row_id = Column(
        UUID(as_uuid=True),
        ForeignKey("record_rows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_path = Column(Text, nullable=False)
    file_name = Column(String(255))
    mime_type = Column(String(120))
    size_bytes = Column(Integer)
    source_hash = Column(String(128))
    attachment_metadata = Column(JSON, default=dict)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)

    row = relationship("RecordRow", back_populates="attachments")


class RecordEvent(Base):
    """Audit/provenance event for project records."""

    __tablename__ = "record_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    table_id = Column(
        UUID(as_uuid=True),
        ForeignKey("record_tables.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    row_id = Column(
        UUID(as_uuid=True),
        ForeignKey("record_rows.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    actor_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    event_type = Column(String(64), nullable=False)
    payload = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_record_events_project_created", "project_id", "created_at"),
    )
