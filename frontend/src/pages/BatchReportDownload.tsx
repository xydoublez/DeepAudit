/**
 * 批量项目报告下载页面
 * GitLab 项目批量获取 + 下载配置 + 实时进度 + 失败重试 / 断点续传 + 汇总报告
 * Cyberpunk Terminal Aesthetic
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FileText,
  FolderDown,
  Loader2,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  ScrollText,
  Upload,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import {
  cancelReportDownload,
  createReportDownload,
  fetchGitLabProjects,
  getReportDownload,
  getReportDownloadItems,
  getReportDownloadLogs,
  getReportDownloadSummary,
  getReportDownloads,
  resumeReportDownload,
  retryReportDownload,
  type FilenamePattern,
  type GitLabMeta,
  type GitLabProjectInfo,
  type ReportDownloadItem,
  type ReportDownloadTask,
} from "@/shared/api/reportDownloads";

const POLL_INTERVAL = 2000;
const ITEM_PAGE_SIZE = 20;
const ACTIVE_STATUSES = new Set(["pending", "running"]);

const TASK_STATUS_META: Record<string, { label: string; className: string }> = {
  pending: { label: "待执行", className: "cyber-badge-muted" },
  running: { label: "下载中", className: "cyber-badge-info" },
  completed: { label: "已完成", className: "cyber-badge-success" },
  partially_failed: { label: "部分失败", className: "cyber-badge-warning" },
  failed: { label: "失败", className: "cyber-badge-danger" },
  cancelled: { label: "已取消", className: "cyber-badge-muted" },
};

const ITEM_STATUS_META: Record<string, { label: string; className: string }> = {
  queued: { label: "排队中", className: "cyber-badge-muted" },
  running: { label: "下载中", className: "cyber-badge-info" },
  completed: { label: "已完成", className: "cyber-badge-success" },
  failed: { label: "失败", className: "cyber-badge-danger" },
  skipped: { label: "已跳过", className: "cyber-badge-warning" },
  cancelled: { label: "已取消", className: "cyber-badge-muted" },
};

const PATTERN_LABELS: Record<FilenamePattern, string> = {
  id_name: "项目ID_项目名称.md",
  name: "项目名称.md",
  id: "项目ID.md",
};

function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", { hour12: false });
}

/** 从粘贴文本解析项目 ID（逗号/分号/空白分隔，去重保序） */
function parseProjectIds(text: string): string[] {
  const seen = new Set<string>();
  const ids: string[] = [];
  for (const chunk of text.replace(/[,;]/g, " ").split(/\s+/)) {
    const id = chunk.trim();
    if (id && !seen.has(id)) {
      seen.add(id);
      ids.push(id);
    }
  }
  return ids;
}

