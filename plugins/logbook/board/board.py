#!/usr/bin/env python3
"""The recorder: an append-only event log in, a state object and the files a static page reads out.

Hooks and the model append events; nothing edits one in place. Everything the page shows is derived
from the whole log each time it is rendered, which is what lets several writers share one board
without coordinating: an append is one `os.write` of one complete line to a file opened with
`O_APPEND`, so two hooks firing together cannot interleave, and the short ids the page shows (`Q1`,
`D1`, `C1`) are handed out by position in the log when it is read, never stored, so two writers can
never hand out the same one.

The log holds untrusted text: prompts, commit subjects, agent reports. So reading it never raises (a
line that does not parse, or an event of a kind this version does not know, is skipped) and the
state is escaped before it is written inside a `<script>` element, so no value can close the element
or open a comment.

Commits are read from `git` at render time: the `start` event records every local branch and its
head, and the commits that count are the ones made since then on the branch the board started on, on
the branch checked out when it renders, and on branches created after it started. Other sessions'
older branches are left out, without comparing a single date. A commit the session made before its
board started is behind those heads, so the one event about commits is for it: a `commit`, read back
from the transcript with the hash the session's own `git commit` printed. The state adds each one git
confirms is a commit on some local branch, with its subject and time read from git. A commit the
board has shown stays on it, so work merged into another branch and deleted is still in the report.

Files changed and commands run are events the hooks record from each work call. The state keeps one
row per file and one per distinct command text, and decides which commands are test runs when it is
derived.

Every file the page reads is written to a temporary file in the same folder and renamed into place,
so the page never reads a half-written state.

The model records what only it knows (a question, a decision, a check) with one short command per
entry, which prints one short line back: `board.py question "TEXT" --default "D"` prints `Q3
recorded`. Without `--board`, the command finds the session's board from `CLAUDE_CODE_SESSION_ID` by
walking up from the current directory.

Next-session mode keeps one standing brief per project for whichever session picks the work up next:
`next on [PATH]` writes the setting, `<project>/.logbook/next-session.json`, naming the brief relative
to the project (`NEXT_SESSION.md` by default); `next off` removes it and leaves the brief alone. Each
session that finishes work rewrites the brief whole; a session start names it and when it was written
(`next_session_context`), never what it holds. `next_session(project)` is the setting as Python reads it.

`<project>/.logbook/index.html` lists the boards, newest first. It is rebuilt from each board
folder's own `state.js` whenever a board starts, changes title or state, or closes, and by a render
that finds the index does not list its board.
"""
import argparse
import html
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

SCHEMA = 1
BOARDS_DIR = ".logbook"
MARKER_FILE = ".logbook"
EVENTS_FILE = "events.jsonl"
STATE_FILE = "state.js"
BOARD_FILE = "board.html"
REPORT_FILE = "report.html"
INDEX_FILE = "index.html"
ANNOUNCED_FILE = "announced"
CATCH_UP_FILE = "catch-up"
# The next-session setting, in the boards folder, and the brief's path when the setting names none.
NEXT_SESSION_FILE = "next-session.json"
DEFAULT_BRIEF = "NEXT_SESSION.md"
# Every file a board folder holds, by exact name: all that pruning ever deletes, the marker last.
BOARD_FILES = (ANNOUNCED_FILE, CATCH_UP_FILE, EVENTS_FILE, STATE_FILE, BOARD_FILE, REPORT_FILE, MARKER_FILE)
# The temporary file `write_atomic` makes beside a board file: `tempfile.mkstemp` puts eight characters
# from this set between the prefix and the suffix. A name that is not exactly this is left alone.
TEMPORARY_FILE = re.compile(
    r"\.(?:" + "|".join(re.escape(n) for n in (STATE_FILE, BOARD_FILE, REPORT_FILE, CATCH_UP_FILE)) + r")\.[a-z0-9_]{8}\.tmp"
)
UNTITLED = "Untitled task"
DEFAULT_RETENTION_DAYS = 14
DIGITS = re.compile(r"[0-9]+")
TEMPLATE_MARKER = "<!-- logbook:state -->"
DEFAULT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")

GIT_TIMEOUT_SECONDS = 5
# How many times one render reads the log and writes the state when the log keeps growing meanwhile.
RENDER_PASSES = 3
REFRESH_SECONDS = 10
DEFAULT_STUCK_MINUTES = 10
THEMES = ("system", "light", "dark")
DENSITIES = ("comfortable", "compact")
ACCENT = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
UTC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
SESSION = re.compile(r"[A-Za-z0-9._-]+")
SESSION_VARIABLE = "CLAUDE_CODE_SESSION_ID"
# What a board being built carries in its folder name before it is published: never a session id.
STARTING = ".starting-"
# Who can close a board, as a `close` event's `by`: the `SessionEnd` hook, or the `close` command.
CLOSERS = ("session-end", "hand")
# Why a command recorded nothing on a board whose boards folder is a symbolic link.
LINKED_BOARDS = "the boards folder is a link, so nothing was recorded"
# The events that say what a step is. One recovered from the transcript carries `early`.
STEP_KINDS = ("step", "step-status", "steps-replace")
# The events a work call records: a file changed, a command run. Recovered the same way, with `early`.
WORK_KINDS = ("change", "command")
# The event an `Agent` call records: the subagent's model and description. Recovered the same way.
AGENT_KINDS = ("agent-info",)
# The event a command read back from the transcript records for each commit git said it made. Only
# ever recovered, with `early`; it changes nothing `derive` builds, and adds to the commits instead.
COMMIT_KINDS = ("commit",)
# A commit's hash as git prints it after making one: 7 to 40 lowercase hex digits.
COMMIT_HASH = re.compile(r"[0-9a-f]{7,40}")
# The most of those commits a render asks git about, the latest named: each one is a call to git, and
# a render has 200 ms. One command can name many (a range picked onto a branch).
EARLY_COMMITS = 10
COMMAND_RESULTS = ("pass", "fail", "background")
# The most command rows the state carries: those run most recently. `commandsTotal` counts every run.
COMMAND_ROWS = 200
# The folder under `CLAUDE_PLUGIN_DATA` where the gate counts each session's work calls, one byte a call.
CALLS_DIR = "calls"
# Set to 1 to use a project or data folder outside the home and temporary folders (see `contained`).
ALLOW_ANY_PATH = "LOGBOOK_ALLOW_ANY_PATH"
# A command that runs a common test runner. `LOGBOOK_TESTS` adds more. The runner has to be
# the command and not one of its arguments (`pip install pytest` installs, it does not test), so it
# is looked for where a command can begin: at the start, or after `;`, `&`, `|`, `(` or a new line,
# past any variable assignments and the words that run another command, and past a path to it.
RUNNER = (
    r"(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S*|sudo|time|env|exec|nice|then|do|else|!|npx"
    r"|(?:uv|poetry|pipenv|hatch|pdm)\s+run|(?:bundle|pnpm|yarn)\s+exec)\s+)*"
    r"(?:[\w.~/-]*/)?(?:"
    r"pytest|py\.test|tox|jest|vitest|rspec|phpunit|ctest"
    r"|python[0-9.]*\s+-m\s+(?:unittest|pytest)"
    r"|(?:swift|go|cargo|mvn|dotnet|gradle|gradlew)\s+test"
    r"|(?:npm|yarn|pnpm)\s+(?:run\s+)?test"
    r"|make\s+(?:test|check)"
    r"|xcodebuild(?:\s+[^\s|;&]+)*?\s+test(?:-without-building)?"
    r")(?![\w.-])"
)
TEST_RUNNERS = re.compile(r"(?:^|[;&|(\n])\s*" + RUNNER)
# A quoted string or an escaped character, where white space and `;`, `&`, `|`, `(` stop separating
# words and commands: a runner named inside one (`echo "swift test; done"`) is text, not a command.
QUOTED = re.compile(r"""'[^']*'|"(?:\\.|[^"\\])*"|\\.""", re.S)
SEPARATING = re.compile(r"[\s;&|()]")
# A quoted string handed to a shell (`sh -c "..."`) or to ssh (`ssh host "..."`) is itself a command.
SHELL_STRING = re.compile(
    r"""(?:^|[\s;&|(])(?:\S*/)?(?:(?:sh|bash|zsh|dash)(?:\s+-[A-Za-z]+)*\s+-[A-Za-z]*c"""
    r"""|ssh(?:\s+-[A-Za-z]\S*(?:\s+\d+)?)*\s+[^\s'"-]\S*)\s+('[^']*'|"(?:\\.|[^"\\])*")""",
    re.S,
)
# A `--` standing alone begins a command too: it is how a wrapper is told that the rest is the
# command to run. Not after `git`, `echo` or `printf`, where what follows `--` is a path or text.
WRAPPED_RUNNERS = re.compile(r"(?<=\s)--(?=\s)\s*" + RUNNER)
TAKES_NO_COMMAND = re.compile(r"(?:^|[;&|(\n])\s*(?:[\w.~/-]*/)?(?:git|echo|printf)\b[^;&|\n]*\s--\s")
# The longest text one command stores in one field; anything longer is cut at this many characters.
TEXT_CAP = 2000
# The localStorage key under which the board page keeps the viewer's theme; the index reads the same one.
THEME_STORE = "logbook-theme"

STEP_STATUSES = ("pending", "in_progress", "completed")
BOARD_STATES = ("live", "idle", "finished")
CHECK_RESULTS = ("pass", "fail")

