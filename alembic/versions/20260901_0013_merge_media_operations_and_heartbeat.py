"""Merge the Media Operations and Heartbeat migration heads.

The Media Operations schedule branch (``20260901_0012``) and the Heartbeat
history branch (``20260901_hb01``) were developed independently.  This
revision is intentionally a schema no-op; it records their convergence so a
fresh upgrade has one canonical Alembic head.
"""

from __future__ import annotations


revision = "20260901_0013"
down_revision = ("20260901_0012", "20260901_hb01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
