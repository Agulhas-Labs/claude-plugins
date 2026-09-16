#!/usr/bin/env python3
"""Where does Claude Code token spend go?

Reads Claude Code transcripts (main sessions and their subagents) and reports: totals split main vs
subagent, spend per day, concentration (does a few long contexts dominate spend), turn shape (single
tool-call turns), what fills a context, the fixed start every
context pays before its first turn, and the largest contexts by spend.

Pricing: input-equivalent prices every token against the uncached input rate — uncached x1, cache read
x0.1, cache write (5-minute) x1.25, cache write (1-hour) x2. When a usage record carries no 5m/1h split,
all of cache_creation_input_tokens is priced at x1.25. This is a price comparison against the uncached
input rate, not a token count. Output tokens are reported separately and never folded into input-equiv.

The transcript format is undocumented and may change; this script names the harness versions it read
so a report can be sanity-checked against the version that produced it.
"""
import argparse
import collections
import glob
import json
import os
import re
import statistics
import sys
import textwrap
from datetime import datetime, timedelta

HOME = os.path.expanduser("~")


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------

def parse_when(value, now):
    """Parse --since/--until: today, yesterday, <N>d, YYYY-MM-DD (local midnight), or an ISO datetime
    (local unless it carries Z/an offset). `now` is an aware local datetime used as the reference point
    and the fallback timezone for naive ISO datetimes."""
    v = value.strip()
    if v == "today":
        d = now.date()
        return datetime(d.year, d.month, d.day).astimezone()
    if v == "yesterday":
        d = (now - timedelta(days=1)).date()
        return datetime(d.year, d.month, d.day).astimezone()
    m = re.fullmatch(r"(\d+)d", v)
    if m:
        return now - timedelta(days=int(m.group(1)))
    iso = v[:-1] + "+00:00" if v.endswith("Z") else v
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        # A naive value is local wall-clock time; resolve it with the offset in effect on *that*
        # date (not `now`'s), so a date on the other side of a DST transition gets its own offset.
        dt = dt.astimezone()
    return dt


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# tool-call classification (category names kept identical so history stays comparable)
# ---------------------------------------------------------------------------

def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for b in c:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(b.get("text", ""))
                elif b.get("type") == "image":
                    out.append("x" * 6000)
                else:
                    out.append(json.dumps(b))
        return "".join(out)
    return json.dumps(c) if c is not None else ""


def bash_class(cmd):
    c = cmd.strip()
    c = re.sub(r"^(cd [^;&]+(&&|;)\s*)+", "", c)
    if re.search(r"\bswift (test|build)\b|xcodebuild", c): return "bash: raw swift build/test"
    if re.search(r"^(npm|pnpm|yarn|bun) (run )?(test|build)\b", c): return "bash: build/test"
    if re.search(r"^cargo (build|test)\b", c): return "bash: build/test"
    if re.search(r"^go (build|test)\b", c): return "bash: build/test"
    if re.search(r"^(pytest|python3? -m pytest)\b", c): return "bash: build/test"
    if re.search(r"^dotnet (build|test)\b", c): return "bash: build/test"
    if re.search(r"^(\./)?gradlew\b|^(gradle|mvn)\b", c): return "bash: build/test"
    if re.search(r"^make\b", c): return "bash: build/test"
    if re.search(r"swiftlint|swiftformat", c): return "bash: lint/format/gates"
    if re.search(r"^(npx )?(eslint|tsc|ruff|mypy|black|prettier)\b", c): return "bash: lint/format/gates"
    if re.search(r"^cargo clippy\b", c): return "bash: lint/format/gates"
    if re.search(r"^go vet\b", c): return "bash: lint/format/gates"
    if re.search(r"^(npm|pnpm|yarn|bun) (run )?lint\b", c): return "bash: lint/format/gates"
    if re.search(r"\bgit (diff|show)\b", c): return "bash: git diff/show"
    if re.search(r"\bgit (log|status|branch|rev-parse|merge-base|ls-remote|worktree|fetch|cherry)", c): return "bash: git log/status/etc"
    if re.search(r"\bgit (commit|push|rebase|merge|checkout|switch|add|reset|stash|cherry-pick)", c): return "bash: git mutate"
    if re.search(r"\bgh\b", c): return "bash: gh"
    if re.search(r"\b(grep|rg|git grep)\b", c): return "bash: grep"
    if re.search(r"^(cat|sed|head|tail|nl|awk)\b|\|\s*(sed|head|tail)", c): return "bash: cat/sed/head window"
    if re.search(r"python3?|jq\b", c): return "bash: python/jq"
    return "bash: other"