# A gate line in an agent's report: `G<n>` first, after any list or emphasis markup.
GATE_LINE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?[*_`]*(G\d+)\b[*_`]*[:.)]?\s*(.*)$")
GATE_PASS = {"pass", "passed", "passes", "ok", "green"}
GATE_FAIL = {"fail", "failed", "fails", "red", "error"}
# Neither a quoted command nor the expected token says how a gate went, so neither is read for words.
NOT_AN_OUTCOME = re.compile(r"`[^`]*`|EXPECT:\s*\S+")
CHECK_COMMAND = re.compile(r"CHECK:\s*(.*?)\s*(?:[—-]+\s*EXPECT:.*)?$")

# The characters that could end a <script> element, open a comment, or end a line inside a script.
INLINE_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
}


class TemplateError(ValueError):
    """The template does not hold exactly one marker line, so no page can be built from it."""


# ---------------------------------------------------------------------------------------------------
# Times and files


def utc(now):
    """`now` (an aware datetime) as the page's time format: `2026-01-05T09:00:00Z`."""
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def board_dir(project, session):
    """The board folder for a session. The session id becomes a path component, so it is checked."""
    session = str(session)
    if not SESSION.fullmatch(session) or set(session) == {"."} or STARTING in session:
        raise ValueError(f"not a usable session id: {session!r}")
    return os.path.join(project, BOARDS_DIR, session)


def contained(path, env):
    """The real path of `path` when it lies inside the home folder or the temporary folder, else None.

    `LOGBOOK_ALLOW_ANY_PATH=1` in `env` adds the root of the file system, so a folder anywhere is used.
    """
    roots = [os.path.expanduser("~"), tempfile.gettempdir()]
    if env.get(ALLOW_ANY_PATH) == "1":
        roots.append(os.path.abspath(os.sep))
    resolved = os.path.realpath(path)
    for root in roots:
        root = os.path.realpath(root)
        try:
            if os.path.commonpath([resolved, root]) == root:
                return resolved
        except ValueError:
            continue
    return None


def temp_mkstemp_kwargs(path):
    """The keyword arguments `write_atomic` gives `tempfile.mkstemp` for `path`: a test that needs to
    leave the same kind of leftover behind (to prove pruning removes it) makes one the same way."""
    return {"dir": os.path.dirname(path) or ".", "prefix": "." + os.path.basename(path) + ".", "suffix": ".tmp"}


