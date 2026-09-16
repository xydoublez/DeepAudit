/**
 * 定时批量审计计划创建 / 编辑对话框
 * Cyberpunk Terminal Aesthetic
 */

import { useState, useEffect, useMemo } from "react";
import { format } from "date-fns";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Switch } from "@/components/ui/switch";
import { Slider } from "@/components/ui/slider";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import MultiSelect from "@/components/ui/multi-select";
import {
  CalendarClock,
  CalendarDays,
  ChevronRight,
  Clock,
  Globe,
  Loader2,
  Package,
  Play,
  Search,
  Settings2,
} from "lucide-react";
import { toast } from "sonner";
import { api } from "@/shared/config/database";
import type { Project } from "@/shared/types";
import { isRepositoryProject } from "@/shared/utils/projectUtils";
import {
  createSchedule,
  updateSchedule,
  type AuditSchedule,
  type CreateScheduleRequest,
  type ProjectScope,
  type ScheduleConfig,
  type ScheduleType,
} from "@/shared/api/batchAudit";

interface CreateBatchScheduleDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 传入则为编辑模式 */
  initial?: AuditSchedule | null;
  /** 保存成功回调 */
  onSaved?: (schedule: AuditSchedule) => void;
}

const DEFAULT_EXCLUDES = [
  "node_modules/**",
  ".git/**",
  "dist/**",
  "build/**",
  "*.log",
];

// 上限需与后端 BATCH_AUDIT_MAX_CONCURRENCY 保持一致。每个审计任务会全程独占
// 一个数据库连接（会话包住整个审计生命周期），并发过高会耗尽连接池，
// 连带把登录、项目列表等普通接口一起拖挂
const MAX_CONCURRENCY = 10;

const WEEKDAY_LABELS = [
  { value: 1, label: "一" },
  { value: 2, label: "二" },
  { value: 3, label: "三" },
  { value: 4, label: "四" },
  { value: 5, label: "五" },
  { value: 6, label: "六" },
  { value: 7, label: "日" },
];

const TIMEZONE_OPTIONS = [
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Asia/Singapore",
  "UTC",
  "Europe/London",
  "Europe/Berlin",
  "America/New_York",
  "America/Los_Angeles",
];

const VULNERABILITY_OPTIONS = [
  { value: "sql_injection", label: "SQL 注入" },
  { value: "xss", label: "XSS 跨站脚本" },
  { value: "command_injection", label: "命令注入" },
  { value: "path_traversal", label: "路径穿越" },
  { value: "ssrf", label: "SSRF" },
  { value: "xxe", label: "XXE" },
  { value: "deserialization", label: "反序列化" },
  { value: "file_upload", label: "文件上传" },
  { value: "auth_bypass", label: "认证绕过" },
  { value: "idor", label: "越权访问 IDOR" },
];

const VERIFICATION_LEVELS = [
  { value: "analysis_only", label: "仅静态分析" },
  { value: "sandbox", label: "沙箱验证" },
  { value: "generate_poc", label: "生成 PoC" },
];

const TIME_PATTERN = /^([01]\d|2[0-3]):[0-5]\d$/;

/** 把日期 + "HH:MM" 拼成 naive ISO 串，由后端按计划时区解释 */
function buildOnceRunAt(date: Date, time: string): string {
  const [hh, mm] = time.split(":");
  return `${format(date, "yyyy-MM-dd")}T${hh}:${mm || "00"}:00`;
}

/** 从 naive run_at 串中提取字面 "HH:MM"
 *
 * once 类型的 schedule_config 只有 run_at、没有 time 字段，编辑回填时必须从
 * run_at 里取时间，否则会静默退回默认的 15:00，用户不改直接保存就把执行时间改错了。
 * 该串按计划时区存储与解释，因此按字面提取即可，无需做时区换算。
 */
function extractTimeFromRunAt(raw?: string): string | undefined {
  if (!raw) return undefined;
  const match = raw.match(/T([01]\d|2[0-3]):([0-5]\d)/);
  return match ? `${match[1]}:${match[2]}` : undefined;
}

