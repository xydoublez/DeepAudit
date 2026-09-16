/**
 * ProjectImportDialog
 * CSV 模板批量导入项目：人工上传模板文件，解析预览后批量调用创建接口
 */

import { useMemo, useRef, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import {
  AlertCircle,
  CheckCircle,
  Download,
  FileText,
  FileUp,
  RefreshCw,
  Terminal,
} from "lucide-react";
import { toast } from "sonner";
import { api } from "@/shared/config/database";
import type { CreateProjectForm } from "@/shared/types";

const TEMPLATE_URL = "/project_import_template.csv";
const IMPORT_CONCURRENCY = 4;      // 并发创建数
const PREVIEW_ROWS = 5;            // 预览行数
const ERROR_DISPLAY_LIMIT = 10;    // 解析错误最多展示条数
const FAILURE_DISPLAY_LIMIT = 30;  // 失败明细最多展示条数
const DEFAULT_LANGUAGES = ["java", "javascript", "typescript"];
const VALID_SOURCE_TYPES = ["repository", "zip"];
const VALID_REPOSITORY_TYPES = ["github", "gitlab", "gitea", "other"];

interface ParsedRow {
  line: number;             // CSV 行号（含表头，便于与 Excel 对照）
  data?: CreateProjectForm;
  error?: string;
}

interface ImportFailure {
  line: number;
  name: string;
  error: string;
}

type Phase = "select" | "preview" | "importing" | "done";

/** 去除 UTF-8 BOM */
function stripBom(text: string): string {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

/** 轻量 CSV 解析：支持引号包裹、转义引号、\r\n 换行（保留空行以保证行号对照） */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += ch;
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      row.push(field); field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") continue; // 交给 \n 统一处理
      row.push(field); field = "";
      rows.push(row); row = [];
    } else {
      field += ch;
    }
  }
  row.push(field);
  rows.push(row);
  return rows;
}

/** 读取文件文本：优先 UTF-8，检测到乱码时回退 GBK */
async function readCsvText(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  let text = new TextDecoder("utf-8").decode(buffer);
  if (text.includes("\uFFFD")) {
    try {
      text = new TextDecoder("gbk").decode(buffer);
    } catch {
      /* GBK 解码不可用时保留 UTF-8 结果 */
    }
  }
  return stripBom(text);
}

/** 将一行数据转为创建表单，校验必填与枚举字段 */
function buildForm(fields: Record<string, string>): { data?: CreateProjectForm; error?: string } {
  const name = (fields.name || "").trim();
  if (!name) return { error: "name 不能为空" };

  const sourceType = (fields.source_type || "repository").trim().toLowerCase();
  if (!VALID_SOURCE_TYPES.includes(sourceType)) {
    return { error: `source_type 无效: ${fields.source_type}` };
  }

  const repositoryType = (fields.repository_type || "other").trim().toLowerCase();
  if (!VALID_REPOSITORY_TYPES.includes(repositoryType)) {
    return { error: `repository_type 无效: ${fields.repository_type}` };
  }

  const url = (fields.repository_url || "").trim();
  const langsRaw = (fields.programming_languages || "").trim();
  const languages = langsRaw
    ? langsRaw.split(/[,，;；、]/).map((s) => s.trim()).filter(Boolean)
    : DEFAULT_LANGUAGES;

  return {
    data: {
      name,
      description: (fields.description || "").trim() || undefined,
      source_type: sourceType as CreateProjectForm["source_type"],
      repository_url: sourceType === "repository" ? url || undefined : undefined,
      repository_type: repositoryType as CreateProjectForm["repository_type"],
      default_branch: (fields.default_branch || "main").trim() || "main",
      programming_languages: languages,
    },
  };
}

