#!/usr/bin/env python3
"""Read-only upkeep checks: size budget, template drift, disk hygiene.

Usage: python3 checks.py size|drift|hygiene [--threshold BYTES] [--cwd DIR]

Reports only. It never writes a file or changes a repository, it makes no network request, and it
prints file paths, sizes and git ref names, never the contents of a settings file. Where it proposes
a command, every name in it is quoted with shlex.quote and is for the user to read before running:
nothing here runs one.
"""
import argparse
import os
import re
import shlex
import subprocess
import sys

DEFAULT_THRESHOLD = 4096
INDEX_LINES = 20
STAMP = re.compile(r"managed by[:\s]+`?([^\s`>*]+)", re.IGNORECASE)
INDEX_MARK = re.compile(r"read this when|^#+\s*index\b|^index\b", re.IGNORECASE | re.MULTILINE)


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def listing(directory, suffix, recursive=False):
    """Files in the directory ending in the suffix, sorted; rules are found in subdirectories too."""
    if recursive:
        return sorted(
            os.path.join(top, n) for top, _, names in os.walk(directory) for n in names if n.endswith(suffix)
        )
    try:
        return sorted(
            os.path.join(directory, n) for n in os.listdir(directory) if n.endswith(suffix)
        )
    except OSError:
        return []


def guides(cwd):
    """(path, kind) for every rule, CLAUDE.md and skill file in the user's and the project's config."""
    found = []
    for base in (config_dir(), os.path.join(cwd, ".claude")):
        found += [(p, "rule") for p in listing(os.path.join(base, "rules"), ".md", recursive=True)]
        skills = os.path.join(base, "skills")
        try:
            names = sorted(os.listdir(skills))
        except OSError:
            names = []
        found += [(os.path.join(skills, n, "SKILL.md"), "skill") for n in names]
    for base in (config_dir(), cwd, os.path.join(cwd, ".claude")):
        for name in ("CLAUDE.md", "CLAUDE.local.md"):
            found.append((os.path.join(base, name), "claude-md"))
    return [(p, k) for p, k in found if os.path.isfile(p)]


def has_paths_frontmatter(text):
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    return end != -1 and re.search(r"^paths\s*:", text[:end], re.MULTILINE) is not None


def size_budget(cwd, threshold):
    rows = []
    for path, kind in guides(cwd):
        text = read(path)
        if text is None or len(text.encode("utf-8")) <= threshold:
            continue
        if kind == "rule" and has_paths_frontmatter(text):
            continue
        if INDEX_MARK.search("\n".join(text.splitlines()[:INDEX_LINES])):
            continue
        size = len(text.encode("utf-8"))
        advice = "add an index block" + ("; add `paths:` frontmatter" if kind == "rule" else "")
        advice += "; or move detail to a linked file or skill"
        rows.append(f"{size / 1024:.1f} KB  ~{len(text) // 4} tokens  {path}\n    propose: {advice}")
    return rows or ["size budget: nothing over the threshold lacks paths or an index"]


def drift(cwd):
    rows = []
    for path, _ in guides(cwd):
        text = read(path)
        if text is None:
            continue
        head = "\n".join(text.splitlines()[:5])
        match = STAMP.search(head)
        if not match:
            continue
        source = os.path.expanduser(match.group(1))
        if not os.path.isabs(source):
            source = os.path.join(os.path.dirname(path), source)
        origin = read(source)
        if origin is None:
            rows.append(f"{path}\n    source named in its stamp is missing")
            continue
        body = lambda t: [l for l in t.splitlines() if not STAMP.search(l)]
        if body(text) != body(origin):
            rows.append(f"{path}\n    differs from its source {source}")
        elif os.path.getmtime(source) > os.path.getmtime(path):
            rows.append(f"{path}\n    same text, but its source {source} is newer")
    return rows or ["template drift: no stamped guide differs from its source"]


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        done = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, env=env, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done


def default_branch(root):
    ref = git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref and ref.returncode == 0 and ref.stdout.strip():
        return ref.stdout.strip()
    for name in ("main", "master"):
        done = git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
        if done and done.returncode == 0:
            return name
    return None