def write_atomic(path, text, unless=None):
    """Write the whole file beside itself and rename it into place; never leave the temporary one.

    `unless`, when given, is asked after the temporary file is written and just before the rename,
    the last moment a writer can find that what it wrote is already out of date. When it answers
    true, the temporary file is removed, the file is left as it was, and False is returned; a write
    that happens returns True.
    """
    fd, temporary = tempfile.mkstemp(**temp_mkstemp_kwargs(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if unless is not None and unless():
            os.unlink(temporary)
            return False
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return True


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------------------------------
# The event log


def heal_surrogates(value):
    """`value` with every lone surrogate (U+D800-U+DFFF) in any string it holds replaced by U+FFFD.

    A valid surrogate pair is combined into one character before this ever sees it (by argv decoding
    or by the JSON decoder), so any surrogate still standing alone cannot be encoded as UTF-8. It gets
    in from a raw byte in an argument (surrogateescape) or a split JSON escape in a hook payload.
    """
    if isinstance(value, str):
        return "".join("�" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in value)
    if isinstance(value, list):
        return [heal_surrogates(item) for item in value]
    if isinstance(value, dict):
        return {key: heal_surrogates(item) for key, item in value.items()}
    return value


def write_all(fd, data):
    """`os.write` may write fewer bytes than given; keep going until all of `data` has landed."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def append(board, kind, now, **fields):
    """Append one event as one complete line in a single write, so concurrent writers never interleave."""
    event = {"t": utc(now), "kind": kind}
    event.update({key: value for key, value in fields.items() if key not in event})
    event = heal_surrogates(event)
    line = (json.dumps(event, ensure_ascii=True) + "\n").encode("utf-8")
    fd = os.open(os.path.join(board, EVENTS_FILE), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            # The last write was torn: no trailing newline. Give it one so it loses only itself.
            write_all(fd, b"\n")
        write_all(fd, line)
    finally:
        os.close(fd)
    return event


def events(board):
    """Every line of the log that parses as a JSON object, in order. Anything else is skipped."""
    try:
        with open(os.path.join(board, EVENTS_FILE), "rb") as f:
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    parsed = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            parsed.append(heal_surrogates(event))
    return parsed


# ---------------------------------------------------------------------------------------------------
# Settings


def settings(env=None):
    """The page's settings from the environment. Anything that is not a valid value means the default."""
    env = os.environ if env is None else env

    def choice(name, allowed):
        value = env.get(name)
        value = value.strip().lower() if isinstance(value, str) else ""
        return value if value in allowed else allowed[0]

    accent = env.get("LOGBOOK_ACCENT")
    accent = accent.strip() if isinstance(accent, str) else ""
    return {
        "theme": choice("LOGBOOK_THEME", THEMES),
        "accent": accent if ACCENT.fullmatch(accent) else None,
        "density": choice("LOGBOOK_DENSITY", DENSITIES),
        "refreshSeconds": REFRESH_SECONDS,
        "stuckAfterSeconds": stuck_minutes(env.get("LOGBOOK_STUCK_MINUTES")) * 60,
    }


def test_pattern(env=None):
    """`LOGBOOK_TESTS` compiled: more commands that are test runs, or None when it names nothing.

    The setting is a comma-separated list of literal text, not a regular expression: a command is a
    test run when it contains any item, case-sensitively. Space around an item is trimmed and an
    empty item is dropped.
    """
    env = os.environ if env is None else env
    value = env.get("LOGBOOK_TESTS")
    escaped = []
    for item in value.split(",") if isinstance(value, str) else []:
        if item.strip():
            escaped.append(re.escape(item.strip()))
    return re.compile("|".join(escaped)) if escaped else None


def is_test(command, pattern=None):
    """Whether a command is a test run: a common test runner, or a match for `pattern`.

    The runners are looked for in the command with its quoted strings made single words; a quote left
    open is read as written. `pattern` is literal text, so it is looked for in the command as written.
    """
    if pattern is not None and pattern.search(command):
        return True
    for shell in SHELL_STRING.finditer(command):
        if is_test(shell.group(1)[1:-1]):
            return True
    words = QUOTED.sub(lambda found: SEPARATING.sub("_", found.group(0)), command)
    if TEST_RUNNERS.search(words):
        return True
    return bool(WRAPPED_RUNNERS.search(words)) and not TAKES_NO_COMMAND.search(words)


def stuck_minutes(value):
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_STUCK_MINUTES
    return minutes if minutes > 0 else DEFAULT_STUCK_MINUTES


# ---------------------------------------------------------------------------------------------------
# Gate lines in an agent's report


def gate_result(line):
    """`pass`, `fail` or `unknown` from the words of a gate line. Words on both sides mean unknown."""
    words = set(re.findall(r"[a-z]+", NOT_AN_OUTCOME.sub(" ", line).lower()))
    passed, failed = bool(words & GATE_PASS), bool(words & GATE_FAIL)
    if passed == failed:
        return "unknown"
    return "pass" if passed else "fail"


def gate_parts(rest):
    """(text, command) of a gate line after its id: the command is whatever follows `CHECK:`."""
    rest = rest.strip().strip("*_").strip()
    head, marker, tail = rest.partition("CHECK:")
    command = None
    if marker:
        found = CHECK_COMMAND.match(marker + tail)
        if found:
            command = found.group(1).strip().strip("`").strip() or None
        rest = head.rstrip(" —-:").strip()
    return rest, command


def gates(message):
    """Each `G<n>` line of a report, first occurrence of each id, as {id, text, result, command}."""
    found = []
    seen = set()
    for line in (message or "").split("\n"):
        match = GATE_LINE.match(line)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        text, command = gate_parts(match.group(2))
        found.append({"id": match.group(1), "text": text, "result": gate_result(match.group(2)), "command": command})
    return found


# ---------------------------------------------------------------------------------------------------
# Deriving the state


def text(value):
    """A value the page shows as text: strings as they are, numbers as strings, anything else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None


def ordered_steps(steps):
    """The steps for the page: ids that are whole numbers first, ascending, then the rest in log order."""
    numbered = [step for step in steps.values() if step["id"].isdigit()]
    numbered.sort(key=lambda step: int(step["id"]))
    rest = [step for step in steps.values() if not step["id"].isdigit()]
    return numbered + rest


def whole(value):
    """A count from the log: a whole number that is not negative, else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def ran_at(event, t):
    """When a work call ran: `at` on one read back from the transcript, else when it was recorded."""
    at = event.get("at")
    return at if isinstance(at, str) and UTC_TIME.fullmatch(at) else t


def in_project(path, project):
    """`path` relative to `project` when the file is inside it, else as recorded."""
    if not isinstance(project, str) or not os.path.isabs(project) or not os.path.isabs(path):
        return path
    try:
        relative = os.path.relpath(path, project)
    except ValueError:
        return path
    if relative in (os.curdir, os.pardir) or relative.startswith(os.pardir + os.sep):
        return path
    return relative


class Derivation:
    """The state being built from one pass over the log. One method per event kind."""

    def __init__(self, project=None, tests=None):
        self.project = project
        self.tests = tests
        self.changes = {}
        self.commands = {}
        self.command_runs = 0
        self.session = None
        self.title = None
        self.title_source = "prompt"
        self.started = None
        self.updated = None
        self.idle = False
        self.finished = False
        self.questions = []
        self.steps = {}
        self.deliverables = []
        self.checks = []
        self.decisions = []
        self.agents = {}
        self.tokens = None

    def apply(self, event):
        handler = HANDLERS.get(event.get("kind"))
        t = event.get("t")
        if handler is None or not isinstance(t, str) or not UTC_TIME.fullmatch(t):
            return
        try:
            if handler(self, event, t) is False:
                return
        except (TypeError, ValueError, AttributeError, KeyError):
            return
        self.updated = t
        self.idle = event["kind"] == "turn-end"
        if event["kind"] == "close":
            self.finished = True
        elif event["kind"] == "reopen":
            self.finished = False

    def on_start(self, event, t):
        if self.started is not None:
            return False
        self.started = t
        self.session = text(event.get("session"))
        if self.title_source == "prompt":
            self.title = text(event.get("title"))

    def on_title(self, event, t):
        title = text(event.get("title"))
        if not title:
            return False
        self.title, self.title_source = title, "model"

    def step(self, step_id, t):
        if step_id not in self.steps:
            self.steps[step_id] = {"id": step_id, "subject": "", "status": "pending", "updated": t}
        return self.steps[step_id]

    def on_step(self, event, t):
        step_id = text(event.get("id"))
        if not step_id:
            return False
        step = self.step(step_id, t)
        step["subject"] = text(event.get("subject")) or step["subject"]
        step["updated"] = t

    def on_step_status(self, event, t):
        step_id, status = text(event.get("id")), text(event.get("status"))
        if not step_id:
            return False
        if status == "deleted":
            self.steps.pop(step_id, None)
            return None
        if status not in STEP_STATUSES:
            return False
        step = self.step(step_id, t)
        step["status"], step["updated"] = status, t

    def on_steps_replace(self, event, t):
        items = event.get("items")
        if not isinstance(items, list):
            return False
        self.steps = {}
        for item in (item for item in items if isinstance(item, dict)):
            step_id = str(len(self.steps) + 1)
            status = text(item.get("status"))
            self.steps[step_id] = {
                "id": step_id,
                "subject": text(item.get("content")) or "",
                "status": status if status in STEP_STATUSES else "pending",
                "updated": t,
            }

    def agent(self, agent_id, t):
        if agent_id not in self.agents:
            self.agents[agent_id] = {
                "id": agent_id, "type": None, "model": None, "description": None,
                "started": t, "ended": None, "outcome": "running", "gates": [], "tokens": None,
            }
        return self.agents[agent_id]

    def on_agent_start(self, event, t):
        agent_id = text(event.get("id"))
        if not agent_id:
            return False
        agent = self.agent(agent_id, t)
        agent["type"] = text(event.get("type")) or agent["type"]
        agent["started"] = t

    def on_agent_info(self, event, t):
        agent_id = text(event.get("id"))
        if not agent_id:
            return False
        agent = self.agent(agent_id, t)
        agent["model"] = text(event.get("model")) or agent["model"]
        agent["description"] = text(event.get("description")) or agent["description"]

    def on_agent_stop(self, event, t):
        agent_id = text(event.get("id"))
        if not agent_id:
            return False
        agent = self.agent(agent_id, t)
        parsed = gates(text(event.get("message")))
        agent["ended"] = t
        agent["tokens"] = whole(event.get("tokens")) if whole(event.get("tokens")) is not None else agent["tokens"]
        agent["gates"] = [{"id": g["id"], "text": g["text"], "result": g["result"]} for g in parsed]
        agent["outcome"] = "failed" if any(g["result"] == "fail" for g in parsed) else "finished"
        for gate in parsed:
            if gate["result"] in CHECK_RESULTS:
                self.checks.append({
                    "id": f"C{len(self.checks) + 1}", "proves": gate["text"], "command": gate["command"],
                    "result": gate["result"], "time": t, "source": "agent", "agent": agent_id,
                })

    def on_session_tokens(self, event, t):
        """The main session's running total, read from its transcript at each turn's end: the latest wins."""
        tokens = whole(event.get("tokens"))
        if tokens is None:
            return False
        self.tokens = tokens

    def on_question(self, event, t):
        question = text(event.get("text"))
        if not question:
            return False
        hard_stop = event.get("hardStop") is True
        self.questions.append({
            "id": f"Q{len(self.questions) + 1}", "text": question,
            "default": None if hard_stop else text(event.get("default")),
            "affects": text(event.get("affects")), "reverse": text(event.get("reverse")),
            "hardStop": hard_stop, "status": "open", "answer": None, "asked": t, "answered": None,
        })

    def on_answer(self, event, t):
        question = find_question(self.questions, event.get("id"))
        answer = text(event.get("answer"))
        if question is None or answer is None:
            return False
        question.update(status="answered", answer=answer, answered=t)

    def on_decision(self, event, t):
        decision = text(event.get("text"))
        if not decision:
            return False
        self.decisions.append({
            "id": f"D{len(self.decisions) + 1}", "text": decision,
            "why": text(event.get("why")), "reverse": text(event.get("reverse")), "time": t,
        })

    def on_deliverable(self, event, t):
        path, url = text(event.get("path")), text(event.get("url"))
        label = text(event.get("label")) or path or url
        if not label:
            return False
        self.deliverables.append({"label": label, "path": path, "url": url, "step": text(event.get("step")), "time": t})

    def on_check(self, event, t):
        result = text(event.get("result"))
        result = result.strip().lower() if result else None
        if result not in CHECK_RESULTS:
            return False
        source = "agent" if event.get("source") == "agent" else "model"
        self.checks.append({
            "id": f"C{len(self.checks) + 1}", "proves": text(event.get("proves")),
            "command": text(event.get("command")), "result": result, "time": t,
            "source": source, "agent": text(event.get("agent")),
        })

    def on_change(self, event, t):
        path = text(event.get("path"))
        if not path:
            return False
        path, when = in_project(path, self.project), ran_at(event, t)
        row = self.changes.get(path)
        if row is None:
            row = self.changes[path] = {
                "path": path, "edits": 0, "added": None, "removed": None,
                "created": event.get("created") is True, "first": when, "last": when, "agents": [],
            }
        row["edits"] += 1
        row["last"] = when
        for key in ("added", "removed"):
            lines = whole(event.get(key))
            if lines is not None:
                row[key] = (row[key] or 0) + lines
        agent = text(event.get("agent"))
        if agent and agent not in row["agents"]:
            row["agents"].append(agent)

    def on_command(self, event, t):
        command, result = text(event.get("command")), text(event.get("result"))
        if not command or result not in COMMAND_RESULTS:
            return False
        self.command_runs += 1
        row = self.commands.get(command)
        if row is None:
            row = self.commands[command] = {"command": command, "runs": 0, "fails": 0, "agents": []}
        exit_code = event.get("exit")
        row.update(
            description=text(event.get("description")), result=result, time=ran_at(event, t),
            exit=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
            ms=whole(event.get("ms")), runs=row["runs"] + 1, fails=row["fails"] + (result == "fail"),
            latest=self.command_runs,
        )
        agent = text(event.get("agent"))
        if agent and agent not in row["agents"]:
            row["agents"].append(agent)

    def on_marker(self, event, t):
        return None

    def command_rows(self):
        """One row per command in order of first run, the COMMAND_ROWS most recently run of them.

        `latestRun` numbers the row's latest run among every command run, from 1, in the order they
        were applied: it orders two runs that were recorded in the same second.
        """
        rows = list(self.commands.values())
        kept = {id(row) for row in sorted(rows, key=lambda row: row["latest"])[-COMMAND_ROWS:]}
        return [
            {
                "command": row["command"], "description": row["description"], "result": row["result"],
                "exit": row["exit"], "runs": row["runs"], "fails": row["fails"], "time": row["time"],
                "ms": row["ms"], "test": is_test(row["command"], self.tests), "agents": row["agents"],
                "latestRun": row["latest"],
            }
            for row in rows
            if id(row) in kept
        ]

    def state(self, commits, settings):
        if self.finished:
            state = "finished"
        elif self.idle:
            state = "idle"
        else:
            state = "live"
        return {
            "schema": SCHEMA,
            "session": self.session,
            "title": self.title,
            "titleSource": self.title_source,
            "started": self.started,
            "updated": self.updated,
            "state": state,
            "settings": settings,
            "questions": self.questions,
            "steps": ordered_steps(self.steps),
            "commits": list(commits),
            "deliverables": self.deliverables,
            "checks": self.checks,
            "decisions": self.decisions,
            "agents": list(self.agents.values()),
            "tokens": self.tokens,
            "changes": list(self.changes.values()),
            "commands": self.command_rows(),
            "commandsTotal": self.command_runs,
        }


def find_question(questions, question_id):
    """The question an answer names: `Q2`, `q2` and `2` all name the second one. None when none does."""
    wanted = (text(question_id) or "").strip().upper()
    wanted = wanted if wanted.startswith("Q") else "Q" + wanted
    return next((q for q in questions if q["id"] == wanted), None)


HANDLERS = {
    "start": Derivation.on_start,
    "title": Derivation.on_title,
    "step": Derivation.on_step,
    "step-status": Derivation.on_step_status,
    "steps-replace": Derivation.on_steps_replace,
    "agent-start": Derivation.on_agent_start,
    "agent-info": Derivation.on_agent_info,
    "agent-stop": Derivation.on_agent_stop,
    "session-tokens": Derivation.on_session_tokens,
    "question": Derivation.on_question,
    "answer": Derivation.on_answer,
    "decision": Derivation.on_decision,
    "deliverable": Derivation.on_deliverable,
    "check": Derivation.on_check,
    "change": Derivation.on_change,
    "command": Derivation.on_command,
    "turn-end": Derivation.on_marker,
    "turn-start": Derivation.on_marker,
    "close": Derivation.on_marker,
    "reopen": Derivation.on_marker,
}


def is_early(event):
    return event.get("early") is True and event.get("kind") in STEP_KINDS + WORK_KINDS + AGENT_KINDS + COMMIT_KINDS


def is_closed(log):
    """Whether a board is closed: the last of its `close` and `reopen` events, if any, is a `close`."""
    for event in reversed(log):
        if isinstance(event, dict) and event.get("kind") in ("close", "reopen"):
            return event["kind"] == "close"
    return False


def closed_by(log):
    """Who closed a closed board: `session-end` or `hand`, from its last `close`; None when it is open.

    A `close` without a known `by` (written by an older version) counts as by hand.
    """
    for event in reversed(log):
        if isinstance(event, dict) and event.get("kind") in ("close", "reopen"):
            if event["kind"] == "reopen":
                return None
            return event.get("by") if event.get("by") in CLOSERS else "hand"
    return None


def derive(events, commits, settings, tests=None):
    """The state the page reads, from the log in order. Events that do not make sense are skipped.

    A step, change, command or agent-info event marked `early` was read back from the transcript, and its
    call was made before the board was published; every other one was recorded by a hook after that. So
    the early ones are applied first, in log order, and then the rest in log order: what a hook recorded
    later is never overwritten by what was read back. A changed file's path
    is shown relative to the project the `start` event names. A command is a test run when it matches
    a common test runner or `tests` (the `LOGBOOK_TESTS` pattern), decided here, not when it
    was recorded, so a changed setting applies to every command on the board.
    """
    log = [event for event in events if isinstance(event, dict)]
    first = start_event(log)
    derivation = Derivation(first.get("project") if first else None, tests)
    for event in [e for e in log if is_early(e)] + [e for e in log if not is_early(e)]:
        derivation.apply(event)
    return derivation.state(commits, settings)


def start_event(log):
    return next((e for e in log if isinstance(e, dict) and e.get("kind") == "start"), None)


# ---------------------------------------------------------------------------------------------------
# Commits, read from git at render time


# Variables that point git at some other repository than the one the project directory is in. A git
# hook exports them, and a board rendered from inside one must still read the project's own history.
REPOSITORY_VARIABLES = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
)


def git(project, *args, stdin=None):
    """git's stdout in the project, or None on any failure: not a repository, no git, a timeout."""
    env = {key: value for key, value in os.environ.items() if key not in REPOSITORY_VARIABLES}
    try:
        done = subprocess.run(
            ["git", *args], cwd=project, env=env, input=stdin, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="replace")


def branch_heads(project):
    """(current branch or None, {local branch: head}) — what the `start` event records.

    The heads are None when git could not list them: not a repository, no git, a timeout.
    """
    listing = git(project, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads")
    heads = None if listing is None else {}
    for line in (listing or "").splitlines():
        sha, _, ref = line.partition(" ")
        if ref.startswith("refs/heads/"):
            heads[ref[len("refs/heads/"):]] = sha
    current = (git(project, "symbolic-ref", "--quiet", "HEAD") or "").strip()
    branch = current[len("refs/heads/"):] if current.startswith("refs/heads/") else None
    return branch, heads


def existing(project, shas):
    """The recorded heads that still name a commit, or None when git could not say.

    A head that no longer exists cannot exclude anything.
    """
    if not shas:
        return []
    answer = git(project, "cat-file", "--batch-check", stdin="".join(s + "\n" for s in shas).encode())
    if answer is None:
        return None
    return [line.split()[0] for line in answer.splitlines() if line.split()[1:2] == ["commit"]]


def commits(project, start_event):
    """Commits made since the board started, on its branch, the checked-out one and new ones. Newest last.

    The branches that count are the starting branch, the branch checked out now, and every local
    branch whose name the `start` event did not record; a commit counts when one of them reaches it
    and no recorded head does (the checked-out branch's own recorded head included). When the heads
    were not read at the start, nothing can be told apart from older history, so nothing counts. Any
    failure of git gives an empty list, never an error and never a partial one.
    """
    if not isinstance(start_event, dict) or not project:
        return []
    recorded = start_event.get("heads")
    recorded = recorded if isinstance(recorded, dict) else {}
    if start_event.get("headsRead", bool(recorded)) is not True:
        return []
    started_on = start_event.get("branch")
    checked_out, now_heads = branch_heads(project)
    if now_heads is None:
        return []
    counting = sorted(name for name in now_heads if name in (started_on, checked_out) or name not in recorded)
    counting.sort(key=lambda name: name != started_on)
    if not counting:
        return []
    shas = [sha for sha in recorded.values() if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{4,64}", sha)]
    boundary = existing(project, shas)
    if boundary is None:
        return []
    exclude = ["--not", *boundary]
    refs = ["refs/heads/" + name for name in counting]

    labels = {}
    for name, ref in zip(counting, refs):
        reachable = git(project, "rev-list", ref, *exclude, "--")
        if reachable is None:
            return []
        for sha in reachable.split():
            labels.setdefault(sha, name)
    listing = git(project, "log", "--date-order", "--reverse", "--format=%H%x1f%h%x1f%ct%x1f%s", *refs, *exclude, "--")
    if listing is None:
        return []
    found = []
    for line in listing.splitlines():
        parts = line.split("\x1f", 3)
        if len(parts) != 4 or parts[0] not in labels:
            continue
        try:
            when = utc(datetime.fromtimestamp(int(parts[2]), timezone.utc))
        except (ValueError, OverflowError, OSError):
            continue
        found.append({"hash": parts[1], "subject": parts[3], "branch": labels[parts[0]], "time": when, "step": None})
    return found


def shown_before(state):
    """The commits a board's previous state showed. An item that is not a well-formed commit is dropped."""
    items = state.get("commits") if isinstance(state, dict) else None
    shown = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        sha, subject, when = item.get("hash"), item.get("subject"), item.get("time")
        if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{4,64}", sha) and isinstance(subject, str)
                and isinstance(when, str) and UTC_TIME.fullmatch(when)):
            continue
        branch, step = item.get("branch"), item.get("step")
        shown.append({
            "hash": sha, "subject": subject, "branch": branch if isinstance(branch, str) else None,
            "time": when, "step": step if isinstance(step, str) else None,
        })
    return shown


def on_some_branch(project, shas):
    """(found, resolvable) for `shas`: hashes git confirms are on some local branch, and hashes git was
    able to answer about at all (found or confirmed off every branch).

    A hash outside `resolvable` is one git could not be asked about (a failure, not an answer) and the
    caller keeps it rather than dropping it. Resolution goes through `cat-file --batch-check` first
    because the state stores abbreviated hashes, which git lengthens as a repository grows; an object
    that check cannot find (the abbreviation no longer resolves) counts as confirmed off every branch,
    not as unresolvable.
    """
    if not shas:
        return set(), set()
    check = git(project, "cat-file", "--batch-check", stdin="".join(s + "\n" for s in shas).encode())
    if check is None or len(check.splitlines()) != len(shas):
        return set(), set()
    resolvable, found = set(), set()
    for original, line in zip(shas, check.splitlines()):
        parts = line.split()
        if len(parts) < 2 or parts[1] != "commit":
            resolvable.add(original)  # the object is gone: confirmed off every branch
            continue
        containing = git(project, "for-each-ref", "--contains", parts[0], "refs/heads")
        if containing is None:
            continue  # git could not be asked; leave it out of `resolvable`
        resolvable.add(original)
        if containing.strip():
            found.add(original)
    return found, resolvable


def same_commit(one, other):
    """Whether two abbreviated hashes name the same commit: git lengthens an abbreviation as a
    repository grows, so one that begins the other is the same one."""
    return one.startswith(other) or other.startswith(one)


def with_early(found, log, project):
    """`found`, and every commit a `commit` event in `log` names that it does not hold, by commit time,
    newest last.

    Such a commit is added only when git confirms it is a commit in `project` on some local branch
    now: a hash from another repository, or of a commit an amend or a rebase rewrote, shows nothing.
    Its subject and time are read from git, never from the transcript's text, and its branch is the
    one git printed when it made it. A log without a `commit` event, or whose commits `found` already
    holds, asks git nothing more, and git is asked about the latest `EARLY_COMMITS` of them at most.
    Any failure of git shows fewer commits, never an error.
    """
    named = {}
    for event in log:
        sha = event.get("hash") if isinstance(event, dict) and event.get("kind") in COMMIT_KINDS else None
        if not isinstance(sha, str) or not COMMIT_HASH.fullmatch(sha):
            continue
        if any(same_commit(sha, other) for other in [item["hash"] for item in found] + list(named)):
            continue
        branch = event.get("branch")
        named[sha] = branch if isinstance(branch, str) else None
    if not named:
        return found
    named = dict(list(named.items())[-EARLY_COMMITS:])
    confirmed, _ = on_some_branch(project, list(named))
    confirmed = [sha for sha in named if sha in confirmed]
    if not confirmed:
        return found
    # Each hash is validated hex and goes before `--`, so git reads it as a revision and nothing else.
    listing = git(project, "log", "--no-walk=unsorted", "--format=%H%x1f%h%x1f%ct%x1f%s", *confirmed, "--")
    rows = {}
    for line in (listing or "").splitlines():
        parts = line.split("\x1f", 3)
        sha = next((s for s in confirmed if len(parts) == 4 and parts[0].startswith(s)), None)
        if sha is None:
            continue
        try:
            when = utc(datetime.fromtimestamp(int(parts[2]), timezone.utc))
        except (ValueError, OverflowError, OSError):
            continue
        rows[sha] = {"hash": parts[1], "subject": parts[3], "branch": named[sha], "time": when, "step": None}
    # The session made these before the board started, so on a tie in time they come first.
    return sorted([rows[sha] for sha in confirmed if sha in rows] + list(found), key=lambda item: item["time"])


def with_shown(found, shown, project=None, started_on=None):
    """`found`, and every commit shown before that it does not hold, by commit time, newest last.

    Hashes are abbreviated, and git lengthens an abbreviation as a repository grows, so one hash
    that begins the other is the same commit.

    A candidate kept from a previous state is checked again: it stays only while git still finds it
    on some local branch, so a commit an amend or a rebase rewrote is dropped rather than kept
    forever next to its replacement. The one exception is the branch the board started on
    (`started_on`): once that branch itself is gone, its last-known commits are the only record the
    board has of its own work, so they stay, matching the promise that work the board has shown is
    never lost. A branch the board only passed through or picked up after it started carries no such
    promise and loses its commits once it is gone.

    `project` is the working directory the commits were read from. Without it (or when git cannot be
    asked at all) the reachability check cannot run, which the caller-could-not-be-asked rule treats
    like any other git failure: every candidate is kept.
    """
    new = [item["hash"] for item in found]
    candidates = [item for item in shown if not any(h.startswith(item["hash"]) or item["hash"].startswith(h) for h in new)]
    if not candidates:
        return sorted(found, key=lambda item: item["time"])
    now_heads = branch_heads(project)[1] if project is not None else None
    if now_heads is None:
        return sorted(found + candidates, key=lambda item: item["time"])
    protected, checkable = [], []
    for item in candidates:
        gone_own_branch = isinstance(started_on, str) and item["branch"] == started_on and started_on not in now_heads
        (protected if gone_own_branch else checkable).append(item)
    found_on_branch, resolvable = on_some_branch(project, [item["hash"] for item in checkable])
    kept = protected + [item for item in checkable if item["hash"] in found_on_branch or item["hash"] not in resolvable]
    return sorted(found + kept, key=lambda item: item["time"])


# ---------------------------------------------------------------------------------------------------
# Rendering


def inline_json(value):
    """JSON that is safe inside a <script> element and a .js file, whatever strings it carries."""
    serialised = json.dumps(value, ensure_ascii=False)
    return re.sub("[<>&\u2028\u2029]", lambda m: INLINE_ESCAPES[m.group(0)], serialised)


def fill_template(template_text, replacement):
    """The template with its one marker line replaced (None removes the line). Raises TemplateError."""
    lines = template_text.split("\n")
    at = [i for i, line in enumerate(lines) if line.strip() == TEMPLATE_MARKER]
    if len(at) != 1 or template_text.count(TEMPLATE_MARKER) != 1:
        raise TemplateError(
            f"the template must hold the line {TEMPLATE_MARKER} exactly once; "
            f"found it {template_text.count(TEMPLATE_MARKER)} times"
        )
    line = lines[at[0]]
    if replacement is None:
        del lines[at[0]]
    else:
        lines[at[0]] = line[: len(line) - len(line.lstrip())] + replacement
    return "\n".join(lines)


def board_page(template_text):
    return fill_template(template_text, None)


def report_page(template_text, state):
    return fill_template(
        template_text, f"<script>window.BOARD = {inline_json(state)}; window.BOARD_FROZEN = true;</script>"
    )


def current_state(board, now, env, before=None):
    """The state from the log, with the commits git reads now, those the log's `commit` events name, and
    those the state `before` showed."""
    log = events(board)
    first = start_event(log)
    project = first.get("project") if first else None
    started_on = first.get("branch") if first else None
    found = with_early(commits(project, first), log, project) if isinstance(project, str) else []
    state = derive(log, with_shown(found, shown_before(before), project, started_on), settings(env), test_pattern(env))
    if state["updated"] is None:
        state["updated"] = utc(now)
    return state


def write_state(board, now, env, template, size=None):
    """Write `state.js`, and `board.html` when it is missing. Returns (state, whether the index shows a change).

    With `size` (the log's size before it was read), `state.js` is left alone when the log is no
    longer that size just before it would be replaced: a newer event has been appended, and whoever
    appended it renders afterwards. A write left undone shows no change to the index.
    """
    before = read_state(board) or {}
    state = current_state(board, now, env, before)
    unless = None if size is None else (lambda: log_size(board) != size)
    written = write_atomic(os.path.join(board, STATE_FILE), f"window.BOARD = {inline_json(state)};\n", unless=unless)
    page = os.path.join(board, BOARD_FILE)
    if not os.path.exists(page):
        write_atomic(page, board_page(read_text(template or DEFAULT_TEMPLATE)))
    return state, written and any(before.get(key) != state[key] for key in ("title", "state"))


def log_size(board):
    try:
        return os.stat(os.path.join(board, EVENTS_FILE)).st_size
    except OSError:
        return 0


def render(board, now, env=None, template=None):
    """Write `state.js`, `board.html` when it is missing, and the index when the title or state changed
    or the index does not list this board.

    Several renders can run at once, and the last to replace `state.js` wins, even one that read the
    log before a newer event. So a pass that finds, just before it would replace `state.js`, that the
    log grew since it read it does not write, and renders again, up to RENDER_PASSES times; the last
    pass stops without writing, since whoever appended the newer event renders after appending and
    their write stands. A render that stops still behind a log that has closed the board writes once
    more, so what it leaves is `finished`. The index is checked for this board on every pass because
    a start killed between publishing the board and rewriting the index leaves it out.
    """
    name = os.path.basename(os.path.normpath(board))
    for _ in range(RENDER_PASSES):
        size = log_size(board)
        state, changed = write_state(board, now, env, template, size=size)
        if changed or (listable(boards_of(board), name) and not index_lists(board)):
            update_index(board, env)
        if log_size(board) == size:
            return state
    if state["state"] != "finished" and is_closed(events(board)):
        state, _ = write_state(board, now, env, template)
        update_index(board, env)
    return state


def close(board, now, env=None, template=None, by="hand"):
    """Append `close`, render, write `report.html` (the page with the state inlined and frozen), then the index.

    `by` says who closed it: `session-end` for the `SessionEnd` hook, `hand` for the `close` command.
    A board the session's end closed is reopened by the next event from that session; one closed by
    hand stays closed until the session is resumed. A board already closed (its last `close`/`reopen` event is a `close`) takes nothing more: nothing
    is appended and nothing is rewritten, except that a `report.html` lost since the close is written
    back. A board that was reopened since its last close is closed again, and its `report.html` is
    rewritten.
    """
    report = os.path.join(board, REPORT_FILE)
    if is_closed(events(board)):
        state = current_state(board, now, env, before=read_state(board))
        if not os.path.isfile(report):
            write_atomic(report, report_page(read_text(template or DEFAULT_TEMPLATE), state))
        return state
    append(board, "close", now, by=by)
    state, _ = write_state(board, now, env, template)
    write_atomic(report, report_page(read_text(template or DEFAULT_TEMPLATE), state))
    update_index(board, env)
    return state


def reopen(board, now, env=None, template=None):
    """Append `reopen` to a closed board, so it derives live again. Returns the state rendered."""
    append(board, "reopen", now)
    state, _ = write_state(board, now, env, template)
    update_index(board, env)
    return state


# ---------------------------------------------------------------------------------------------------
# The index of boards


def read_state(board):
    """The state a board's `state.js` holds, or None when it is missing or does not parse as one."""
    try:
        raw = read_text(os.path.join(board, STATE_FILE)).strip()
    except (OSError, ValueError):
        return None
    prefix = "window.BOARD = "
    if not raw.startswith(prefix):
        return None
    try:
        state = json.loads(raw[len(prefix):].rstrip(";"))
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def boards_of(board):
    """The folder that holds a board and its siblings: `<project>/.logbook`."""
    return os.path.dirname(os.path.normpath(os.path.abspath(board)))


def board_row(boards, name):
    """One row of the index. A board whose state cannot be read is shown by its folder name."""
    folder = os.path.join(boards, name)
    state = read_state(folder) or {}
    title, status, started = state.get("title"), state.get("state"), state.get("started")
    page = REPORT_FILE if status == "finished" and os.path.isfile(os.path.join(folder, REPORT_FILE)) else BOARD_FILE
    return {
        "title": title if isinstance(title, str) and title.strip() else name,
        "state": status if status in BOARD_STATES else None,
        "started": started if isinstance(started, str) and UTC_TIME.fullmatch(started) else None,
        "link": quote(name) + "/" + page,
        "name": name,
    }


def listable(boards, name):
    """Whether a folder is one the index lists: it carries the marker file and is not still being built."""
    return STARTING not in name and is_board(os.path.join(boards, name))


def board_rows(boards):
    """A row for every folder that carries the marker file, newest first; unknown start times last.

    A board still being built is not listed.
    """
    try:
        names = os.listdir(boards)
    except OSError:
        return []
    names = [n for n in names if listable(boards, n)]
    rows = sorted((board_row(boards, n) for n in names), key=lambda r: r["name"])
    return sorted(rows, key=lambda r: r["started"] or "", reverse=True)


INDEX_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Logbooks</title>
<script>
(function () {{
  var mode = null;
  try {{ mode = window.localStorage.getItem('{store}'); }} catch (e) {{ mode = null; }}
  if (mode !== 'light' && mode !== 'dark' && mode !== 'system') mode = '{theme}';
  document.documentElement.setAttribute('data-theme', mode);
}})();
</script>
<style>
:root {{
  color-scheme: light;
  --bg: #f6f6f4; --surface: #ffffff; --fg: #1d1d1f; --muted: #5d5d64; --border: #dcdcd7;
  --link: #1c52ad; --ok: #1b6a33; --warn: #874d00;
  --font: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --bg: #141416; --surface: #1c1c1f; --fg: #ececef; --muted: #a4a4ad; --border: #34343a;
    --link: #8fb5ff; --ok: #7fd494; --warn: #f1b560;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --bg: #141416; --surface: #1c1c1f; --fg: #ececef; --muted: #a4a4ad; --border: #34343a;
  --link: #8fb5ff; --ok: #7fd494; --warn: #f1b560;
}}
body {{ margin: 0; background: var(--bg); color: var(--fg); font-family: var(--font); font-size: 15px; line-height: 1.5; }}
main {{ max-width: 56rem; margin: 0 auto; padding: 20px 16px 48px; }}
h1 {{ font-size: 1.3em; margin: 0 0 12px; }}
table {{ width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--border); }}
th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }}
th {{ color: var(--muted); font-weight: 600; font-size: 0.9em; }}
a {{ color: var(--link); overflow-wrap: anywhere; }}
.state-live {{ color: var(--ok); }}
.state-idle {{ color: var(--warn); }}
.state-finished, .muted {{ color: var(--muted); }}
</style>
</head>
<body>
<main>
<h1>Logbooks</h1>
{body}
</main>
<script>
(function () {{
  var times = document.querySelectorAll('time[datetime]');
  for (var i = 0; i < times.length; i++) {{
    var when = new Date(times[i].getAttribute('datetime'));
    if (!isNaN(when.getTime())) times[i].textContent = when.toLocaleString();
  }}
}})();
</script>
</body>
</html>
"""


def index_row(row):
    started = row["started"]
    when = (
        f'<time datetime="{html.escape(started)}">{html.escape(started[:10] + " " + started[11:16])} UTC</time>'
        if started else '<span class="muted">unknown</span>'
    )
    state = f'<span class="state-{row["state"]}">{row["state"]}</span>' if row["state"] else '<span class="muted">unknown</span>'
    return (
        f'<tr><td><a href="{html.escape(row["link"])}">{html.escape(row["title"])}</a></td>'
        f"<td>{state}</td><td>{when}</td></tr>"
    )


def index_page(rows, theme):
    """The index as a self-contained page. Every value from a board is escaped; titles are prompts."""
    if rows:
        body = (
            "<table>\n<thead><tr><th>Board</th><th>State</th><th>Started</th></tr></thead>\n<tbody>\n"
            + "\n".join(index_row(row) for row in rows) + "\n</tbody>\n</table>"
        )
    else:
        body = '<p class="muted">No boards yet.</p>'
    return INDEX_PAGE.format(store=THEME_STORE, theme=theme if theme in THEMES else "system", body=body)


def update_index(board, env=None):
    """Rewrite `index.html` beside a board, when the board sits where boards live and that is a real folder."""
    boards = boards_of(board)
    if os.path.basename(boards) == BOARDS_DIR and not refused_boards(boards):
        write_atomic(os.path.join(boards, INDEX_FILE), index_page(board_rows(boards), settings(env)["theme"]))


def index_lists(board):
    """Whether `index.html` beside a board links to it; an index that cannot be read does not.

    One read and a substring test for the link `index_row` writes, since it runs on every render.
    """
    link = html.escape(quote(os.path.basename(os.path.normpath(board))) + "/")
    try:
        page = read_text(os.path.join(boards_of(board), INDEX_FILE))
    except (OSError, ValueError):
        return False
    return any(f'href="{link}{name}"' in page for name in (BOARD_FILE, REPORT_FILE))


def ensure_ignored(boards):
    """`<project>/.logbook/.gitignore` holding `*`, so no board ever shows in `git status`."""
    path = os.path.join(boards, ".gitignore")
    if not os.path.exists(path):
        write_atomic(path, "*\n")


def refused_boards(boards):
    """Why no board may be started in `boards`, or None: it is a symbolic link, or it is not a folder."""
    if os.path.islink(boards):
        return "the boards folder is a link, so no board was started"
    if os.path.lexists(boards) and not os.path.isdir(boards):
        return "the boards folder is not a folder, so no board was started"
    return None


def start(project, session, now, title, env=None, template=None, early=()):
    """Create the board for a session. Returns its folder when this call created it, else None.

    `early` is a list of (kind, fields) events recorded straight after `start`. The board is built
    whole in a sibling folder, `<session>.starting-<pid>-<random>`, and published with one rename,
    so a hook never finds a board that is missing its `start` event or the steps read with it. When
    two calls start the same board at once, the rename lets exactly one of them publish; the other
    removes the folder it built and returns None. A boards folder that is a symbolic link, or not a
    folder, is refused: nothing is created and None is returned. A board whose marker has no `start`
    event (left by an older start that was cut short) is repaired in place.
    """
    board = board_dir(project, session)
    boards = os.path.dirname(board)
    if refused_boards(boards):
        return None
    marker = os.path.join(board, MARKER_FILE)
    if os.path.isfile(marker) and start_event(events(board)) is not None:
        return None
    os.makedirs(boards, exist_ok=True)
    ensure_ignored(boards)
    branch, heads = branch_heads(project)
    fields = dict(
        session=str(session), title=title, project=project, branch=branch, heads=heads or {},
        headsRead=heads is not None,
    )
    if os.path.isfile(marker):
        if start_event(events(board)) is not None:
            return None
        write_log(board, now, fields, early)
        render(board, now, env, template)
        return board
    building = f"{board}{STARTING}{os.getpid()}-{os.urandom(4).hex()}"
    os.mkdir(building)
    try:
        with open(os.path.join(building, MARKER_FILE), "w", encoding="utf-8") as f:
            f.write(str(session))
        write_log(building, now, fields, early)
        write_state(building, now, env, template)
    except BaseException:
        remove_board(building)
        raise
    try:
        os.rename(building, board)
    except OSError:
        remove_board(building)
        if is_board(board):
            return None
        raise
    update_index(board, env)
    return board


def write_log(board, now, fields, early):
    """The `start` event, then each early event, leaving out the fields that are None."""
    append(board, "start", now, **fields)
    for kind, found in early:
        append(board, kind, now, **{key: value for key, value in found.items() if value is not None})


# ---------------------------------------------------------------------------------------------------
# Pruning old boards


def retention_days(env=None):
    """`LOGBOOK_RETENTION_DAYS` when it is a positive integer, else the default."""
    env = os.environ if env is None else env
    value = env.get("LOGBOOK_RETENTION_DAYS")
    value = value.strip() if isinstance(value, str) else ""
    return int(value) if DIGITS.fullmatch(value) and int(value) > 0 else DEFAULT_RETENTION_DAYS


def is_regular(path):
    """Whether `path` itself is a regular file: a symbolic link is not, whatever it points at."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def last_activity(folder):
    """The time of the board's last event, else the modification time of its marker file."""
    for event in reversed(events(folder)):
        t = event.get("t")
        if isinstance(t, str) and UTC_TIME.fullmatch(t):
            try:
                return datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.fromtimestamp(os.lstat(os.path.join(folder, MARKER_FILE)).st_mtime, timezone.utc)


