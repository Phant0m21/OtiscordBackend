"""Add message sync timestamps and private file attachments.

Revision ID: 0002_realtime_attachments
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_realtime_attachments"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE messages SET updated_at = created_at WHERE updated_at IS NULL")
    with op.batch_alter_table("messages") as batch:
        batch.alter_column(
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
    op.create_index("ix_messages_updated_at", "messages", ["updated_at"])
    op.create_table(
        "attachments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("uploader_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=256), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=127), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploader_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index("ix_attachments_conversation_id", "attachments", ["conversation_id"])
    op.create_index("ix_attachments_message_id", "attachments", ["message_id"])
    op.create_index("ix_attachments_uploader_id", "attachments", ["uploader_id"])


def downgrade() -> None:
    op.drop_index("ix_attachments_uploader_id", table_name="attachments")
    op.drop_index("ix_attachments_message_id", table_name="attachments")
    op.drop_index("ix_attachments_conversation_id", table_name="attachments")
    op.drop_table("attachments")
    op.drop_index("ix_messages_updated_at", table_name="messages")
    op.drop_column("messages", "updated_at")
