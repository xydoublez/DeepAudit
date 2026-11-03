"""Add API Key fields to users

Revision ID: 011_add_api_key_to_users
Revises: 010_add_report_download_tables
Create Date: 2026-11-03

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '011_add_api_key_to_users'
down_revision = '010_add_report_download_tables'
branch_labels = None
depends_on = None


def _existing_columns() -> set:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns("users")}


def upgrade() -> None:
    columns = _existing_columns()

    if "api_key_hash" not in columns:
        op.add_column("users", sa.Column("api_key_hash", sa.String(64), nullable=True))
        op.create_index("ix_users_api_key_hash", "users", ["api_key_hash"], unique=True)
    if "api_key_prefix" not in columns:
        op.add_column("users", sa.Column("api_key_prefix", sa.String(16), nullable=True))
    if "api_key_created_at" not in columns:
        op.add_column("users", sa.Column("api_key_created_at", sa.DateTime(timezone=True), nullable=True))
    if "api_key_expires_at" not in columns:
        op.add_column("users", sa.Column("api_key_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    columns = _existing_columns()

    if "api_key_expires_at" in columns:
        op.drop_column("users", "api_key_expires_at")
    if "api_key_created_at" in columns:
        op.drop_column("users", "api_key_created_at")
    if "api_key_prefix" in columns:
        op.drop_column("users", "api_key_prefix")
    if "api_key_hash" in columns:
        op.drop_index("ix_users_api_key_hash", table_name="users")
        op.drop_column("users", "api_key_hash")