def prunable(folder, keep, cutoff):
    """Whether `folder` is a board this plugin made, other than `keep`, last active before `cutoff`."""
    name = os.path.basename(folder)
    if not SESSION.fullmatch(name) or set(name) == {"."}:
        return False
    if os.path.islink(folder) or not stat.S_ISDIR(os.lstat(folder).st_mode):
        return False
    if not is_regular(os.path.join(folder, MARKER_FILE)):
        return False
    if os.path.normpath(folder) == keep:
        return False
    return last_activity(folder) < cutoff


# A start builds its board in milliseconds, so a building folder this old belongs to a start that died.
ABANDONED_START_SECONDS = 3600


def abandoned_start(folder, now):
    """Whether `folder` is a board a start was building when it was killed, before it wrote the marker.

    Such a folder carries no marker, so `prunable` never takes it. It is taken when it is a real
    directory (not a link) named as a session id with `STARTING` in it, has no marker at all, holds
    nothing but regular files named as board files or their temporary files, and was last modified
    more than ABANDONED_START_SECONDS before `now`.
    """
    name = os.path.basename(folder)
    if STARTING not in name or not SESSION.fullmatch(name):
        return False
    info = os.lstat(folder)
    if not stat.S_ISDIR(info.st_mode) or os.path.lexists(os.path.join(folder, MARKER_FILE)):
        return False
    for entry in os.listdir(folder):
        if not (entry in BOARD_FILES or TEMPORARY_FILE.fullmatch(entry)) or not is_regular(os.path.join(folder, entry)):
            return False
    return now.timestamp() - info.st_mtime > ABANDONED_START_SECONDS


