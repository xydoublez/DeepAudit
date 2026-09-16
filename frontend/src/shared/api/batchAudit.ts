/**
 * Batch Audit API
 * 定时批量审计（计划 / 批次 / 批次明细）相关的类型与接口封装
 */

import { apiClient } from "./serverClient";

// ============ Types ============

/** 调度类型 */
export type ScheduleType = "once" | "daily" | "weekly" | "interval" | "manual";

/** 项目范围 */
export type ProjectScope = "all" | "selected";

/** 批次状态 */
export type BatchRunStatus =
  | "pending"
  | "running"
  | "completed"
  | "partially_failed"
  | "failed"
  | "cancelled";

/** 批次明细状态 */
export type BatchRunItemStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "skipped"
  | "cancelled";

/** 调度参数（按 schedule_type 取用不同字段） */
export interface ScheduleConfig {
  /** daily / weekly：本地触发时刻，如 "15:00" */
  time?: string;
  /** weekly：1=周一 .. 7=周日 */
  weekdays?: number[];
  /** interval：间隔小时数 */
  interval_hours?: number;
  /** once：ISO8601 时间字符串 */
  run_at?: string;
}

/** 审计参数模板（批次内每个项目创建 AgentTask 时使用） */
export interface ScheduleTaskTemplate {
  target_vulnerabilities?: string[] | null;
  verification_level?: string;
  exclude_patterns?: string[] | null;
  target_files?: string[] | null;
  max_iterations?: number;
  timeout_seconds?: number;
  /** 留空表示各项目使用自身默认分支 */
  branch_name?: string | null;
}

/** 批次执行记录 */
export interface AuditBatchRun {
  id: string;
  schedule_id: string | null;
  created_by: string;
  trigger_type: "scheduled" | "manual";
  status: BatchRunStatus;
  concurrency: number;
  total_projects: number;
  queued_count: number;
  running_count: number;
  completed_count: number;
  failed_count: number;
  skipped_count: number;
  /** 后端计算的完成百分比 0-100 */
  progress: number;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  /** 仅 GET /runs/{id} 返回 */
  schedule_name?: string | null;
}

/** 定时计划 */
export interface AuditSchedule {
  id: string;
  name: string;
  description: string | null;
  created_by: string;
  project_scope: ProjectScope;
  project_ids: string[];
  /** scope=selected 时为已选项目数，scope=all 时为 null */
  project_count: number | null;
  concurrency: number;
  skip_if_running: boolean;
  schedule_type: ScheduleType;
  schedule_config: ScheduleConfig;
  /** 后端生成的可读调度描述，如「每天 15:00 (Asia/Shanghai)」 */
  schedule_description: string;
  timezone: string;
  task_template: ScheduleTaskTemplate;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_id: string | null;
  latest_run: AuditBatchRun | null;
  created_at: string | null;
  updated_at: string | null;
}

/** 批次内单项目明细 */
export interface AuditBatchRunItem {
  id: string;
  run_id: string;
  project_id: string | null;
  project_name: string | null;
  agent_task_id: string | null;
  status: BatchRunItemStatus;
  attempts: number;
  findings_count: number;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface CreateScheduleRequest {
  name: string;
  description?: string | null;
  project_scope: ProjectScope;
  project_ids?: string[] | null;
  concurrency: number;
  skip_if_running: boolean;
  schedule_type: ScheduleType;
  schedule_config?: ScheduleConfig | null;
  timezone: string;
  task_template?: ScheduleTaskTemplate | null;
  enabled: boolean;
}

export type UpdateScheduleRequest = Partial<CreateScheduleRequest>;

export interface Paginated<T> {
  items: T[];
  total: number;
}

export interface TriggerScheduleResult {
  run_id: string;
  schedule_id: string;
  trigger_type: "scheduled" | "manual";
}

export interface CancelRunResult {
  message: string;
  run_id: string;
  /** 被中断的、正在执行的 Agent 任务数 */
  interrupted_tasks: number;
  /** 被直接标记取消的排队项目数 */
  cancelled_queued: number;
}

// ============ 计划接口 ============

/**
 * 创建定时批量审计计划
 */
export async function createSchedule(
  data: CreateScheduleRequest
): Promise<AuditSchedule> {
  const response = await apiClient.post("/batch-audit/schedules", data);
  return response.data;
}

/**
 * 计划列表（含下次执行时间与最近批次状态）
 */
export async function getSchedules(params?: {
  skip?: number;
  limit?: number;
}): Promise<Paginated<AuditSchedule>> {
  const response = await apiClient.get("/batch-audit/schedules", { params });
  return response.data;
}

/**
 * 修改计划（含启停）；调度参数变更后后端会重算 next_run_at
 */
export async function updateSchedule(
  scheduleId: string,
  data: UpdateScheduleRequest
): Promise<AuditSchedule> {
  const response = await apiClient.patch(
    `/batch-audit/schedules/${scheduleId}`,
    data
  );
  return response.data;
}

/**
 * 删除计划（历史批次保留）
 */
export async function deleteSchedule(
  scheduleId: string
): Promise<{ message: string; id: string }> {
  const response = await apiClient.delete(`/batch-audit/schedules/${scheduleId}`);
  return response.data;
}

/**
 * 立即触发一次批量审计（不等待定时）
 */
export async function triggerSchedule(
  scheduleId: string
): Promise<TriggerScheduleResult> {
  const response = await apiClient.post(
    `/batch-audit/schedules/${scheduleId}/trigger`
  );
  return response.data;
}

// ============ 批次接口 ============

/**
 * 批次列表
 */
export async function getBatchRuns(params?: {
  schedule_id?: string;
  status?: string;
  skip?: number;
  limit?: number;
}): Promise<Paginated<AuditBatchRun>> {
  const response = await apiClient.get("/batch-audit/runs", { params });
  return response.data;
}

/**
 * 批次详情 + 实时进度计数
 */
export async function getBatchRun(runId: string): Promise<AuditBatchRun> {
  const response = await apiClient.get(`/batch-audit/runs/${runId}`);
  return response.data;
}

/**
 * 批次明细分页（1000 个项目必须分页）
 */
export async function getBatchRunItems(
  runId: string,
  params?: { status?: string; skip?: number; limit?: number }
): Promise<Paginated<AuditBatchRunItem>> {
  const response = await apiClient.get(`/batch-audit/runs/${runId}/items`, {
    params,
  });
  return response.data;
}

/**
 * 取消批次：未出队项目直接标记取消，已在跑的逐个中断
 */
export async function cancelBatchRun(runId: string): Promise<CancelRunResult> {
  const response = await apiClient.post(`/batch-audit/runs/${runId}/cancel`);
  return response.data;
}
