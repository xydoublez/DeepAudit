"""Add report download tables (tasks / items)

Revision ID: 010_add_report_download_tables
Revises: 009_add_batch_audit_tables
Create Date: 2026-09-21

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '010_add_report_download_tables'
down_revision = '009_add_batch_audit_tables'
branch_labels = None
depends_on = None


def _existing_tables() -> set:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def upgrade() -> None:
    tables = _existing_tables()

    # ============ 批量报告下载任务 ============
    if 'report_download_tasks' not in tables:
        op.create_table(
            'report_download_tasks',
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('created_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),

            # 下载配置
            sa.Column('output_dir', sa.String(512), nullable=False),
            sa.Column('concurrency', sa.Integer(), nullable=False, server_default='5'),
            sa.Column('filename_pattern', sa.String(20), nullable=False, server_default='id_name'),
            sa.Column('gitlab_url', sa.String(512), nullable=True),
            sa.Column('gitlab_ref', sa.String(255), nullable=True),

            # 进度计数
            sa.Column('total_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('completed_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('failed_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('skipped_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('current_items', sa.JSON(), nullable=True),

            # 状态与结果
            sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('summary_path', sa.String(512), nullable=True),
            sa.Column('log_path', sa.String(512), nullable=True),

            sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        )

    # ============ 批量报告下载明细 ============
    if 'report_download_items' not in tables:
        op.create_table(
            'report_download_items',
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('task_id', sa.String(36),
                      sa.ForeignKey('report_download_tasks.id', ondelete='CASCADE'), nullable=False),

            # 项目信息
            sa.Column('project_id', sa.String(36), nullable=True),
            sa.Column('project_name', sa.String(255), nullable=True),
            sa.Column('gitlab_project_id', sa.Integer(), nullable=True),
            sa.Column('gitlab_namespace', sa.String(512), nullable=True),
            sa.Column('gitlab_web_url', sa.String(512), nullable=True),

            # 报告来源
            sa.Column('agent_task_id', sa.String(36), nullable=True),

            sa.Column('status', sa.String(20), nullable=False, server_default='queued'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('error_message', sa.Text(), nullable=True),

            # 产物信息
            sa.Column('file_path', sa.String(512), nullable=True),
            sa.Column('file_size', sa.Integer(), nullable=True),

            sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            'ix_report_download_items_task_status',
            'report_download_items',
            ['task_id', 'status'],
        )


def downgrade() -> None:
    op.drop_index('ix_report_download_items_task_status', table_name='report_download_items')
    op.drop_table('report_download_items')
    op.drop_table('report_download_tasks')