export default function CreateBatchScheduleDialog({
  open,
  onOpenChange,
  initial,
  onSaved,
}: CreateBatchScheduleDialogProps) {
  const isEdit = !!initial;

  // 项目
  const [projects, setProjects] = useState<Project[]>([]);
  const [loadingProjects, setLoadingProjects] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");
  const [scope, setScope] = useState<ProjectScope>("all");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  // 基本与并发
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [concurrency, setConcurrency] = useState(5);
  const [skipIfRunning, setSkipIfRunning] = useState(true);
  const [enabled, setEnabled] = useState(true);

  // 调度
  const [scheduleType, setScheduleType] = useState<ScheduleType>("daily");
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [timeStr, setTimeStr] = useState("15:00");
  const [weekdays, setWeekdays] = useState<number[]>([1, 2, 3, 4, 5]);
  const [intervalHours, setIntervalHours] = useState(6);
  const [onceDate, setOnceDate] = useState<Date | undefined>(undefined);

  // 审计参数模板
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [verificationLevel, setVerificationLevel] = useState("sandbox");
  const [targetVulns, setTargetVulns] = useState<string[]>([]);
  const [excludePatterns, setExcludePatterns] = useState<string[]>(DEFAULT_EXCLUDES);
  const [maxIterations, setMaxIterations] = useState(50);
  const [timeoutSeconds, setTimeoutSeconds] = useState(1800);
  const [branchName, setBranchName] = useState("");

  const [saving, setSaving] = useState(false);

  // 打开时加载项目并回填表单
  useEffect(() => {
    if (!open) return;

    setLoadingProjects(true);
    api
      .getProjects()
      .then((data) => setProjects(data.filter((p) => p.is_active)))
      .catch(() => toast.error("加载项目列表失败"))
      .finally(() => setLoadingProjects(false));

    if (initial) {
      const config = initial.schedule_config || {};
      // 项目搜索词属于临时 UI 状态，必须随表单一起重置，
      // 否则上一次搜过的关键字会残留，让用户以为项目丢了
      setSearchTerm("");
      setName(initial.name);
      setDescription(initial.description || "");
      setScope(initial.project_scope);
      setSelectedIds(initial.project_ids || []);
      setConcurrency(initial.concurrency);
      setSkipIfRunning(initial.skip_if_running);
      setEnabled(initial.enabled);
      setScheduleType(initial.schedule_type);
      setTimezone(initial.timezone || "Asia/Shanghai");
      setTimeStr(config.time || extractTimeFromRunAt(config.run_at) || "15:00");
      setWeekdays(config.weekdays?.length ? config.weekdays : [1, 2, 3, 4, 5]);
      setIntervalHours(config.interval_hours || 6);
      setOnceDate(
        config.run_at ? parseRunAtToDate(config.run_at) : undefined
      );

      const tpl = initial.task_template || {};
      setVerificationLevel(tpl.verification_level || "sandbox");
      setTargetVulns(tpl.target_vulnerabilities || []);
      setExcludePatterns(tpl.exclude_patterns || DEFAULT_EXCLUDES);
      setMaxIterations(tpl.max_iterations || 50);
      setTimeoutSeconds(tpl.timeout_seconds || 1800);
      setBranchName(tpl.branch_name || "");
      setShowAdvanced(false);
    } else {
      resetForm();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initial?.id]);

  function resetForm() {
    setName("");
    setDescription("");
    setSearchTerm("");
    setScope("all");
    setSelectedIds([]);
    setConcurrency(5);
    setSkipIfRunning(true);
    setEnabled(true);
    setScheduleType("daily");
    setTimezone("Asia/Shanghai");
    setTimeStr("15:00");
    setWeekdays([1, 2, 3, 4, 5]);
    setIntervalHours(6);
    setOnceDate(undefined);
    setShowAdvanced(false);
    setVerificationLevel("sandbox");
    setTargetVulns([]);
    setExcludePatterns(DEFAULT_EXCLUDES);
    setMaxIterations(50);
    setTimeoutSeconds(1800);
    setBranchName("");
  }

  const filteredProjects = useMemo(() => {
    if (!searchTerm) return projects;
    const term = searchTerm.toLowerCase();
    return projects.filter(
      (p) =>
        p.name.toLowerCase().includes(term) ||
        p.description?.toLowerCase().includes(term)
    );
  }, [projects, searchTerm]);

  const allFilteredSelected = useMemo(
    () =>
      filteredProjects.length > 0 &&
      filteredProjects.every((p) => selectedIds.includes(p.id)),
    [filteredProjects, selectedIds]
  );

  const validationError = useMemo<string | null>(() => {
    if (!name.trim()) return "请填写计划名称";
    if (scope === "selected" && selectedIds.length === 0)
      return "请至少选择一个项目";
    if (scheduleType === "once") {
      if (!onceDate) return "请选择执行日期";
      if (!TIME_PATTERN.test(timeStr)) return "执行时间格式应为 HH:MM";
    }
    if ((scheduleType === "daily" || scheduleType === "weekly") &&
        !TIME_PATTERN.test(timeStr)) {
      return "触发时间格式应为 HH:MM";
    }
    if (scheduleType === "weekly" && weekdays.length === 0)
      return "请至少选择一个星期";
    if (scheduleType === "interval" && !(intervalHours > 0))
      return "间隔小时数必须大于 0";
    if (concurrency < 1 || concurrency > MAX_CONCURRENCY)
      return `并发度需在 1 - ${MAX_CONCURRENCY} 之间`;
    return null;
  }, [name, scope, selectedIds, scheduleType, onceDate, timeStr, weekdays, intervalHours, concurrency]);

  function buildScheduleConfig(): ScheduleConfig {
    switch (scheduleType) {
      case "once":
        return { run_at: onceDate ? buildOnceRunAt(onceDate, timeStr) : undefined };
      case "daily":
        return { time: timeStr };
      case "weekly":
        return { time: timeStr, weekdays: [...weekdays].sort((a, b) => a - b) };
      case "interval":
        return { interval_hours: intervalHours };
      default:
        return {};
    }
  }

  async function handleSubmit() {
    if (validationError) {
      toast.error(validationError);
      return;
    }

    const payload: CreateScheduleRequest = {
      name: name.trim(),
      description: description.trim() || null,
      project_scope: scope,
      project_ids: scope === "selected" ? selectedIds : null,
      concurrency,
      skip_if_running: skipIfRunning,
      schedule_type: scheduleType,
      schedule_config: buildScheduleConfig(),
      timezone,
      task_template: {
        verification_level: verificationLevel,
        target_vulnerabilities: targetVulns.length ? targetVulns : null,
        exclude_patterns: excludePatterns.length ? excludePatterns : null,
        max_iterations: maxIterations,
        timeout_seconds: timeoutSeconds,
        branch_name: branchName.trim() || null,
      },
      enabled,
    };

    setSaving(true);
    try {
      const saved = isEdit && initial
        ? await updateSchedule(initial.id, payload)
        : await createSchedule(payload);

      toast.success(isEdit ? "计划已更新" : "定时批量审计计划已创建");
      onSaved?.(saved);
      onOpenChange(false);
    } catch (err) {
      const msg = extractErrorMessage(err);
      toast.error(msg);
    } finally {
      setSaving(false);
    }
  }

  function toggleProject(id: string, checked: boolean) {
    setSelectedIds((prev) =>
      checked ? [...new Set([...prev, id])] : prev.filter((x) => x !== id)
    );
  }

  function toggleAllFiltered() {
    if (allFilteredSelected) {
      const ids = new Set(filteredProjects.map((p) => p.id));
      setSelectedIds((prev) => prev.filter((id) => !ids.has(id)));
    } else {
      setSelectedIds((prev) => [
        ...new Set([...prev, ...filteredProjects.map((p) => p.id)]),
      ]);
    }
  }

  function toggleWeekday(value: number) {
    setWeekdays((prev) =>
      prev.includes(value) ? prev.filter((d) => d !== value) : [...prev, value]
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="!w-[min(94vw,720px)] !max-w-none max-h-[88vh] flex flex-col p-0 gap-0 cyber-dialog border border-border rounded-lg">
        {/* Header */}
        <DialogHeader className="px-5 py-4 border-b border-border flex-shrink-0 bg-muted">
          <DialogTitle className="flex items-center gap-3 font-mono text-foreground">
            <div className="p-2 bg-primary/20 rounded border border-primary/30">
              <CalendarClock className="w-5 h-5 text-primary" />
            </div>
            <div>
              <span className="text-base font-bold uppercase tracking-wider">
                {isEdit ? "Edit Batch Schedule" : "New Batch Schedule"}
              </span>
              <p className="text-xs text-muted-foreground font-normal mt-0.5">
                定时并发执行 Agent 深度审计
              </p>
            </div>
          </DialogTitle>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto p-5 space-y-6">
          {/* 基本信息 */}
          <Section title="Basic Info" icon={<Settings2 className="w-4 h-4" />}>
            <div className="space-y-2">
              <Label className="font-mono text-xs uppercase text-muted-foreground">
                计划名称
              </Label>
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="例如：全量项目每日审计"
                className="h-10 cyber-input"
                maxLength={255}
              />
            </div>
            <div className="space-y-2">
              <Label className="font-mono text-xs uppercase text-muted-foreground">
                描述（可选）
              </Label>
              <Input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="计划用途说明"
                className="h-10 cyber-input"
              />
            </div>
          </Section>

          {/* 项目范围 */}
          <Section
            title="Project Scope"
            icon={<Globe className="w-4 h-4" />}
            extra={
              <Badge className="cyber-badge-muted font-mono text-xs">
                {scope === "all"
                  ? `全部 ${projects.length} 个项目`
                  : `已选 ${selectedIds.length} 个`}
              </Badge>
            }
          >
            <RadioGroup
              value={scope}
              onValueChange={(v) => setScope(v as ProjectScope)}
              className="flex gap-6"
            >
              <div className="flex items-center gap-2">
                <RadioGroupItem value="all" id="scope-all" />
                <Label htmlFor="scope-all" className="font-mono text-sm cursor-pointer">
                  全部项目
                </Label>
              </div>
              <div className="flex items-center gap-2">
                <RadioGroupItem value="selected" id="scope-selected" />
                <Label htmlFor="scope-selected" className="font-mono text-sm cursor-pointer">
                  指定项目
                </Label>
              </div>
            </RadioGroup>

            {scope === "selected" && (
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <div className="relative flex-1">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                    <Input
                      placeholder="搜索项目..."
                      value={searchTerm}
                      onChange={(e) => setSearchTerm(e.target.value)}
                      className="!pl-9 h-9 cyber-input"
                    />
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={toggleAllFiltered}
                    disabled={filteredProjects.length === 0}
                    className="h-9 text-xs cyber-btn-outline font-mono whitespace-nowrap"
                  >
                    {allFilteredSelected ? "取消全选" : "全选筛选结果"}
                  </Button>
                  {selectedIds.length > 0 && (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => setSelectedIds([])}
                      className="h-9 text-xs text-rose-400 hover:bg-rose-900/30 hover:text-rose-300 whitespace-nowrap"
                    >
                      清空
                    </Button>
                  )}
                </div>

                <ScrollArea className="h-[220px] border border-border rounded bg-muted/50">
                  {loadingProjects ? (
                    <div className="flex items-center justify-center h-full">
                      <Loader2 className="w-5 h-5 animate-spin text-primary" />
                    </div>
                  ) : filteredProjects.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-full text-muted-foreground font-mono">
                      <Package className="w-8 h-8 mb-2 opacity-50" />
                      <span className="text-sm">
                        {searchTerm ? "无匹配项目" : "暂无可用项目"}
                      </span>
                    </div>
                  ) : (
                    <div className="p-1">
                      {filteredProjects.map((project) => {
                        const checked = selectedIds.includes(project.id);
                        const isRepo = isRepositoryProject(project);
                        return (
                          <label
                            key={project.id}
                            className={`flex items-center gap-3 p-2.5 cursor-pointer rounded transition-all ${
                              checked
                                ? "bg-primary/10 border border-primary/40"
                                : "hover:bg-muted border border-transparent"
                            }`}
                          >
                            <Checkbox
                              checked={checked}
                              onCheckedChange={(c) => toggleProject(project.id, c === true)}
                              className="size-4"
                            />
                            <span className="font-mono text-sm text-foreground truncate flex-1">
                              {project.name}
                            </span>
                            <Badge
                              className={`text-xs px-1 py-0 font-mono ${
                                isRepo
                                  ? "bg-blue-500/20 text-blue-400 border-blue-500/30"
                                  : "bg-amber-500/20 text-amber-400 border-amber-500/30"
                              }`}
                            >
                              {isRepo ? "REPO" : "ZIP"}
                            </Badge>
                          </label>
                        );
                      })}
                    </div>
                  )}
                </ScrollArea>
              </div>
            )}
          </Section>

          {/* 并发与执行策略 */}
          <Section title="Concurrency" icon={<Play className="w-4 h-4" />}>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label className="font-mono text-xs uppercase text-muted-foreground">
                  并发项目数
                </Label>
                <Badge className="cyber-badge-info font-mono text-xs">
                  {concurrency} / {MAX_CONCURRENCY}
                </Badge>
              </div>
              <Slider
                value={[concurrency]}
                onValueChange={(v) => setConcurrency(v[0])}
                min={1}
                max={MAX_CONCURRENCY}
                step={1}
              />
              <p className="text-xs text-muted-foreground font-mono">
                同时审计 {concurrency} 个项目，其余排队顺序执行。并发越高越容易触发 LLM 限流，建议 5 左右。
              </p>
            </div>

            <div className="flex items-center justify-between p-3 border border-border rounded bg-muted/50">
              <div>
                <p className="font-mono text-sm text-foreground">上一批未跑完时跳过本次触发</p>
                <p className="text-xs text-muted-foreground mt-0.5">
                  避免长批次跨天时重复叠加执行
                </p>
              </div>
              <Switch checked={skipIfRunning} onCheckedChange={setSkipIfRunning} />
            </div>

            <div className="flex items-center justify-between p-3 border border-border rounded bg-muted/50">
              <div>
                <p className="font-mono text-sm text-foreground">启用计划</p>
                <p className="text-xs text-muted-foreground mt-0.5">
                  关闭后不会自动触发，仍可手动立即执行
                </p>
              </div>
              <Switch checked={enabled} onCheckedChange={setEnabled} />
            </div>
          </Section>

          {/* 调度方式 */}
          <Section title="Schedule" icon={<Clock className="w-4 h-4" />}>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label className="font-mono text-xs uppercase text-muted-foreground">
                  调度方式
                </Label>
                <Select
                  value={scheduleType}
                  onValueChange={(v) => setScheduleType(v as ScheduleType)}
                >
                  <SelectTrigger className="h-10 cyber-input">
                    <SelectValue placeholder="选择调度方式" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="daily">每天</SelectItem>
                    <SelectItem value="weekly">每周</SelectItem>
                    <SelectItem value="interval">固定间隔</SelectItem>
                    <SelectItem value="once">一次性</SelectItem>
                    <SelectItem value="manual">仅手动触发</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              {scheduleType !== "manual" && scheduleType !== "interval" && (
                <div className="space-y-2">
                  <Label className="font-mono text-xs uppercase text-muted-foreground">
                    时区
                  </Label>
                  <Select value={timezone} onValueChange={setTimezone}>
                    <SelectTrigger className="h-10 cyber-input">
                      <SelectValue placeholder="选择时区" />
                    </SelectTrigger>
                    <SelectContent>
                      {TIMEZONE_OPTIONS.map((tz) => (
                        <SelectItem key={tz} value={tz}>
                          {tz}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
            </div>

            {/* 一次性：日期 + 时间 */}
            {scheduleType === "once" && (
              <div className="flex flex-wrap items-end gap-3">
                <div className="space-y-2">
                  <Label className="font-mono text-xs uppercase text-muted-foreground">
                    执行日期
                  </Label>
                  <Popover>
                    <PopoverTrigger asChild>
                      <Button
                        type="button"
                        variant="outline"
                        className="h-10 justify-start text-left font-mono cyber-btn-outline"
                      >
                        <CalendarDays className="w-4 h-4 mr-2" />
                        {onceDate ? format(onceDate, "yyyy-MM-dd") : "选择日期"}
                      </Button>
                    </PopoverTrigger>
                    <PopoverContent className="w-auto p-0" align="start">
                      <Calendar
                        mode="single"
                        selected={onceDate}
                        onSelect={setOnceDate}
                        initialFocus
                      />
                    </PopoverContent>
                  </Popover>
                </div>
                <div className="space-y-2">
                  <Label className="font-mono text-xs uppercase text-muted-foreground">
                    执行时间
                  </Label>
                  <Input
                    type="time"
                    value={timeStr}
                    onChange={(e) => setTimeStr(e.target.value)}
                    className="h-10 cyber-input w-[140px]"
                  />
                </div>
              </div>
            )}

            {/* 每天 / 每周：时间 */}
            {(scheduleType === "daily" || scheduleType === "weekly") && (
              <div className="space-y-2 max-w-[200px]">
                <Label className="font-mono text-xs uppercase text-muted-foreground">
                  触发时间
                </Label>
                <Input
                  type="time"
                  value={timeStr}
                  onChange={(e) => setTimeStr(e.target.value)}
                  className="h-10 cyber-input"
                />
              </div>
            )}

            {/* 每周：星期多选 */}
            {scheduleType === "weekly" && (
              <div className="space-y-2">
                <Label className="font-mono text-xs uppercase text-muted-foreground">
                  执行日
                </Label>
                <div className="flex flex-wrap gap-2">
                  {WEEKDAY_LABELS.map((day) => {
                    const active = weekdays.includes(day.value);
                    return (
                      <button
                        key={day.value}
                        type="button"
                        onClick={() => toggleWeekday(day.value)}
                        className={`w-10 h-10 rounded border font-mono text-sm transition-colors ${
                          active
                            ? "bg-primary/20 border-primary/60 text-primary font-bold"
                            : "bg-muted/50 border-border text-muted-foreground hover:text-foreground"
                        }`}
                      >
                        {day.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* 间隔 */}
            {scheduleType === "interval" && (
              <div className="space-y-2 max-w-[200px]">
                <Label className="font-mono text-xs uppercase text-muted-foreground">
                  间隔小时数
                </Label>
                <Input
                  type="number"
                  min={0.5}
                  step={0.5}
                  value={intervalHours}
                  onChange={(e) => setIntervalHours(Number(e.target.value))}
                  className="h-10 cyber-input"
                />
              </div>
            )}

            {scheduleType === "manual" && (
              <p className="text-xs text-muted-foreground font-mono p-3 border border-dashed border-border rounded bg-muted/50">
                该计划不会自动触发，只能在计划列表中点击「立即执行」手动启动批次。
              </p>
            )}
          </Section>

          {/* 审计参数 */}
          <Collapsible open={showAdvanced} onOpenChange={setShowAdvanced}>
            <CollapsibleTrigger className="flex items-center gap-2 text-xs font-mono text-muted-foreground hover:text-foreground transition-colors">
              <ChevronRight
                className={`w-4 h-4 transition-transform ${showAdvanced ? "rotate-90" : ""}`}
              />
              <Settings2 className="w-4 h-4" />
              <span className="uppercase font-bold">审计参数模板</span>
            </CollapsibleTrigger>

            <CollapsibleContent className="mt-3 space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label className="font-mono text-xs uppercase text-muted-foreground">
                    验证级别
                  </Label>
                  <Select value={verificationLevel} onValueChange={setVerificationLevel}>
                    <SelectTrigger className="h-10 cyber-input">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {VERIFICATION_LEVELS.map((level) => (
                        <SelectItem key={level.value} value={level.value}>
                          {level.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-2">
                  <Label className="font-mono text-xs uppercase text-muted-foreground">
                    分支（留空则用各项目默认分支）
                  </Label>
                  <Input
                    value={branchName}
                    onChange={(e) => setBranchName(e.target.value)}
                    placeholder="main"
                    className="h-10 cyber-input"
                  />
                </div>

                <div className="space-y-2">
                  <Label className="font-mono text-xs uppercase text-muted-foreground">
                    最大迭代次数
                  </Label>
                  <Input
                    type="number"
                    min={1}
                    max={200}
                    value={maxIterations}
                    onChange={(e) => setMaxIterations(Number(e.target.value))}
                    className="h-10 cyber-input"
                  />
                </div>

                <div className="space-y-2">
                  <Label className="font-mono text-xs uppercase text-muted-foreground">
                    单项目超时（秒）
                  </Label>
                  <Input
                    type="number"
                    min={60}
                    max={7200}
                    step={60}
                    value={timeoutSeconds}
                    onChange={(e) => setTimeoutSeconds(Number(e.target.value))}
                    className="h-10 cyber-input"
                  />
                </div>
              </div>

              <div className="space-y-2">
                <Label className="font-mono text-xs uppercase text-muted-foreground">
                  目标漏洞类型（留空使用系统默认）
                </Label>
                <MultiSelect
                  options={VULNERABILITY_OPTIONS}
                  value={targetVulns}
                  onChange={setTargetVulns}
                />
              </div>

              <div className="p-3 border border-dashed border-border rounded bg-muted/50 space-y-3">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-xs uppercase font-bold text-muted-foreground">
                    排除模式
                  </span>
                  <button
                    type="button"
                    onClick={() => setExcludePatterns(DEFAULT_EXCLUDES)}
                    className="text-xs font-mono text-primary hover:text-primary/80"
                  >
                    Reset
                  </button>
                </div>

                <div className="flex flex-wrap gap-1.5">
                  {excludePatterns.map((p) => (
                    <Badge
                      key={p}
                      className="bg-muted text-foreground border-0 font-mono text-xs cursor-pointer hover:bg-rose-900/50 hover:text-rose-400"
                      onClick={() =>
                        setExcludePatterns((prev) => prev.filter((x) => x !== p))
                      }
                    >
                      {p} ×
                    </Badge>
                  ))}
                </div>

                <Input
                  placeholder="添加排除模式后按回车..."
                  className="h-8 cyber-input text-sm"
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && e.currentTarget.value) {
                      const val = e.currentTarget.value.trim();
                      if (val && !excludePatterns.includes(val)) {
                        setExcludePatterns((prev) => [...prev, val]);
                      }
                      e.currentTarget.value = "";
                    }
                  }}
                />
              </div>
            </CollapsibleContent>
          </Collapsible>
        </div>

        {/* Footer */}
        <div className="flex-shrink-0 flex items-center justify-between gap-3 px-5 py-4 bg-muted border-t border-border">
          <span className="text-xs font-mono text-rose-400 min-h-[16px]">
            {validationError || ""}
          </span>
          <div className="flex gap-3">
            <Button
              variant="ghost"
              onClick={() => onOpenChange(false)}
              disabled={saving}
              className="px-4 h-10 font-mono text-muted-foreground hover:text-foreground hover:bg-muted"
            >
              取消
            </Button>
            <Button
              onClick={handleSubmit}
              disabled={!!validationError || saving}
              className="px-5 h-10 cyber-btn-primary font-mono font-bold uppercase"
            >
              {saving ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin mr-2" />
                  Saving...
                </>
              ) : (
                <>
                  <CalendarClock className="w-4 h-4 mr-2" />
                  {isEdit ? "保存修改" : "创建计划"}
                </>
              )}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** 把 naive ISO 串解析为本地 Date（仅用于日期选择器展示） */
function parseRunAtToDate(raw: string): Date | undefined {
  const match = raw.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!match) return undefined;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(date.getTime()) ? undefined : date;
}

/** 从 axios 错误中提取后端 detail */
function extractErrorMessage(err: unknown): string {
  const anyErr = err as {
    response?: { data?: { detail?: unknown } };
    message?: string;
  };
  const detail = anyErr?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: string };
    if (first?.msg) return first.msg;
  }
  return anyErr?.message || "保存失败";
}

/** 分区容器 */
function Section({
  title,
  icon,
  extra,
  children,
}: {
  title: string;
  icon?: React.ReactNode;
  extra?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 text-xs font-mono font-bold uppercase text-muted-foreground">
          {icon}
          {title}
        </span>
        {extra}
      </div>
      <div className="space-y-3">{children}</div>
    </div>
  );
}