/** 解析整份 CSV 文本：表头映射 + 逐行校验 */
function buildRows(text: string): { rows: ParsedRow[]; headerError?: string } {
  const raw = parseCsv(text);
  if (raw.length === 0 || raw.every((r) => r.every((c) => !c.trim()))) {
    return { rows: [], headerError: "CSV 内容为空" };
  }
  const header = raw[0].map((h) => stripBom(h).trim().toLowerCase());
  if (!header.includes("name")) {
    return { rows: [], headerError: "缺少必填列: name（请使用最新模板）" };
  }
  const rows: ParsedRow[] = [];
  for (let i = 1; i < raw.length; i++) {
    const cells = raw[i];
    if (cells.every((c) => !c.trim())) continue; // 跳过空行
    const fields: Record<string, string> = {};
    header.forEach((key, idx) => { fields[key] = (cells[idx] ?? "").trim(); });
    const { data, error } = buildForm(fields);
    rows.push({ line: i + 1, data, error });
  }
  return { rows };
}

interface ProjectImportDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onImported: () => void;
}

export default function ProjectImportDialog({ open, onOpenChange, onImported }: ProjectImportDialogProps) {
  const [phase, setPhase] = useState<Phase>("select");
  const [fileName, setFileName] = useState("");
  const [rows, setRows] = useState<ParsedRow[]>([]);
  const [progress, setProgress] = useState({ done: 0, total: 0, ok: 0, fail: 0 });
  const [failures, setFailures] = useState<ImportFailure[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const validRows = useMemo(() => rows.filter((r) => r.data), [rows]);
  const invalidRows = useMemo(() => rows.filter((r) => !r.data), [rows]);

  const resetAll = () => {
    setPhase("select");
    setFileName("");
    setRows([]);
    setProgress({ done: 0, total: 0, ok: 0, fail: 0 });
    setFailures([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const handleOpenChange = (next: boolean) => {
    if (!next && phase === "importing") return; // 导入过程中禁止关闭
    if (!next) resetAll();
    onOpenChange(next);
  };

  const handleSelectFile = async (file: File | null) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".csv")) {
      toast.error("请选择 .csv 格式的文件");
      return;
    }
    try {
      const text = await readCsvText(file);
      const { rows: parsed, headerError } = buildRows(text);
      if (headerError) {
        toast.error(headerError);
        return;
      }
      if (parsed.length === 0) {
        toast.error("CSV 中没有可导入的数据行");
        return;
      }
      setFileName(file.name);
      setRows(parsed);
      setPhase("preview");
    } catch (e: any) {
      toast.error(`读取文件失败: ${e?.message || e}`);
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const handleImport = async () => {
    const targets = validRows;
    if (targets.length === 0) return;
    setPhase("importing");
    setProgress({ done: 0, total: targets.length, ok: 0, fail: 0 });
    setFailures([]);

    let nextIndex = 0;
    let okCount = 0;
    const failureList: ImportFailure[] = [];

    const worker = async () => {
      while (true) {
        const idx = nextIndex++;
        if (idx >= targets.length) return;
        const row = targets[idx];
        try {
          await api.createProject(row.data!);
          okCount++;
        } catch (e: any) {
          failureList.push({
            line: row.line,
            name: row.data!.name,
            error: e?.response?.data?.detail || e?.message || "创建失败",
          });
        }
        setProgress((p) => ({ ...p, done: p.done + 1, ok: okCount, fail: failureList.length }));
      }
    };

    await Promise.all(
      Array.from({ length: Math.min(IMPORT_CONCURRENCY, targets.length) }, () => worker())
    );

    setFailures([...failureList]);
    setPhase("done");
    if (okCount > 0) {
      onImported();
      toast.success(`导入完成：成功 ${okCount} 个，失败 ${failureList.length} 个`);
    } else {
      toast.error(`导入失败：${failureList.length} 个项目均未创建成功`);
    }
  };

  const pct = progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="!w-[min(92vw,800px)] !max-w-none max-h-[85vh] flex flex-col p-0 gap-0 cyber-dialog border border-border rounded-lg"
        onInteractOutside={(e) => { if (phase === "importing") e.preventDefault(); }}
        onEscapeKeyDown={(e) => { if (phase === "importing") e.preventDefault(); }}
      >
        {/* Terminal Header */}
        <div className="flex items-center gap-2 px-4 py-3 cyber-bg-elevated border-b border-border flex-shrink-0">
          <div className="flex items-center gap-1.5">
            <div className="w-3 h-3 rounded-full bg-red-500/80" />
            <div className="w-3 h-3 rounded-full bg-yellow-500/80" />
            <div className="w-3 h-3 rounded-full bg-green-500/80" />
          </div>
          <span className="ml-2 font-mono text-xs text-muted-foreground tracking-wider">
            import_projects@deepaudit
          </span>
        </div>

        <DialogHeader className="px-6 pt-4 flex-shrink-0">
          <DialogTitle className="font-mono text-lg uppercase tracking-wider flex items-center gap-2 text-foreground">
            <FileUp className="w-5 h-5 text-primary" />
            CSV 模板批量导入
          </DialogTitle>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto p-6 space-y-4">
          {phase === "select" && (
            <div className="space-y-4">
              <div className="font-mono text-sm text-muted-foreground space-y-1">
                <p>1. 下载 CSV 模板，按模板格式填写项目数据（name 为必填列）；</p>
                <p>2. 上传填写好的 .csv 文件，预览确认后批量创建项目。</p>
              </div>
              <div className="flex gap-3">
                <Button asChild variant="outline" className="font-mono">
                  <a href={TEMPLATE_URL} download="project_import_template.csv">
                    <Download className="w-4 h-4 mr-2" />
                    下载 CSV 模板
                  </a>
                </Button>
                <Button className="cyber-btn-primary font-mono" onClick={() => fileInputRef.current?.click()}>
                  <FileUp className="w-4 h-4 mr-2" />
                  选择 CSV 文件
                </Button>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".csv,text/csv"
                  className="hidden"
                  onChange={(e) => handleSelectFile(e.target.files?.[0] || null)}
                />
              </div>
              <div className="text-xs font-mono text-muted-foreground border border-border bg-muted/40 rounded p-3 leading-relaxed">
                字段: name（必填）· description · source_type（repository/zip）· repository_url ·
                repository_type（github/gitlab/gitea/other）· default_branch ·
                programming_languages（逗号分隔，留空默认 java,javascript,typescript）
              </div>
            </div>
          )}

          {phase === "preview" && (
            <div className="space-y-4">
              <div className="flex items-center gap-3 font-mono text-sm">
                <FileText className="w-4 h-4 text-primary" />
                <span className="text-foreground">{fileName}</span>
                <span className="text-muted-foreground">共 {rows.length} 行</span>
                <span className="text-emerald-500">有效 {validRows.length}</span>
                {invalidRows.length > 0 && (
                  <span className="text-destructive">无效 {invalidRows.length}</span>
                )}
              </div>

              <div className="border border-border rounded overflow-hidden">
                <table className="w-full text-xs font-mono">
                  <thead className="bg-muted text-muted-foreground">
                    <tr>
                      <th className="text-left px-3 py-2 font-normal">name</th>
                      <th className="text-left px-3 py-2 font-normal">repository_url</th>
                      <th className="text-left px-3 py-2 font-normal">类型</th>
                      <th className="text-left px-3 py-2 font-normal">分支</th>
                      <th className="text-left px-3 py-2 font-normal">技术栈</th>
                    </tr>
                  </thead>
                  <tbody>
                    {validRows.slice(0, PREVIEW_ROWS).map((r) => (
                      <tr key={r.line} className="border-t border-border">
                        <td className="px-3 py-2 text-foreground">{r.data!.name}</td>
                        <td className="px-3 py-2 text-muted-foreground max-w-[220px] truncate">
                          {r.data!.repository_url || "-"}
                        </td>
                        <td className="px-3 py-2 text-muted-foreground">{r.data!.repository_type}</td>
                        <td className="px-3 py-2 text-muted-foreground">{r.data!.default_branch}</td>
                        <td className="px-3 py-2 text-muted-foreground">
                          {r.data!.programming_languages.join(",")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {validRows.length > PREVIEW_ROWS && (
                  <div className="px-3 py-2 text-xs font-mono text-muted-foreground border-t border-border bg-muted/40">
                    ... 仅预览前 {PREVIEW_ROWS} 行，其余 {validRows.length - PREVIEW_ROWS} 行将在导入时一并处理
                  </div>
                )}
              </div>

              {invalidRows.length > 0 && (
                <div className="text-xs font-mono border border-destructive/40 bg-destructive/10 rounded p-3 space-y-1 max-h-32 overflow-y-auto">
                  <p className="text-destructive font-bold">
                    以下行将被跳过（{invalidRows.length} 行）:
                  </p>
                  {invalidRows.slice(0, ERROR_DISPLAY_LIMIT).map((r) => (
                    <p key={r.line} className="text-muted-foreground">第 {r.line} 行: {r.error}</p>
                  ))}
                  {invalidRows.length > ERROR_DISPLAY_LIMIT && (
                    <p className="text-muted-foreground">
                      ... 其余 {invalidRows.length - ERROR_DISPLAY_LIMIT} 行省略
                    </p>
                  )}
                </div>
              )}

              <div className="flex gap-3">
                <Button
                  variant="outline"
                  className="font-mono"
                  onClick={() => { setPhase("select"); setRows([]); setFileName(""); }}
                >
                  <RefreshCw className="w-4 h-4 mr-2" />
                  重新选择
                </Button>
                <Button
                  className="cyber-btn-primary font-mono"
                  disabled={validRows.length === 0}
                  onClick={handleImport}
                >
                  <FileUp className="w-4 h-4 mr-2" />
                  开始导入（{validRows.length} 个项目）
                </Button>
              </div>
            </div>
          )}

          {phase === "importing" && (
            <div className="space-y-5">
              <div className="flex items-center gap-2 font-mono text-sm text-foreground">
                <Terminal className="w-4 h-4 text-primary animate-pulse" />
                正在导入 {fileName} ...
              </div>
              <Progress value={pct} />
              <div className="font-mono text-sm text-muted-foreground">
                已处理 {progress.done} / {progress.total}（{pct}%） · 成功{" "}
                <span className="text-emerald-500">{progress.ok}</span> · 失败{" "}
                <span className="text-destructive">{progress.fail}</span>
              </div>
              <p className="text-xs font-mono text-muted-foreground">导入过程中请勿关闭窗口</p>
            </div>
          )}

          {phase === "done" && (
            <div className="space-y-4">
              <div className="flex items-center gap-3 font-mono text-sm">
                {progress.fail === 0 ? (
                  <CheckCircle className="w-5 h-5 text-emerald-500" />
                ) : (
                  <AlertCircle className="w-5 h-5 text-destructive" />
                )}
                <span className="text-foreground">
                  导入完成：成功 {progress.ok} 个，失败 {progress.fail} 个（共 {progress.total} 个）
                </span>
              </div>

              {failures.length > 0 && (
                <div className="border border-destructive/40 rounded max-h-56 overflow-y-auto">
                  {failures.slice(0, FAILURE_DISPLAY_LIMIT).map((f, i) => (
                    <div
                      key={`${f.line}-${i}`}
                      className="px-3 py-2 border-b border-border last:border-b-0 text-xs font-mono"
                    >
                      <span className="text-muted-foreground">第 {f.line} 行</span>
                      <span className="text-foreground mx-2">{f.name}</span>
                      <span className="text-destructive">{f.error}</span>
                    </div>
                  ))}
                  {failures.length > FAILURE_DISPLAY_LIMIT && (
                    <div className="px-3 py-2 text-xs font-mono text-muted-foreground">
                      ... 其余 {failures.length - FAILURE_DISPLAY_LIMIT} 条省略
                    </div>
                  )}
                </div>
              )}

              <div className="flex gap-3">
                <Button variant="outline" className="font-mono" onClick={resetAll}>
                  <RefreshCw className="w-4 h-4 mr-2" />
                  再导入一批
                </Button>
                <Button className="cyber-btn-primary font-mono" onClick={() => handleOpenChange(false)}>
                  完成
                </Button>
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
