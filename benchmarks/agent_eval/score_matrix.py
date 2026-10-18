#!/usr/bin/env python3
"""Score the full xv6 A/B/C LLM matrix (15 tasks x 3 conditions).

Answers: `.tmp/llm_eval/full/<task>_<cond>[_rN].md` (written by the Lead's
workflow). Ground truth: `tasks.json`. Reuses the citation/caller extraction
from `score_llm.py`.

Metrics per run: auto fact F1 (GT location/caller coverage, in-scope precision),
rubric-point proxy (deterministic checklist over the task rubric's semantic
points; documented as a proxy, not human review), combined = 0.7*F1+0.3*proxy,
citation/phantom counts, effort (parsed from the answer's `## effort` section
when present). Aggregates report mean±std per class/condition, paired per-task
deltas with an exact sign test (initial significance judgement only), and
failure-mode flags.

Output: `benchmarks/agent_eval/results/llm_matrix_xv6.json`.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from statistics import mean, median, stdev

import score_llm as S

REPO = Path(__file__).resolve().parents[2]
ANSWERS = REPO / ".tmp/llm_eval/full"
OUT = REPO / "benchmarks/agent_eval/results/llm_matrix_xv6.json"
TASKS = {t["id"]: t for t in S.TASKS["tasks"]}
ALL_TASKS = list(TASKS)
CONDS = ("A", "B", "C")

# Rubric-point proxy: each point is matched when any regex fires. These encode
# the semantic content a *good* answer should contain for the task rubric
# (lock discipline, cleanup pairing, test-design coverage, CA-gap candour).
CHECKLIST = {
    "D1": [
        ("tickslock 获取/释放", r"tickslock"),
        ("sleep_prepare/sleep 拆分", r"sleep_prepare|sched\("),
        ("p->lock/sched 语义", r"p->lock|sched\("),
        ("lost-wakeup/先释放锁原因", r"lost.?wakeup|丢失唤醒|先释放|释放.*睡眠|唤醒.*丢失"),
    ],
    "D2": [
        ("allocproc 失败返回", r"allocproc"),
        ("uvmcopy 失败清理", r"uvmcopy"),
        ("freeproc/release 清理", r"freeproc|release\(&np->lock\)"),
        ("返回值 diff 说明", r"return -1|返回 -1|资源.*泄漏|泄漏"),
    ],
    "D3": [
        ("argstr 失败无清理", r"argstr|E1"),
        ("namei 失败仅 end_op", r"namei|E3"),
        ("filealloc/fdalloc 差异", r"filealloc|fdalloc|fileclose"),
        ("iunlockput/end_op 配对", r"iunlockput|end_op"),
        ("常量/参数语义", r"MAXPATH|NDEV|NOFILE|NFILE"),
    ],
    "D4": [
        ("b->valid 置位来源", r"valid|virtio_disk_rw"),
        ("refcnt/睡眠等待", r"refcnt|acquiresleep|sleep|等"),
        ("bread/bget 关系", r"bget|bread"),
        ("brelse/引用释放", r"brelse|引用"),
    ],
    "D5": [
        ("定位 kalloc/proc_mapstacks", r"proc_mapstacks"),
        ("判定误报/false positive", r"误报|false.?positive|不是.*bug|并非.*bug"),
        ("解释内核常驻生命周期", r"常驻|生命周期|内核页表|不会释放|有意"),
        ("findings 仅线索建议", r"线索|需人工|advisory|确认"),
    ],
    "C1": [
        ("调用者集合完整", r"bzero|balloc|bfree|ialloc|iupdate|ifree|bmap|writei"),
        ("写路径/事务影响", r"writei|iupdate|log|事务|路径"),
        ("文件范围", r"fs\.c|kernel/fs\.c"),
    ],
    "C2": [
        ("调用者数量 12", r"12"),
        ("调用点 15/8 文件", r"15|8 个文件|8 files"),
        ("跨模块清单", r"pipe|console|uart|virtio|log|trap|sleeplock|proc"),
        ("原型声明 defs.h", r"defs\.h|声明|原型"),
    ],
    "C3": [
        ("调用者集合", r"fileread|kexec|loadseg|isdirempty|dirlookup|dirlink"),
        ("用户/内核路径区分", r"用户|exec|目录|dirlookup|fileread"),
        ("返回值/越界语义", r"越界|返回值|off|size"),
    ],
    "C4": [
        ("直接调用者", r"kalloc"),
        ("2-hop 传播", r"2.?hop|间接|传递|二跳|两层"),
        ("函数集合/路径示例", r"allocproc|pipealloc|vmfault|uvmalloc|walk|sys_exec|proc_mapstacks"),
        ("调用图查询方法", r"graph|反向|reverse"),
    ],
    "C5": [
        ("调用者", r"pipealloc|sys_open"),
        ("syscall 表间接分发", r"syscall|表|函数指针|dispatch|间接"),
        ("call_targets/盲区", r"call_targets|盲区|不可见|缺失|未建模"),
    ],
    "T1": [
        ("allocproc 失败注入", r"allocproc"),
        ("uvmcopy 失败注入", r"uvmcopy"),
        ("freeproc/锁配对断言", r"freeproc|release|锁"),
        ("fd/打开文件复制", r"ofile|filedup|fd"),
    ],
    "T2": [
        ("分支清单/覆盖", r"分支|branch|覆盖"),
        ("rooted/skipelem/空路径", r"skipelem|rooted|空路径|\"/\"|'/'"),
        ("nameiparent 语义", r"nameiparent"),
        ("孤儿/nlink==0", r"nlink|孤儿|orphan"),
        ("name 输出参数", r"name 输出|输出参数|name，”|name 缓冲"),
    ],
    "T3": [
        ("O_CREATE/create 路径", r"O_CREATE|create"),
        ("T_DIR/T_DEVICE 分支", r"T_DIR|T_DEVICE"),
        ("filealloc/fdalloc 失败", r"filealloc|fdalloc"),
        ("end_op 配对/清理", r"end_op|iunlockput"),
        ("常量边界", r"NOFILE|NDEV|MAXPATH|NFILE"),
    ],
    "T4": [
        ("越界/MAXFILE 分支", r"MAXFILE|越界|off.*size"),
        ("addr==0/copyin 失败", r"addr ?== ?0|copyin"),
        ("log_write/brelse 配对", r"log_write|brelse"),
        ("返回值/部分写", r"return|tot|部分"),
    ],
    "T5": [
        ("walk 失败分支", r"walk"),
        ("PTE_V 检查", r"PTE_V"),
        ("kalloc 失败", r"kalloc"),
        ("mappages 失败清理", r"mappages|uvmunmap|kfree"),
    ],
}


def answer_files(task_id: str, cond: str):
    return sorted(ANSWERS.glob(f"{task_id}_{cond}*.md"))


def score_internal(task, text):
    auto, _claims, _gt = S.score_citation_run(task, text)
    return auto


def score_callers(task, text):
    gt = S.gt_callers(task)
    # Claimed callers: known caller names that appear in a caller-listing
    # context (line with a `.c` file mention, or a line about callers/tables).
    claims = set()
    for line in text.splitlines():
        if not re.search(r"\.(?:c|h)\b|调用者|caller|受影响|impact", line, re.I):
            continue
        for tok in re.findall(r"`([A-Za-z_]\w*)`|(?<![\w])([a-z_]\w*)\(", line):
            name = tok[0] or tok[1]
            if name in gt:
                claims.add(name)
    hit = claims & gt
    p = len(hit) / len(claims) if claims else 0.0
    r = len(hit) / len(gt) if gt else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return dict(claims=sorted(claims), gt=sorted(gt), hits=len(hit),
                precision=round(p, 4), recall=round(r, 4), f1=round(f1, 4),
                phantom=[], claims_in_scope=len(claims),
                extras_out_of_scope=[])


def score_run(task, cond, path):
    text = path.read_text()
    if task["kind"] in ("callers", "reverse2"):
        auto = score_callers(task, text)
    else:
        auto = score_internal(task, text)
    # rubric-point proxy
    checks = CHECKLIST.get(task["id"], [])
    hits = []
    for name, pattern in checks:
        if re.search(pattern, text, re.I):
            hits.append(name)
    proxy = round(len(hits) / len(checks), 4) if checks else None
    combined = round(0.7 * auto["f1"] + 0.3 * proxy, 4) if proxy is not None else round(auto["f1"], 4)
    caveats = len(re.findall(r"CA 未覆盖|CA 无|未索引|缺失|no results|无法由 CA|未提供", text))
    effort = S.effort_from_answer(text)
    effort["cited_locations"] = auto.get("claims", 0)
    effort["answer_chars"] = len(text)
    try:
        answer_file = str(path.resolve().relative_to(REPO))
    except ValueError:
        answer_file = str(path)
    return dict(task=task["id"], cls=task["cls"], condition=cond,
                answer_file=answer_file,
                auto=auto, manual_proxy=proxy, manual_hits=hits, combined=combined,
                caveat_mentions=caveats,
                phantom=auto.get("phantom", []),
                effort_auto=effort)


def mean_std(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None, None
    return round(mean(vals), 4), round(stdev(vals), 4) if len(vals) > 1 else 0.0


def sign_test_p(wins, losses):
    """Two-sided exact sign test p-value (binomial n=wins+losses, p=0.5)."""
    n = wins + losses
    if n == 0:
        return None
    k = min(wins, losses)
    p = 2 * sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return round(min(1.0, p), 4)


def paired_deltas(runs, id_a, id_b):
    """Per-task paired delta (b - a) for F1 and combined."""
    out = {"f1": [], "combined": []}
    by_task = {}
    for r in runs:
        by_task.setdefault(r["task"], {})[r["condition"]] = r
    for tid, conds in by_task.items():
        if id_a in conds and id_b in conds:
            out["f1"].append(conds[id_b]["auto"]["f1"] - conds[id_a]["auto"]["f1"])
            out["combined"].append(conds[id_b]["combined"] - conds[id_a]["combined"])
    deltas = {}
    for key, vals in out.items():
        if not vals:
            continue
        wins = sum(1 for v in vals if v > 1e-9)
        losses = sum(1 for v in vals if v < -1e-9)
        deltas[key] = dict(n=len(vals), mean_delta=round(mean(vals), 4),
                           std_delta=round(stdev(vals), 4) if len(vals) > 1 else 0.0,
                           wins=wins, losses=losses, ties=len(vals) - wins - losses,
                           sign_test_p=sign_test_p(wins, losses))
    return deltas


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--tasks", type=Path, default=None,
                    help="task/GT JSON (default: benchmarks/agent_eval/tasks.json)")
    args = ap.parse_args(argv)
    answers = args.answers or ANSWERS
    global OUT, TASKS
    OUT = args.out or OUT
    if args.tasks:
        TASKS = {t["id"]: t for t in json.loads(args.tasks.read_text())["tasks"]}

    runs, missing = [], []
    for tid in ALL_TASKS:
        for cond in CONDS:
            files = sorted(answers.glob(f"{tid}_{cond}*.md"))
            if not files:
                missing.append(f"{tid}_{cond}")
                continue
            for path in files:
                runs.append(score_run(TASKS[tid], cond, path))

    aggregate = {}
    classes = sorted({t["cls"] for t in TASKS.values()})
    for key in ["ALL"] + classes:
        aggregate[key] = {}
        for cond in CONDS:
            sel = [r for r in runs if cond == r["condition"]
                   and (key == "ALL" or r["cls"] == key)]
            if not sel:
                continue
            f1_mean, f1_std = mean_std([r["auto"]["f1"] for r in sel])
            m_mean, m_std = mean_std([r["manual_proxy"] for r in sel])
            c_mean, c_std = mean_std([r["combined"] for r in sel])
            ctx_vals = [r["effort_auto"].get("context_bytes_est") for r in sel]
            calls_vals = [r["effort_auto"].get("tool_calls") for r in sel]
            file_vals = [r["effort_auto"].get("files") for r in sel]
            aggregate[key][cond] = dict(
                runs=len(sel), tasks=len({r["task"] for r in sel}),
                f1_mean=f1_mean, f1_std=f1_std,
                manual_mean=m_mean, manual_std=m_std,
                combined_mean=c_mean, combined_std=c_std,
                phantom_total=sum(len(r["phantom"]) for r in sel),
                caveat_mentions_mean=round(mean([r["caveat_mentions"] for r in sel]), 2),
                effort_parse_rate=round(mean([1 if r["effort_auto"].get("tool_calls") else 0
                                              for r in sel]), 2),
                tool_calls_mean=(round(mean([c for c in calls_vals if c is not None]), 1)
                                 if any(c is not None for c in calls_vals) else None),
                context_bytes_mean=int(mean([b for b in ctx_vals if b is not None])
                                       if any(b is not None for b in ctx_vals) else 0),
                context_bytes_median=(median([b for b in ctx_vals if b is not None])
                                      if any(b is not None for b in ctx_vals) else None),
                context_bytes_max=(max([b for b in ctx_vals if b is not None])
                                   if any(b is not None for b in ctx_vals) else None),
                files_mean=(round(mean([f for f in file_vals if f is not None]), 1)
                            if any(f is not None for f in file_vals) else None),
                answer_chars_mean=int(mean([r["effort_auto"]["answer_chars"] for r in sel])),
            )

    across = {}
    for key in ["ALL"] + classes:
        sub = [r for r in runs if key == "ALL" or r["cls"] == key]
        across[key] = {"A_vs_B": paired_deltas(sub, "A", "B"),
                       "A_vs_C": paired_deltas(sub, "A", "C"),
                       "B_vs_C": paired_deltas(sub, "B", "C")}

    # failure-mode flags
    failures = []
    for r in runs:
        flags = []
        if r["auto"]["f1"] < 0.3:
            flags.append("low_f1")
        if r["auto"].get("claims_in_scope", r["auto"].get("claims", 0)) == 0:
            flags.append("no_claims")
        if len(r["phantom"]) > 0:
            flags.append("phantom_location")
        if r["caveat_mentions"] >= 3:
            flags.append("many_CA_gaps_reported")
        if not (r["effort_auto"].get("tool_calls")
                or r["effort_auto"].get("context_bytes_est")):
            flags.append("effort_not_reported")
        if r["manual_proxy"] is not None and r["manual_proxy"] < 0.4:
            flags.append("rubric_proxy_low")
        if flags:
            failures.append(dict(task=r["task"], condition=r["condition"],
                                 f1=r["auto"]["f1"], flags=flags))

    payload = dict(
        meta=dict(tool="benchmarks/agent_eval/score_matrix.py",
                  generated_at=__import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
                  matrix="xv6 15 tasks x A/B/C",
                  answers=str(answers),
                  scoring="auto F1 = GT location/caller coverage vs tasks.json; manual_proxy = "
                          "deterministic rubric-point checklist (proxy, not human review); "
                          "combined = 0.7*F1+0.3*proxy",
                  stats="mean±std across tasks; paired per-task deltas with exact sign test "
                        "(initial significance judgement, n=15 tasks, single run per cell)",
                  limitations="single run per cell, subagent not production agent, xv6 small repo, "
                              "citation extraction heuristic; manual_proxy is a saturated rubric-point "
                              "checklist, not human judgment (human scores exist only for the 6-run "
                              "subset in results/llm_pilot_xv6.json); F1 measures structural fact "
                              "coverage / citation fidelity, not semantic depth",
                  conditions={
                      "A": "source only (grep/read, no CA)",
                      "B": "CA basic: functions/callers/callees/detail/positions only "
                           "(note: detail carries L1 branch/return/resource facts, so B is not "
                           "fact-starved; C adds graph + explicit deep queries)",
                      "C": "CA basic + deep: branches/returns/resources/assignments/graph",
                  }),
        missing_answers=missing, runs=runs, aggregate=aggregate,
        paired_deltas=across, failures=failures,
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"wrote {OUT} ({len(runs)} runs, missing={len(missing)})")
    for key in ["ALL"] + classes:
        if key not in aggregate:
            continue
        print(f"[{key}]")
        for cond in CONDS:
            a = aggregate[key].get(cond)
            if a:
                print(f"  {cond}: F1={a['f1_mean']}±{a['f1_std']} "
                      f"proxy={a['manual_mean']} combined={a['combined_mean']} "
                      f"chars={a['answer_chars_mean']} phant={a['phantom_total']}")
    for key in ["ALL"] + classes:
        if key not in across:
            continue
        print(f"paired[{key}]", json.dumps(across[key], ensure_ascii=False))
    print("failures:", len(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