def remove_board(folder):
    """Delete a board's own files by exact name, the marker last, then the folder. Returns whether it went.

    Nothing is recursed into and no link is followed: a name that is not a board file, or is not a
    regular file, stays, and so does the folder around it.
    """
    names = os.listdir(folder)
    doomed = [n for n in names if n != MARKER_FILE and (n in BOARD_FILES or TEMPORARY_FILE.fullmatch(n))]
    for name in doomed + [MARKER_FILE]:
        path = os.path.join(folder, name)
        if is_regular(path):
            os.unlink(path)
    try:
        os.rmdir(folder)
    except OSError:
        return False
    return True


def calls_file(env, session):
    """The file in which `gate.sh` counts a session's work calls, or None without a usable `CLAUDE_PLUGIN_DATA`
    (see `contained`).

    The session id becomes a path component of a file that is read and removed, so it is checked
    here as `board_dir` checks it, and a folder of counts that is a symbolic link is never used.
    """
    data = env.get("CLAUDE_PLUGIN_DATA")
    if not isinstance(data, str) or not data:
        return None
    data = contained(data, env)
    if data is None:
        return None
    session = str(session)
    if not SESSION.fullmatch(session) or set(session) == {"."}:
        return None
    if os.path.islink(os.path.join(data, CALLS_DIR)):
        return None
    return os.path.join(data, CALLS_DIR, session)


