"""FW申請 Docsツールの参照ストア8テーブルを追加

Revision ID: 20260707_0001
Revises: 20260706_0011
Create Date: 2026-07-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260707_0001"
down_revision: Union[str, None] = "20260706_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _provenance_columns() -> list:
    """全FW参照テーブル共通の provenance 列(平文)。"""
    return [
        sa.Column("source", sa.Text()),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
        sa.Column(
            "is_partial", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "import_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_import_jobs.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    ]


def _base_columns() -> list:
    """id + workspace_id FK。"""
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    # --- fw_segments ---
    op.create_table(
        "fw_segments",
        *_base_columns(),
        sa.Column("cidr", postgresql.CIDR(), nullable=False),
        sa.Column("zone", sa.Text(), nullable=False),
        sa.Column("label", sa.Text()),
        sa.Column("note", sa.Text()),
        *_provenance_columns(),
    )
    op.create_index(
        "ix_fw_segments_cidr",
        "fw_segments",
        ["cidr"],
        postgresql_using="gist",
        postgresql_ops={"cidr": "inet_ops"},
    )
    op.create_index("ix_fw_segments_workspace", "fw_segments", ["workspace_id"])

    # --- fw_matrix ---
    op.create_table(
        "fw_matrix",
        *_base_columns(),
        sa.Column("src_zone", sa.Text(), nullable=False),
        sa.Column("dst_zone", sa.Text(), nullable=False),
        sa.Column("devices", sa.Text()),  # ";" 区切り保持
        sa.Column("note", sa.Text()),
        sa.Column("raw", sa.Text()),
        *_provenance_columns(),
    )
    op.create_index("ix_fw_matrix_src_dst", "fw_matrix", ["src_zone", "dst_zone"])
    op.create_index("ix_fw_matrix_workspace", "fw_matrix", ["workspace_id"])

    # --- fw_nsg_subnets ---
    op.create_table(
        "fw_nsg_subnets",
        *_base_columns(),
        sa.Column("scope", sa.Text()),
        sa.Column("region", sa.Text()),
        sa.Column("nsg_name", sa.Text(), nullable=False),
        sa.Column("subnet", postgresql.CIDR(), nullable=False),
        sa.Column("resource_group", sa.Text()),
        sa.Column("subscription", sa.Text()),
        *_provenance_columns(),
    )
    op.create_index(
        "ix_fw_nsg_subnets_subnet",
        "fw_nsg_subnets",
        ["subnet"],
        postgresql_using="gist",
        postgresql_ops={"subnet": "inet_ops"},
    )
    op.create_index("ix_fw_nsg_subnets_workspace", "fw_nsg_subnets", ["workspace_id"])

    # --- fw_object_map ---
    op.create_table(
        "fw_object_map",
        *_base_columns(),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("palo_object", sa.Text()),
        sa.Column("cp_object", sa.Text()),
        sa.Column("note", sa.Text()),
        *_provenance_columns(),
    )
    op.create_index("ix_fw_object_map_keyword", "fw_object_map", ["keyword"])
    op.create_index("ix_fw_object_map_workspace", "fw_object_map", ["workspace_id"])

    # --- fw_devices ---
    op.create_table(
        "fw_devices",
        *_base_columns(),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text()),
        sa.Column("mgmt_url", sa.Text()),
        sa.Column("evidence_dir", sa.Text()),
        sa.Column("cp_layer", sa.Text()),
        sa.Column("palo_vsys", sa.Text()),
        sa.Column("note", sa.Text()),
        *_provenance_columns(),
        sa.UniqueConstraint(
            "workspace_id", "device_id", name="uq_fw_devices_device_id"
        ),
    )
    op.create_index("ix_fw_devices_workspace", "fw_devices", ["workspace_id"])

    # --- fw_locations ---
    op.create_table(
        "fw_locations",
        *_base_columns(),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("zone_hint", sa.Text()),  # "|" 区切り保持
        sa.Column("note", sa.Text()),
        *_provenance_columns(),
    )
    op.create_index("ix_fw_locations_workspace", "fw_locations", ["workspace_id"])

    # --- fw_palo_zones ---
    op.create_table(
        "fw_palo_zones",
        *_base_columns(),
        sa.Column("zone", sa.Text(), nullable=False),
        sa.Column("palo_zone", sa.Text()),
        sa.Column("note", sa.Text()),
        *_provenance_columns(),
    )
    op.create_index("ix_fw_palo_zones_zone", "fw_palo_zones", ["zone"])
    op.create_index("ix_fw_palo_zones_workspace", "fw_palo_zones", ["workspace_id"])

    # --- fw_object_inventory ---
    op.create_table(
        "fw_object_inventory",
        *_base_columns(),
        sa.Column("device_role", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text()),
        sa.Column("object_name", sa.Text(), nullable=False),
        sa.Column("members", sa.Text()),  # ";" 区切り
        sa.Column("resolved_cidrs", sa.Text()),  # ";" 区切り
        sa.Column("note", sa.Text()),
        *_provenance_columns(),
    )
    op.create_index(
        "ix_fw_object_inventory_device_role", "fw_object_inventory", ["device_role"]
    )
    op.create_index(
        "ix_fw_object_inventory_workspace", "fw_object_inventory", ["workspace_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fw_object_inventory_workspace", table_name="fw_object_inventory"
    )
    op.drop_index(
        "ix_fw_object_inventory_device_role", table_name="fw_object_inventory"
    )
    op.drop_table("fw_object_inventory")

    op.drop_index("ix_fw_palo_zones_workspace", table_name="fw_palo_zones")
    op.drop_index("ix_fw_palo_zones_zone", table_name="fw_palo_zones")
    op.drop_table("fw_palo_zones")

    op.drop_index("ix_fw_locations_workspace", table_name="fw_locations")
    op.drop_table("fw_locations")

    op.drop_index("ix_fw_devices_workspace", table_name="fw_devices")
    op.drop_table("fw_devices")

    op.drop_index("ix_fw_object_map_workspace", table_name="fw_object_map")
    op.drop_index("ix_fw_object_map_keyword", table_name="fw_object_map")
    op.drop_table("fw_object_map")

    op.drop_index("ix_fw_nsg_subnets_workspace", table_name="fw_nsg_subnets")
    op.drop_index("ix_fw_nsg_subnets_subnet", table_name="fw_nsg_subnets")
    op.drop_table("fw_nsg_subnets")

    op.drop_index("ix_fw_matrix_workspace", table_name="fw_matrix")
    op.drop_index("ix_fw_matrix_src_dst", table_name="fw_matrix")
    op.drop_table("fw_matrix")

    op.drop_index("ix_fw_segments_workspace", table_name="fw_segments")
    op.drop_index("ix_fw_segments_cidr", table_name="fw_segments")
    op.drop_table("fw_segments")