export default function BatchReportDownload() {
  // ---- 创建任务表单 ----
  const [gitlabUrl, setGitlabUrl] = useState("");
  const [gitlabToken, setGitlabToken] = useState("");
  const [glSearch, setGlSearch] = useState("");
  const [glGroup, setGlGroup] = useState("");
  const [skipArchived, setSkipArchived] = useState(true);
  const [fetchingProjects, setFetchingProjects] = useState(false);
  const [gitlabProjects, setGitlabProjects] = useState<GitLabProjectInfo[]>([]);
  const [gitlabUser, setGitlabUser] = useState<string | null>(null);

  const [manualText, setManualText] = useState("");

  const [outputDir, setOutputDir] = useState("");
  const [concurrency, setConcurrency] = useState(5);
  const [filenamePattern, setFilenamePattern] = useState<FilenamePattern>("id_name");
  const [creating, setCreating] = useState(false);

  // 已选项目：project_id -> gitlab 元信息（手动导入的没有元信息）
  const [selectedMeta, setSelectedMeta] = useState<Record<string, GitLabMeta | null>>({});

  // ---- 任务列表与详情 ----
  const [tasks, setTasks] = useState<ReportDownloadTask[]>([]);
  const [detailTask, setDetailTask] = useState<ReportDownloadTask | null>(null);
  const [detailItems, setDetailItems] = useState<ReportDownloadItem[]>([]);
  const [detailItemsTotal, setDetailItemsTotal] = useState(0);
  const [itemStatusFilter, setItemStatusFilter] = useState<string>("failed");
  const [logs, setLogs] = useState<string[]>([]);
  const [summaryText, setSummaryText] = useState<string | null>(null);
  const [loadingSummary, setLoadingSummary] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const detailBodyRef = useRef<HTMLDivElement>(null);

  const selectedIds = useMemo(() => Object.keys(selectedMeta), [selectedMeta]);
  const matchedProjects = useMemo(
    () => gitlabProjects.filter((p) => p.matched_project_id),
    [gitlabProjects]
  );
  const selectedMatchedCount = useMemo(
    () =>
      gitlabProjects.filter(
        (p) => p.matched_project_id && p.matched_project_id in selectedMeta
      ).length,
    [gitlabProjects, selectedMeta]
  );

  const hasActiveTask = useMemo(
    () => tasks.some((t) => ACTIVE_STATUSES.has(t.status) || t.running),
    [tasks]
  );
  const detailActive = useMemo(
    () => !!detailTask && (ACTIVE_STATUSES.has(detailTask.status) || detailTask.running),
    [detailTask]
  );

  const refreshTasks = useCallback(async () => {
    try {
      const data = await getReportDownloads({ limit: 50 });
      setTasks(data.items);
    } catch {
      // 静默失败，下轮轮询重试
    }
  }, []);

  const loadDetail = useCallback(async (taskId: string) => {
    try {
      const [task, items] = await Promise.all([
        getReportDownload(taskId),
        getReportDownloadItems(taskId, {
          status: itemStatusFilter === "all" ? undefined : itemStatusFilter,
          limit: ITEM_PAGE_SIZE,
        }),
      ]);
      setDetailTask(task);
      setDetailItems(items.items);
      setDetailItemsTotal(items.total);
    } catch {
      // 忽略：任务可能已被清理
    }
  }, [itemStatusFilter]);

  const loadLogs = useCallback(async (taskId: string) => {
    try {
      const data = await getReportDownloadLogs(taskId, 200);
      setLogs(
        data.logs.map(
          (l) =>
            `${l.timestamp ? new Date(l.timestamp).toLocaleString("zh-CN", { hour12: false }) : ""} [${l.level}] ${l.message}`
        )
      );
    } catch {
      setLogs([]);
    }
  }, []);

  // 定时轮询：任务列表 + 详情
  useEffect(() => {
    refreshTasks();
  }, [refreshTasks]);

  useEffect(() => {
    if (!hasActiveTask && !detailActive) return;
    const timer = setInterval(() => {
      refreshTasks();
      if (detailTask?.id) {
        loadDetail(detailTask.id);
        if (detailActive) loadLogs(detailTask.id);
      }
    }, POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [hasActiveTask, detailActive, detailTask?.id, refreshTasks, loadDetail, loadLogs]);

  // 筛选条件或任务变化时立即刷新明细
  useEffect(() => {
    if (detailTask?.id) {
      loadDetail(detailTask.id);
    }
  }, [detailTask?.id, itemStatusFilter, loadDetail]);

  // ---- 事件处理 ----

  const handleFetchProjects = async () => {
    if (!gitlabUrl.trim()) {
      toast.error("请填写 GitLab 地址");
      return;
    }
    setFetchingProjects(true);
    try {
      const data = await fetchGitLabProjects({
        gitlab_url: gitlabUrl.trim(),
        token: gitlabToken.trim() || null,
        search: glSearch.trim() || null,
        group: glGroup.trim() || null,
        skip_archived: skipArchived,
      });
      setGitlabProjects(data.projects);
      setGitlabUser(data.gitlab_user.username);
      toast.success(
        `认证成功（${data.gitlab_user.username}），获取 ${data.total} 个项目，其中 ${data.matched} 个已匹配本地项目`
      );
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "GitLab 项目获取失败";
      toast.error(message);
    } finally {
      setFetchingProjects(false);
    }
  };

  const toggleProject = (project: GitLabProjectInfo) => {
    const pid = project.matched_project_id;
    if (!pid) return;
    setSelectedMeta((prev) => {
      const next = { ...prev };
      if (pid in next) {
        delete next[pid];
      } else {
        next[pid] = {
          gitlab_project_id: project.gitlab_id,
          namespace: project.namespace,
          web_url: project.web_url,
        };
      }
      return next;
    });
  };

  const selectAllMatched = () => {
    setSelectedMeta((prev) => {
      const next = { ...prev };
      for (const p of matchedProjects) {
        if (p.matched_project_id && !(p.matched_project_id in next)) {
          next[p.matched_project_id] = {
            gitlab_project_id: p.gitlab_id,
            namespace: p.namespace,
            web_url: p.web_url,
          };
        }
      }
      return next;
    });
  };

  const importManualIds = () => {
    const ids = parseProjectIds(manualText);
    if (!ids.length) {
      toast.error("未解析到任何项目 ID");
      return;
    }
    setSelectedMeta((prev) => {
      const next = { ...prev };
      for (const id of ids) {
        if (!(id in next)) next[id] = null;
      }
      return next;
    });
    toast.success(`已导入 ${ids.length} 个项目 ID（去重后）`);
  };

  const handleUploadIdFile = async (file: File) => {
    const text = await file.text();
    setManualText((prev) => (prev ? `${prev}\n${text}` : text));
    toast.success(`已读取文件 ${file.name}，点击“导入”解析项目 ID`);
  };

  const clearSelection = () => {
    setSelectedMeta({});
    setGitlabProjects([]);
    setGitlabUser(null);
    setManualText("");
  };

  const handleCreate = async () => {
    if (!selectedIds.length) {
      toast.error("请先选择或导入要下载报告的项目");
      return;
    }
    setCreating(true);
    try {
      const gitlabMeta: Record<string, GitLabMeta> = {};
      for (const [pid, meta] of Object.entries(selectedMeta)) {
        if (meta) gitlabMeta[pid] = meta;
      }
      const task = await createReportDownload({
        project_ids: selectedIds,
        gitlab_meta: Object.keys(gitlabMeta).length ? gitlabMeta : null,
        output_dir: outputDir.trim() || null,
        concurrency,
        filename_pattern: filenamePattern,
        gitlab_url: gitlabUrl.trim() || null,
      });
      toast.success(`下载任务已创建并启动（${task.total_count} 个项目）`);
      setSelectedMeta({});
      setSummaryText(null);
      await refreshTasks();
      setDetailTask(task);
      setItemStatusFilter("failed");
      loadDetail(task.id);
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "任务创建失败";
      toast.error(message);
    } finally {
      setCreating(false);
    }
  };

  const openDetail = async (taskId: string) => {
    setSummaryText(null);
    setLogs([]);
    setItemStatusFilter("failed");
    await loadDetail(taskId);
    loadLogs(taskId);
  };

  const handleRetry = async (taskId: string) => {
    try {
      const res = await retryReportDownload(taskId);
      toast.success(`${res.message}（${res.retry_count} 个失败项目）`);
      loadDetail(taskId);
      refreshTasks();
    } catch (error: unknown) {
      toast.error(
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
          "重试失败"
      );
    }
  };

  const handleResume = async (taskId: string) => {
    try {
      const res = await resumeReportDownload(taskId);
      toast.success(`${res.message}${res.remaining ? `，剩余 ${res.remaining} 个项目` : ""}`);
      loadDetail(taskId);
      refreshTasks();
    } catch (error: unknown) {
      toast.error(
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
          "断点续传失败"
      );
    }
  };

  const handleCancel = async (taskId: string) => {
    try {
      const res = await cancelReportDownload(taskId);
      toast.success(res.message);
      setTimeout(() => {
        loadDetail(taskId);
        refreshTasks();
      }, 800);
    } catch {
      toast.error("取消失败");
    }
  };

  const handleLoadSummary = async (taskId: string) => {
    setLoadingSummary(true);
    try {
      const text = await getReportDownloadSummary(taskId);
      setSummaryText(text);
      if (detailBodyRef.current) detailBodyRef.current.scrollTop = 0;
    } catch {
      toast.error("汇总报告尚未生成");
    } finally {
      setLoadingSummary(false);
    }
  };

  // ---- 渲染 ----

  return (
    <div className="container mx-auto p-6 space-y-6">
      {/* 页头 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <FolderDown className="w-6 h-6 text-primary" />
            批量报告下载
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            批量获取 GitLab 项目的 Markdown 审查报告，支持并发下载、失败重试、断点续传与汇总报告
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={refreshTasks}>
          <RefreshCw className="w-4 h-4 mr-1" />
          刷新
        </Button>
      </div>

      {/* 创建任务 */}
      <div
        className="rounded-lg border p-5 space-y-5"
        style={{ background: "var(--cyber-bg)", borderColor: "var(--cyber-border)" }}
      >
        <h2 className="text-base font-semibold flex items-center gap-2">
          <Download className="w-4 h-4 text-primary" />
          新建下载任务
        </h2>

        <Tabs defaultValue="gitlab">
          <TabsList>
            <TabsTrigger value="gitlab">GitLab 项目获取</TabsTrigger>
            <TabsTrigger value="manual">手动导入项目 ID</TabsTrigger>
          </TabsList>

          {/* GitLab 获取 */}
          <TabsContent value="gitlab" className="space-y-4 pt-3">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label>GitLab 地址 *</Label>
                <Input
                  placeholder="https://gitlab.example.com"
                  value={gitlabUrl}
                  onChange={(e) => setGitlabUrl(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label>Access Token (PAT)</Label>
                <Input
                  type="password"
                  placeholder="留空使用服务端配置的 Token"
                  value={gitlabToken}
                  onChange={(e) => setGitlabToken(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label>项目名搜索</Label>
                <Input
                  placeholder="可选，模糊匹配项目名"
                  value={glSearch}
                  onChange={(e) => setGlSearch(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label>组 (Group)</Label>
                <Input
                  placeholder="可选，只拉取指定组"
                  value={glGroup}
                  onChange={(e) => setGlGroup(e.target.value)}
                />
              </div>
            </div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Switch checked={skipArchived} onCheckedChange={setSkipArchived} />
                <span className="text-sm text-muted-foreground">跳过已归档项目</span>
                {gitlabUser && (
                  <Badge className="cyber-badge-success">已认证: {gitlabUser}</Badge>
                )}
              </div>
              <Button onClick={handleFetchProjects} disabled={fetchingProjects}>
                {fetchingProjects ? (
                  <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                ) : (
                  <Search className="w-4 h-4 mr-1" />
                )}
                获取项目列表
              </Button>
            </div>

            {gitlabProjects.length > 0 && (
              <div className="border rounded-md max-h-72 overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-10"></TableHead>
                      <TableHead>GitLab 项目</TableHead>
                      <TableHead>命名空间</TableHead>
                      <TableHead>本地项目匹配</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {gitlabProjects.map((p) => {
                      const pid = p.matched_project_id;
                      const checked = !!pid && pid in selectedMeta;
                      return (
                        <TableRow key={p.gitlab_id}>
                          <TableCell>
                            <input
                              type="checkbox"
                              className="accent-primary"
                              disabled={!pid}
                              checked={checked}
                              onChange={() => toggleProject(p)}
                            />
                          </TableCell>
                          <TableCell className="font-medium">{p.name}</TableCell>
                          <TableCell className="text-muted-foreground">
                            {p.namespace || "—"}
                          </TableCell>
                          <TableCell>
                            {pid ? (
                              <Badge className="cyber-badge-success">
                                {p.matched_project_name}
                              </Badge>
                            ) : (
                              <Badge className="cyber-badge-muted">未匹配</Badge>
                            )}
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
            {gitlabProjects.length > 0 && (
              <div className="flex items-center gap-3">
                <Button variant="outline" size="sm" onClick={selectAllMatched}>
                  全选已匹配（{matchedProjects.length}）
                </Button>
                <span className="text-xs text-muted-foreground">
                  共 {gitlabProjects.length} 个项目，已匹配 {matchedProjects.length} 个，已选{" "}
                  {selectedMatchedCount} 个
                </span>
              </div>
            )}
          </TabsContent>

          {/* 手动导入 */}
          <TabsContent value="manual" className="space-y-3 pt-3">
            <div className="flex items-center justify-between">
              <Label>项目 ID 列表（换行 / 逗号分隔）</Label>
              <div className="flex gap-2">
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".txt,.csv"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) handleUploadIdFile(file);
                    e.target.value = "";
                  }}
                />
                <Button variant="outline" size="sm" onClick={() => fileInputRef.current?.click()}>
                  <Upload className="w-4 h-4 mr-1" />
                  上传 ID 文件
                </Button>
                <Button size="sm" onClick={importManualIds}>
                  <Play className="w-4 h-4 mr-1" />
                  导入
                </Button>
              </div>
            </div>
            <Textarea
              rows={6}
              placeholder={"每行一个项目 ID，或用逗号分隔\n例如：\n3f2b8c1e-xxxx\n9a7d4e2f-xxxx"}
              value={manualText}
              onChange={(e) => setManualText(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              导入后与 GitLab 勾选的项目合并（自动去重）
            </p>
          </TabsContent>
        </Tabs>

        {/* 下载参数配置 */}
        <div className="border-t pt-4 grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="space-y-1.5">
            <Label>报告存储路径</Label>
            <Input
              placeholder="留空使用服务端默认目录"
              value={outputDir}
              onChange={(e) => setOutputDir(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              相对路径挂在默认根目录下；按 GitLab 命名空间分目录存储
            </p>
          </div>
          <div className="space-y-1.5">
            <Label>并发下载数量：{concurrency}</Label>
            <Slider
              min={1}
              max={20}
              step={1}
              value={[concurrency]}
              onValueChange={([v]) => setConcurrency(v)}
            />
            <p className="text-xs text-muted-foreground">服务端上限 20</p>
          </div>
          <div className="space-y-1.5">
            <Label>报告命名规则</Label>
            <Select
              value={filenamePattern}
              onValueChange={(v) => setFilenamePattern(v as FilenamePattern)}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(PATTERN_LABELS) as FilenamePattern[]).map((k) => (
                  <SelectItem key={k} value={k}>
                    {PATTERN_LABELS[k]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">文件名自动去除非法字符</p>
          </div>
        </div>

        <div className="flex items-center justify-between border-t pt-4">
          <div className="text-sm">
            已选项目：
            <span className="font-bold text-primary mx-1">{selectedIds.length}</span> 个
            {selectedIds.length > 0 && (
              <Button variant="ghost" size="sm" className="ml-2" onClick={clearSelection}>
                清空
              </Button>
            )}
          </div>
          <Button onClick={handleCreate} disabled={creating || !selectedIds.length}>
            {creating ? (
              <Loader2 className="w-4 h-4 mr-1 animate-spin" />
            ) : (
              <Download className="w-4 h-4 mr-1" />
            )}
            开始批量下载
          </Button>
        </div>
      </div>

      {/* 任务列表 */}
      <div
        className="rounded-lg border p-5 space-y-3"
        style={{ background: "var(--cyber-bg)", borderColor: "var(--cyber-border)" }}
      >
        <h2 className="text-base font-semibold flex items-center gap-2">
          <ScrollText className="w-4 h-4 text-primary" />
          下载任务
        </h2>
        {tasks.length === 0 ? (
          <p className="text-sm text-muted-foreground py-6 text-center">暂无下载任务</p>
        ) : (
          <div className="border rounded-md overflow-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>任务名称</TableHead>
                  <TableHead className="w-24">状态</TableHead>
                  <TableHead className="w-64">进度</TableHead>
                  <TableHead className="w-40">成功 / 失败 / 跳过</TableHead>
                  <TableHead className="w-40">创建时间</TableHead>
                  <TableHead className="w-20">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {tasks.map((t) => {
                  const meta = TASK_STATUS_META[t.status] ?? TASK_STATUS_META.pending;
                  return (
                    <TableRow key={t.id}>
                      <TableCell className="font-medium">
                        {t.name}
                        <div className="text-xs text-muted-foreground truncate max-w-72">
                          {t.output_dir}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge className={meta.className}>{meta.label}</Badge>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <Progress value={t.progress} className="h-2 flex-1" />
                          <span className="text-xs w-10 text-right">{t.progress}%</span>
                        </div>
                      </TableCell>
                      <TableCell className="text-sm">
                        <span className="text-green-500">{t.completed_count}</span> /{" "}
                        <span className="text-red-500">{t.failed_count}</span> /{" "}
                        <span className="text-yellow-500">{t.skipped_count}</span>
                        <span className="text-muted-foreground"> （共 {t.total_count}）</span>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {formatDateTime(t.created_at)}
                      </TableCell>
                      <TableCell>
                        <Button variant="ghost" size="sm" onClick={() => openDetail(t.id)}>
                          详情
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      {/* 任务详情 */}
      <Sheet open={!!detailTask} onOpenChange={(open) => !open && setDetailTask(null)}>
        <SheetContent side="right" className="w-full sm:max-w-2xl p-0 flex flex-col">
          {detailTask && (
            <>
              <SheetHeader className="border-b px-5 py-4">
                <SheetTitle className="flex items-center gap-2 text-base">
                  <FileText className="w-4 h-4 text-primary" />
                  {detailTask.name}
                </SheetTitle>
                <SheetDescription className="flex items-center gap-2 flex-wrap">
                  <Badge className={(TASK_STATUS_META[detailTask.status] ?? TASK_STATUS_META.pending).className}>
                    {(TASK_STATUS_META[detailTask.status] ?? TASK_STATUS_META.pending).label}
                  </Badge>
                  <span className="text-xs">存储目录: {detailTask.output_dir}</span>
                </SheetDescription>
              </SheetHeader>

              <div ref={detailBodyRef} className="flex-1 overflow-auto px-5 py-4 space-y-5">
                {/* 进度概览 */}
                <div className="space-y-3">
                  <div className="flex items-center gap-3">
                    <Progress value={detailTask.progress} className="h-3 flex-1" />
                    <span className="text-sm font-bold w-14 text-right">
                      {detailTask.progress}%
                    </span>
                  </div>
                  <div className="grid grid-cols-4 gap-2 text-center">
                    <div className="rounded border p-2">
                      <div className="text-lg font-bold">{detailTask.total_count}</div>
                      <div className="text-xs text-muted-foreground">总数</div>
                    </div>
                    <div className="rounded border p-2">
                      <div className="text-lg font-bold text-green-500">
                        {detailTask.completed_count}
                      </div>
                      <div className="text-xs text-muted-foreground">已下载</div>
                    </div>
                    <div className="rounded border p-2">
                      <div className="text-lg font-bold text-red-500">
                        {detailTask.failed_count}
                      </div>
                      <div className="text-xs text-muted-foreground">失败</div>
                    </div>
                    <div className="rounded border p-2">
                      <div className="text-lg font-bold text-yellow-500">
                        {detailTask.skipped_count}
                      </div>
                      <div className="text-xs text-muted-foreground">跳过</div>
                    </div>
                  </div>
                  {detailTask.current_items.length > 0 && (
                    <div className="text-xs text-muted-foreground">
                      当前下载：
                      {detailTask.current_items.slice(0, 8).join("、")}
                      {detailTask.current_items.length > 8 &&
                        ` 等 ${detailTask.current_items.length} 个`}
                    </div>
                  )}
                  {detailTask.error_message && (
                    <div className="text-xs text-red-500">{detailTask.error_message}</div>
                  )}
                </div>

                {/* 操作 */}
                <div className="flex flex-wrap gap-2 border-t pt-4">
                  {detailActive && (
                    <Button variant="destructive" size="sm" onClick={() => handleCancel(detailTask.id)}>
                      <XCircle className="w-4 h-4 mr-1" />
                      取消
                    </Button>
                  )}
                  {!detailActive && detailTask.failed_count > 0 && (
                    <Button size="sm" onClick={() => handleRetry(detailTask.id)}>
                      <RotateCcw className="w-4 h-4 mr-1" />
                      重试失败（{detailTask.failed_count}）
                    </Button>
                  )}
                  {!detailActive &&
                    detailTask.status !== "completed" &&
                    detailTask.completed_count + detailTask.skipped_count <
                      detailTask.total_count && (
                      <Button size="sm" onClick={() => handleResume(detailTask.id)}>
                        <Play className="w-4 h-4 mr-1" />
                        断点续传
                      </Button>
                    )}
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => loadLogs(detailTask.id)}
                  >
                    <ScrollText className="w-4 h-4 mr-1" />
                    刷新日志
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={loadingSummary}
                    onClick={() => handleLoadSummary(detailTask.id)}
                  >
                    {loadingSummary ? (
                      <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                    ) : (
                      <CheckCircle2 className="w-4 h-4 mr-1" />
                    )}
                    查看汇总报告
                  </Button>
                </div>

                {/* 汇总报告 */}
                {summaryText !== null && (
                  <div className="border rounded-md p-3 bg-black/20">
                    <pre className="text-xs whitespace-pre-wrap max-h-80 overflow-auto">
                      {summaryText}
                    </pre>
                  </div>
                )}

                {/* 运行日志 */}
                {logs.length > 0 && (
                  <div className="space-y-2">
                    <h3 className="text-sm font-semibold flex items-center gap-1">
                      <ScrollText className="w-4 h-4" />
                      运行日志（最近 200 行）
                    </h3>
                    <div className="border rounded-md p-3 bg-black/40 max-h-72 overflow-auto">
                      <pre className="text-[11px] leading-5 whitespace-pre-wrap font-mono">
                        {logs.join("\n")}
                      </pre>
                    </div>
                  </div>
                )}

                {/* 明细 */}
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <h3 className="text-sm font-semibold flex items-center gap-1">
                      <AlertTriangle className="w-4 h-4" />
                      项目明细（{detailItemsTotal}）
                    </h3>
                    <Select
                      value={itemStatusFilter}
                      onValueChange={(v) => setItemStatusFilter(v)}
                    >
                      <SelectTrigger className="h-7 w-32 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="failed">失败</SelectItem>
                        <SelectItem value="skipped">跳过</SelectItem>
                        <SelectItem value="completed">已完成</SelectItem>
                        <SelectItem value="queued">排队中</SelectItem>
                        <SelectItem value="all">全部</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="border rounded-md overflow-auto max-h-96">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>项目</TableHead>
                          <TableHead className="w-20">状态</TableHead>
                          <TableHead className="w-14">次数</TableHead>
                          <TableHead>原因 / 产物</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {detailItems.length === 0 ? (
                          <TableRow>
                            <TableCell
                              colSpan={4}
                              className="text-center text-muted-foreground py-6"
                            >
                              无匹配明细
                            </TableCell>
                          </TableRow>
                        ) : (
                          detailItems.map((item) => {
                            const meta =
                              ITEM_STATUS_META[item.status] ?? ITEM_STATUS_META.queued;
                            return (
                              <TableRow key={item.id}>
                                <TableCell className="font-medium">
                                  {item.project_name || item.project_id}
                                </TableCell>
                                <TableCell>
                                  <Badge className={meta.className}>{meta.label}</Badge>
                                </TableCell>
                                <TableCell className="text-xs">{item.attempts}</TableCell>
                                <TableCell className="text-xs text-muted-foreground">
                                  {item.status === "completed" && item.file_path ? (
                                    <span className="break-all">
                                      {item.file_path}
                                      {item.file_size ? ` (${(item.file_size / 1024).toFixed(1)} KB)` : ""}
                                    </span>
                                  ) : (
                                    (item.error_message || "—")
                                  )}
                                </TableCell>
                              </TableRow>
                            );
                          })
                        )}
                      </TableBody>
                    </Table>
                  </div>
                </div>
              </div>
            </>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}