def calls_made(env, session):
    """How many work calls the gate has counted for a session: the length of its file, else 0."""
    path = calls_file(env, session)
    try:
        return os.stat(path).st_size if path else 0
    except OSError:
        return 0


def calls_changed(env, session):
    """Whether the gate has counted a work call that changed something for a session: its file holds a
    `c`, the byte the gate writes for a file tool that succeeded or a `git commit`. Never writes."""
    path = calls_file(env, session)
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            return b"c" in f.read()
    except OSError:
        return False


def forget_calls(env, session):
    """Remove the gate's count of this session's work calls.

    The gate starts the hook's handler on every work call while the count is at the threshold or over
    it, so the count goes once a start has been tried, whether or not it worked. A board that cannot
    be started (a project that cannot be written to, a boards folder that is refused) is then tried
    again a threshold of calls later, not on every call.
    """
    path = calls_file(env, session)
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def prune_calls(now, env):
    """Remove the gate's count files last written before the retention began. Never raises.

    Only a direct child of `$CLAUDE_PLUGIN_DATA/calls` is removed, by exact path, and only when it is
    named as a session id and is itself a regular file: a link is never followed or removed.
    """
    try:
        data = env.get("CLAUDE_PLUGIN_DATA")
        if not isinstance(data, str) or not data:
            return
        data = contained(data, env)
        if data is None:
            return
        folder = os.path.join(data, CALLS_DIR)
        if os.path.islink(folder) or not os.path.isdir(folder):
            return
        cutoff = (now - timedelta(days=retention_days(env))).timestamp()
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            try:
                if not SESSION.fullmatch(name) or set(name) == {"."} or not is_regular(path):
                    continue
                if os.lstat(path).st_mtime < cutoff:
                    os.unlink(path)
            except OSError:
                continue
    except Exception:
        pass


