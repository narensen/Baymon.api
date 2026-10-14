"""Tools the agent can call. They run locally, on the user's machine.

Reads inside the working directory are automatic. Writes, edits, shell commands,
reads outside the working directory and forgetting memories ask the user first
(unless auto-approve is on).
"""

from __future__ import annotations

import difflib
import fnmatch
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from baymon.cli.client import Client

MAX_RESULT_CHARS = 40_000
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".next", ".nuxt", ".cache", ".idea", ".gradle",
    "dist", "build", "target", ".turbo", ".parcel-cache", "coverage",
}
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------


@dataclass
class Decision:
    allowed: bool
    feedback: str = ""
    always: bool = False


class ToolUI(Protocol):
    def approve(self, tool: str, title: str, preview: str | None, preview_lang: str) -> Decision: ...
    def command_output(self, line: str) -> None: ...
    def show_plan(self, steps: list[dict[str, str]]) -> None: ...


@dataclass
class ToolContext:
    cwd: Path
    client: Client
    ui: ToolUI
    auto_approve: bool = False
    always_allowed: set[str] = field(default_factory=set)
    requested_mode: str | None = None


@dataclass
class ToolResult:
    content: str  # sent back to the model
    summary: str = ""  # one line for the terminal
    error: bool = False


class ToolError(Exception):
    pass


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[dict[str, Any], ToolContext], ToolResult]
    label: Callable[[dict[str, Any]], str]  # short "what it's doing" line

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(path or ".")))
    return (p if p.is_absolute() else ctx.cwd / p).resolve()


def _inside(ctx: ToolContext, path: Path) -> bool:
    root = ctx.cwd.resolve()
    return path == root or root in path.parents


def _rel(ctx: ToolContext, path: Path) -> str:
    try:
        return str(path.relative_to(ctx.cwd.resolve())) or "."
    except ValueError:
        return str(path)


def _ask(ctx: ToolContext, tool: str, title: str, preview: str | None = None, lang: str = "text") -> None:
    """Raise ToolError if the user declines."""
    if ctx.auto_approve or tool in ctx.always_allowed:
        return
    decision = ctx.ui.approve(tool, title, preview, lang)
    if decision.always:
        ctx.always_allowed.add(tool)
    if not decision.allowed:
        msg = "The user declined this action."
        if decision.feedback:
            msg += f" They said: {decision.feedback}"
        raise ToolError(msg)


def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    tail = limit - head
    return f"{text[:head]}\n\n[... {len(text) - limit} characters omitted ...]\n\n{text[-tail:]}"


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return False


def _walk_files(root: Path, include: str | None = None) -> list[Path]:
    out: list[Path] = []
    if root.is_file():
        return [root]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
        for name in filenames:
            if include and not fnmatch.fnmatch(name, include):
                continue
            out.append(Path(dirpath) / name)
    return out


def _diff(old: str, new: str, path: str) -> str:
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}", n=3)
    return "".join(lines)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file's existing line endings on Windows-style files.
    newline = "\r\n" if path.exists() and b"\r\n" in path.read_bytes()[:65536] else "\n"
    with path.open("w", encoding="utf-8", newline=newline) as f:
        f.write(content.replace("\r\n", "\n"))


# --------------------------------------------------------------------------
# File tools
# --------------------------------------------------------------------------


def read_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _resolve(ctx, args["path"])
    if not _inside(ctx, path):
        _ask(ctx, "read_outside", f"Read {path} (outside the working directory)?")
    if not path.exists():
        raise ToolError(f"File not found: {_rel(ctx, path)}")
    if path.is_dir():
        raise ToolError(f"{_rel(ctx, path)} is a directory; use list_dir.")
    if _is_binary(path):
        return ToolResult(f"{_rel(ctx, path)} is a binary file ({path.stat().st_size} bytes).", "binary file")
    lines = _read_text(path).splitlines()
    start = max(int(args.get("offset") or 1), 1)
    limit = min(int(args.get("limit") or 400), 2000)
    chunk = lines[start - 1 : start - 1 + limit]
    body = "\n".join(f"{n:>6}\t{line[:2000]}" for n, line in enumerate(chunk, start))
    end = start + len(chunk) - 1
    more = f" — {len(lines) - end} more lines; use offset={end + 1}" if end < len(lines) else ""
    header = f"{_rel(ctx, path)} (lines {start}-{end} of {len(lines)}{more})"
    return ToolResult(_clip(f"{header}\n{body}"), f"{len(chunk)} lines")


