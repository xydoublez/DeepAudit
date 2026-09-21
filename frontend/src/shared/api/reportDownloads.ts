/**
 * Report Downloads API
 * 批量项目报告下载（GitLab 项目获取 / 任务创建 / 进度 / 重试 / 续传 / 日志 / 汇总）
 */

import { apiClient } from "./serverClient";

// ============ Types ============

/** 命名规则 */
export type FilenamePattern = "id_name" | "name" | "id";

/** 下载任务状态 */
export type DownloadTaskStatus =
  | "pending"
  | "running"
  | "completed"
  | "partially_failed"
  | "failed"
  | "cancelled";

/** 下载明细状态 */
export type DownloadItemStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "skipped"
  | "cancelled";

/** GitLab 项目信息（含本地匹配结果） */
export interface GitLabProjectInfo {
  gitlab_id: number;
  name: string;
  path_with_namespace: string | null;
  namespace: string | null;
  web_url: string | null;
  default_branch: string | null;
  archived: boolean;
  matched_project_id: string | null;
  matched_project_name: string | null;
}

/** GitLab 批量获取请求 */
export interface FetchGitLabProjectsRequest {
  gitlab_url: string;
  token?: string | null;
  search?: string | null;
  group?: string | null;
  owned_only?: boolean;
  skip_archived?: boolean;
  limit?: number;
}

/** GitLab 批量获取响应 */
export interface FetchGitLabProjectsResponse {
  gitlab_user: { id: number; username: string };
  total: number;
  matched: number;
  projects: GitLabProjectInfo[];
}

/** 单项目的 GitLab 附加信息 */
export interface GitLabMeta {
  gitlab_project_id?: number | null;
  namespace?: string | null;
  web_url?: string | null;
}

/** 创建下载任务请求 */
export interface CreateDownloadRequest {
  name?: string | null;
  project_ids: string[];
  gitlab_meta?: Record<string, GitLabMeta> | null;
  output_dir?: string | null;
  concurrency: number;
  filename_pattern: FilenamePattern;
  gitlab_url?: string | null;
}

/** 批量下载任务 */
export interface ReportDownloadTask {
  id: string;
  name: string;
  created_by: string;
  output_dir: string;
  concurrency: number;
  filename_pattern: FilenamePattern;
  gitlab_url: string | null;
  total_count: number;
  completed_count: number;
  failed_count: number;
  skipped_count: number;
  /** 当前正在下载的项目名称 */
  current_items: string[];
  status: DownloadTaskStatus;
  running: boolean;
  /** 完成百分比 0-100 */
  progress: number;
  error_message: string | null;
  summary_path: string | null;
  log_path: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
}

/** 下载明细 */
export interface ReportDownloadItem {
  id: string;
  task_id: string;
  project_id: string | null;
  project_name: string | null;
  gitlab_project_id: number | null;
  gitlab_namespace: string | null;
  gitlab_web_url: string | null;
  agent_task_id: string | null;
  status: DownloadItemStatus;
  attempts: number;
  error_message: string | null;
  file_path: string | null;
  file_size: number | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface Paginated<T> {
  items: T[];
  total: number;
}

export interface DownloadLogEntry {
  timestamp: string | null;
  level: string;
  message: string;
}

// ============ GitLab 项目批量获取 ============

/**
 * 通过 GitLab API 批量获取项目信息（PAT 认证），并匹配本地项目
 */
export async function fetchGitLabProjects(
  data: FetchGitLabProjectsRequest
): Promise<FetchGitLabProjectsResponse> {
  const response = await apiClient.post("/report-downloads/gitlab/projects", data);
  return response.data;
}

// ============ 下载任务 ============

/** 创建批量下载任务并立即启动 */
export async function createReportDownload(
  data: CreateDownloadRequest
): Promise<ReportDownloadTask> {
  const response = await apiClient.post("/report-downloads", data);
  return response.data;
}

/** 任务列表 */
export async function getReportDownloads(params?: {
  skip?: number;
  limit?: number;
}): Promise<Paginated<ReportDownloadTask>> {
  const response = await apiClient.get("/report-downloads", { params });
  return response.data;
}

/** 任务详情 + 实时进度 */
export async function getReportDownload(
  taskId: string
): Promise<ReportDownloadTask> {
  const response = await apiClient.get(`/report-downloads/${taskId}`);
  return response.data;
}

/** 任务明细（可按状态过滤失败清单） */
export async function getReportDownloadItems(
  taskId: string,
  params?: { status?: string; skip?: number; limit?: number }
): Promise<Paginated<ReportDownloadItem>> {
  const response = await apiClient.get(`/report-downloads/${taskId}/items`, {
    params,
  });
  return response.data;
}

/** 重试失败的项目 */
export async function retryReportDownload(
  taskId: string
): Promise<{ message: string; task_id: string; retry_count: number }> {
  const response = await apiClient.post(`/report-downloads/${taskId}/retry`);
  return response.data;
}

/** 断点续传 */
export async function resumeReportDownload(
  taskId: string
): Promise<{ message: string; task_id: string; remaining: number }> {
  const response = await apiClient.post(`/report-downloads/${taskId}/resume`);
  return response.data;
}

/** 取消任务 */
export async function cancelReportDownload(
  taskId: string
): Promise<{ message: string; task_id: string }> {
  const response = await apiClient.post(`/report-downloads/${taskId}/cancel`);
  return response.data;
}

/** 任务日志 */
export async function getReportDownloadLogs(
  taskId: string,
  limit?: number
): Promise<{ task_id: string; running: boolean; logs: DownloadLogEntry[] }> {
  const response = await apiClient.get(`/report-downloads/${taskId}/logs`, {
    params: limit ? { limit } : undefined,
  });
  return response.data;
}

/** 汇总报告内容（Markdown 文本） */
export async function getReportDownloadSummary(
  taskId: string
): Promise<string> {
  const response = await apiClient.get(`/report-downloads/${taskId}/summary`, {
    responseType: "text",
    transformResponse: [(data) => data],
  });
  return response.data;
}