def merged(root, branch, base):
    """True when the branch is an ancestor of base and has commits of its own: a branch whose tip is
    the base's tip is also an ancestor, and is usually someone's work that has not started yet."""
    tips = [git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{r}" if r == branch else r) for r in (branch, base)]
    if all(t and t.returncode == 0 for t in tips) and tips[0].stdout == tips[1].stdout:
        return False
    done = git(root, "merge-base", "--is-ancestor", branch, base)
    return bool(done and done.returncode == 0)


def worktrees(root):
    done = git(root, "worktree", "list", "--porcelain")
    entries, current = [], {}
    for line in (done.stdout if done and done.returncode == 0 else "").splitlines():
        if not line:
            if current:
                entries.append(current)
            current = {}
        else:
            key, _, value = line.partition(" ")
            current[key] = value
    if current:
        entries.append(current)
    return entries


MEMORY_TOKEN = re.compile(r"`([^`\n]{2,120})`")
PATH_LIKE = re.compile(r"^[~/.\w][\w./~+-]*$")
CODE_EXT = (".swift", ".py", ".js", ".ts", ".md", ".json", ".sh", ".yml", ".yaml", ".rs", ".go", ".toml")


def stale_memory(root):
    common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    main = os.path.dirname(common.stdout.strip()) if common and common.returncode == 0 and common.stdout.strip() else root
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(main))
    rows = []
    for path in listing(os.path.join(config_dir(), "projects", slug, "memory"), ".md"):
        text = read(path) or ""
        gone = []
        for token in dict.fromkeys(MEMORY_TOKEN.findall(text)):
            if token.endswith("()") and re.fullmatch(r"[A-Za-z_]\w{3,}\(\)", token):
                found = git(root, "grep", "-qF", "--", token[:-2])
                if found and found.returncode == 1:
                    gone.append(token)
            elif PATH_LIKE.match(token) and ("/" in token or token.endswith(CODE_EXT)):
                if token.startswith(("http", "-")) or "*" in token:
                    continue
                full = os.path.expanduser(token)
                if not os.path.isabs(full):
                    full = os.path.join(root, full)
                if not os.path.exists(full):
                    gone.append(token)
        if gone:
            rows.append(f"{path}\n    names that no longer exist here: " + ", ".join(gone))
    return rows


def hygiene(cwd):
    top = git(cwd, "rev-parse", "--show-toplevel")
    if not top or top.returncode != 0:
        return ["disk hygiene: not inside a git repository"]
    root = top.stdout.strip()
    base = default_branch(root)
    rows = []
    checked_out = set()
    if base is None:
        rows.append("disk hygiene: no main, master or origin/HEAD to compare against; branches skipped")
    for entry in worktrees(root):
        branch = entry.get("branch", "").removeprefix("refs/heads/")
        checked_out.add(branch)
        where = entry.get("worktree", "")
        if os.path.realpath(where) == os.path.realpath(root) or not re.search(r"/(\.build|\.claude/worktrees)/", where + "/"):
            continue
        if "locked" in entry:
            continue
        if base and branch and merged(root, branch, base):
            rows.append(
                f"worktree {where} (branch {branch}) is merged into {base}\n"
                f"    propose: git -C {shlex.quote(root)} worktree remove {shlex.quote(where)} && "
                f"git -C {shlex.quote(root)} branch -d {shlex.quote(branch)}"
            )
    if base:
        heads = git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        current = git(root, "branch", "--show-current")
        skip = checked_out | {base.removeprefix("origin/"), (current.stdout.strip() if current else "")}
        for branch in (heads.stdout.split() if heads and heads.returncode == 0 else []):
            if branch not in skip and merged(root, branch, base):
                rows.append(
                    f"local branch {branch} is merged into {base}\n"
                    f"    propose: git -C {shlex.quote(root)} branch -d {shlex.quote(branch)}"
                )
    rows += stale_memory(root)
    rows.append("(not detected: squash-merged branches, and a branch whose tip is still the default branch's tip)")
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=("size", "drift", "hygiene"))
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--cwd", default=os.getcwd())
    args = parser.parse_args()
    cwd = os.path.abspath(args.cwd)
    rows = {"size": lambda: size_budget(cwd, args.threshold), "drift": lambda: drift(cwd), "hygiene": lambda: hygiene(cwd)}[args.check]()
    print("\n".join(rows))


if __name__ == "__main__":
    sys.exit(main())
