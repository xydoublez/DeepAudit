/**
 * 定时批量审计页面
 * 计划管理 + 批次进度 + 批次明细
 * Cyberpunk Terminal Aesthetic
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Progress } from "@/components/ui/progress";
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
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  AlertTriangle,
  ArrowUpRight,
  CalendarClock,
  Inbox,
  Loader2,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import CreateBatchScheduleDialog from "@/components/batch/CreateBatchScheduleDialog";
import {
  cancelBatchRun,
  deleteSchedule,
  getBatchRunItems,
  getBatchRuns,
  getSchedules,
  triggerSchedule,
  updateSchedule,
  type AuditBatchRun,
  type AuditBatchRunItem,
  type AuditSchedule,
} from "@/shared/api/batchAudit";

const PAGE_SIZE = 50;
const POLL_INTERVAL = 3000;
const ACTIVE_RUN_STATUSES = new Set(["pending", "running"]);

const RUN_STATUS_META: Record<string, { label: string; className: string }> = {
  pending: { label: "待执行", className: "cyber-badge-muted" },
  running: { label: "执行中", className: "cyber-badge-info" },
  completed: { label: "已完成", className: "cyber-badge-success" },
  partially_failed: { label: "部分失败", className: "cyber-badge-warning" },
  failed: { label: "失败", className: "cyber-badge-danger" },
  cancelled: { label: "已取消", className: "cyber-badge-muted" },
};

const ITEM_STATUS_META: Record<string, { label: string; className: string }> = {
  queued: { label: "排队中", className: "cyber-badge-muted" },
  running: { label: "审计中", className: "cyber-badge-info" },
  completed: { label: "已完成", className: "cyber-badge-success" },
  failed: { label: "失败", className: "cyber-badge-danger" },
  skipped: { label: "已跳过", className: "cyber-badge-warning" },
  cancelled: { label: "已取消", className: "cyber-badge-muted" },
};

function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", { hour12: false });
}

function extractErrorMessage(err: unknown, fallback: string): string {
  const anyErr = err as {
    response?: { data?: { detail?: unknown } };
    message?: string;
  };
  const detail = anyErr?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  return anyErr?.message || fallback;
}

export default function BatchAudit() {
  // 计划
  const [schedules, setSchedules] = useState<AuditSchedule[]>([]);
  const [loadingSchedules, setLoadingSchedules] = useState(true);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [editingSchedule, setEditingSchedule] = useState<AuditSchedule | null>(null);
  const [deletingSchedule, setDeletingSchedule] = useState<AuditSchedule | null>(null);
  const [triggeringId, setTriggeringId] = useState<string | null>(null);
  const [togglingId, setTogglingId] = useState<string | null>(null);

  // 批次
  const [runs, setRuns] = useState<AuditBatchRun[]>([]);
  const [loadingRuns, setLoadingRuns] = useState(true);
  const [runStatusFilter, setRunStatusFilter] = useState("all");
  const [cancellingRun, setCancellingRun] = useState<AuditBatchRun | null>(null);

  // 明细抽屉
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  /** 打开抽屉那一刻的快照，列表翻页后找不到该批次时兜底展示 */
  const [activeRunSnapshot, setActiveRunSnapshot] = useState<AuditBatchRun | null>(null);
  const [items, setItems] = useState<AuditBatchRunItem[]>([]);
  const [itemsTotal, setItemsTotal] = useState(0);
  const [itemsPage, setItemsPage] = useState(0);
  const [itemsStatusFilter, setItemsStatusFilter] = useState("all");
  const [loadingItems, setLoadingItems] = useState(false);

  const loadSchedules = useCallback(async (silent = false) => {
    try {
      if (!silent) setLoadingSchedules(true);
      const data = await getSchedules({ limit: 200 });
      setSchedules(data.items);
    } catch (err) {
      if (!silent) toast.error(extractErrorMessage(err, "加载计划列表失败"));
    } finally {
      if (!silent) setLoadingSchedules(false);
    }
  }, []);

  const loadRuns = useCallback(
    async (silent = false, status = runStatusFilter) => {
      try {
        if (!silent) setLoadingRuns(true);
        const data = await getBatchRuns({
          status: status === "all" ? undefined : status,
          limit: 50,
        });
        setRuns(data.items);
      } catch (err) {
        if (!silent) toast.error(extractErrorMessage(err, "加载批次列表失败"));
      } finally {
        if (!silent) setLoadingRuns(false);
      }
    },
    [runStatusFilter]
  );

  const loadItems = useCallback(
    async (runId: string, page: number, status: string, silent = false) => {
      try {
        if (!silent) setLoadingItems(true);
        const data = await getBatchRunItems(runId, {
          status: status === "all" ? undefined : status,
          skip: page * PAGE_SIZE,
          limit: PAGE_SIZE,
        });
        setItems(data.items);
        setItemsTotal(data.total);
      } catch (err) {
        if (!silent) toast.error(extractErrorMessage(err, "加载批次明细失败"));
      } finally {
        if (!silent) setLoadingItems(false);
      }
    },
    []
  );

  useEffect(() => {
    loadSchedules();
    loadRuns();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadRuns(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runStatusFilter]);

  /** 抽屉展示的批次：从 runs 派生，而不是存一份快照
   *
   * 存快照会让轮询判断基于打开抽屉那一刻的状态：批次跑完后 status 仍是
   * "running"，明细轮询定时器永不停止，且抽屉里的进度数字冻结不更新。
   * 列表翻页后找不到该批次时退回快照。
   */
  const activeRun = useMemo(
    () =>
      activeRunId
        ? runs.find((r) => r.id === activeRunId) ?? activeRunSnapshot
        : null,
    [activeRunId, runs, activeRunSnapshot]
  );
  const activeRunStatus = activeRun?.status ?? "";

  // 有活跃批次时轮询刷新进度
  const hasActiveRun = useMemo(
    () => runs.some((r) => ACTIVE_RUN_STATUSES.has(r.status)),
    [runs]
  );

  useEffect(() => {
    if (!hasActiveRun) return;
    const timer = setInterval(() => {
      loadRuns(true);
    }, POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [hasActiveRun, loadRuns]);

  // 抽屉打开且批次活跃时轮询明细
  useEffect(() => {
    if (!activeRunId || !activeRun) return;
    loadItems(activeRunId, itemsPage, itemsStatusFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRunId, itemsPage, itemsStatusFilter]);

  useEffect(() => {
    if (!activeRunId || !activeRun || !ACTIVE_RUN_STATUSES.has(activeRunStatus)) return;
    const timer = setInterval(() => {
      loadItems(activeRunId, itemsPage, itemsStatusFilter, true);
    }, POLL_INTERVAL);
    return () => clearInterval(timer);
    // 依赖 activeRunStatus 而非整个对象，避免每次列表刷新都重建定时器
  }, [activeRunId, activeRunStatus, itemsPage, itemsStatusFilter, loadItems]);

  // ============ 计划操作 ============

  async function handleTrigger(schedule: AuditSchedule) {
    setTriggeringId(schedule.id);
    try {
      const result = await triggerSchedule(schedule.id);
      toast.success(`已触发批次 ${result.run_id.slice(0, 8)}`);
      await Promise.all([loadRuns(true), loadSchedules(true)]);
    } catch (err) {
      toast.error(extractErrorMessage(err, "触发失败"));
    } finally {
      setTriggeringId(null);
    }
  }

  async function handleToggleEnabled(schedule: AuditSchedule, next: boolean) {
    setTogglingId(schedule.id);
    try {
      const saved = await updateSchedule(schedule.id, { enabled: next });
      setSchedules((prev) => prev.map((s) => (s.id === saved.id ? saved : s)));
      toast.success(next ? "计划已启用" : "计划已停用");
    } catch (err) {
      toast.error(extractErrorMessage(err, "更新计划失败"));
    } finally {
      setTogglingId(null);
    }
  }

  async function handleDeleteSchedule() {
    if (!deletingSchedule) return;
    const target = deletingSchedule;
    setDeletingSchedule(null);
    try {
      await deleteSchedule(target.id);
      toast.success(`计划「${target.name}」已删除`);
      await loadSchedules(true);
    } catch (err) {
      toast.error(extractErrorMessage(err, "删除计划失败"));
    }
  }

  // ============ 批次操作 ============

  async function handleCancelRun() {
    if (!cancellingRun) return;
    const target = cancellingRun;
    setCancellingRun(null);
    try {
      const result = await cancelBatchRun(target.id);
      toast.success(
        `已提交取消：中断 ${result.interrupted_tasks} 个执行中项目，取消 ${result.cancelled_queued} 个排队项目`
      );
      await Promise.all([loadRuns(true), loadSchedules(true)]);
      if (activeRunId === target.id) {
        // activeRun 会从刷新后的 runs 里派生出最新状态，
        // 这里同步快照，避免翻页后退回旧值
        setActiveRunSnapshot({ ...target, status: "cancelled" });
      }
    } catch (err) {
      toast.error(extractErrorMessage(err, "取消批次失败"));
    }
  }

  function openItems(run: AuditBatchRun) {
    setActiveRunId(run.id);
    setActiveRunSnapshot(run);
    setItemsPage(0);
    setItemsStatusFilter("all");
    setItems([]);
    setItemsTotal(0);
  }

  function closeItems() {
    setActiveRunId(null);
    setActiveRunSnapshot(null);
  }

  const totalPages = Math.max(1, Math.ceil(itemsTotal / PAGE_SIZE));

  /** 批次列表接口不返回计划名，用已加载的计划做映射 */
  const scheduleNameById = useMemo(() => {
    const map: Record<string, string> = {};
    schedules.forEach((s) => {
      map[s.id] = s.name;
    });
    return map;
  }, [schedules]);

  function runLabel(run: AuditBatchRun): string {
    if (run.schedule_name) return run.schedule_name;
    if (run.schedule_id && scheduleNameById[run.schedule_id]) {
      return scheduleNameById[run.schedule_id];
    }
    return run.trigger_type === "manual" ? "手动批次" : run.id.slice(0, 8);
  }

  return (
    <div className="space-y-6 p-6 cyber-bg-elevated min-h-screen font-mono relative">
      <div className="absolute inset-0 cyber-grid-subtle pointer-events-none" />

      {/* 页头 */}
      <div className="flex flex-wrap items-center justify-between gap-3 relative z-10">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-primary/20 rounded border border-primary/30">
            <CalendarClock className="w-5 h-5 text-primary" />
          </div>
          <div>
            <h2 className="text-lg font-bold uppercase tracking-wider text-foreground">
              定时批量审计
            </h2>
            <p className="text-xs text-muted-foreground">
              按计划并发执行 Agent 深度审计，支持全部项目或指定项目
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            className="h-9 cyber-btn-outline"
            onClick={() => {
              loadSchedules(true);
              loadRuns(true);
            }}
          >
            <RefreshCw className="w-4 h-4 mr-2" />
            刷新
          </Button>
          <Button
            size="sm"
            className="h-9 cyber-btn-primary font-bold uppercase"
            onClick={() => {
              setEditingSchedule(null);
              setShowCreateDialog(true);
            }}
          >
            <Plus className="w-4 h-4 mr-2" />
            新建计划
          </Button>
        </div>
      </div>

      {/* 计划区 */}
      <div className="cyber-card p-0 relative z-10">
        <div className="cyber-card-header">
          <CalendarClock className="w-5 h-5 text-primary" />
          <h3 className="text-base font-bold uppercase tracking-wider text-foreground">
            审计计划
          </h3>
          <Badge className="ml-2 cyber-badge-muted">{schedules.length} 个</Badge>
        </div>

        {loadingSchedules ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="w-6 h-6 animate-spin text-primary" />
          </div>
        ) : schedules.length === 0 ? (
          <div className="empty-state py-16">
            <Inbox className="empty-state-icon" />
            <p className="empty-state-title">还没有审计计划</p>
            <p className="empty-state-description">
              创建计划后可定时或手动批量审计项目
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>名称</TableHead>
                  <TableHead>项目范围</TableHead>
                  <TableHead>并发</TableHead>
                  <TableHead>调度</TableHead>
                  <TableHead>下次执行</TableHead>
                  <TableHead>最近批次</TableHead>
                  <TableHead>启用</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {schedules.map((schedule) => (
                  <TableRow key={schedule.id}>
                    <TableCell>
                      <div className="font-bold text-foreground">{schedule.name}</div>
                      {schedule.description && (
                        <div className="text-xs text-muted-foreground truncate max-w-[220px]">
                          {schedule.description}
                        </div>
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge className="cyber-badge-info">
                        {schedule.project_scope === "all"
                          ? "全部项目"
                          : `已选 ${schedule.project_count ?? schedule.project_ids.length} 个`}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-foreground">
                      {schedule.concurrency}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs max-w-[240px]">
                      <div className="truncate">{schedule.schedule_description}</div>
                      {schedule.skip_if_running && (
                        <div className="text-[11px] text-muted-foreground/70">
                          上批未结束时跳过
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                      {schedule.enabled
                        ? formatDateTime(schedule.next_run_at)
                        : "已停用"}
                    </TableCell>
                    <TableCell>
                      {schedule.latest_run ? (
                        <button
                          type="button"
                          className="text-left"
                          onClick={() => openItems(schedule.latest_run as AuditBatchRun)}
                        >
                          <Badge className={RUN_STATUS_META[schedule.latest_run.status]?.className}>
                            {RUN_STATUS_META[schedule.latest_run.status]?.label ??
                              schedule.latest_run.status}
                          </Badge>
                          <div className="text-[11px] text-muted-foreground mt-1">
                            {formatDateTime(schedule.latest_run.created_at)}
                          </div>
                        </button>
                      ) : (
                        <span className="text-xs text-muted-foreground">未执行</span>
                      )}
                    </TableCell>
                    <TableCell>
                      <Switch
                        checked={schedule.enabled}
                        disabled={togglingId === schedule.id}
                        onCheckedChange={(v) => handleToggleEnabled(schedule, v)}
                      />
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-8 px-2 text-emerald-400 hover:bg-emerald-900/30 hover:text-emerald-300"
                          disabled={triggeringId === schedule.id}
                          onClick={() => handleTrigger(schedule)}
                          title="立即执行"
                        >
                          {triggeringId === schedule.id ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                          ) : (
                            <Play className="w-4 h-4" />
                          )}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-8 px-2 text-muted-foreground hover:text-foreground"
                          onClick={() => {
                            setEditingSchedule(schedule);
                            setShowCreateDialog(true);
                          }}
                          title="编辑"
                        >
                          <Pencil className="w-4 h-4" />
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-8 px-2 text-rose-400 hover:bg-rose-900/30 hover:text-rose-300"
                          onClick={() => setDeletingSchedule(schedule)}
                          title="删除"
                        >
                          <Trash2 className="w-4 h-4" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      {/* 批次区 */}
      <div className="cyber-card p-0 relative z-10">
        <div className="cyber-card-header">
          <Inbox className="w-5 h-5 text-primary" />
          <h3 className="text-base font-bold uppercase tracking-wider text-foreground">
            批次执行
          </h3>
          <Badge className="ml-2 cyber-badge-muted">{runs.length} 个</Badge>
          <div className="ml-auto w-[180px]">
            <Select value={runStatusFilter} onValueChange={setRunStatusFilter}>
              <SelectTrigger className="h-8 text-xs">
                <SelectValue placeholder="全部状态" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部状态</SelectItem>
                {Object.entries(RUN_STATUS_META).map(([value, meta]) => (
                  <SelectItem key={value} value={value}>
                    {meta.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {loadingRuns ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="w-6 h-6 animate-spin text-primary" />
          </div>
        ) : runs.length === 0 ? (
          <div className="empty-state py-16">
            <Inbox className="empty-state-icon" />
            <p className="empty-state-title">暂无批次记录</p>
            <p className="empty-state-description">
              计划触发或点击「立即执行」后会生成批次
            </p>
          </div>
        ) : (
          <div className="divide-y divide-border">
            {runs.map((run) => {
              const meta = RUN_STATUS_META[run.status];
              const active = ACTIVE_RUN_STATUSES.has(run.status);
              return (
                <div key={run.id} className="p-4 space-y-3">
                  <div className="flex flex-wrap items-center gap-3">
                    <Badge className={meta?.className}>{meta?.label ?? run.status}</Badge>
                    <span className="text-sm text-foreground font-bold">
                      {runLabel(run)}
                    </span>
                    <Badge className="cyber-badge-muted">
                      {run.trigger_type === "manual" ? "手动触发" : "定时触发"}
                    </Badge>
                    <Badge className="cyber-badge-muted">并发 {run.concurrency}</Badge>
                    <span className="text-xs text-muted-foreground">
                      {formatDateTime(run.created_at)}
                    </span>

                    <div className="ml-auto flex items-center gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-8 text-xs cyber-btn-outline"
                        onClick={() => openItems(run)}
                      >
                        查看明细
                        <ArrowUpRight className="w-3 h-3 ml-1" />
                      </Button>
                      {active && (
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-8 text-xs cyber-btn-outline text-rose-400 border-rose-500/30 hover:bg-rose-500/10"
                          onClick={() => setCancellingRun(run)}
                        >
                          <XCircle className="w-3 h-3 mr-1" />
                          取消批次
                        </Button>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-3">
                    <Progress value={run.progress} className="flex-1 h-2" />
                    <span className="text-xs text-muted-foreground whitespace-nowrap">
                      {run.progress.toFixed(1)}%
                    </span>
                  </div>

                  <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
                    <span>总计 {run.total_projects}</span>
                    <span className="text-sky-400">执行中 {run.running_count}</span>
                    <span>排队 {run.queued_count}</span>
                    <span className="text-emerald-400">成功 {run.completed_count}</span>
                    <span className="text-rose-400">失败 {run.failed_count}</span>
                    <span className="text-amber-400">跳过/取消 {run.skipped_count}</span>
                    {run.started_at && <span>开始 {formatDateTime(run.started_at)}</span>}
                    {run.finished_at && <span>结束 {formatDateTime(run.finished_at)}</span>}
                  </div>

                  {run.error_message && (
                    <div className="flex items-start gap-2 text-xs text-rose-400 bg-rose-500/10 border border-rose-500/30 rounded p-2">
                      <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                      <span className="break-all">{run.error_message}</span>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* 明细抽屉 */}
      <Sheet open={!!activeRunId} onOpenChange={(o) => !o && closeItems()}>
        <SheetContent
          side="right"
          className="!w-[min(96vw,860px)] !max-w-none p-0 flex flex-col gap-0 cyber-dialog"
        >
          <SheetHeader className="px-5 py-4 border-b border-border bg-muted flex-shrink-0">
            <SheetTitle className="font-mono text-base uppercase tracking-wider text-foreground">
              批次明细 · {activeRun ? runLabel(activeRun) : ""}
            </SheetTitle>
            <SheetDescription className="font-mono text-xs text-muted-foreground">
              共 {itemsTotal} 个项目 ·{" "}
              {RUN_STATUS_META[activeRun?.status ?? ""]?.label ?? activeRun?.status}
            </SheetDescription>
          </SheetHeader>

          <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-border flex-shrink-0">
            <div className="w-[180px]">
              <Select value={itemsStatusFilter} onValueChange={setItemsStatusFilter}>
                <SelectTrigger className="h-8 text-xs">
                  <SelectValue placeholder="全部状态" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部状态</SelectItem>
                  {Object.entries(ITEM_STATUS_META).map(([value, meta]) => (
                    <SelectItem key={value} value={value}>
                      {meta.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Button
                size="sm"
                variant="outline"
                className="h-8 cyber-btn-outline"
                disabled={itemsPage === 0}
                onClick={() => setItemsPage((p) => Math.max(0, p - 1))}
              >
                上一页
              </Button>
              <span>
                {itemsPage + 1} / {totalPages}
              </span>
              <Button
                size="sm"
                variant="outline"
                className="h-8 cyber-btn-outline"
                disabled={itemsPage + 1 >= totalPages}
                onClick={() => setItemsPage((p) => p + 1)}
              >
                下一页
              </Button>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto">
            {loadingItems ? (
              <div className="flex items-center justify-center py-16">
                <Loader2 className="w-6 h-6 animate-spin text-primary" />
              </div>
            ) : items.length === 0 ? (
              <div className="empty-state py-16">
                <Inbox className="empty-state-icon" />
                <p className="empty-state-title">暂无明细</p>
                <p className="empty-state-description">该批次还没有项目记录</p>
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>项目</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>发现</TableHead>
                    <TableHead>开始 / 结束</TableHead>
                    <TableHead>错误信息</TableHead>
                    <TableHead className="text-right">执行详情</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((item) => {
                    const meta = ITEM_STATUS_META[item.status];
                    return (
                      <TableRow key={item.id}>
                        <TableCell className="font-bold text-foreground max-w-[180px]">
                          <div className="truncate">
                            {item.project_name || item.project_id || "—"}
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge className={meta?.className}>
                            {meta?.label ?? item.status}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-foreground">
                          {item.findings_count}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                          <div>{formatDateTime(item.started_at)}</div>
                          <div>{formatDateTime(item.finished_at)}</div>
                        </TableCell>
                        <TableCell className="text-xs text-rose-400 max-w-[240px]">
                          <div className="truncate" title={item.error_message || ""}>
                            {item.error_message || "—"}
                          </div>
                        </TableCell>
                        <TableCell className="text-right">
                          {item.agent_task_id ? (
                            <Link
                              to={`/agent-audit/${item.agent_task_id}`}
                              className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                            >
                              查看
                              <ArrowUpRight className="w-3 h-3" />
                            </Link>
                          ) : (
                            <span className="text-xs text-muted-foreground">—</span>
                          )}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            )}
          </div>
        </SheetContent>
      </Sheet>

      {/* 创建 / 编辑对话框 */}
      <CreateBatchScheduleDialog
        open={showCreateDialog}
        onOpenChange={setShowCreateDialog}
        initial={editingSchedule}
        onSaved={() => {
          loadSchedules(true);
          loadRuns(true);
        }}
      />

      {/* 删除计划确认 */}
      <AlertDialog open={!!deletingSchedule} onOpenChange={(o) => !o && setDeletingSchedule(null)}>
        <AlertDialogContent className="cyber-card p-0 cyber-dialog max-w-md !fixed">
          <AlertDialogHeader className="p-4 border-b border-rose-500/30 bg-rose-500/10 flex flex-row items-center gap-2">
            <AlertTriangle className="w-5 h-5 text-rose-400" />
            <AlertDialogTitle className="text-lg font-bold uppercase tracking-wider text-rose-400">
              删除审计计划
            </AlertDialogTitle>
          </AlertDialogHeader>
          <AlertDialogDescription className="p-6 text-muted-foreground">
            确定删除计划{" "}
            <span className="font-bold text-foreground">
              "{deletingSchedule?.name}"
            </span>{" "}
            吗？
            <br />
            <br />
            删除后不再自动触发，历史批次记录会保留。
          </AlertDialogDescription>
          <AlertDialogFooter className="p-4 border-t border-border flex gap-3">
            <AlertDialogCancel className="cyber-btn-outline">取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteSchedule}
              className="cyber-btn-primary bg-rose-600 hover:bg-rose-500 border-rose-500"
            >
              确认删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 取消批次确认 */}
      <AlertDialog open={!!cancellingRun} onOpenChange={(o) => !o && setCancellingRun(null)}>
        <AlertDialogContent className="cyber-card p-0 cyber-dialog max-w-md !fixed">
          <AlertDialogHeader className="cyber-card-header">
            <XCircle className="w-5 h-5 text-rose-400" />
            <AlertDialogTitle className="text-lg font-bold uppercase tracking-wider text-foreground">
              取消批次
            </AlertDialogTitle>
          </AlertDialogHeader>
          <AlertDialogDescription className="p-6 text-muted-foreground">
            确定取消批次{" "}
            <span className="font-bold text-foreground">
              {cancellingRun?.id.slice(0, 8)}
            </span>{" "}
            吗？
            <br />
            <br />
            排队中的项目将直接标记为已取消，正在执行的项目会逐个中断其 Agent 审计。
          </AlertDialogDescription>
          <AlertDialogFooter className="p-4 border-t border-border flex gap-3">
            <AlertDialogCancel className="cyber-btn-outline">返回</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleCancelRun}
              className="cyber-btn-primary bg-rose-600 hover:bg-rose-500 border-rose-500"
            >
              确认取消
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
