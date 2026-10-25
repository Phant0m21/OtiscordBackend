"""Add profile details and presence tracking.

Revision ID: 0003_profile_presence
Revises: 0002_realtime_attachments
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_profile_presence"
down_revision = "0002_realtime_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("bio", sa.String(length=190), nullable=False, server_default=""))
    op.add_column("users", sa.Column("birth_date", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("avatar_key", sa.String(length=256), nullable=True))
    op.add_column("users", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("is_online", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_users_last_seen_at", "users", ["last_seen_at"])
    with op.batch_alter_table("users") as batch:
        batch.alter_column("bio", server_default=None)
        batch.alter_column("is_online", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_users_last_seen_at", table_name="users")
    op.drop_column("users", "is_online")
    op.drop_column("users", "last_seen_at")
    op.drop_column("users", "avatar_key")
    op.drop_column("users", "birth_date")
    op.drop_column("users", "bio")
