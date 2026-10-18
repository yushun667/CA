#!/usr/bin/env python3
"""Agent-utility pilot: source-only reading (A) vs CA basic facts (B) vs CA deep facts (C).

Controlled, deterministic three-condition harness. It does not call an LLM: each
condition is a fixed retrieval policy plus a condition-neutral extractor that turns
retrieved evidence into fact claims. Metrics: fact-level precision/recall/F1 against
a source-validated ground truth, plus read volume (bytes), tool calls and latency.

Usage:
  PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py --build-tasks
  PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py --out benchmarks/agent_eval/results/pilot_xv6.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO / ".tmp/lead/final_xv6.db"
DEFAULT_CORPUS = REPO / ".tmp/corpus/xv6-riscv"
TASKS_PATH = REPO / "benchmarks/agent_eval/tasks.json"
RESULTS_DIR = REPO / "benchmarks/agent_eval/results"
PYTHON = REPO / ".venv/bin/python"
SRC = REPO / "src"

# Condition vocabulary. A: shell over the source tree only. B: basic index
# (functions/callers/callees/detail). C: basic + deep facts (branches/returns/
# resources/assignments/leaks) + recursive graph traversal.
CONDITIONS = ("A", "B", "C")
BASIC_QUERIES = ("functions", "callers", "callees", "detail")
DEEP_QUERIES = ("branches", "returns", "resources", "assignments", "leaks")
STOPWORDS = {"if", "for", "while", "switch", "case", "return", "sizeof", "do",
             "else", "goto", "defined", "typeof", "__atomic_exchange_n",
             "__sync_synchronize"}

# ---------------------------------------------------------------------------
# Task specs. GT is enumerated from the DB, then every locator is validated
# against the corpus source; a fact whose source line does not contain the
# expected token is dropped and reported (source-validated ground truth).
# ---------------------------------------------------------------------------

TASK_SPECS = [
    # ---- debug agent -----------------------------------------------------
    dict(id="D1", cls="debug", kind="internal",
         targets=["sys_pause", "sleep"],
         prompt="用户态 sleep() 返回前，内核 sys_pause/sleep 中自旋锁的获取/释放点与"
                "关键分支分别在哪里？为什么 sleep 前必须先释放 tickslock？",
         gt=["internal", "branches:sys_pause,sleep", "returns:sys_pause,sleep",
             "resources:sys_pause,sleep"],
         rubric="0.6*F1(facts) + 0.4*manual(锁纪律解释)；事实=锁操作/分支/返回点定位。",
         manual_note="解释 lost-wakeup：sleep_prepare 在持锁下设置 chan，释放锁后 sleep()，"
                     "否则 wakeup 可能发生在等待前。"),
    dict(id="D2", cls="debug", kind="internal", targets=["kfork"],
         prompt="用户态 fork() 返回 -1 的所有路径：列出 kfork 中的失败返回点、错误路径上的"
                "资源/锁清理点，以及哪一步失败会导致子进程资源泄漏。",
         gt=["internal", "branches:kfork", "returns:kfork", "resources:kfork"],
         rubric="0.7*F1(facts) + 0.3*manual(allocproc/uvmcopy 失败后 freeproc+release 解释)。",
         manual_note="uvmcopy 失败时 freeproc(np)+release(&np->lock) 后 return -1；"
                     "allocproc 失败直接 return -1。"),
    dict(id="D3", cls="debug", kind="internal", targets=["sys_open"],
         prompt="open() 返回 -1 的可能原因：列出 sys_open 的每个错误返回点及对应的清理动作，"
                "并说明 namei/文件描述符分配失败时的差异。",
         gt=["internal", "branches:sys_open", "returns:sys_open", "resources:sys_open"],
         rubric="0.7*F1(facts) + 0.3*manual(namei/filealloc 失败区别、iunlockput/end_op 顺序)。",
         manual_note="namei 失败 -> end_op 后 -1；filealloc/fdalloc 失败 -> fileclose+iunlockput+end_op。"),
    dict(id="D4", cls="debug", kind="internal", targets=["bget", "bread"],
         prompt="文件读取偶发读到旧数据：bget/bread 中与缓冲区有效性、引用计数和睡眠等待相关的"
                "分支与资源操作在哪里？谁负责把 b->valid 置为 1？",
         gt=["internal", "branches:bget,bread", "returns:bget,bread", "resources:bget,bread"],
         rubric="0.7*F1(facts) + 0.3*manual(valid 由 virtio_disk_rw 完成后置位；bget 引用计数/睡眠)。",
         manual_note="bread: bget 后若 !b->valid 调 virtio_disk_rw(b,0) 再置 valid=1；"
                     "bget 在 refcnt>0 时 acquiresleep 等释放。"),
    dict(id="D5", cls="debug", kind="finding", targets=["proc_mapstacks"],
         prompt="深层分析报告 proc_mapstacks 存在 kalloc 泄漏告警。请定位告警对应的分配点，"
                "并判断这是真 bug 还是误报；说明依据。",
         gt=["finding:proc_mapstacks", "calls:proc_mapstacks"],
         rubric="0.5*F1(facts) + 0.5*manual(判定误报：内核栈页表常驻，无释放路径是有意的)。",
         manual_note="已知 finding 为误报（kernel page table 生命周期 = 内核生命周期），"
                     "说明深析 finding 需人工确认。"),
    # ---- coding agent ----------------------------------------------------
    dict(id="C1", cls="coding", kind="callers", symbols=["log_write"],
         prompt="要修改 kernel/log.c 的 log_write 语义（例如允许重复记录/改变返回值），"
                "列出所有直接调用者（函数名）及所在文件，评估影响面。",
         gt=["callers:log_write"],
         rubric="0.8*F1(caller set) + 0.2*manual(指出 writei/iupdate 等写路径受影响)。",
         manual_note="调用者：bzero balloc bfree ialloc iupdate ifree bmap writei（均在 fs.c）。"),
    dict(id="C2", cls="coding", kind="callers", symbols=["wakeup"],
         prompt="要把 wakeup(chan) 改成 wakeup_locked 语义，列出所有直接调用者（函数名）"
                "及文件，说明哪些模块需要同步修改。",
         gt=["callers:wakeup"],
         rubric="0.8*F1(caller set) + 0.2*manual(按模块归类 proc/fs/pipe/console/uart/virtio/trap)。",
         manual_note="12 个调用者函数，跨 7-8 个文件；适合用 callers 查询而非通读。"),
    dict(id="C3", cls="coding", kind="callers", symbols=["readi"],
         prompt="要修改 readi 的返回值/越界约定，列出所有直接调用者（函数名），"
                "并区分用户拷贝路径与文件系统内部路径。",
         gt=["callers:readi"],
         rubric="0.8*F1(caller set) + 0.2*manual(区分 fileread/exec/dir 路径)。",
         manual_note="调用者：fileread kexec loadseg isdirempty dirlookup dirlink。"),
    dict(id="C4", cls="coding", kind="reverse2", symbols=["kalloc"],
         prompt="kalloc 的分配语义若要改为可失败/可追踪，直接和间接（2 跳）受影响的调用者"
                "函数有哪些？列出函数名集合。",
         gt=["reverse2:kalloc"],
         rubric="0.8*F1(2-hop caller set) + 0.2*manual(区分直接 13 处 vs 2-hop 传播面)。",
         manual_note="call graph 递归查询一次即可得到 2-hop 集合；grep+通读需多轮扩展。"),
    dict(id="C5", cls="coding", kind="callers", symbols=["filealloc"],
         prompt="filealloc 返回 0 的错误语义若要改动，哪些函数需要检查？另外说明为什么"
                "sys_open 的调用者（syscall 入口）在静态调用图中不可见。",
         gt=["callers:filealloc"],
         rubric="0.7*F1(caller set) + 0.3*manual(指出 syscall 表函数指针分发导致入口边缺失)。",
         manual_note="直接调用者 pipealloc(2 处)/sys_open(1 处)；initcode/table dispatch 是间接边，"
                     "本 DB call_targets 为空，暴露静态分析盲区。"),
    # ---- test-gen agent --------------------------------------------------
    dict(id="T1", cls="testgen", kind="internal", targets=["kfork"],
         prompt="为 kfork 生成测试要点：覆盖哪些分支、错误返回、资源清理点？给出可映射到断言/"
                "故障注入的测试清单。",
         gt=["internal", "branches:kfork", "returns:kfork", "resources:kfork"],
         rubric="0.7*F1(facts) + 0.3*manual(错误注入点：allocproc/uvmcopy 失败；锁配对)。",
         manual_note="测试要点：allocproc 失败、uvmcopy 失败、fd 表复制、锁配对 release。"),
    dict(id="T2", cls="testgen", kind="internal", targets=["namex"],
         prompt="为 namex 设计路径覆盖测试：\"/\"、\".\"、\"..\"、不存在、非目录、父目录参数"
                "等分支如何覆盖？列出分支条件与返回点。",
         gt=["internal", "branches:namex", "returns:namex", "resources:namex"],
         rubric="0.7*F1(facts) + 0.3*manual(用 mkdir/link/chdir 触发各分支)。",
         manual_note="分支含 rooted、skipelem 循环、非目录、nlink==0、nameiparent 末尾判定。"),
    dict(id="T3", cls="testgen", kind="internal", targets=["sys_open"],
         prompt="为 open() 写测试：O_CREATE、O_TRUNC、目录/设备文件、fd 耗尽等错误路径如何覆盖？"
                "列出分支条件、错误返回点和清理动作。",
         gt=["internal", "branches:sys_open", "returns:sys_open", "resources:sys_open"],
         rubric="0.7*F1(facts) + 0.3*manual(NOFILE 耗尽、cwd 权限、设备 branch)。",
         manual_note="7 个 -1 返回点；配合 fd 耗尽（NOFILE）与只读目录测试。"),
    dict(id="T4", cls="testgen", kind="internal", targets=["writei"],
         prompt="为 writei 设计边界/错误覆盖测试：越界、MAXFILE、addr==0、copyin 失败、"
                "跨块写、日志提交。列出分支条件、返回点和资源操作。",
         gt=["internal", "branches:writei", "returns:writei", "resources:writei"],
         rubric="0.7*F1(facts) + 0.3*manual(log_write 与 brelse 配对、MAXFILE 边界)。",
         manual_note="分支：off>size、off+n<off、off+n>MAXFILE*BSIZE、addr==0、copyin 失败、off>size 更新。"),
    dict(id="T5", cls="testgen", kind="internal", targets=["uvmcopy"],
         prompt="为 uvmcopy 设计 fork 失败注入测试：哪些分支与返回点需要覆盖？"
                "如何制造 walk 失败、kalloc 失败、mappages 失败？",
         gt=["internal", "branches:uvmcopy", "returns:uvmcopy", "resources:uvmcopy"],
         rubric="0.7*F1(facts) + 0.3*manual(失败后 uvmunmap/kfree 清理断言)。",
         manual_note="分支：walk==0、!PTE_V、kalloc==0、mappages!=0；返回 0/-1。"),
]

# ---------------------------------------------------------------------------
# SQLite helpers and ground-truth construction
# ---------------------------------------------------------------------------

def db_connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def function_row(db: sqlite3.Connection, name: str):
    return db.execute(
        "select * from functions where name=? and is_definition=1 "
        "order by file, line limit 1", (name,)).fetchone()


def fact_key(kind: str, file: str, line: int) -> str:
    return f"{file}:{line}:{kind}"


def build_ground_truth(db: sqlite3.Connection, corpus: Path):
    """Enumerate GT facts from the DB and validate each locator against source."""
    src_cache: dict[str, list[str]] = {}

    def src_lines(path: str) -> list[str]:
        if path not in src_cache:
            try:
                src_cache[path] = (corpus / path).read_text(errors="replace").splitlines()
            except OSError:
                src_cache[path] = []
        return src_cache[path]

    def src_line(file: str, line: int) -> str:
        rows = src_lines(file)
        return rows[line - 1].strip() if 0 < line <= len(rows) else ""

    tasks = []
    for spec in TASK_SPECS:
        facts: dict[str, dict] = {}
        invalid = []

        def add(key, kind, file, line, detail=""):
            line_text = src_line(file, int(line))
            if not line_text:
                invalid.append({"key": key, "reason": "empty source line"})
                return
            facts.setdefault(key, {"key": key, "kind": kind, "file": file,
                                   "line": int(line), "detail": detail,
                                   "source": line_text})

        for g in spec["gt"]:
            if g == "internal":
                for target in spec.get("targets", []):
                    f = function_row(db, target)
                    if not f:
                        invalid.append({"target": target, "reason": "missing function"})
                        continue
                    for r in db.execute("select * from branches where function_id=?", (f["id"],)):
                        add(fact_key("branch", r["file"], r["line"]), "branch",
                            r["file"], r["line"], f"{r['kind']}: {r['condition']}")
                    for r in db.execute("select * from returns where function_id=?", (f["id"],)):
                        add(fact_key("return", r["file"], r["line"]), "return",
                            r["file"], r["line"], f"return {r['expr']}")
                    for r in db.execute("select * from resource_ops where function_id=?", (f["id"],)):
                        add(fact_key("resource", r["file"], r["line"]), "resource",
                            r["file"], r["line"], f"{r['op']} {r['api_name']}")
                    for r in db.execute("select * from calls where caller_id=?", (f["id"],)):
                        add(fact_key("call", r["file"], r["line"]), "call",
                            r["file"], r["line"], r["callee_name"])
            elif (g.startswith("branches:") or g.startswith("returns:")
                  or g.startswith("resources:")):
                kind, names = g.split(":", 1)
                table = {"branches": "branches", "returns": "returns",
                         "resources": "resource_ops"}[kind]
                singular = {"branches": "branch", "returns": "return",
                            "resources": "resource"}[kind]
                for name in names.split(","):
                    f = function_row(db, name)
                    if not f:
                        invalid.append({"target": name, "reason": "missing function"})
                        continue
                    for r in db.execute(f"select * from {table} where function_id=?", (f["id"],)):
                        detail = r["condition"] if singular == "branch" else (
                            r["expr"] if singular == "return" else f"{r['op']} {r['api_name']}")
                        add(fact_key(singular, r["file"], r["line"]), singular,
                            r["file"], r["line"], detail)
            elif g.startswith("calls:"):
                for name in g.split(":", 1)[1].split(","):
                    f = function_row(db, name)
                    if not f:
                        invalid.append({"target": name, "reason": "missing function"})
                        continue
                    for r in db.execute("select * from calls where caller_id=?", (f["id"],)):
                        add(fact_key("call", r["file"], r["line"]), "call",
                            r["file"], r["line"], r["callee_name"])
            elif g.startswith("callers:"):
                name = g.split(":", 1)[1]
                for r in db.execute(
                        "select cf.name as caller_name, cf.file as caller_file, "
                        "c.line as call_line, c.callee_name "
                        "from calls c join functions cf on cf.id=c.caller_id "
                        "where c.callee_name=? and cf.is_definition=1 "
                        "order by cf.name", (name,)):
                    add(f"fn:{r['caller_name']}", "caller", r["caller_file"],
                        r["call_line"], f"{r['caller_name']} calls {r['callee_name']}")
            elif g.startswith("reverse2:"):
                name = g.split(":", 1)[1]
                root = function_row(db, name)
                def callers_of(sym: str) -> set[str]:
                    return {r[0] for r in db.execute(
                        "select cf.name from calls c join functions cf on cf.id=c.caller_id "
                        "where c.callee_name=? and cf.is_definition=1", (sym,))}

                depth1 = callers_of(name)
                depth2 = set()
                for caller in sorted(depth1):
                    for fn in callers_of(caller):
                        if fn not in depth1 and fn != name:
                            depth2.add(fn)
                for fn in sorted(depth1 | depth2):
                    add(f"fn:{fn}", "caller", root["file"], root["line"], fn)
            elif g.startswith("finding:"):
                name = g.split(":", 1)[1]
                for r in db.execute(
                        "select fd.*, f.file, f.line from findings fd "
                        "join functions f on f.id=fd.function_id "
                        "where f.name=? and f.is_definition=1", (name,)):
                    add(f"finding:{name}", "finding", r["file"], r["line"],
                        f"{r['kind']}: {r['message']}")
        tasks.append(dict(spec, facts=list(facts.values()), invalid=invalid))
    return tasks


# ---------------------------------------------------------------------------
# Source helpers (condition A)
# ---------------------------------------------------------------------------

class SourceView:
    """Condition A: shell-like access to the source tree (grep + read)."""

    def __init__(self, corpus: Path):
        self.corpus = corpus
        self._files: dict[str, list[str]] = {}
        self._defs: dict[str, tuple[str, int, int, list[str]]] = {}
        self.bytes_read = 0
        self.files_read: set[str] = set()
        self.tool_calls = 0

    def _load(self, rel: str) -> list[str]:
        """Cheap in-process file load used by grep/scanning (not billed as a read)."""
        if rel not in self._files:
            try:
                text = (self.corpus / rel).read_text(errors="replace")
            except OSError:
                text = ""
            self._files[rel] = text.splitlines()
        return self._files[rel]

    def read(self, rel: str) -> list[str]:
        lines = self._load(rel)
        if rel not in self.files_read:
            self.files_read.add(rel)
            self.bytes_read += sum(len(x.encode()) + 1 for x in lines)
            self.tool_calls += 1
        return lines

    def grep(self, pattern: str, include_suffix=".c"):
        """Emulate `grep -rn pattern --include='*.c'`; returns [(rel, line, text)]."""
        self.tool_calls += 1
        rx = re.compile(pattern)
        hits = []
        for path in sorted(self.corpus.rglob(f"*{include_suffix}")):
            rel = str(path.relative_to(self.corpus))
            for i, text in enumerate(self._load(rel), 1):
                if rx.search(text):
                    hits.append((rel, i, text))
        self.bytes_read += sum(len(t.encode()) + 1 for _f, _i, t in hits)  # grep output
        return hits

    def find_span(self, name: str):
        if not self._defs:
            self._scan_all()
            self.tool_calls += 1                 # one grep/symbol lookup
            self.bytes_read += 256               # search output
        return self._defs.get(name)

    def _candidate_name(self, s: str):
        if not s or s.endswith(";") or s.startswith(("}", "//", "/*", "*")):
            return None
        if re.match(r"^(if|for|while|else|return|case|switch|do|goto|sizeof)\b", s):
            return None
        if "=" in s or ";" in s:
            return None
        idx = s.find("(")
        if idx <= 0:
            return None
        ids = re.findall(r"[A-Za-z_]\w*", s[:idx])
        return ids[-1] if ids else None

    def _scan_all(self):
        for path in sorted(self.corpus.rglob("*.c")):
            rel = str(path.relative_to(self.corpus))
            lines = self._load(rel)
            i = 0
            while i < len(lines):
                name = self._candidate_name(lines[i].strip())
                if name and name not in STOPWORDS:
                    brace = None
                    for j in range(i + 1, min(i + 5, len(lines))):
                        t = lines[j].strip()
                        if t.startswith("{"):
                            brace = j
                            break
                        if t and not t.endswith((",", ")", "(")):
                            break
                    if brace is not None:
                        depth = 0
                        k = brace
                        while k < len(lines):
                            depth += lines[k].count("{") - lines[k].count("}")
                            if depth <= 0:
                                break
                            k += 1
                        self._defs.setdefault(
                            name, (rel, i + 1, min(k + 1, len(lines)), lines))
                        i = k
                i += 1

    def enclosing_function(self, rel: str, line_no: int):
        if not self._defs:
            self._scan_all()
        best = None
        for name, (f, start, end, _lines) in self._defs.items():
            if f == rel and start <= line_no <= end:
                if best is None or start > best[1]:
                    best = (name, start, end)
        return best[0] if best else None

    def read_span(self, name: str, charges: bool = True):
        span = self.find_span(name)
        if span is None:
            return None
        rel, start, end, lines = span
        if charges:
            self.read(rel)
        return span


def source_internal_claims(sv: SourceView, target: str, api_whitelist: set[str]):
    span = sv.read_span(target)
    if span is None:
        return set()
    _file, start, end, lines = span
    claims = set()
    for i in range(start + 1, end + 1):
        s = lines[i - 1]
        if re.search(r"\b(if|for|while|switch|case)\s*\(", s):
            claims.add(fact_key("branch", _file, i))
        if re.match(r"^\s*return\b", s):
            claims.add(fact_key("return", _file, i))
        for api in api_whitelist:
            if re.search(rf"\b{re.escape(api)}\s*\(", s):
                claims.add(fact_key("resource", _file, i))
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", s):
            name = m.group(1)
            if name not in STOPWORDS and not name.isupper():
                claims.add(fact_key("call", _file, i))
    return claims


def source_caller_claims(sv: SourceView, symbol: str):
    hits = sv.grep(rf"\b{re.escape(symbol)}\s*\(")
    claims = set()
    span = sv.find_span(symbol)
    def_loc = (span[0], span[1]) if span else None
    for rel in sorted({r for r, _l, _t in hits}):
        sv.read(rel)
    for rel, line_no, text in hits:
        if def_loc == (rel, line_no):
            continue  # definition header, not a call site
        caller = sv.enclosing_function(rel, line_no)
        if caller and caller != symbol:
            claims.add(f"fn:{caller}")
    return claims


def source_reverse2_claims(sv: SourceView, symbol: str, max_depth=2):
    frontier = {symbol}
    seen = {symbol}
    claims = set()
    for _depth in range(max_depth):
        nxt = set()
        for name in sorted(frontier):
            hits = sv.grep(rf"\b{re.escape(name)}\s*\(")
            for rel in sorted({r for r, _l, _t in hits}):
                sv.read(rel)
            for rel, line_no, _text in hits:
                caller = sv.enclosing_function(rel, line_no)
                if caller and caller not in seen and caller != name:
                    claims.add(f"fn:{caller}")
                    nxt.add(caller)
        seen |= nxt
        frontier = nxt
        if not frontier:
            break
    return claims


# ---------------------------------------------------------------------------
# CA CLI helpers (conditions B and C)
# ---------------------------------------------------------------------------

class CAView:
    def __init__(self, db: Path):
        self.db = db
        self.bytes_read = 0
        self.tool_calls = 0
        self.latency_ms = 0.0
        self.commands = []

    def query(self, kind: str, symbol: str | None = None, extra: list[str] | None = None):
        cmd = [str(PYTHON), "-m", "ca", "query", kind]
        if symbol:
            cmd.append(symbol)
        cmd += ["--db", str(self.db), "--json"] + (extra or [])
        return self._run(cmd)

    def graph(self, symbol: str, depth: int, reverse=True):
        cmd = [str(PYTHON), "-m", "ca", "graph", "--from", symbol, "--depth", str(depth),
               "--format", "json", "--db", str(self.db)]
        if reverse:
            cmd.append("--reverse")
        return self._run(cmd)

    def _run(self, cmd: list[str]):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        started = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        self.latency_ms += (time.perf_counter() - started) * 1000
        self.tool_calls += 1
        self.bytes_read += len(proc.stdout.encode()) + len(proc.stderr.encode())
        self.commands.append(" ".join(cmd[2:]))
        if proc.returncode != 0:
            return []
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []


def claims_from_rows(rows, kind: str) -> set[str]:
    claims = set()
    for r in rows or []:
        file = r.get("file") or r.get("call_file")
        line = r.get("line") or r.get("call_line")
        if file and line:
            claims.add(fact_key(kind, file, int(line)))
    return claims


def ca_internal_claims(ca: CAView, targets: list[str], deep: bool):
    claims = set()
    for target in targets:
        claims |= claims_from_rows(ca.query("callees", target), "call")
        if deep:
            claims |= claims_from_rows(ca.query("branches", target), "branch")
            claims |= claims_from_rows(ca.query("returns", target), "return")
            claims |= claims_from_rows(ca.query("resources", target), "resource")
    return claims


def ca_caller_claims(ca: CAView, symbol: str):
    return {f"fn:{r['caller_name']}" for r in ca.query("callers", symbol)}


def ca_reverse2_claims(ca: CAView, symbol: str, deep: bool, db_conn):
    if deep:
        g = ca.graph(symbol, depth=2, reverse=True)
        return {f"fn:{n['name']}" for n in g.get("nodes", [])
                if n.get("depth", 0) > 0 and n.get("name") != symbol}
    # basic condition: walk callers level by level through the CLI
    depth1 = {r["caller_name"] for r in ca.query("callers", symbol)}
    depth2 = set()
    for caller in sorted(depth1):
        for r in ca.query("callers", caller):
            if r["caller_name"] not in depth1 and r["caller_name"] != symbol:
                depth2.add(r["caller_name"])
    return {f"fn:{n}" for n in depth1 | depth2}


def ca_finding_claims(ca: CAView, target: str, deep: bool):
    claims = claims_from_rows(ca.query("callees", target), "call")
    if deep:
        for r in ca.query("leaks"):
            if r.get("function_name") == target:
                claims.add(f"finding:{target}")
    return claims


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_condition(condition: str, task: dict, corpus: Path, db: Path,
                  api_whitelist: set[str], db_conn) -> dict:
    started = time.perf_counter()
    claims: set[str] = set()
    metrics = {}
    kind = task["kind"]

    if condition == "A":
        sv = SourceView(corpus)
        if kind == "internal":
            for target in task["targets"]:
                claims |= source_internal_claims(sv, target, api_whitelist)
        elif kind == "callers":
            for symbol in task["symbols"]:
                claims |= source_caller_claims(sv, symbol)
        elif kind == "reverse2":
            for symbol in task["symbols"]:
                claims |= source_reverse2_claims(sv, symbol)
        elif kind == "finding":
            for target in task["targets"]:
                claims |= source_internal_claims(sv, target, api_whitelist)
            claims = {c for c in claims if c.endswith(":call")}
        metrics = dict(bytes=sv.bytes_read, tool_calls=sv.tool_calls,
                       files_read=len(sv.files_read), files=sorted(sv.files_read),
                       latency_ms=(time.perf_counter() - started) * 1000)
    else:
        ca = CAView(db)
        deep = condition == "C"
        if kind == "internal":
            claims |= ca_internal_claims(ca, task["targets"], deep)
        elif kind == "callers":
            for symbol in task["symbols"]:
                claims |= ca_caller_claims(ca, symbol)
        elif kind == "reverse2":
            for symbol in task["symbols"]:
                claims |= ca_reverse2_claims(ca, symbol, deep, db_conn)
        elif kind == "finding":
            for target in task["targets"]:
                claims |= ca_finding_claims(ca, target, deep)
        metrics = dict(bytes=ca.bytes_read, tool_calls=ca.tool_calls,
                       files_read=0, commands=ca.commands,
                       latency_ms=ca.latency_ms)

    gt = {f["key"] for f in task["facts"]}
    hit = claims & gt
    precision = len(hit) / len(claims) if claims else 0.0
    recall = len(hit) / len(gt) if gt else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return dict(condition=condition, claims=sorted(claims), metrics=metrics,
                score=dict(precision=round(precision, 4), recall=round(recall, 4),
                           f1=round(f1, 4), hits=len(hit), gt=len(gt),
                           claimed=len(claims)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--seed", type=int, default=18)
    ap.add_argument("--build-tasks", action="store_true")
    args = ap.parse_args(argv)

    db = db_connect(args.db)
    tasks = build_ground_truth(db, args.corpus)
    api_whitelist = {r["api_name"] for r in db.execute("select distinct api_name from resource_ops")}
    if args.build_tasks:
        TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        TASKS_PATH.write_text(json.dumps(
            dict(meta=dict(db=str(args.db.relative_to(REPO)),
                           corpus=str(args.corpus.relative_to(REPO)),
                           note="GT enumerated from DB, each locator source-validated"),
                 tasks=tasks), indent=2, ensure_ascii=False))
        print(f"wrote {TASKS_PATH} ({len(tasks)} tasks, "
              f"{sum(len(t['facts']) for t in tasks)} facts)")
        return 0

    rng = random.Random(args.seed)
    runs = []
    for rep in range(1, args.reps + 1):
        for task in tasks:
            order = list(CONDITIONS)
            rng.shuffle(order)
            for condition in order:
                res = run_condition(condition, task, args.corpus, args.db,
                                    api_whitelist, db)
                res.update(task_id=task["id"], cls=task["cls"], rep=rep,
                           prompt=task["prompt"], rubric=task["rubric"])
                runs.append(res)

    aggregate = {}
    for cls in sorted({t["cls"] for t in tasks}):
        for condition in CONDITIONS:
            sel = [r for r in runs if r["cls"] == cls and r["condition"] == condition]
            aggregate.setdefault(cls, {}).setdefault(condition, {})
            aggregate[cls][condition] = dict(
                tasks=len({r["task_id"] for r in sel}),
                runs=len(sel),
                mean_f1=round(sum(r["score"]["f1"] for r in sel) / len(sel), 4),
                mean_precision=round(sum(r["score"]["precision"] for r in sel) / len(sel), 4),
                mean_recall=round(sum(r["score"]["recall"] for r in sel) / len(sel), 4),
                mean_bytes=int(sum(r["metrics"]["bytes"] for r in sel) / len(sel)),
                mean_tool_calls=round(sum(r["metrics"]["tool_calls"] for r in sel) / len(sel), 2),
                mean_latency_ms=round(sum(r["metrics"]["latency_ms"] for r in sel) / len(sel), 2),
            )

    payload = dict(meta=dict(tool="benchmarks/agent_eval/run_eval.py",
                             generated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                             db=str(args.db), corpus=str(args.corpus),
                             reps=args.reps, seed=args.seed,
                             conditions={c: ("source-only" if c == "A" else
                                             "CA basic index" if c == "B" else
                                             "CA basic+deep facts") for c in CONDITIONS},
                             model="deepseek-flash (controlled deterministic policies, no sub-agents)",
                             limitation="Not an LLM-agent run: each condition is a fixed retrieval "
                                        "policy + condition-neutral fact extractor; A/GT advantage "
                                        "notes in REPORT.md."),
                     tasks=tasks, runs=runs, aggregate=aggregate)
    out = args.out or (RESULTS_DIR / "pilot_xv6.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"wrote {out}")
    for cls, conds in aggregate.items():
        print(f"\n[{cls}]")
        for c in CONDITIONS:
            a = conds[c]
            print(f"  {c}: F1={a['mean_f1']:.3f} P={a['mean_precision']:.3f} "
                  f"R={a['mean_recall']:.3f} bytes={a['mean_bytes']} "
                  f"calls={a['mean_tool_calls']} lat={a['mean_latency_ms']:.0f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
