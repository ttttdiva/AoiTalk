"""Compatibility marker for the Docs lifecycle migration chain.

Some deployed development databases were stamped with ``20260831_0006`` by
an unreleased branch that is not present in the public migration tree.  Keep a
no-op revision at that identifier so those databases can advance safely to
the explicit-blank projection migration instead of failing with an unknown
revision error.  This revision intentionally performs no schema work.
"""

from __future__ import annotations


revision = "20260831_0006"
down_revision = "20260830_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