def list_dir(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _resolve(ctx, args.get("path") or ".")
    if not _inside(ctx, path):
        _ask(ctx, "read_outside", f"List {path} (outside the working directory)?")
    if not path.is_dir():
        raise ToolError(f"Not a directory: {_rel(ctx, path)}")
    depth = min(int(args.get("depth") or 1), 4)
    lines: list[str] = []

    def walk(d: Path, level: int) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            lines.append(f"{'  ' * level}[unreadable: {exc.strerror}]")
            return
        for entry in entries:
            if len(lines) >= 500:
                return
            if entry.is_dir():
                skipped = entry.name in IGNORED_DIRS
                lines.append(f"{'  ' * level}{entry.name}/" + ("  (skipped)" if skipped else ""))
                if not skipped and level + 1 < depth:
                    walk(entry, level + 1)
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{'  ' * level}{entry.name}  ({size:,} B)")

    walk(path, 0)
    return ToolResult(f"{_rel(ctx, path)}/\n" + "\n".join(lines or ["(empty)"]), f"{len(lines)} entries")


def glob_files(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    root = _resolve(ctx, args.get("path") or ".")
    if not _inside(ctx, root):
        _ask(ctx, "read_outside", f"Search {root} (outside the working directory)?")
    pattern = args["pattern"]
    matches = [
        p for p in root.glob(pattern)
        if p.is_file() and not any(part in IGNORED_DIRS for part in p.relative_to(root).parts)
    ]
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    shown = matches[:200]
    body = "\n".join(_rel(ctx, p) for p in shown) or "No files matched."
    if len(matches) > len(shown):
        body += f"\n[... {len(matches) - len(shown)} more]"
    return ToolResult(body, f"{len(matches)} files")


def grep(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    root = _resolve(ctx, args.get("path") or ".")
    if not _inside(ctx, root):
        _ask(ctx, "read_outside", f"Search {root} (outside the working directory)?")
    pattern, include = args["pattern"], args.get("include")
    ignore_case = bool(args.get("ignore_case"))
    limit = min(int(args.get("max_results") or 200), 1000)

    if rg := shutil.which("rg"):
        cmd = [rg, "--line-number", "--no-heading", "--color", "never", "--max-columns", "300", "-m", "50"]
        if ignore_case:
            cmd.append("-i")
        if include:
            cmd += ["-g", include]
        target = "." if root.is_dir() else root.name
        cmd += ["-e", pattern, target]
        proc = subprocess.run(cmd, cwd=root if root.is_dir() else root.parent, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        if proc.returncode == 2:
            raise ToolError(proc.stderr.strip() or "ripgrep failed")
        base = root if root.is_dir() else root.parent
        hits = []
        for line in proc.stdout.splitlines():
            rel_path, sep, rest = line.partition(":")  # paths are relative here, so no drive-letter colons
            if sep:
                hits.append(f"{_rel(ctx, (base / rel_path).resolve())}:{rest}")
    else:
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise ToolError(f"Invalid regex: {exc}") from exc
        hits = []
        for file in _walk_files(root, include):
            if len(hits) >= limit or _is_binary(file):
                continue
            try:
                if file.stat().st_size > 2_000_000:
                    continue
                for n, line in enumerate(_read_text(file).splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{_rel(ctx, file)}:{n}:{line[:300]}")
                        if len(hits) >= limit:
                            break
            except OSError:
                continue
    body = "\n".join(hits[:limit]) or "No matches."
    if len(hits) > limit:
        body += f"\n[... more matches; narrow the search]"
    return ToolResult(_clip(body), f"{min(len(hits), limit)} matches")


def write_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _resolve(ctx, args["path"])
    content: str = args["content"]
    rel = _rel(ctx, path)
    if path.is_dir():
        raise ToolError(f"{rel} is a directory.")
    if path.exists():
        old = _read_text(path)
        if old == content:
            return ToolResult(f"{rel} already has this content; nothing changed.", "unchanged")
        _ask(ctx, "write_file", f"Overwrite {rel}?", _diff(old, content, rel), "diff")
    else:
        preview = "\n".join(content.splitlines()[:60])
        _ask(ctx, "write_file", f"Create {rel} ({len(content.splitlines())} lines)?", preview, _lang(path))
    _write_text(path, content)
    return ToolResult(f"Wrote {len(content.splitlines())} lines to {rel}.", f"wrote {len(content.splitlines())} lines")


def edit_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _resolve(ctx, args["path"])
    rel = _rel(ctx, path)
    if not path.is_file():
        raise ToolError(f"File not found: {rel}. Use write_file to create it.")
    old_string, new_string = args["old_string"], args["new_string"]
    if old_string == new_string:
        raise ToolError("old_string and new_string are identical.")
    text = _read_text(path).replace("\r\n", "\n")
    old_norm = old_string.replace("\r\n", "\n")
    count = text.count(old_norm)
    if count == 0:
        raise ToolError(f"old_string was not found in {rel}. Read the file again and copy the exact text, including indentation.")
    if count > 1 and not args.get("replace_all"):
        raise ToolError(f"old_string appears {count} times in {rel}. Include more surrounding lines to make it unique, or set replace_all.")
    new_text = text.replace(old_norm, new_string.replace("\r\n", "\n"))
    diff = _diff(text, new_text, rel)
    _ask(ctx, "edit_file", f"Edit {rel}?", diff, "diff")
    _write_text(path, new_text)
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return ToolResult(f"Edited {rel} ({count} replacement{'s' if count > 1 else ''}).", f"+{added} -{removed}")


def _lang(path: Path) -> str:
    return {
        ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx", ".json": "json",
        ".md": "markdown", ".sh": "bash", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".html": "html",
        ".css": "css", ".rs": "rust", ".go": "go", ".java": "java", ".sql": "sql", ".c": "c", ".cpp": "cpp",
    }.get(path.suffix.lower(), "text")


# --------------------------------------------------------------------------
# Shell
# --------------------------------------------------------------------------


def _shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        return ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    shell = shutil.which("bash") or "/bin/sh"
    return [shell, "-c", command]


def run_command(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command: str = args["command"]
    cwd = _resolve(ctx, args.get("cwd") or ".")
    timeout = min(max(int(args.get("timeout") or 120), 1), 1800)
    where = "" if cwd == ctx.cwd.resolve() else f"  (in {_rel(ctx, cwd)})"
    _ask(ctx, "run_command", f"Run this command?{where}", command, "bash" if sys.platform != "win32" else "powershell")
    if not cwd.is_dir():
        raise ToolError(f"Directory not found: {_rel(ctx, cwd)}")

    env = {**os.environ, "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0"}
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        _shell_argv(command), cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs,
    )
    output: list[str] = []
    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        _kill(proc)

    watchdog = threading.Timer(timeout, on_timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:  # ends when the process exits or is killed
            clean = ANSI.sub("", line.rstrip("\n"))
            output.append(clean)
            ctx.ui.command_output(clean)
        proc.wait()
    except KeyboardInterrupt:
        _kill(proc)
        raise
    finally:
        watchdog.cancel()
    code = proc.poll()
    timed_out = timed_out.is_set()
    text = "\n".join(output)
    status = f"timed out after {timeout}s" if timed_out else f"exit code {code}"
    return ToolResult(_clip(f"$ {command}\n{text}\n[{status}]", 30_000), status, error=timed_out or code != 0)


def _kill(proc: subprocess.Popen[str]) -> None:
    try:
        if sys.platform == "win32":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()


# --------------------------------------------------------------------------
# Web
# --------------------------------------------------------------------------


def fetch_url(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    url: str = args["url"]
    if not re.match(r"^https?://", url):
        raise ToolError("Only http(s) URLs are supported.")
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=20, headers={"User-Agent": "Baymon/0.2 (+cli agent)"})
    except httpx.HTTPError as exc:
        raise ToolError(f"Couldn't fetch {url}: {exc}") from exc
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "html" in ctype:
        text = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", text)
        text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    elif "json" in ctype:
        try:
            text = json.dumps(resp.json(), indent=2)[:MAX_RESULT_CHARS]
        except ValueError:
            pass
    return ToolResult(_clip(f"{resp.status_code} {url}\n\n{text}", 30_000), f"{resp.status_code}, {len(text):,} chars")


# --------------------------------------------------------------------------
# Memory, plan, mode
# --------------------------------------------------------------------------


def remember(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    item = ctx.client.memory_add(args["content"], args.get("category") or "other")
    return ToolResult(f"Saved memory [{item['id']}].", "saved")


def recall(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    items = ctx.client.memory_list()
    query = (args.get("query") or "").lower().split()
    if query:
        items = [m for m in items if any(w in m["content"].lower() for w in query)]
    body = "\n".join(f"[{m['id']}] ({m['category']}) {m['content']}" for m in items) or "No matching memories."
    return ToolResult(_clip(body), f"{len(items)} memories")


def forget(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    memory_id = args["memory_id"]
    match = next((m for m in ctx.client.memory_list() if m["id"] == memory_id), None)
    if match is None:
        raise ToolError(f"No memory with id {memory_id}.")
    _ask(ctx, "forget", "Forget this memory?", match["content"])
    ctx.client.memory_delete(memory_id)
    return ToolResult(f"Forgot: {match['content']}", "forgotten")


def update_plan(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    steps = [
        {"step": str(s.get("step", "")), "status": s.get("status", "pending")}
        for s in args.get("steps", []) if isinstance(s, dict)
    ]
    ctx.ui.show_plan(steps)
    done = sum(s["status"] == "done" for s in steps)
    return ToolResult("Plan updated.", f"{done}/{len(steps)} done")


def switch_mode(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    mode = args.get("mode")
    if mode not in ("talk", "type"):
        raise ToolError("mode must be 'talk' or 'type'.")
    ctx.requested_mode = mode
    return ToolResult(f"Switching to {mode} mode after this reply. Briefly confirm to the user.", mode)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def _short(text: str, n: int = 70) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


TOOLS: list[Tool] = [
    Tool("read_file", "Read a text file. Returns numbered lines. Use offset/limit for large files.",
         _obj({"path": {"type": "string"}, "offset": {"type": "integer", "description": "1-based start line"},
               "limit": {"type": "integer", "description": "max lines (default 400)"}}, ["path"]),
         read_file, lambda a: f"Read {a.get('path')}"),
    Tool("list_dir", "List a directory (dirs end with /). depth 1-4.",
         _obj({"path": {"type": "string"}, "depth": {"type": "integer"}}),
         list_dir, lambda a: f"List {a.get('path') or '.'}"),
    Tool("glob", "Find files by glob pattern, e.g. '**/*.py' or 'src/**/test_*.ts'. Newest first.",
         _obj({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
         glob_files, lambda a: f"Find {a.get('pattern')}"),
    Tool("grep", "Search file contents with a regular expression. Returns path:line:text.",
         _obj({"pattern": {"type": "string"}, "path": {"type": "string"},
               "include": {"type": "string", "description": "filename glob, e.g. '*.py'"},
               "ignore_case": {"type": "boolean"}, "max_results": {"type": "integer"}}, ["pattern"]),
         grep, lambda a: f"Search {_short(a.get('pattern', ''), 40)!r}" + (f" in {a['path']}" if a.get("path") else "")),
    Tool("write_file", "Create a file or replace its entire content. Prefer edit_file for small changes.",
         _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
         write_file, lambda a: f"Write {a.get('path')}"),
    Tool("edit_file", "Replace an exact string in a file. old_string must match exactly (including "
         "indentation) and be unique unless replace_all is true. new_string REPLACES old_string entirely: "
         "to insert code next to an anchor line, repeat the anchor line in new_string.",
         _obj({"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
               "replace_all": {"type": "boolean"}}, ["path", "old_string", "new_string"]),
         edit_file, lambda a: f"Edit {a.get('path')}"),
    Tool("run_command", "Run a shell command (bash on Linux/macOS, PowerShell on Windows) and return its output. "
         "Non-interactive: stdin is closed. Default timeout 120 s.",
         _obj({"command": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "integer"}}, ["command"]),
         run_command, lambda a: f"Run {_short(a.get('command', ''))}"),
    Tool("fetch_url", "Fetch a web page or API over HTTP(S) and return its text.",
         _obj({"url": {"type": "string"}}, ["url"]),
         fetch_url, lambda a: f"Fetch {_short(a.get('url', ''), 60)}"),
    Tool("remember", "Save one durable fact about the user to long-term memory, as a self-contained "
         "third-person sentence (e.g. 'User prefers pnpm over npm.'). Never store secrets.",
         _obj({"content": {"type": "string"},
               "category": {"type": "string", "enum": ["preference", "fact", "task", "decision", "other"]}}, ["content"]),
         remember, lambda a: f"Remember: {_short(a.get('content', ''), 60)}"),
    Tool("recall", "Search long-term memory about the user. Empty query lists everything.",
         _obj({"query": {"type": "string"}}),
         recall, lambda a: f"Recall {a.get('query') or 'all'}"),
    Tool("forget", "Delete a memory by id (ids are shown in brackets).",
         _obj({"memory_id": {"type": "string"}}, ["memory_id"]),
         forget, lambda a: "Forget memory"),
    Tool("update_plan", "Show the user a short task plan and its progress. Send the full list each time.",
         _obj({"steps": {"type": "array", "items": _obj(
             {"step": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
             ["step", "status"])}}, ["steps"]),
         update_plan, lambda a: "Plan"),
    Tool("switch_mode", "Switch how the user talks to you. Call ONLY when the user explicitly asks, e.g. "
         "'switch to typing' or 'let's talk by voice'.",
         _obj({"mode": {"type": "string", "enum": ["talk", "type"]}}, ["mode"]),
         switch_mode, lambda a: f"Switch to {a.get('mode')} mode"),
]
BY_NAME = {t.name: t for t in TOOLS}


def schemas() -> list[dict[str, Any]]:
    return [t.schema() for t in TOOLS]


def execute(name: str, raw_args: str, ctx: ToolContext) -> tuple[Tool | None, dict[str, Any], ToolResult]:
    tool = BY_NAME.get(name)
    if tool is None:
        return None, {}, ToolResult(f"Unknown tool: {name}. Available: {', '.join(BY_NAME)}.", "unknown tool", True)
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as exc:
        return tool, {}, ToolResult(f"Invalid arguments for {name}: {exc}", "invalid arguments", True)
    return tool, args, run_tool(tool, args, ctx)


def run_tool(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    from baymon.cli.client import ApiError

    try:
        return tool.run(args, ctx)
    except ToolError as exc:
        return ToolResult(str(exc), str(exc)[:80], True)
    except KeyError as exc:
        return ToolResult(f"Missing required argument: {exc.args[0]}", "missing argument", True)
    except ApiError as exc:
        return ToolResult(f"Baymon server error: {exc.message}", exc.message[:80], True)
    except OSError as exc:
        return ToolResult(f"{type(exc).__name__}: {exc}", exc.strerror or str(exc), True)
