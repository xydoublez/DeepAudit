"""Add batch audit tables (schedules / batch runs / batch run items)

Revision ID: 009_add_batch_audit_tables
Revises: 008_add_files_with_findings
Create Date: 2026-09-16

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '009_add_batch_audit_tables'
down_revision = '008_add_files_with_findings'
branch_labels = None
depends_on = None


def _existing_tables() -> set:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def upgrade() -> None:
    tables = _existing_tables()

    # ============ 定时批量审计计划 ============
    if 'audit_schedules' not in tables:
        op.create_table(
            'audit_schedules',
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('name', sa.String(255), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('created_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),

            # 项目范围
            sa.Column('project_scope', sa.String(20), nullable=False, server_default='all'),
            sa.Column('project_ids', sa.JSON(), nullable=True),

            # 并发控制
            sa.Column('concurrency', sa.Integer(), nullable=False, server_default='5'),
            sa.Column('skip_if_running', sa.Boolean(), nullable=False, server_default=sa.true()),

            # 调度配置
            sa.Column('schedule_type', sa.String(20), nullable=False, server_default='daily'),
            sa.Column('schedule_config', sa.JSON(), nullable=True),
            sa.Column('timezone', sa.String(64), nullable=False, server_default='Asia/Shanghai'),

            # 审计参数模板
            sa.Column('task_template', sa.JSON(), nullable=True),

            # 状态与执行记录
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_run_id', sa.String(36), nullable=True),

            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            'ix_audit_schedules_enabled_next_run',
            'audit_schedules', ['enabled', 'next_run_at'],
        )

    # ============ 批次执行记录 ============
    if 'audit_batch_runs' not in tables:
        op.create_table(
            'audit_batch_runs',
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column(
                'schedule_id', sa.String(36),
                sa.ForeignKey('audit_schedules.id', ondelete='SET NULL'),
                nullable=True,
            ),
            sa.Column('created_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('trigger_type', sa.String(20), nullable=False, server_default='scheduled'),

            sa.Column('status', sa.String(30), nullable=False, server_default='pending'),
            sa.Column('concurrency', sa.Integer(), nullable=False, server_default='5'),

            # 进度计数
            sa.Column('total_projects', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('queued_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('running_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('completed_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('failed_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('skipped_count', sa.Integer(), nullable=False, server_default='0'),

            sa.Column('error_message', sa.Text(), nullable=True),

            sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index('ix_audit_batch_runs_schedule_id', 'audit_batch_runs', ['schedule_id'])
        op.create_index('ix_audit_batch_runs_status', 'audit_batch_runs', ['status'])

    # ============ 批次内单项目明细 ============
    if 'audit_batch_run_items' not in tables:
        op.create_table(
            'audit_batch_run_items',
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column(
                'run_id', sa.String(36),
                sa.ForeignKey('audit_batch_runs.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column(
                'project_id', sa.String(36),
                sa.ForeignKey('projects.id', ondelete='SET NULL'),
                nullable=True,
            ),
            sa.Column('project_name', sa.String(255), nullable=True),

            sa.Column('agent_task_id', sa.String(36), nullable=True),
            sa.Column('status', sa.String(20), nullable=False, server_default='queued'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('findings_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('error_message', sa.Text(), nullable=True),

            sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index('ix_batch_run_items_run_id', 'audit_batch_run_items', ['run_id'])
        op.create_index(
            'ix_batch_run_items_run_status',
            'audit_batch_run_items', ['run_id', 'status'],
        )
        op.create_index(
            'ix_batch_run_items_agent_task',
            'audit_batch_run_items', ['agent_task_id'],
        )


def downgrade() -> None:
    tables = _existing_tables()

    if 'audit_batch_run_items' in tables:
        op.drop_index('ix_batch_run_items_agent_task', table_name='audit_batch_run_items')
        op.drop_index('ix_batch_run_items_run_status', table_name='audit_batch_run_items')
        op.drop_index('ix_batch_run_items_run_id', table_name='audit_batch_run_items')
        op.drop_table('audit_batch_run_items')

    if 'audit_batch_runs' in tables:
        op.drop_index('ix_audit_batch_runs_status', table_name='audit_batch_runs')
        op.drop_index('ix_audit_batch_runs_schedule_id', table_name='audit_batch_runs')
        op.drop_table('audit_batch_runs')

    if 'audit_schedules' in tables:
        op.drop_index('ix_audit_schedules_enabled_next_run', table_name='audit_schedules')
        op.drop_table('audit_schedules')
