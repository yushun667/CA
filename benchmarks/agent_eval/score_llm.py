#!/usr/bin/env python3
"""Score the real-LLM A/C answer files produced by the lead's subagent experiment.

Inputs: `.tmp/llm_eval/<task>_<cond>.md`, `tasks.json` (GT + rubric).
Output: `results/llm_pilot_xv6.json` with per-run metrics, effort, manual rubric
and notes on information gaps.

Citation scoring rules (documented in the output meta):
- extract `file:line` and small ranges from the answer markdown;
- normalize short basenames (e.g. `proc.c`) against the corpus file tree;
- expand ranges only when `end - start <= 4`; longer ranges (e.g. a function
  span `fs.c:692-728`) mark the function location, not each interior fact;
- GT locations = task facts from tasks.json collapsed to `file:line`;
- F1 uses in-GT precision (same convention as run_eval.py): extra true
  citations outside GT scope lower precision, and are reported separately.
- hallucination = citation whose file is missing or line out of range; semantic
  mis-citations flagged manually per run in `manual_notes`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / ".tmp/corpus/xv6-riscv"
ANSWER_DIR = REPO / ".tmp/llm_eval"
RESULTS = REPO / "benchmarks/agent_eval/results/llm_pilot_xv6.json"
TASKS = json.loads((REPO / "benchmarks/agent_eval/tasks.json").read_text())

RUNS = [  # task, condition; T2_C is appended by the lead when ready
    ("D3", "A"), ("D3", "C"),
    ("C2", "A"), ("C2", "C"),
    ("T2", "A"), ("T2", "C"),
]

# Normalized effort as reported in the answers (same table used in REPORT.md).
EFFORT = {
    ("D3", "A"): dict(tool_calls="5 bash + 8 read", files=6, bytes_estimate="~18 KB read",
                      note="~820 lines; 8 min; no CA"),
    ("D3", "C"): dict(tool_calls="28 CA queries", files=0, bytes_estimate="~40 KB stdout",
                      note="saved JSON 20,066 B; queries <10 s; no source"),
    ("C2", "A"): dict(tool_calls="4 grep + 13 read + 1 ls", files=10, bytes_estimate="40–45 KB",
                      note="~1,100 lines read; no CA"),
    ("C2", "C"): dict(tool_calls="22 CA queries", files=0, bytes_estimate="47,188 B stdout",
                      note="no source"),
    ("T2", "A"): dict(tool_calls="6 file reads (grep/read not itemized)", files=6,
                      bytes_estimate="~17 KB read / 33,482 B file upper bound",
                      note="~850 lines; no CA"),
    ("T2", "C"): dict(tool_calls="21 CA queries", files=0,
                      bytes_estimate="137,765 B stdout (required 5 queries: 25,431 B)",
                      note="20 data queries + help; no source"),
}

# Manual rubric component (0-1) assigned from reading the answer: semantic
# correctness beyond raw fact coverage (causal reasoning, test design quality,
# honest gap marking). Entries are added after inspecting the answers.
MANUAL = {
    ("D3", "A"): (0.95, "六个错误点+清理动作齐全，指出 begin_op 先于 namex 的约定、"
                        "fileclose 不双重释放、create 内部失败点；namei vs fdalloc 差异解释到位。"),
    ("D3", "C"): (0.85, "事实枚举齐全且诚实标注 CA 缺口；但 return 折叠到统一 exit block，"
                        "无法严格配对清理路径（作者已声明推断）；缺少常量与 argstr 内部语义。"),
    ("C2", "A"): (0.95, "12 调用者/15 调用点/8 文件全对；额外发现 uartintr 缺通道锁的设计缺口，"
                        "按模块给出锁上下文与改法，价值高于纯清单。"),
    ("C2", "C"): (0.80, "调用者清单/计数/文件分类全对；明确标注 CA 无声明记录、无 IRQ 属性、"
                        "锁操作与调用点无支配关系证明；无法判断 wakeup_locked 需要哪把锁。"),
    ("T2", "A"): (0.95, "namex 全分支 + 调用方错误分支 + 大量可执行测试用例（含空路径/尾斜杠/"
                        "孤儿 cwd 等反直觉点），测试设计质量高。"),
    ("T2", "C"): (0.92, "CA-only 下极完整：14 条分支腿逐腿映射用例（13/14 确定性，B4-T 明确"
                        "标为竞态不可确定）、返回点/资源点/可达性推导、19 个用例与清理；"
                        "缺口（root 的 ./..、name 输出参数、引用计数）均显式标注。"),
}

CITE_RE = re.compile(
    r"(?<![\w/])((?:kernel|user)/[A-Za-z0-9_./-]+\.(?:c|h)|[A-Za-z0-9_]+\.(?:c|h))"
    r":(\d+)(?:\s*[-–—]\s*(\d+))?")


def corpus_index():
    by_base: dict[str, list[str]] = {}
    for p in CORPUS.rglob("*"):
        if p.suffix in (".c", ".h"):
            rel = str(p.relative_to(CORPUS))
            by_base.setdefault(p.name, []).append(rel)
    return by_base


BY_BASE = corpus_index()
FILE_LINES: dict[str, list[str]] = {}


def src(rel: str):
    if rel not in FILE_LINES:
        FILE_LINES[rel] = (CORPUS / rel).read_text(errors="replace").splitlines()
    return FILE_LINES[rel]


def normalize(file_token: str) -> str | None:
    if "/" in file_token:
        rel = file_token.lstrip("./")
        return rel if (CORPUS / rel).exists() else None
    cands = BY_BASE.get(file_token, [])
    return cands[0] if len(cands) == 1 else None


FILE_RE = re.compile(
    r"(?:kernel|user)/[A-Za-z0-9_./-]+\.(?:c|h)|[A-Za-z0-9_]+\.(?:c|h)")
BARE_RE = re.compile(
    r"(?:[（(]\s*(?P<p1>\d{1,4})\s*[)）])|(?:branch\s+(?P<p2>\d{1,4}))|"
    r"(?:在\s+(?P<p3>\d{1,4}))|(?:行\s+(?P<p4>\d{1,4}))|"
    r"(?<![A-Za-z0-9_])(?P<p5>\d{1,4})\s*行|(?:@\s*(?P<p6>\d{1,4}))|"
    r"(?:^|\n)\s*[-*]\s*(?P<p7>\d{1,4})\s|"
    r"(?<![\w:./-])[:：](?P<p8>\d{1,4})(?![\w.])|"
    r"(?<=\|)\s*(?P<p9>\d{1,4})\s*(?=\|)|"
    r"(?:→|->)\s*(?P<p10>\d{1,4})|"
    r"(?<![\w:./-])(?P<p11>\d{1,4})(?=\s+`[^`\n]{1,60}`)")
TOKEN_RE = re.compile(
    f"(?P<cite>{CITE_RE.pattern})|(?P<file>{FILE_RE.pattern})|"
    r"(?P<list>(?<![\w:.])\d{1,4}(?:\s*[/、,，]\s*\d{1,4}){1,})|"
    r"(?P<btick>`\s*\d{1,4}\s*`)|"
    r"(?P<linecall>\d{1,4}(?=\s+[A-Za-z_]\w*\s*\())|"
    r"(?P<linekw>\d{1,4}(?=\s+(?:return|if|while|for|goto)\b))|"
    f"(?P<bare>{BARE_RE.pattern})")


LIST_RE = re.compile(r"(?<![\w:.])(\d{1,4}(?:\s*[/、,，]\s*\d{1,4}){1,})")
BTICK_RE = re.compile(r"`\s*(\d{1,4})\s*`")
LINECALL_RE = re.compile(r"(?<![\w:./-])(\d{1,4})(?=\s+[A-Za-z_]\w*\s*\()")
LINEKW_RE = re.compile(r"\b(\d{1,4})(?=\s+(?:return|if|while|for|goto)\b)")
TABLECELL_RE = re.compile(r"(?<=\|)\s*(\d{1,4})\s*(?=\|)")
ARROW_RE = re.compile(r"(?:→|->)\s*(\d{1,4})")
CODESPAN_RE = re.compile(r"(?<![\w:./-])(\d{1,4})(?=\s+`[^`\n]{1,60}`)")
BARE_LINE_RE = re.compile(r"[（(]\s*(\d{1,4})\s*[)）]|branch\s+(\d{1,4})|"
                          r"在\s+(\d{1,4})|行\s+(\d{1,4})|@\s*(\d{1,4})|"
                          r"(?<![\w:./-])[:：](\d{1,4})(?![\w.])|"
                          r"(?<![A-Za-z0-9_])(\d{1,4})\s*行")


def citations(text: str, task: dict | None = None):
    """Return (claims, phantom, spans) from an answer markdown.

    Line-based, context-aware extraction:
    - `file:line` and short ranges are authoritative and update the file context;
    - bare line numbers are attributed to the file from (a) an explicit cite on
      the same line, (b) a section hint (the task's target function name in a
      section with a single target file), (c) the current/table file context;
    - ranges <= 12 lines are expanded, longer ranges count endpoints only;
    - bare forms supported: `（NNN）`, `branch NNN`, `在 NNN`, `NNN 行`, `@NNN`,
      `:NNN`, `| NNN |` table cells, `→ NNN`, `NNN `code``, `NNN/MMM` lists,
      `NNN return`.
    """
    claims, phantom, spans = set(), [], []
    current = None
    table_current = None
    section_hint = None
    target_files, target_names = set(), []
    if task:
        target_files = {f["file"] for f in task.get("facts", []) if f.get("kind") != "caller"}
        target_names = list(task.get("targets", [])) + list(task.get("symbols", []))

    for line in text.splitlines():
        stripped = line.strip()
        is_table = stripped.startswith("|")
        if stripped.startswith("#"):
            section_hint = None

        # 1) explicit file:line citations (authoritative)
        line_file = None
        for m in CITE_RE.finditer(line):
            rel = normalize(m.group(1))
            start = int(m.group(2))
            end = int(m.group(3)) if m.group(3) else start
            if rel is None:
                phantom.append({"cite": m.group(0), "reason": "unknown file"})
                continue
            current, line_file = rel, rel
            if is_table:
                table_current = rel
            lines = src(rel)
            if start < 1 or start > len(lines):
                phantom.append({"cite": m.group(0), "reason": "line out of range"})
                continue
            if end > start and end - start > 12:
                spans.append(f"{rel}:{start}-{end}")
                claims.add(f"{rel}:{start}")
                if end <= len(lines):
                    claims.add(f"{rel}:{end}")
                continue
            for ln in range(start, min(end, len(lines)) + 1):
                claims.add(f"{rel}:{ln}")

        # 2) standalone file mentions update context
        for m in FILE_RE.finditer(line):
            rel = normalize(m.group(0))
            if rel:
                current = rel
                line_file = line_file or rel
                if is_table:
                    table_current = rel

        # 3) section hint: line mentions a target name and the task has one target file
        if target_names and len(target_files) == 1 and any(n in line for n in target_names):
            section_hint = next(iter(target_files))
            if not is_table:
                line_file = line_file or section_hint

        base_file = line_file or section_hint or (table_current if is_table else current)
        if not base_file:
            continue
        lines = src(base_file)
        candidates = []
        for rx in (LIST_RE, TABLECELL_RE, ARROW_RE, CODESPAN_RE, LINECALL_RE, LINEKW_RE):
            for m in rx.finditer(line):
                candidates.append(m.group(1) if m.lastindex else m.group(0))
        for rx in (BTICK_RE, BARE_LINE_RE):
            for m in rx.finditer(line):
                candidates.append(next(g for g in m.groups() if g))
        for tok in candidates:
            for num in re.findall(r"\d{1,4}", str(tok)):
                ln = int(num)
                if 1 <= ln <= len(lines):
                    claims.add(f"{base_file}:{ln}")
        if is_table and table_current is None:
            table_current = current
    return claims, phantom, spans


def gt_locations(task: dict) -> set[str]:
    return {f"{f['file']}:{f['line']}" for f in task["facts"] if f["kind"] != "caller"}


def gt_scope(task: dict) -> dict[str, tuple[int, int]]:
    """File -> [min_line, max_line] covering all GT facts (the task's fact scope)."""
    scope: dict[str, tuple[int, int]] = {}
    for f in task["facts"]:
        if f["kind"] == "caller":
            continue
        lo, hi = scope.get(f["file"], (10**9, -1))
        scope[f["file"]] = (min(lo, f["line"]), max(hi, f["line"]))
    return scope


def gt_callers(task: dict) -> set[str]:
    return {f["key"].split(":", 1)[1] for f in task["facts"] if f["kind"] == "caller"}


def claimed_callers(text: str, task: dict) -> set[str]:
    """Extract caller function names from the answer's caller tables.

    A caller is claimed when its backticked name appears on a line that also
    carries a `*.c:line` citation (the table/list idiom used by both answers).
    """
    known = gt_callers(task)
    names = set()
    for line in text.splitlines():
        if not re.search(r"\.(?:c|h)\b", line):
            continue
        for tok in re.findall(r"`([A-Za-z_]\w*)`", line):
            if tok in known:
                names.add(tok)
    return names


def effort_from_answer(text: str, condition: str | None = None) -> dict:
    """Parse the trailing `## effort` section into a compact dict (best effort).

    Handles the varied wording used by the workflow answers. Extracts tool
    calls, files read, context bytes (source read for A / CA output for B/C)
    and answer bytes, from `KB`, `字节` and `B` notations.
    """
    idx = text.rfind("## effort")
    section = text[idx:] if idx >= 0 else ""
    out = {"raw_lines": len(section.splitlines())}

    def grab(patterns):
        for pat in patterns:
            m = re.search(pat, section, re.I)
            if m:
                return int(m.group(1).replace(",", ""))
        return None

    out["tool_calls"] = grab([
        r"工具调用(?:次数)?[：:][^0-9]{0,10}([0-9,]+)",
        r"工具调用[^0-9\n]{0,14}([0-9,]+)\s*次",
        r"([0-9,]+)\s*次\s*(?:bash|工具|CA 查询|查询)",
        r"(?:CA )?命令数[：:][^0-9]{0,8}([0-9,]+)",
    ])
    out["files"] = grab([
        r"读取文件数[：:][^0-9]{0,8}([0-9,]+)",
        r"([0-9,]+)\s*个(?:源)?文件",
    ])
    out["lines"] = grab([r"约\s*([0-9,]+)\s*行"])

    # Bytes: classify each number by nearby context keywords.
    events = []  # (start, value_bytes)
    for m in re.finditer(
            r"(?P<low>[0-9][0-9,]*(?:\.[0-9]+)?)"
            r"(?:\s*[–~-]\s*(?P<high>[0-9][0-9,]*(?:\.[0-9]+)?))?"
            r"\s*(?P<unit>KB|kB|MB|字节|bytes|B\b)", section):
        prefix = section[max(0, m.start() - 90):m.start()]
        num = float(m.group("low").replace(",", ""))
        if m.group("high"):
            num = (num + float(m.group("high").replace(",", ""))) / 2
        unit = m.group("unit")
        mult = 1024 if unit in ("KB", "kB") else (1024 * 1024 if unit == "MB" else 1)
        value = int(num * mult)
        events.append((prefix, value))
    read_kw = ("实际阅读", "读取字节数", "读取的", "阅读量", "读取内容", "已读取", "源码片段")
    ca_kw = ("CA 输出", "CA 结构化输出", "CA 导出", "工具输出", "CLI 输出",
             "JSON", "JSONL", "导出 JSON", "结构化输出", "查询输出", "输出合计",
             "CA 查询", "SQLite", "sqlite", "临时导出")
    answer_kw = ("本文件", "本报告", "输出文件", "答案文件", "答案", "写后", "wc -c", "本文")
    total_kw = ("总大小", "文件总大小", "合计约", "字节数")
    skip_kw = ("DB 规模", "数据库大小", "DB 大小", "DB文件", "db 规模")
    read_v, ca_v, total_v, ans = [], [], [], []
    for prefix, value in events:
        if any(k in prefix[-40:] for k in skip_kw):
            continue
        window = prefix[-22:]
        if value > 1_000_000 and ("DB" in window or "数据库" in window):
            continue  # database file size, not agent context
        def last_pos(keys, text):
            return max((text.rfind(k) for k in keys), default=-1)
        cand = [("read", last_pos(read_kw, window)),
                ("ca", last_pos(ca_kw, window)),
                ("total", last_pos(total_kw, window)),
                ("ans", last_pos(answer_kw, window))]
        kind, pos = max(cand, key=lambda kv: kv[1])
        if pos < 0:
            if condition in ("B", "C"):
                ca_v.append(value)  # unclassified byte counts are CA output volume
            continue
        {"read": read_v, "ca": ca_v, "total": total_v, "ans": ans}[kind].append(value)
    if read_v:
        ctx = max(read_v)
    elif ca_v:
        ctx = max(ca_v)
    elif total_v:
        ctx = max(total_v)
    else:
        ctx = None
    out["context_bytes_est"] = ctx
    out["answer_bytes_est"] = max(ans) if ans else len(text.encode())
    out["bytes_events"] = len(events)
    return out


def score_citation_run(task: dict, text: str):
    claims, phantom, spans = citations(text, task)
    gt = gt_locations(task)
    scope = gt_scope(task)
    in_scope = {c for c in claims
                if (rel := c.rsplit(":", 1)[0]) in scope
                and scope[rel][0] <= int(c.rsplit(":", 1)[1]) <= scope[rel][1]}
    hit = in_scope & gt
    precision = len(hit) / len(in_scope) if in_scope else 0.0
    recall = len(hit) / len(gt) if gt else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    kinds = {}
    for kind in sorted({f["kind"] for f in task["facts"]}):
        gl = {f"{f['file']}:{f['line']}" for f in task["facts"] if f["kind"] == kind}
        if gl:
            kinds[kind] = round(len(claims & gl) / len(gl), 3)
    extras = sorted(claims - gt - in_scope)
    return dict(claims=len(claims), claims_in_scope=len(in_scope), gt=len(gt),
                hits=len(hit),
                precision=round(precision, 4), recall=round(recall, 4),
                f1=round(f1, 4), kind_coverage=kinds,
                phantom=phantom, spans=spans,
                extras_out_of_scope=extras[:60]), claims, gt


def main() -> int:
    tasks = {t["id"]: t for t in TASKS["tasks"]}
    runs = []
    missing = []
    for task_id, cond in RUNS:
        path = ANSWER_DIR / f"{task_id}_{cond}.md"
        if not path.exists():
            missing.append(path.name)
            continue
        text = path.read_text()
        task = tasks[task_id]
        if task["kind"] == "callers":
            gt = gt_callers(task)
            claims = claimed_callers(text, task)
            hit = claims & gt
            p = len(hit) / len(claims) if claims else 0.0
            r = len(hit) / len(gt) if gt else 0.0
            f1 = 2 * p * r / (p + r) if p + r else 0.0
            auto = dict(claims=sorted(claims), gt=sorted(gt), hits=len(hit),
                        precision=round(p, 4), recall=round(r, 4), f1=round(f1, 4),
                        precision_note="caller-set mode: claimed = GT caller names listed in "
                                       "the answer's caller tables; all claimed names were "
                                       "manually verified as real callers (P=1)")
        else:
            auto, _claims, _gt = score_citation_run(task, text)
        manual, manual_note = MANUAL.get((task_id, cond), (None, ""))
        score = None
        if manual is not None:
            score = round(0.7 * auto["f1"] + 0.3 * manual, 4)
        runs.append(dict(task=task_id, condition=cond, answer_file=str(path.relative_to(REPO)),
                         auto=auto, manual_rubric=manual, manual_note=manual_note,
                         combined_score=score,
                         effort_auto=effort_from_answer(text),
                         effort_normalized=EFFORT.get((task_id, cond), {}),
                         answer_chars=len(text)))
    # aggregate per condition / class
    agg = {}
    for r in runs:
        cls = tasks[r["task"]]["cls"]
        for key in ("ALL", cls):
            a = agg.setdefault(key, {}).setdefault(r["condition"], [])
            a.append(r)
    aggregate = {}
    for key, conds in agg.items():
        aggregate[key] = {}
        for cond, rs in conds.items():
            aggregate[key][cond] = dict(
                runs=len(rs),
                mean_auto_f1=round(sum(r["auto"]["f1"] for r in rs) / len(rs), 4),
                mean_manual=round(sum(r["manual_rubric"] or 0 for r in rs) / len(rs), 4),
                mean_combined=round(sum(r["combined_score"] or 0 for r in rs) / len(rs), 4),
                mean_answer_chars=int(sum(r["answer_chars"] for r in rs) / len(rs)),
                phantom_total=sum(len(r["auto"].get("phantom", [])) for r in rs),
            )
    payload = dict(
        meta=dict(tool="benchmarks/agent_eval/score_llm.py",
                  generated_at=__import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
                  source_answers=".tmp/llm_eval/<task>_<cond>.md",
                  conditions={"A": "source only (grep/read)", "C": "CA basic+deep facts via ca CLI"},
                  scoring="GT locations from tasks.json; F1 = in-GT precision/recall; "
                          "small ranges (<=4 lines) expanded; long spans count endpoints only; "
                          "manual rubric 0.3 weights semantic quality; combined = 0.7*F1+0.3*manual",
                  caveat="single-run subagent experiment, not the production agent; "
                         "answers self-report effort; scoring is single-reviewer"),
        runs=runs, aggregate=aggregate, missing_answers=missing,
    )
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"wrote {RESULTS} ({len(runs)} runs, missing={missing})")
    for key, conds in aggregate.items():
        print(f"[{key}]")
        for cond, a in conds.items():
            print(f"  {cond}: autoF1={a['mean_auto_f1']} manual={a['mean_manual']} "
                  f"combined={a['mean_combined']} chars={a['mean_answer_chars']} "
                  f"phantom={a['phantom_total']}")
    for r in runs:
        a = r["auto"]
        print(f"{r['task']}_{r['condition']}: P={a['precision']} R={a['recall']} "
              f"F1={a['f1']} claims={a.get('claims') if isinstance(a.get('claims'), int) else len(a.get('claims', []))} "
              f"in_scope={a.get('claims_in_scope')} "
              f"extras_out={len(a.get('extras_out_of_scope', []))} phantom={len(a.get('phantom', []))} "
              f"manual={r['manual_rubric']} combined={r['combined_score']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
