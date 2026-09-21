"""
Markdown 审计报告生成服务

从 agent_tasks.py 的报告生成逻辑抽取，供单任务报告导出与
批量报告下载共用，保证两处报告格式完全一致。
"""

from datetime import datetime
from typing import List, Sequence

from app.models.agent_task import AgentFinding
from app.models.project import Project


def _normalize_severity(sev) -> str:
    return str(sev).lower().strip() if sev else ""


# 按扩展名推断代码块语言（与 agent_tasks.py 保持一致）
_EXT_LANG_MAP = {
    # Python
    'py': 'python', 'pyw': 'python', 'pyi': 'python',
    # JavaScript/TypeScript
    'js': 'javascript', 'mjs': 'javascript', 'cjs': 'javascript',
    'ts': 'typescript', 'mts': 'typescript',
    'jsx': 'jsx', 'tsx': 'tsx',
    # Web
    'html': 'html', 'htm': 'html',
    'css': 'css', 'scss': 'scss', 'sass': 'sass', 'less': 'less',
    'vue': 'vue', 'svelte': 'svelte',
    # Backend
    'java': 'java', 'kt': 'kotlin', 'kts': 'kotlin',
    'go': 'go', 'rs': 'rust',
    'rb': 'ruby', 'erb': 'erb',
    'php': 'php', 'phtml': 'php',
    # C-family
    'c': 'c', 'h': 'c',
    'cpp': 'cpp', 'cc': 'cpp', 'cxx': 'cpp', 'hpp': 'cpp',
    'cs': 'csharp',
    # Shell/Script
    'sh': 'bash', 'bash': 'bash', 'zsh': 'zsh',
    'ps1': 'powershell', 'psm1': 'powershell',
    # Config
    'json': 'json', 'yaml': 'yaml', 'yml': 'yaml',
    'toml': 'toml', 'ini': 'ini', 'cfg': 'ini',
    'xml': 'xml', 'xhtml': 'xml',
    # Database
    'sql': 'sql',
    # Other
    'md': 'markdown', 'markdown': 'markdown',
    'sol': 'solidity',
    'swift': 'swift',
    'r': 'r',
    'lua': 'lua',
    'pl': 'perl', 'pm': 'perl',
    'ex': 'elixir', 'exs': 'elixir',
    'erl': 'erlang',
    'hs': 'haskell',
    'scala': 'scala', 'sc': 'scala',
    'clj': 'clojure', 'cljs': 'clojure',
    'dart': 'dart',
    'groovy': 'groovy', 'gradle': 'groovy',
}