def prune(project, now, env=None, keep=None):
    """Remove the boards in `<project>/.logbook` idle for longer than the retention. Never raises.

    A candidate is a direct child of that folder, and it is pruned only when it is a real directory
    (not a link), is named as a session id, carries the marker file as a regular file, is not `keep`
    (the current session's board) and last recorded anything before the retention began. A folder a
    start was building when it was killed, before it wrote the marker, is removed once it is an hour
    old (see `abandoned_start`). Returns the folders removed; after touching any board, the index is
    rewritten. The gate's count files older than the retention go too (`prune_calls`); they are not
    in what it returns.
    """
    removed, touched = [], False
    prune_calls(now, os.environ if env is None else env)
    try:
        boards = os.path.join(os.path.abspath(project), BOARDS_DIR)
        if os.path.islink(boards) or not os.path.isdir(boards):
            return removed
        keep = os.path.normpath(os.path.abspath(keep)) if keep else None
        cutoff = now - timedelta(days=retention_days(env))
        for name in sorted(os.listdir(boards)):
            folder = os.path.join(boards, name)
            try:
                if not (prunable(folder, keep, cutoff) or abandoned_start(folder, now)):
                    continue
                touched = True
                if remove_board(folder):
                    removed.append(folder)
            except Exception:
                continue
        if touched:
            write_atomic(os.path.join(boards, INDEX_FILE), index_page(board_rows(boards), settings(env)["theme"]))
    except Exception:
        pass
    return removed


# ---------------------------------------------------------------------------------------------------
# Next-session mode


def brief_path(project, path):
    """The real path of `path` (relative to `project`, or absolute) when it is a file path inside the project, else None."""
    project = os.path.realpath(project)
    resolved = os.path.realpath(os.path.join(project, path))
    try:
        inside = os.path.commonpath([resolved, project]) == project
    except ValueError:
        return None
    if not inside or resolved == project or os.path.isdir(resolved):
        return None
    return resolved


def next_session(project, *, count_lines=True):
    """The project's next-session setting, or None when the mode is off or its setting cannot be used.

    On: `{"path": the brief's real path, "exists": bool, "written": utc time or None, "lines": int or None}`.
    With `count_lines` false the brief is never opened and `lines` stays None. A brief that is not a regular file
    (a FIFO, a device) is never opened either, and counts as not written.
    The setting is `<project>/.logbook/next-session.json`, `{"path": "<relative to the project>"}`; one in a
    linked boards folder, one that does not parse, or one naming a path outside the project is no setting.
    """
    boards = os.path.join(project, BOARDS_DIR)
    if os.path.islink(boards):
        return None
    try:
        setting = json.loads(read_text(os.path.join(boards, NEXT_SESSION_FILE)))
    except (OSError, ValueError):
        return None
    relative = setting.get("path") if isinstance(setting, dict) else None
    path = brief_path(project, relative) if isinstance(relative, str) and relative else None
    if path is None:
        return None
    found = {"path": path, "exists": False, "written": None, "lines": None}
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return found
        written, lines = info.st_mtime, None
        if count_lines:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = len(f.read().splitlines())
    except OSError:
        return found
    found.update(exists=True, written=utc(datetime.fromtimestamp(written, timezone.utc)), lines=lines)
    return found


def next_session_context(project):
    """What a session start tells the model about the brief, at most three short lines, or None when off.

    It names the brief and when it was written, never what it holds.
    """
    brief = next_session(project)
    if brief is None:
        return None
    if not brief["exists"]:
        return (
            f"Next-session brief: {brief['path']} does not exist yet.\n"
            "When the work is done, write it for the session after, using /logbook next."
        )
    return (
        f"Next-session brief: {brief['path']} (written {brief['written']}, {brief['lines']} lines).\n"
        "Read it when the user asks you to pick up or carry on the work, not otherwise.\n"
        "When the work it describes is done, or the user asks, rewrite it whole for the session after, "
        "using /logbook next."
    )


def one_line(value):
    return " ".join(str(value).split())


def next_facts(state):
    """What a board holds that a next-session brief should carry forward, one line each.

    Unanswered questions with their defaults, hard stops still open, decisions, checks that failed and
    deliverables, in that order. A failed check is dropped once a later check that passed proves the same thing
    or ran the same command (each compared with its whitespace collapsed, and only when it has one).
    """
    lines = []
    for question in state.get("questions") or []:
        if question.get("status") == "answered":
            continue
        if question.get("hardStop"):
            lines.append(f"{question['id']} stop: {one_line(question['text'])}")
        else:
            default = f" (default: {one_line(question['default'])})" if question.get("default") else ""
            lines.append(f"{question['id']} open: {one_line(question['text'])}{default}")
    for decision in state.get("decisions") or []:
        why = f" (why: {one_line(decision['why'])})" if decision.get("why") else ""
        lines.append(f"{decision['id']} decision: {one_line(decision['text'])}{why}")
    checks = state.get("checks") or []
    for index, check in enumerate(checks):
        if check.get("result") == "fail" and not any(
            later.get("result") == "pass" and any(
                check.get(key) and later.get(key) and one_line(check[key]) == one_line(later[key])
                for key in ("proves", "command")
            )
            for later in checks[index + 1:]
        ):
            command = f" (command: {one_line(check['command'])})" if check.get("command") else ""
            lines.append(f"{check['id']} failed: {one_line(check.get('proves') or '')}{command}")
    for deliverable in state.get("deliverables") or []:
        where = deliverable.get("path") or deliverable.get("url")
        where = f" ({one_line(where)})" if where else ""
        lines.append(f"deliverable: {one_line(deliverable['label'])}{where}")
    return lines


# ---------------------------------------------------------------------------------------------------
# Command line


def is_board(board):
    return os.path.isfile(os.path.join(board, MARKER_FILE))


class Refused(Exception):
    """A command that would record something the board cannot take. The message is its one line."""


def clock():
    """The time a command records. The functions under the command line take `now` instead."""
    return datetime.now(timezone.utc)


def find_board(directory, env):
    """(board, None) for the session's board in `directory` or the nearest folder above it; (None, why) otherwise."""
    session = env.get(SESSION_VARIABLE) or ""
    if not session:
        return None, "no board is active for this session"
    try:
        board_dir(directory, session)
    except ValueError:
        return None, f"not a usable session id in {SESSION_VARIABLE}"
    directory = os.path.abspath(directory)
    while True:
        board = board_dir(directory, session)
        if is_board(board):
            if os.path.islink(boards_of(board)):
                return None, LINKED_BOARDS
            return board, None
        parent = os.path.dirname(directory)
        if parent == directory:
            return None, "no board is active for this session"
        directory = parent


def entry_text(value):
    """A text argument: refused when empty, cut at TEXT_CAP characters, otherwise stored as given."""
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value[:TEXT_CAP]


def open_state(board):
    """The board's state from its log alone, refusing a board that has been closed."""
    state = derive(events(board), [], {})
    if state["state"] == "finished":
        raise Refused("this board is closed and records nothing more")
    return state


def recorded(board, now, kind, **fields):
    """Append one entry to an open board and render it. Returns (the event appended, the state rendered)."""
    open_state(board)
    event = append(board, kind, now, **fields)
    return event, render(board, now)


def entry_id(items, **wanted):
    """The id the state gives the entry just recorded: the latest item carrying exactly these values."""
    return next((item["id"] for item in reversed(items) if all(item.get(k) == v for k, v in wanted.items())), "")


def project_of(args):
    project = contained(os.path.abspath(args.project or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()), os.environ)
    if project is None:
        raise Refused(f"the project folder is outside the home and temporary folders; set {ALLOW_ANY_PATH}=1 to use it")
    return project


