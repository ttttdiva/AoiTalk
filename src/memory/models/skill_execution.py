"""Business outcomes of existing Skills, distinct from prompt usage receipts."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Integer, JSON, String, UniqueConstraint

from .base import Base, _encrypted_json_property


class SkillExecution(Base):
    __tablename__ = "skill_executions"

    id = Column(String(64), primary_key=True)
    user_id = Column(String(200), nullable=False, index=True)
    session_id = Column(String(200), nullable=False)
    request_id = Column(String(160), nullable=False)
    skill_name = Column(String(160), nullable=False)
    skill_path = Column(String(512), nullable=False)
    skill_hash = Column(String(64), nullable=False)
    input_digest = Column(String(64), nullable=False)
    active_key = Column(String(64), nullable=True, unique=True)
    commit_key = Column(String(64), nullable=True, unique=True)
    status = Column(String(32), nullable=False, default="running")
    reason = Column(String(160), nullable=False, default="")
    commit_dispatched = Column(Boolean, nullable=False, default=False)
    steps_completed = Column(Integer, nullable=False, default=0)
    lease_owner = Column(String(64), nullable=True)
    lease_until = Column(DateTime, nullable=True)
    _context = Column("context", JSON, nullable=False, default=dict)
    context = _encrypted_json_property("_context", "skill_executions.context")
    _evidence = Column("evidence", JSON, nullable=False, default=dict)
    evidence = _encrypted_json_property("_evidence", "skill_executions.evidence")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="uq_skill_execution_request"),
        CheckConstraint("status IN ('running','succeeded','failed','interrupted','needs_user','unknown')",
                        name="ck_skill_execution_status"),
    )

    def to_dict(self):
        return {"execution_id": self.id, "request_id": self.request_id,
                "skill_name": self.skill_name, "skill_hash": self.skill_hash,
                "status": self.status, "reason": self.reason,
                "success": self.status == "succeeded", "steps_completed": self.steps_completed,
                "completion_verified": self.status == "succeeded",
                "commit_dispatched": self.commit_dispatched, "evidence": self.evidence or {},
                "resume_required": self.status in {"interrupted", "needs_user", "unknown"} and not self.context.get("cancelled")}