def build_markdown_report(task, project: Project, findings: Sequence[AgentFinding]) -> str:
    """
    构建 Markdown 格式安全审计报告

    参数:
        task: AgentTask 审计任务
        project: 所属项目
        findings: 按 severity 排序后的漏洞发现列表
    返回:
        Markdown 报告文本
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Calculate statistics
    total = len(findings)
    critical = sum(1 for f in findings if _normalize_severity(f.severity) == 'critical')
    high = sum(1 for f in findings if _normalize_severity(f.severity) == 'high')
    medium = sum(1 for f in findings if _normalize_severity(f.severity) == 'medium')
    low = sum(1 for f in findings if _normalize_severity(f.severity) == 'low')
    verified = sum(1 for f in findings if f.is_verified)
    with_poc = sum(1 for f in findings if f.has_poc)

    # Calculate duration
    duration_str = "N/A"
    if task.completed_at and task.started_at:
        duration = (task.completed_at - task.started_at).total_seconds()
        if duration >= 3600:
            duration_str = f"{duration / 3600:.1f} 小时"
        elif duration >= 60:
            duration_str = f"{duration / 60:.1f} 分钟"
        else:
            duration_str = f"{int(duration)} 秒"

    md_lines: List[str] = []

    # Header
    md_lines.append("# DeepAudit 安全审计报告")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("")

    # Report Info
    md_lines.append("## 报告信息")
    md_lines.append("")
    md_lines.append("| 属性 | 内容 |")
    md_lines.append("|----------|-------|")
    md_lines.append(f"| **项目名称** | {project.name} |")
    md_lines.append(f"| **任务 ID** | `{task.id[:8]}...` |")
    md_lines.append(f"| **生成时间** | {timestamp} |")
    md_lines.append(f"| **任务状态** | {task.status.upper()} |")
    md_lines.append(f"| **耗时** | {duration_str} |")
    md_lines.append("")

    # Executive Summary
    md_lines.append("## 执行摘要")
    md_lines.append("")

    score = task.security_score
    if score is not None:
        if score >= 80:
            score_assessment = "良好 - 建议进行少量优化"
            score_icon = "通过"
        elif score >= 60:
            score_assessment = "中等 - 存在若干问题需要关注"
            score_icon = "警告"
        else:
            score_assessment = "严重 - 需要立即进行修复"
            score_icon = "未通过"
        md_lines.append(f"**安全评分: {int(score)}/100** [{score_icon}]")
        md_lines.append(f"*{score_assessment}*")
    else:
        md_lines.append("**安全评分:** 未计算")
    md_lines.append("")

    # Findings Summary
    md_lines.append("### 漏洞发现概览")
    md_lines.append("")
    md_lines.append("| 严重程度 | 数量 | 已验证 |")
    md_lines.append("|----------|-------|----------|")
    if critical > 0:
        md_lines.append(f"| **严重 (CRITICAL)** | {critical} | {sum(1 for f in findings if _normalize_severity(f.severity) == 'critical' and f.is_verified)} |")
    if high > 0:
        md_lines.append(f"| **高危 (HIGH)** | {high} | {sum(1 for f in findings if _normalize_severity(f.severity) == 'high' and f.is_verified)} |")
    if medium > 0:
        md_lines.append(f"| **中危 (MEDIUM)** | {medium} | {sum(1 for f in findings if _normalize_severity(f.severity) == 'medium' and f.is_verified)} |")
    if low > 0:
        md_lines.append(f"| **低危 (LOW)** | {low} | {sum(1 for f in findings if _normalize_severity(f.severity) == 'low' and f.is_verified)} |")
    md_lines.append(f"| **总计** | {total} | {verified} |")
    md_lines.append("")

    # Audit Metrics
    md_lines.append("### 审计指标")
    md_lines.append("")
    md_lines.append(f"- **分析文件数:** {task.analyzed_files} / {task.total_files}")
    md_lines.append(f"- **Agent 迭代次数:** {task.total_iterations}")
    md_lines.append(f"- **工具调用次数:** {task.tool_calls_count}")
    md_lines.append(f"- **Token 消耗:** {task.tokens_used:,}")
    if with_poc > 0:
        md_lines.append(f"- **生成的 PoC:** {with_poc}")
    md_lines.append("")

    # Detailed Findings
    if not findings:
        md_lines.append("## 漏洞详情")
        md_lines.append("")
        md_lines.append("*本次审计未发现安全漏洞。*")
        md_lines.append("")
    else:
        # Group findings by severity
        severity_map = {
            'critical': '严重 (Critical)',
            'high': '高危 (High)',
            'medium': '中危 (Medium)',
            'low': '低危 (Low)'
        }

        for severity_level, severity_name in severity_map.items():
            severity_findings = [f for f in findings if _normalize_severity(f.severity) == severity_level]
            if not severity_findings:
                continue

            md_lines.append(f"## {severity_name} 漏洞")
            md_lines.append("")

            for i, f in enumerate(severity_findings, 1):
                verified_badge = "[已验证]" if f.is_verified else "[未验证]"
                poc_badge = " [含 PoC]" if f.has_poc else ""

                md_lines.append(f"### {severity_level.upper()}-{i}: {f.title}")
                md_lines.append("")
                md_lines.append(f"**{verified_badge}**{poc_badge} | 类型: `{f.vulnerability_type}`")
                md_lines.append("")

                if f.file_path:
                    location = f"`{f.file_path}"
                    if f.line_start:
                        location += f":{f.line_start}"
                        if f.line_end and f.line_end != f.line_start:
                            location += f"-{f.line_end}"
                    location += "`"
                    md_lines.append(f"**位置:** {location}")
                    md_lines.append("")

                if f.ai_confidence:
                    md_lines.append(f"**AI 置信度:** {int(f.ai_confidence * 100)}%")
                    md_lines.append("")

                if f.description:
                    md_lines.append("**漏洞描述:**")
                    md_lines.append("")
                    md_lines.append(f.description)
                    md_lines.append("")

                lang = "text"
                if f.code_snippet:
                    if f.file_path:
                        ext = f.file_path.split('.')[-1].lower()
                        lang = _EXT_LANG_MAP.get(ext, 'text')
                    md_lines.append("**漏洞代码:**")
                    md_lines.append("")
                    md_lines.append(f"```{lang}")
                    md_lines.append(f.code_snippet.strip())
                    md_lines.append("```")
                    md_lines.append("")

                if f.suggestion:
                    md_lines.append("**修复建议:**")
                    md_lines.append("")
                    md_lines.append(f.suggestion)
                    md_lines.append("")

                if f.fix_code:
                    md_lines.append("**参考修复代码:**")
                    md_lines.append("")
                    md_lines.append(f"```{lang if f.file_path else 'text'}")
                    md_lines.append(f.fix_code.strip())
                    md_lines.append("```")
                    md_lines.append("")

                # PoC 详情
                if f.has_poc:
                    md_lines.append("**概念验证 (PoC):**")
                    md_lines.append("")

                    if f.poc_description:
                        md_lines.append(f"*{f.poc_description}*")
                        md_lines.append("")

                    if f.poc_steps:
                        md_lines.append("**复现步骤:**")
                        md_lines.append("")
                        for step_idx, step in enumerate(f.poc_steps, 1):
                            md_lines.append(f"{step_idx}. {step}")
                        md_lines.append("")

                    if f.poc_code:
                        md_lines.append("**PoC 代码:**")
                        md_lines.append("")
                        md_lines.append("```")
                        md_lines.append(f.poc_code.strip())
                        md_lines.append("```")
                        md_lines.append("")

                md_lines.append("---")
                md_lines.append("")

    # Remediation Priority
    if critical > 0 or high > 0:
        md_lines.append("## 修复优先级建议")
        md_lines.append("")
        md_lines.append("基于已发现的漏洞，我们建议按以下优先级进行修复：")
        md_lines.append("")
        priority_idx = 1
        if critical > 0:
            md_lines.append(f"{priority_idx}. **立即修复:** 处理 {critical} 个严重漏洞 - 可能造成严重影响")
            priority_idx += 1
        if high > 0:
            md_lines.append(f"{priority_idx}. **高优先级:** 在 1 周内修复 {high} 个高危漏洞")
            priority_idx += 1
        if medium > 0:
            md_lines.append(f"{priority_idx}. **中优先级:** 在 2-4 周内修复 {medium} 个中危漏洞")
            priority_idx += 1
        if low > 0:
            md_lines.append(f"{priority_idx}. **低优先级:** 在日常维护中处理 {low} 个低危漏洞")
            md_lines.append("")
        md_lines.append("")

    # Footer
    md_lines.append("---")
    md_lines.append("")
    md_lines.append("*本报告由 DeepAudit - AI 驱动的安全分析系统生成*")
    md_lines.append("")

    return "\n".join(md_lines)