def command_start(args, board, now):
    """Start this session's board, already announced; on one that is running, only set the title."""
    session, project = os.environ.get(SESSION_VARIABLE) or "", project_of(args)
    try:
        board = board_dir(project, session)
    except ValueError:
        raise Refused(f"not a usable session id in {SESSION_VARIABLE}")
    page = os.path.join(board, BOARD_FILE)
    if is_board(board):
        open_state(board)
        if args.title:
            recorded(board, now, "title", title=args.title)
        return f"board already running: {page}"
    if start(project, session, now, args.title or UNTITLED) is None and not is_board(board):
        raise Refused(refused_boards(os.path.dirname(board)) or "no board was started")
    if args.title:
        recorded(board, now, "title", title=args.title)
    try:
        os.close(os.open(os.path.join(board, ANNOUNCED_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        pass
    return f"board started: {page}"


def command_prune(args, board, now):
    project, session = project_of(args), os.environ.get(SESSION_VARIABLE) or ""
    try:
        keep = board_dir(project, session)
    except ValueError:
        keep = None
    return f"pruned {len(prune(project, now, keep=keep))} boards"


def command_render(args, board, now):
    render(board, now)


def command_close(args, board, now):
    close(board, now)
    return f"report written: {os.path.join(os.path.abspath(board), REPORT_FILE)}"


def command_title(args, board, now):
    recorded(board, now, "title", title=args.text)
    return "title set"


def command_question(args, board, now):
    event, state = recorded(
        board, now, "question", text=args.text, default=args.default, affects=args.affects,
        reverse=args.reverse, hardStop=False,
    )
    return f"{entry_id(state['questions'], text=event['text'], asked=event['t'], hardStop=False)} recorded"


def command_stop(args, board, now):
    event, state = recorded(
        board, now, "question", text=args.text, default=None, affects=args.affects, reverse=None, hardStop=True,
    )
    return f"{entry_id(state['questions'], text=event['text'], asked=event['t'], hardStop=True)} recorded"


def command_answer(args, board, now):
    question = find_question(open_state(board)["questions"], args.id)
    if question is None:
        raise Refused(f"no question {args.id} on this board")
    if question["status"] == "answered":
        raise Refused(f"{question['id']} is already answered")
    append(board, "answer", now, id=question["id"], answer=args.text)
    render(board, now)
    return f"{question['id']} answered"


def command_decision(args, board, now):
    event, state = recorded(board, now, "decision", text=args.text, why=args.why, reverse=args.reverse)
    return f"{entry_id(state['decisions'], text=event['text'], time=event['t'])} recorded"


def command_deliverable(args, board, now):
    recorded(board, now, "deliverable", label=args.label, path=args.path, url=args.url, step=args.step)
    return "deliverable recorded"


def command_check(args, board, now):
    event, state = recorded(
        board, now, "check", proves=args.text, command=args.command, result=args.result, source="model", agent=None,
    )
    found = entry_id(state["checks"], proves=event["proves"], command=args.command, time=event["t"], source="model")
    return f"{found} recorded: {args.result}"


def brief_line(brief):
    if not brief["exists"]:
        return f"brief: {brief['path']} (not written yet)"
    return f"brief: {brief['path']} (written {brief['written']}, {brief['lines']} lines)"


def command_next_on(args, board, now):
    """Turn next-session mode on for the project: the setting names the brief, relative to the project."""
    project = project_of(args)
    path = brief_path(project, args.path)
    if path is None:
        raise Refused(f"{args.path} is not a file path inside the project {project}")
    boards = os.path.join(project, BOARDS_DIR)
    if os.path.islink(boards):
        raise Refused(LINKED_BOARDS)
    if refused_boards(boards):
        raise Refused("the boards folder is not a folder, so nothing was recorded")
    os.makedirs(boards, exist_ok=True)
    ensure_ignored(boards)
    write_atomic(os.path.join(boards, NEXT_SESSION_FILE), json.dumps({"path": os.path.relpath(path, project)}) + "\n")
    return f"next-session mode on\n{brief_line(next_session(project))}"


def command_next_off(args, board, now):
    """Turn next-session mode off: the setting goes, the brief is never touched."""
    project = project_of(args)
    boards = os.path.join(project, BOARDS_DIR)
    if os.path.islink(boards):
        raise Refused(LINKED_BOARDS)
    try:
        os.unlink(os.path.join(boards, NEXT_SESSION_FILE))
    except FileNotFoundError:
        return "next-session mode was already off"
    return "next-session mode off; the brief was left as it is"


def command_next_status(args, board, now):
    brief = next_session(project_of(args))
    if brief is None:
        return "next-session mode off"
    return f"next-session mode on\n{brief_line(brief)}"


def command_next_facts(args, board, now):
    """This session's board's entries that a brief should carry forward; nothing when there is no board."""
    board = args.board
    if board is None:
        board, _ = find_board(os.getcwd(), os.environ)
    elif os.path.islink(boards_of(board)) or not is_board(board):
        board = None
    state = read_state(board) if board else None
    return "\n".join(next_facts(state)) if state else ""


def parser():
    """The command line. Each sub-command sets `run` to its function, and `needs_board` when it works on one.

    `start`, `prune` and `next on|off|status` work on a project: `--project DIR`, else `CLAUDE_PROJECT_DIR`,
    else the current directory. `render` is the hooks' and needs `--board`. The rest take `--board DIR`, and
    without it find the session's board from the current directory; `next facts` prints nothing without one.
    """
    top = argparse.ArgumentParser(prog="board.py", description="Record and render a logbook.")
    commands = top.add_subparsers(dest="subcommand", metavar="COMMAND")
    commands.required = True
    project_help = "the project folder (default: CLAUDE_PROJECT_DIR, else the current directory)"
    sub = commands.add_parser("start", help="start this session's board")
    sub.add_argument("title", nargs="?", type=entry_text)
    sub.add_argument("--project", help=project_help)
    sub.set_defaults(run=command_start, needs_board=False)
    sub = commands.add_parser("prune", help="remove boards older than the retention setting")
    sub.add_argument("--project", help=project_help)
    sub.set_defaults(run=command_prune, needs_board=False)
    sub = commands.add_parser("render", help="write state.js (and board.html if it is missing)")
    sub.add_argument("--board", required=True, help="the board folder")
    sub.set_defaults(run=command_render, needs_board=True)

    def model_command(name, run, summary):
        sub = commands.add_parser(name, help=summary)
        sub.add_argument("--board", help="the board folder (default: this session's board)")
        sub.set_defaults(run=run, needs_board=True)
        return sub

    model_command("close", command_close, "record the end of the task and write report.html")

    sub = model_command("title", command_title, "set the board's title")
    sub.add_argument("text", type=entry_text)
    sub = model_command("question", command_question, "ask a question, and say what happens if nobody answers")
    sub.add_argument("text", type=entry_text)
    sub.add_argument("--default", required=True, type=entry_text, help="what is done if nobody answers")
    sub.add_argument("--affects", type=entry_text, help="what the answer changes")
    sub.add_argument("--reverse", type=entry_text, help="how to undo the default later")
    sub = model_command("stop", command_stop, "record a hard stop: the task is blocked until it is answered")
    sub.add_argument("text", type=entry_text)
    sub.add_argument("--affects", type=entry_text, help="what is blocked")
    sub = model_command("answer", command_answer, "answer a question by its id (Q3, q3 or 3)")
    sub.add_argument("id")
    sub.add_argument("text", type=entry_text)
    sub = model_command("decision", command_decision, "record a decision taken")
    sub.add_argument("text", type=entry_text)
    sub.add_argument("--why", type=entry_text)
    sub.add_argument("--reverse", type=entry_text, help="how to undo it")
    sub = model_command("deliverable", command_deliverable, "record something made: a file or a link")
    sub.add_argument("label", type=entry_text)
    where = sub.add_mutually_exclusive_group(required=True)
    where.add_argument("--path", type=entry_text)
    where.add_argument("--url", type=entry_text)
    sub.add_argument("--step", type=entry_text, help="the step it belongs to")
    sub = model_command("check", command_check, "record a check run and what it proved")
    sub.add_argument("text", type=entry_text)
    sub.add_argument("--command", required=True, type=entry_text)
    sub.add_argument("--result", required=True, choices=CHECK_RESULTS)

    sub = commands.add_parser("next", help="next-session mode: one standing brief for the session that picks the work up")
    modes = sub.add_subparsers(dest="mode", metavar="MODE")
    modes.required = True
    sub = modes.add_parser("on", help="turn the mode on; the brief's path is relative to the project")
    sub.add_argument("path", nargs="?", default=DEFAULT_BRIEF, type=entry_text, help=f"default: {DEFAULT_BRIEF}")
    sub.add_argument("--project", help=project_help)
    sub.set_defaults(run=command_next_on, needs_board=False)
    sub = modes.add_parser("off", help="turn the mode off; the brief is left as it is")
    sub.add_argument("--project", help=project_help)
    sub.set_defaults(run=command_next_off, needs_board=False)
    sub = modes.add_parser("status", help="on or off, the brief's path, when it was written and its length")
    sub.add_argument("--project", help=project_help)
    sub.set_defaults(run=command_next_status, needs_board=False)
    sub = modes.add_parser("facts", help="what this session's board holds that the brief should carry forward")
    sub.add_argument("--board", help="the board folder (default: this session's board)")
    sub.set_defaults(run=command_next_facts, needs_board=False)
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    board = args.board if args.needs_board else None
    if args.needs_board and board is None:
        board, problem = find_board(os.getcwd(), os.environ)
        if board is None:
            print(f"logbook: {problem}", file=sys.stderr)
            return 1
    elif args.needs_board and os.path.islink(boards_of(board)):
        print(f"logbook: {LINKED_BOARDS}", file=sys.stderr)
        return 1
    elif args.needs_board and not is_board(board):
        print(f"logbook: no board at {board}", file=sys.stderr)
        return 1
    try:
        line = args.run(args, board, clock())
    except (Refused, OSError, ValueError) as error:
        print(f"logbook: {error}", file=sys.stderr)
        return 1
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