def classify(name, inp):
    if name == "Bash": return bash_class(inp.get("command", ""))
    if name == "Read":
        return "Read (ranged)" if inp.get("offset") or inp.get("limit") else "Read (whole file)"
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            _, server, tool = parts
            return "MCP " + server + " " + tool
        return "MCP " + name[len("mcp__"):]
    if name in ("Edit", "Write", "MultiEdit"): return "Edit/Write"
    if name in ("Grep", "Glob"): return "Grep/Glob tool"
    return name


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------

def input_equivalent(usage):
    uncached = usage.get("input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cc = usage.get("cache_creation") or {}
    if cc:
        w5 = cc.get("ephemeral_5m_input_tokens", 0)
        w1 = cc.get("ephemeral_1h_input_tokens", 0)
        cache_write_eq = w5 * 1.25 + w1 * 2.0
    else:
        cache_write_eq = usage.get("cache_creation_input_tokens", 0) * 1.25
    return uncached * 1.0 + cache_read * 0.1 + cache_write_eq


def context_size(usage):
    return usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def encoded(path):
    """How Claude Code spells a path as a project folder name: every character that is not a letter or
    a digit becomes `-` (`/home/me/App` -> `-home-me-App`, `D:\\work\\App` -> `D--work-App`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def project_display_name(project_dir_name):
    encoded_home_prefix = encoded(HOME) + "-"
    if project_dir_name.startswith(encoded_home_prefix):
        return project_dir_name[len(encoded_home_prefix):]
    return project_dir_name


def display_path(path):
    """A filesystem path with the home directory replaced by `~`, so a shared report never carries
    the username. Claude Code's project folders encode the home directory too
    (`-Users-<name>-Developer-App`), so that spelling becomes `~` as well."""
    if path == HOME:
        return "~"
    if path.startswith(HOME + os.sep):
        path = "~" + path[len(HOME):]
    return path.replace(encoded(HOME), "~")


def default_projects_dir():
    """Where Claude Code keeps transcripts: `$CLAUDE_CONFIG_DIR/projects` when that is set, else
    `~/.claude/projects`."""
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude"), "projects")


def iter_context_files(projects_dir):
    """Yield (kind, path, project_dir_name, session_id, agent_id) for every main session and every
    subagent transcript under `projects_dir`. The two globs are independent, so a subagent is found
    even if its parent session file is absent (already rotated away, or a synthetic fixture)."""
    for session_path in sorted(glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))):
        project_dir = os.path.basename(os.path.dirname(session_path))
        session_id = os.path.splitext(os.path.basename(session_path))[0]
        yield ("main", session_path, project_dir, session_id, None)
    for agent_path in sorted(glob.glob(os.path.join(projects_dir, "*", "*", "subagents", "agent-*.jsonl"))):
        session_dir = os.path.dirname(os.path.dirname(agent_path))
        session_id = os.path.basename(session_dir)
        project_dir = os.path.basename(os.path.dirname(session_dir))
        agent_id = os.path.basename(agent_path)[len("agent-"):-len(".jsonl")]
        yield ("subagent", agent_path, project_dir, session_id, agent_id)


def contexts_for_transcript(path):
    """--transcript mode: a single named transcript. A main session file also pulls in its
    subagents/. An agent-*.jsonl path is treated as a single subagent context."""
    path = os.path.abspath(path)
    base = os.path.basename(path)
    if base.startswith("agent-"):
        # .../<project>/<session>/subagents/agent-X.jsonl
        session_dir = os.path.dirname(os.path.dirname(path))
        session_id = os.path.basename(session_dir)
        project_dir = os.path.basename(os.path.dirname(session_dir))
        agent_id = base[len("agent-"):-len(".jsonl")]
        return [("subagent", path, project_dir, session_id, agent_id)]
    project_dir = os.path.basename(os.path.dirname(path))
    session_id = os.path.splitext(base)[0]
    out = [("main", path, project_dir, session_id, None)]
    subdir = os.path.join(os.path.dirname(path), session_id, "subagents")
    for agent_path in sorted(glob.glob(os.path.join(subdir, "agent-*.jsonl"))):
        agent_id = os.path.basename(agent_path)[len("agent-"):-len(".jsonl")]
        out.append(("subagent", agent_path, project_dir, session_id, agent_id))
    return out


def agent_type_of(path, kind):
    if kind == "main":
        return "main"
    meta_path = path[:-len(".jsonl")] + ".meta.json"
    try:
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f).get("agentType") or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# per-context load
# ---------------------------------------------------------------------------

class Context:
    def __init__(self, kind, path, project_dir, session_id, agent_id):
        self.kind = kind
        self.path = path
        self.project_dir = project_dir
        self.session_id = session_id
        self.agent_id = agent_id
        self.agent_type = agent_type_of(path, kind)
        self.turns = []       # list of dict(ts, ctx, usage, model, n_tools, tools)
        self.events = []      # list of (turn_index, category, chars) turn_index = len(turns) at event time
        self.versions = set()
        self.start = None     # fixed-start composition dict, filled from attachments before first turn


def load_context(kind, path, project_dir, session_id, agent_id):
    ctx = Context(kind, path, project_dir, session_id, agent_id)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None

    seen_mid = set()
    mid_index = {}         # message.id -> index into ctx.turns
    pending = {}          # tool_use_id -> category
    first_turn_seen = False
    start = dict(instructions=[], deferred_counts={}, deferred_chars={}, deferred_nonmcp_count=0,
                 deferred_nonmcp_chars=0, mcp_instr={}, skill_chars=0, hook_chars=0, first_prompt_chars=0)
    deferred_active = collections.defaultdict(set)   # server -> set(tool names); "" = non-mcp
    mcp_active = {}        # mcp server display name -> instructions text currently attached

    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        v = e.get("version")
        if v:
            ctx.versions.add(v)
        etype = e.get("type")
        m = e.get("message") or {}

        if etype == "attachment" and not first_turn_seen:
            a = e.get("attachment") or {}
            at = a.get("type")
            if at == "instructions":
                for f in a.get("files") or []:
                    start["instructions"].append((f.get("path", ""), len(f.get("content", "") or "")))
            elif at == "deferred_tools_delta":
                for name in a.get("addedNames") or []:
                    server = name.split("__")[1] if name.startswith("mcp__") else ""
                    deferred_active[server].add(name)
                for name in a.get("readdedNames") or []:
                    server = name.split("__")[1] if name.startswith("mcp__") else ""
                    deferred_active[server].add(name)
                for name in a.get("removedNames") or []:
                    server = name.split("__")[1] if name.startswith("mcp__") else ""
                    deferred_active[server].discard(name)
            elif at == "mcp_instructions_delta":
                for name, block in zip(a.get("addedNames") or [], a.get("addedBlocks") or []):
                    mcp_active[name] = block or ""
                for name in a.get("removedNames") or []:
                    mcp_active.pop(name, None)
            elif at == "skill_listing":
                start["skill_chars"] = len(a.get("content", "") or "")
            elif at == "hook_additional_context":
                for block in a.get("content") or []:
                    if isinstance(block, dict):
                        start["hook_chars"] += len(block.get("content", "") or "")
                    elif isinstance(block, str):
                        start["hook_chars"] += len(block)

        if etype == "user" and not first_turn_seen:
            c = m.get("content")
            txt = text_of(c) if not isinstance(c, str) else c
            if txt and start["first_prompt_chars"] == 0:
                start["first_prompt_chars"] = len(txt)

        if etype == "assistant":
            u = m.get("usage") or {}
            mid = m.get("id")
            if mid and mid not in seen_mid and u:
                seen_mid.add(mid)
                ts = e.get("timestamp")
                if not ts:
                    continue
                if not first_turn_seen:
                    first_turn_seen = True
                    start["deferred_counts"] = {s: len(names) for s, names in deferred_active.items() if s}
                    start["deferred_chars"] = {s: sum(len(n) for n in names)
                                                for s, names in deferred_active.items() if s}
                    nonmcp = deferred_active.get("", set())
                    start["deferred_nonmcp_count"] = len(nonmcp)
                    start["deferred_nonmcp_chars"] = sum(len(n) for n in nonmcp)
                    start["mcp_instr"] = {name: len(text) for name, text in mcp_active.items()}
                    ctx.start = start
                mid_index[mid] = len(ctx.turns)
                ctx.turns.append(dict(ts=parse_ts(ts), ctx=context_size(u), usage=u,
                                       model=m.get("model", "?"), n_tools=0, tools=collections.Counter()))
            elif mid and mid in mid_index and u:
                # Streaming writes the same message on several lines. input/cache tokens are fixed for
                # the turn, but output_tokens accumulates across an agentic-loop message's blocks, so
                # a later line's usage is the more complete one — keep the latest, not the first.
                turn = ctx.turns[mid_index[mid]]
                turn["usage"] = u
                turn["ctx"] = context_size(u)
            for b in m.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    cat = classify(b.get("name", ""), b.get("input") or {})
                    tid = b.get("id")
                    if tid and tid not in pending:
                        pending[tid] = cat
                        if ctx.turns:
                            ctx.turns[-1]["n_tools"] += 1
                            ctx.turns[-1]["tools"][b.get("name") or "?"] += 1
                    ctx.events.append((len(ctx.turns), "assistant: tool-call inputs (edits, commands)", len(json.dumps(b.get("input")))))
                elif b.get("type") == "text":
                    ctx.events.append((len(ctx.turns), "assistant: text", len(b.get("text", ""))))
        elif etype == "user":
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        cat = pending.get(b.get("tool_use_id"), "?")
                        ctx.events.append((len(ctx.turns), cat, len(text_of(b.get("content")))))
                    elif isinstance(b, dict) and b.get("type") == "text":
                        ctx.events.append((len(ctx.turns), "user/system text (prompt, reminders)", len(b.get("text", ""))))
            elif isinstance(c, str) and first_turn_seen:
                ctx.events.append((len(ctx.turns), "user/system text (prompt, reminders)", len(c)))

    return ctx


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def median(xs):
    xs = list(xs)
    return statistics.median(xs) if xs else 0


def fmt_tok(n):
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.0f}k"
    return f"{n:.0f}"


class Loaded:
    """A Context plus the turns that fall in [since, until)."""
    def __init__(self, ctx, since, until):
        self.ctx = ctx
        self.window_turns = [t for t in ctx.turns if since <= t["ts"] < until]
        self.first_in_window = bool(ctx.turns) and since <= ctx.turns[0]["ts"] < until
        for t in self.window_turns:
            t["ie"] = input_equivalent(t["usage"])


def load_all(projects_dir, transcript, since, until):
    if transcript:
        specs = contexts_for_transcript(transcript)  # window ignored in --transcript mode
    else:
        specs = None
    loaded = []
    if specs is not None:
        for kind, path, project_dir, session_id, agent_id in specs:
            ctx = load_context(kind, path, project_dir, session_id, agent_id)
            if ctx is None or not ctx.turns:
                continue
            lc = Loaded(ctx, ctx.turns[0]["ts"] - timedelta(days=1), ctx.turns[-1]["ts"] + timedelta(days=1))
            loaded.append(lc)
        return loaded
    for kind, path, project_dir, session_id, agent_id in iter_context_files(projects_dir):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=since.tzinfo)
        except OSError:
            continue
        if mtime < since:
            continue
        ctx = load_context(kind, path, project_dir, session_id, agent_id)
        if ctx is None or not ctx.turns:
            continue
        lc = Loaded(ctx, since, until)
        if not lc.window_turns:
            continue
        loaded.append(lc)
    return loaded


def section_totals(out, loaded):
    out.append("=== Totals ===")
    for label, kind in (("main", "main"), ("subagent", "subagent")):
        group = [l for l in loaded if l.ctx.kind == kind]
        n_ctx = len(group)
        n_turns = sum(len(l.window_turns) for l in group)
        raw = sum(t["ctx"] for l in group for t in l.window_turns)
        ie = sum(t["ie"] for l in group for t in l.window_turns)
        outp = sum(t["usage"].get("output_tokens", 0) for l in group for t in l.window_turns)
        out.append(f"  {label:10} contexts {n_ctx:6}  turns {n_turns:7}  raw input {fmt_tok(raw):>8}  input-eq {fmt_tok(ie):>8}  output {fmt_tok(outp):>8}")

    out.append("")
    out.append("  by model:")
    by_model = collections.defaultdict(list)
    for l in loaded:
        if l.window_turns:
            model = l.window_turns[0]["model"]
            by_model[model].append(l)
    for model, group in sorted(by_model.items(), key=lambda kv: -sum(t["ie"] for l in kv[1] for t in l.window_turns)):
        ie = sum(t["ie"] for l in group for t in l.window_turns)
        starts = [l.ctx.turns[0]["ctx"] for l in group if l.first_in_window]
        out.append(f"    {model:24} contexts {len(group):6}  input-eq {fmt_tok(ie):>8}  median start {fmt_tok(median(starts)):>8}")
    out.append("")


def section_per_day(out, loaded):
    out.append("=== Per day (local day of the turn) ===")
    by_day = collections.defaultdict(list)          # day -> list of (context, turn)
    ctx_started_day = collections.defaultdict(list)  # day -> list of starting-context sizes
    for l in loaded:
        if l.first_in_window:
            day = l.ctx.turns[0]["ts"].astimezone().date()
            ctx_started_day[day].append(l.ctx.turns[0]["ctx"])
        for t in l.window_turns:
            day = t["ts"].astimezone().date()
            by_day[day].append((l, t))
    out.append(f"  {'day':12} {'contexts':>9} {'median start':>13} {'median turns':>13} {'input-eq':>9} {'top10% share':>13}")
    for day in sorted(by_day):
        entries = by_day[day]
        ctxs = {id(l): l for l, t in entries}
        per_ctx_turns = collections.Counter(id(l) for l, t in entries)
        per_ctx_ie = collections.Counter()
        for l, t in entries:
            per_ctx_ie[id(l)] += t["ie"]
        day_ie = sum(per_ctx_ie.values())
        ranked = sorted(per_ctx_ie.values(), reverse=True)
        top_n = max(1, len(ranked) // 10)
        top_share = (sum(ranked[:top_n]) / day_ie * 100) if day_ie else 0.0
        out.append(f"  {day.isoformat():12} {len(ctxs):9} {fmt_tok(median(ctx_started_day.get(day, []))):>13} "
                    f"{median(per_ctx_turns.values()):13.0f} {fmt_tok(day_ie):>9} {top_share:12.1f}%")
    out.append("")


def section_concentration(out, loaded):
    out.append("=== Concentration ===")
    per_ctx = []
    for l in loaded:
        ie = sum(t["ie"] for t in l.window_turns)
        per_ctx.append((ie, len(l.window_turns)))
    per_ctx.sort(key=lambda p: -p[0])
    total_ie = sum(p[0] for p in per_ctx) or 1
    top_n = max(1, len(per_ctx) // 10)
    top = per_ctx[:top_n]
    top_ie = sum(p[0] for p in top)
    out.append(f"  top 10% of contexts ({top_n} of {len(per_ctx)}): {100*top_ie/total_ie:.1f}% of input-eq spend, median turns {median(p[1] for p in top):.0f}")
    under50_ie = sum(p[0] for p in per_ctx if p[1] < 50)
    out.append(f"  contexts under 50 turns: {100*under50_ie/total_ie:.1f}% of input-eq spend")
    out.append("")


def section_turn_shape(out, loaded):
    out.append("=== Turn shape ===")
    for label, kind in (("main", "main"), ("subagent", "subagent"), ("combined", None)):
        turns = [t for l in loaded for t in l.window_turns if kind is None or l.ctx.kind == kind]
        n = len(turns) or 1
        total_ie = sum(t["ie"] for t in turns) or 1
        one_tool = [t for t in turns if t["n_tools"] == 1]
        ie_one = sum(t["ie"] for t in one_tool)
        out.append(f"  {label:10} turns carrying exactly one tool call: {100*len(one_tool)/n:5.1f}% of turns, {100*ie_one/total_ie:5.1f}% of input-eq spend")
    out.append("")


def section_fills_context(out, loaded):
    out.append("=== What fills the context ===")
    agg = collections.Counter()
    reread = collections.Counter()
    calls = collections.Counter()
    for l in loaded:
        wt = l.window_turns
        if len(wt) < 2:
            continue
        orig_index = {id(t): i for i, t in enumerate(l.ctx.turns)}
        included = {orig_index[id(t)] for t in wt if id(t) in orig_index}
        idx_map = {orig: new for new, orig in enumerate(sorted(included))}
        base = wt[0]["ctx"]
        last = wt[-1]["ctx"]
        n = len(wt)
        later = [(idx_map[ti], cat, ch) for ti, cat, ch in l.ctx.events if ti in idx_map and idx_map[ti] >= 1]
        chars = sum(ch for _, _, ch in later) or 1
        ratio = max(last - base, 1) / chars
        # A context whose first *in-window* turn is not its true first turn already carries whatever
        # history came before the window — that isn't "system prompt", it's prior work. Book it
        # separately so it doesn't masquerade as fixed start.
        base_label = ("(base) system prompt + tools + first prompt" if l.first_in_window
                      else "(carried in) context accumulated before the window")
        agg[base_label] += base
        reread[base_label] += base * (n - 1)
        for ti, cat, ch in later:
            tok = ch * ratio
            agg[cat] += tok
            # An event recorded after `ti` turns is first sent in turn index `ti` (0-based), so it is
            # re-sent on every later turn *after* that one: n - ti - 1 times, floored at 0.
            reread[cat] += tok * max(n - ti - 1, 0)
            calls[cat] += 1
    tot_res = sum(agg.values()) or 1
    tot_rr = sum(reread.values()) or 1
    out.append(f"  {'category':52} {'calls':>6} {'resident':>9} {'%':>5} {'re-sent':>10} {'%':>5}")
    for cat, v in sorted(reread.items(), key=lambda kv: -kv[1]):
        out.append(f"  {cat[:52]:52} {calls.get(cat,0):6} {fmt_tok(agg[cat]):>9} {100*agg[cat]/tot_res:5.1f} {fmt_tok(v):>10} {100*v/tot_rr:5.1f}")
    out.append(f"  {'TOTAL':52} {'':6} {fmt_tok(tot_res):>9} {'':5} {fmt_tok(tot_rr):>10}")
    out.append("")


def context_estimate_total(start):
    """Sum of chars/4 over everything itemised for one context's fixed start: instructions files,
    deferred-tool names (per MCP server, plus non-MCP), MCP instructions text (per server),
    skill listing, hook context, first user prompt."""
    chars = (sum(c for _, c in start["instructions"]) + sum(start["deferred_chars"].values()) +
             start["deferred_nonmcp_chars"] + sum(start["mcp_instr"].values()) +
             start["skill_chars"] + start["hook_chars"] + start["first_prompt_chars"])
    return chars / 4


def context_remainder(l):
    """A single context's own remainder: its own first-turn context minus its own itemised estimate
    (never another context's, and never a blend across contexts)."""
    return l.ctx.turns[0]["ctx"] - context_estimate_total(l.ctx.start)


def section_fixed_start(out, loaded):
    out.append("=== Fixed start ===")
    starters = [l for l in loaded if l.first_in_window and l.ctx.start is not None]
    out.append("  median first-turn context and median remainder by model / agent type:")
    by_key = collections.defaultdict(list)
    for l in starters:
        key = (l.ctx.turns[0]["model"], l.ctx.agent_type)
        by_key[key].append(l)
    for (model, atype), group in sorted(by_key.items(),
                                         key=lambda kv: -median(l.ctx.turns[0]["ctx"] for l in kv[1])):
        starts = [l.ctx.turns[0]["ctx"] for l in group]
        rems = [context_remainder(l) for l in group]
        out.append(f"    {model:22} {atype:18} n={len(group):4}  median start {fmt_tok(median(starts)):>8}"
                    f"  median remainder {fmt_tok(median(rems)):>8}")
    out.append("")
    out.append("  composition (median chars, and ≈tokens at chars/4, over the contexts that had each item):")

    instr_by_path = collections.defaultdict(list)
    deferred_counts_by_server = collections.defaultdict(list)
    deferred_chars_by_server = collections.defaultdict(list)
    deferred_nonmcp_chars = []
    mcp_instr_by_server = collections.defaultdict(list)
    skill_chars = []
    hook_chars = []
    prompt_chars = []
    first_ctx_tokens = []
    remainders = []
    for l in starters:
        s = l.ctx.start
        for path, chars in s["instructions"]:
            instr_by_path[path].append(chars)
        for server, count in s["deferred_counts"].items():
            deferred_counts_by_server[server].append(count)
            deferred_chars_by_server[server].append(s["deferred_chars"][server])
        if s["deferred_nonmcp_chars"]:
            deferred_nonmcp_chars.append(s["deferred_nonmcp_chars"])
        for server, chars in s["mcp_instr"].items():
            mcp_instr_by_server[server].append(chars)
        if s["skill_chars"]:
            skill_chars.append(s["skill_chars"])
        if s["hook_chars"]:
            hook_chars.append(s["hook_chars"])
        if s["first_prompt_chars"]:
            prompt_chars.append(s["first_prompt_chars"])
        first_ctx_tokens.append(l.ctx.turns[0]["ctx"])
        remainders.append(context_remainder(l))

    out.append("  instructions per file:")
    for path, chs in sorted(instr_by_path.items(), key=lambda kv: -median(kv[1])):
        m = median(chs)
        out.append(f"    {display_path(path):70} {m:7.0f} chars  ≈{m/4:6.0f} tok  (n={len(chs)})")

    out.append("  deferred tools:")
    for server, counts in sorted(deferred_counts_by_server.items(), key=lambda kv: -median(kv[1])):
        mc = median(deferred_chars_by_server[server])
        out.append(f"    mcp__{server:20} {median(counts):5.0f} tools  ≈{mc:6.0f} chars  ≈{mc/4:5.0f} tok"
                    f"  (n={len(counts)})")
    if deferred_nonmcp_chars:
        mc = median(deferred_nonmcp_chars)
        out.append(f"    (non-MCP built-in)          ≈{mc:6.0f} chars  ≈{mc/4:5.0f} tok  (n={len(deferred_nonmcp_chars)})")

    out.append("  MCP instructions:")
    for server, chs in sorted(mcp_instr_by_server.items(), key=lambda kv: -median(kv[1])):
        m = median(chs)
        out.append(f"    {server:26} {m:7.0f} chars  ≈{m/4:6.0f} tok  (n={len(chs)})")

    m_skill = median(skill_chars)
    m_hook = median(hook_chars)
    m_prompt = median(prompt_chars)
    out.append(f"  skill listing:           {m_skill:7.0f} chars  ≈{m_skill/4:6.0f} tok  (n={len(skill_chars)})")
    out.append(f"  hook additional context: {m_hook:7.0f} chars  ≈{m_hook/4:6.0f} tok  (n={len(hook_chars)})")
    out.append(f"  handoff / first prompt:  {m_prompt:7.0f} chars  ≈{m_prompt/4:6.0f} tok  (n={len(prompt_chars)})")

    m_first_ctx = median(first_ctx_tokens)
    m_remainder = median(remainders)
    out.append(f"  remainder: harness system prompt, built-in tool schemas and anything not itemised:"
                f" ≈{m_remainder:.0f} tok (median, n={len(remainders)})")
    out.append(f"  median first-turn context across these contexts: {fmt_tok(m_first_ctx)}")
    out.append("")


def section_largest(out, loaded, top_n):
    out.append(f"=== Largest contexts (top {top_n} by input-eq) ===")
    rows = []
    for l in loaded:
        ie = sum(t["ie"] for t in l.window_turns)
        if not l.window_turns:
            continue
        peak = max(t["ctx"] for t in l.window_turns)
        start = l.window_turns[0]["ctx"]
        model = l.window_turns[0]["model"]
        rows.append((ie, l, peak, start, model))
    rows.sort(key=lambda r: -r[0])
    out.append(f"  {'project':30} {'session':9} {'agent':17} {'type':14} {'model':16} {'turns':>6} {'start':>8} {'peak':>8} {'input-eq':>9}")
    for ie, l, peak, start, model in rows[:top_n]:
        proj = project_display_name(l.ctx.project_dir)[:30]
        sess = l.ctx.session_id[:8]
        agent = (l.ctx.agent_id or "main")[:17]
        out.append(f"  {proj:30} {sess:9} {agent:17} {l.ctx.agent_type[:14]:14} {model[:16]:16} {len(l.window_turns):6} {fmt_tok(start):>8} {fmt_tok(peak):>8} {fmt_tok(ie):>9}")
    out.append("")


def section_tools(out, loaded, per_type=25):
    """What each agent type actually called in the window — the evidence for an agent definition's `tools:` list."""
    out.append("=== Tools called (by agent type) ===")
    by_type = collections.defaultdict(list)
    for l in loaded:
        by_type[l.ctx.agent_type].append(l)
    for agent_type in sorted(by_type):
        contexts = by_type[agent_type]
        calls = collections.Counter()
        for l in contexts:
            for t in l.window_turns:
                calls.update(t["tools"])
        out.append(f"  {agent_type:24} n={len(contexts):4} contexts")
        if not calls:
            out.append("    (no tool calls)")
            continue
        named = ", ".join(f"{name} {n}" for name, n in calls.most_common(per_type))
        for line in textwrap.wrap(named, width=110, initial_indent="    ", subsequent_indent="    "):
            out.append(line)
        if len(calls) > per_type:
            out.append(f"    … and {len(calls) - per_type} more")
    out.append("  (an agent definition's `tools:` list is built from what its type calls here, plus what the")
    out.append("   jobs you plan to give it will need. A tool a type never calls is context re-sent every turn.)")
    out.append("")


def build_report(loaded, top_n, tools=False):
    out = []
    section_totals(out, loaded)
    section_per_day(out, loaded)
    section_concentration(out, loaded)
    section_turn_shape(out, loaded)
    section_fills_context(out, loaded)
    section_fixed_start(out, loaded)
    section_largest(out, loaded, top_n)
    if tools:
        section_tools(out, loaded)
    versions = sorted({v for l in loaded for v in l.ctx.versions})
    out.append(f"harness versions in this window: {', '.join(versions) if versions else '(none found)'}")
    out.append("note: input-eq is a price comparison against the uncached input rate, not a token count.")
    return "\n".join(out)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # an ASCII-only terminal shows ? for the report's glyphs, not a crash
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", default="7d")
    p.add_argument("--until", default=None)
    p.add_argument("--projects", default=default_projects_dir())
    p.add_argument("--transcript", default=None)
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--tools", action="store_true",
                   help="add a section listing the tools each agent type actually called")
    args = p.parse_args(argv)

    now = datetime.now().astimezone()
    since = parse_when(args.since, now)
    until = parse_when(args.until, now) if args.until else now

    loaded = load_all(args.projects, args.transcript, since, until)
    if not any(l.window_turns for l in loaded):
        where = args.transcript or args.projects
        print(f"no turns between {since:%Y-%m-%d %H:%M} and {until:%Y-%m-%d %H:%M} under {where}: "
              "nothing to report (check --projects, --since and --until)")
        return
    print(build_report(loaded, args.top, tools=args.tools))


if __name__ == "__main__":
    main()
