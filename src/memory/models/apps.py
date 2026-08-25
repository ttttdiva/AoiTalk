"""永続的な Apps 機能の SQLAlchemy モデル。

App は Project の子要素ではなく、ProjectApp を介して複数の Project から
利用できる第一級エンティティとして扱う。Manifest と workspace が App の
作業コピーの正本で、app_targets は検索・表示用の派生スナップショットである。

App 越境参照（別 App の Target / Release を指す行）は API 検証だけでなく
DB でも禁止する。app_targets / app_releases に (app_id, id) の複合一意キーを
置き、参照側は単独 FK ではなく (app_id, 参照 id) の複合 FK で結ぶ。
実 DB 側の ON DELETE は PostgreSQL 15 以降の列指定付き
``SET NULL (列名)`` を使い、複合 FK でも app_id を NULL 化しない。
SQLAlchemy 2.0 は列指定付き ON DELETE を DDL 生成できないため、
モデル側は ``ondelete="SET NULL"`` と表記する。スキーマの正本は
Alembic（実 DB）であり、差分は scripts/check_schema_drift.py で検知する。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import Base


APP_BINDING_MODES = {"development", "installed"}
APP_GRANT_PERMISSIONS = {"viewer", "runner", "developer", "maintainer", "admin"}
APP_TARGET_SURFACES = {
    "embedded_web",
    "standalone_web",
    "desktop_gui",
    "headless",
    "office",
}
APP_TARGET_RUNTIMES = {
    "static_web",
    "node",
    "python",
    "powershell",
    "batch",
    "vba",
    "executable",
}
APP_EXECUTION_HOSTS = {
    "aoitalk",
    "server",
    "client",
    "browser",
    "office",
    "download_only",
}
APP_RELEASE_STATUSES = {"published", "deprecated"}
APP_JOB_TYPES = {"build", "test", "run", "package"}
APP_JOB_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}
TASK_APP_RELATIONS = {"develops", "fixes", "tests", "releases", "uses", "related"}


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _uuid(value: Any) -> str | None:
    return str(value) if value else None


class App(Base):
    """App source, manifest and release namespace."""

    __tablename__ = "apps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    origin_project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name = Column(String(255), nullable=False)
    slug = Column(String(120), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    visibility = Column(String(32), nullable=False, default="private", index=True)
    default_target_key = Column(String(80), nullable=True)
    readme_node_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        index=True,
    )
    archived_at = Column(DateTime, nullable=True, index=True)

    # Let the database execute the declared ON DELETE CASCADE.  Without
    # passive_deletes SQLAlchemy attempts to NULL owner_user_id first, which
    # violates this required ownership column and can leave the filesystem
    # cleanup out of sync with the database deletion.
    owner = relationship(
        "User",
        foreign_keys=[owner_user_id],
        backref="owned_apps",
        passive_deletes=True,
    )
    origin_project = relationship("Project", foreign_keys=[origin_project_id])
    readme_node = relationship("KnowledgeNode", foreign_keys=[readme_node_id])
    targets = relationship(
        "AppTarget",
        back_populates="app",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AppTarget.target_key",
    )
    releases = relationship(
        "AppRelease",
        back_populates="app",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AppRelease.created_at.desc()",
    )
    project_bindings = relationship(
        "ProjectApp",
        back_populates="app",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    grants = relationship(
        "AppGrant",
        back_populates="app",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    jobs = relationship(
        "AppJob",
        back_populates="app",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "visibility IN ('private','shared','public')",
            name="ck_apps_visibility",
        ),
    )

    def to_dict(self, *, include_targets: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "origin_project_id": _uuid(self.origin_project_id),
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "visibility": self.visibility,
            "default_target_key": self.default_target_key,
            "readme_node_id": _uuid(self.readme_node_id),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
            "archived_at": _dt(self.archived_at),
        }
        if include_targets:
            payload["targets"] = [target.to_dict() for target in self.targets or []]
        return payload


class ProjectApp(Base):
    """A Project-to-App binding; deleting it never deletes the App."""

    __tablename__ = "project_apps"

    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    app_id = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="CASCADE"),
        primary_key=True,
    )
    binding_mode = Column(String(20), nullable=False, default="development")
    # 単独 FK ではなく (app_id, installed_release_id) の複合 FK で結ぶ。
    # 別 App の Release を install 済みとして記録できないようにする。
    installed_release_id = Column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    enabled = Column(Boolean, nullable=False, default=True)
    pinned = Column(Boolean, nullable=False, default=False)
    display_alias = Column(String(255), nullable=True)
    config_json = Column(JSON, nullable=False, default=dict)
    capability_grants_json = Column(JSON, nullable=False, default=dict)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    project = relationship("Project", backref="app_bindings", passive_deletes=True)
    app = relationship("App", back_populates="project_bindings")
    # 複合 FK に app_id が含まれるため、relationship の join は
    # installed_release_id 単独に固定して ORM の重複管理を避ける。
    installed_release = relationship(
        "AppRelease",
        primaryjoin="ProjectApp.installed_release_id == AppRelease.id",
        foreign_keys=[installed_release_id],
    )
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        CheckConstraint(
            "binding_mode IN ('development','installed')",
            name="ck_project_apps_binding_mode",
        ),
        # 実 DB は ON DELETE SET NULL (installed_release_id)。
        ForeignKeyConstraint(
            ["app_id", "installed_release_id"],
            ["app_releases.app_id", "app_releases.id"],
            name="fk_project_apps_installed_release_app",
            ondelete="SET NULL",
        ),
        Index("ix_project_apps_project_enabled", "project_id", "enabled"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": _uuid(self.project_id),
            "app_id": _uuid(self.app_id),
            "binding_mode": self.binding_mode,
            "installed_release_id": _uuid(self.installed_release_id),
            "enabled": bool(self.enabled),
            "pinned": bool(self.pinned),
            "display_alias": self.display_alias,
            "config_json": self.config_json or {},
            "capability_grants_json": self.capability_grants_json or {},
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }


class AppGrant(Base):
    """Viewer/runner/developer/etc. grant for one user or one Project."""

    __tablename__ = "app_grants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    app_id = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    permission = Column(String(20), nullable=False, default="viewer")
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    app = relationship("App", back_populates="grants")
    user = relationship("User", foreign_keys=[user_id])
    project = relationship("Project")
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        CheckConstraint(
            "(user_id IS NULL) <> (project_id IS NULL)",
            name="ck_app_grants_exactly_one_subject",
        ),
        CheckConstraint(
            "permission IN ('viewer','runner','developer','maintainer','admin')",
            name="ck_app_grants_permission",
        ),
        # Grant は 1 App × 1 主体につき 1 行で、permission は上書き更新する
        # （POST /api/apps/{id}/grants は (app_id, user_id, project_id) で
        # 既存行を引いて permission を差し替える）。permission を含む一意キーは
        # 同じ主体に別 permission の行を並行 INSERT できてしまうため、
        # 主体単位の部分一意インデックスで重複を防ぐ。SQL の UNIQUE は NULL を
        # 相異なる値として扱うので、対象主体側が NOT NULL の行だけを対象にする。
        Index(
            "uq_app_grants_app_user",
            "app_id",
            "user_id",
            unique=True,
            postgresql_where=user_id.is_not(None),
            sqlite_where=user_id.is_not(None),
        ),
        Index(
            "uq_app_grants_app_project",
            "app_id",
            "project_id",
            unique=True,
            postgresql_where=project_id.is_not(None),
            sqlite_where=project_id.is_not(None),
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "app_id": _uuid(self.app_id),
            "user_id": _uuid(self.user_id),
            "project_id": _uuid(self.project_id),
            "permission": self.permission,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }


class AppTarget(Base):
    """Manifest-derived target metadata."""

    __tablename__ = "app_targets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    app_id = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_key = Column(String(80), nullable=False)
    display_name = Column(String(255), nullable=False)
    surface = Column(String(32), nullable=False)
    runtime = Column(String(32), nullable=False)
    execution_host = Column(String(32), nullable=False)
    entrypoint = Column(Text, nullable=False)
    manifest_snapshot = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    app = relationship("App", back_populates="targets")
    artifacts = relationship(
        "AppArtifact",
        back_populates="target",
        primaryjoin="AppTarget.id == AppArtifact.target_id",
        foreign_keys="AppArtifact.target_id",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("app_id", "target_key", name="uq_app_targets_app_target_key"),
        # 参照側（app_artifacts / app_jobs / task_app_links）から
        # (app_id, target_id) の複合 FK を張るための一意キー。
        UniqueConstraint("app_id", "id", name="uq_app_targets_app_id_id"),
        CheckConstraint(
            "surface IN ('embedded_web','standalone_web','desktop_gui','headless','office')",
            name="ck_app_targets_surface",
        ),
        CheckConstraint(
            "runtime IN ('static_web','node','python','powershell','batch','vba','executable')",
            name="ck_app_targets_runtime",
        ),
        CheckConstraint(
            "execution_host IN ('aoitalk','server','client','browser','office','download_only')",
            name="ck_app_targets_execution_host",
        ),
        Index("ix_app_targets_app_surface", "app_id", "surface"),
    )

    def to_dict(self) -> Dict[str, Any]:
        snapshot = self.manifest_snapshot or {}
        return {
            "id": _uuid(self.id),
            "app_id": _uuid(self.app_id),
            "target_key": self.target_key,
            "display_name": self.display_name,
            "surface": self.surface,
            "runtime": self.runtime,
            "execution_host": self.execution_host,
            "entrypoint": self.entrypoint,
            "manifest_snapshot": snapshot,
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }


class AppRelease(Base):
    """Immutable release pointer to an App Git revision."""

    __tablename__ = "app_releases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    app_id = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version = Column(String(80), nullable=False)
    git_revision = Column(String(80), nullable=False)
    manifest_hash = Column(String(64), nullable=False)
    readme_hash = Column(String(64), nullable=False)
    changelog = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="published", index=True)
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    app = relationship("App", back_populates="releases")
    creator = relationship("User", foreign_keys=[created_by])
    artifacts = relationship(
        "AppArtifact",
        back_populates="release",
        primaryjoin="AppRelease.id == AppArtifact.release_id",
        foreign_keys="AppArtifact.release_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("app_id", "version", name="uq_app_releases_app_version"),
        # 参照側（app_artifacts / app_jobs / project_apps）から
        # (app_id, release_id) の複合 FK を張るための一意キー。
        UniqueConstraint("app_id", "id", name="uq_app_releases_app_id_id"),
        CheckConstraint(
            "status IN ('published','deprecated')",
            name="ck_app_releases_status",
        ),
        Index("ix_app_releases_app_status_created", "app_id", "status", "created_at"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "app_id": _uuid(self.app_id),
            "version": self.version,
            "git_revision": self.git_revision,
            "manifest_hash": self.manifest_hash,
            "readme_hash": self.readme_hash,
            "changelog": self.changelog,
            "status": self.status,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts or []],
        }


class AppArtifact(Base):
    """Release artifact stored below workspaces/_app_artifacts."""

    __tablename__ = "app_artifacts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Release と Target が同じ App に属することを DB で保証するための非正規化列。
    # INSERT 時に app_id を省略しても BEFORE INSERT トリガー
    # （trg_app_artifacts_set_app_id）が release_id から補完する。明示指定した
    # 値が Release / Target の App と食い違う場合は複合 FK が INSERT を弾く。
    app_id = Column(UUID(as_uuid=True), nullable=False)
    release_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    target_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    artifact_type = Column(String(32), nullable=False)
    file_path = Column(Text, nullable=False)
    filename = Column(String(255), nullable=False)
    sha256 = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    release = relationship(
        "AppRelease",
        back_populates="artifacts",
        primaryjoin="AppArtifact.release_id == AppRelease.id",
        foreign_keys=[release_id],
    )
    target = relationship(
        "AppTarget",
        back_populates="artifacts",
        primaryjoin="AppArtifact.target_id == AppTarget.id",
        foreign_keys=[target_id],
    )

    __table_args__ = (
        UniqueConstraint(
            "release_id",
            "target_id",
            "artifact_type",
            "filename",
            name="uq_app_artifacts_release_target_file",
        ),
        ForeignKeyConstraint(
            ["app_id", "release_id"],
            ["app_releases.app_id", "app_releases.id"],
            name="fk_app_artifacts_release_app",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["app_id", "target_id"],
            ["app_targets.app_id", "app_targets.id"],
            name="fk_app_artifacts_target_app",
            ondelete="RESTRICT",
        ),
        Index("ix_app_artifacts_release_target", "release_id", "target_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "release_id": _uuid(self.release_id),
            "target_id": _uuid(self.target_id),
            "artifact_type": self.artifact_type,
            "file_path": self.file_path,
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "created_at": _dt(self.created_at),
        }


class AppJob(Base):
    """Durable build/test/run/package job record."""

    __tablename__ = "app_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    app_id = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Target / Release は (app_id, ...) の複合 FK で App に閉じ込める。
    target_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    release_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    agent_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    job_type = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="queued", index=True)
    input_json = Column(JSON, nullable=False, default=dict)
    result_json = Column(JSON, nullable=False, default=dict)
    log_path = Column(Text, nullable=True)
    exit_code = Column(Integer, nullable=True)
    started_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)

    app = relationship("App", back_populates="jobs")
    target = relationship(
        "AppTarget",
        primaryjoin="AppJob.target_id == AppTarget.id",
        foreign_keys=[target_id],
    )
    project = relationship("Project")
    release = relationship(
        "AppRelease",
        primaryjoin="AppJob.release_id == AppRelease.id",
        foreign_keys=[release_id],
    )
    agent_run = relationship("AgentRun")
    starter = relationship("User", foreign_keys=[started_by])

    __table_args__ = (
        CheckConstraint(
            "job_type IN ('build','test','run','package')",
            name="ck_app_jobs_job_type",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_app_jobs_status",
        ),
        # 実 DB は ON DELETE SET NULL (target_id) / SET NULL (release_id)。
        ForeignKeyConstraint(
            ["app_id", "target_id"],
            ["app_targets.app_id", "app_targets.id"],
            name="fk_app_jobs_target_app",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["app_id", "release_id"],
            ["app_releases.app_id", "app_releases.id"],
            name="fk_app_jobs_release_app",
            ondelete="SET NULL",
        ),
        Index("ix_app_jobs_app_status_started", "app_id", "status", "started_at"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "app_id": _uuid(self.app_id),
            "target_id": _uuid(self.target_id),
            "project_id": _uuid(self.project_id),
            "release_id": _uuid(self.release_id),
            "agent_run_id": _uuid(self.agent_run_id),
            "job_type": self.job_type,
            "status": self.status,
            "input_json": self.input_json or {},
            "result_json": self.result_json or {},
            "log_path": self.log_path,
            "exit_code": self.exit_code,
            "started_by": _uuid(self.started_by),
            "started_at": _dt(self.started_at),
            "ended_at": _dt(self.ended_at),
        }


class TaskAppLink(Base):
    """Formal Task ↔ App relation, including optional target scope."""

    __tablename__ = "task_app_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    app_id = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # (app_id, target_id) の複合 FK で、別 App の Target を指す Link を禁じる。
    target_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    relation_type = Column(String(20), nullable=False, default="related")
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    task = relationship("Task", backref="app_links")
    app = relationship("App", backref="task_links")
    target = relationship(
        "AppTarget",
        primaryjoin="TaskAppLink.target_id == AppTarget.id",
        foreign_keys=[target_id],
    )
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        CheckConstraint(
            "relation_type IN ('develops','fixes','tests','releases','uses','related')",
            name="ck_task_app_links_relation_type",
        ),
        # 実 DB は ON DELETE SET NULL (target_id)。
        ForeignKeyConstraint(
            ["app_id", "target_id"],
            ["app_targets.app_id", "app_targets.id"],
            name="fk_task_app_links_target_app",
            ondelete="SET NULL",
        ),
        UniqueConstraint(
            "task_id",
            "app_id",
            "target_id",
            "relation_type",
            name="uq_task_app_links_target_relation",
        ),
        # SQL NULLs are distinct in a normal UNIQUE constraint.  Keep a
        # separate partial key for links without a Target so concurrent
        # requests cannot create duplicate task/App relations.
        Index(
            "uq_task_app_links_no_target",
            "task_id",
            "app_id",
            "relation_type",
            unique=True,
            postgresql_where=target_id.is_(None),
            sqlite_where=target_id.is_(None),
        ),
        Index("ix_task_app_links_task_relation", "task_id", "relation_type"),
        Index("ix_task_app_links_app_relation", "app_id", "relation_type"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "task_id": _uuid(self.task_id),
            "app_id": _uuid(self.app_id),
            "target_id": _uuid(self.target_id),
            "relation_type": self.relation_type,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }


__all__ = [
    "APP_BINDING_MODES",
    "APP_EXECUTION_HOSTS",
    "APP_GRANT_PERMISSIONS",
    "APP_JOB_STATUSES",
    "APP_JOB_TYPES",
    "APP_RELEASE_STATUSES",
    "APP_TARGET_RUNTIMES",
    "APP_TARGET_SURFACES",
    "TASK_APP_RELATIONS",
    "App",
    "ProjectApp",
    "AppGrant",
    "AppTarget",
    "AppRelease",
    "AppArtifact",
    "AppJob",
    "TaskAppLink",
]
