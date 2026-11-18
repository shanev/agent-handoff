#!/usr/bin/env python3
"""agent-handoff: move a coding-agent session to another machine.

The agent is running in an hc terminal (Herdr 2). This runs on the machine
that has the session. Everything that must happen on the target is done by
piping this same file to `python3 -` over ssh, so the target needs only
python3, git, hc, and the agent CLI (no install of this tool).

Stdlib only, Python 3.9+ (macOS ships 3.9).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VERSION = "0.6.3"
RESULT_MARK = "AGENT_HANDOFF_RESULT:"
HOME = Path.home()
CONFIG_DIR = HOME / ".config" / "agent-handoff"
TEXT_SUFFIXES = {".jsonl", ".json", ".md", ".txt", ".log", ""}


class HandoffError(Exception):
    pass


# ---------------------------------------------------------------- process helpers


LOGIN_PATH: Optional[str] = None  # set by adopt_login_path() on the target
LOGIN_ENV: Dict[str, str] = {}     # agent config vars (CLAUDE_CONFIG_DIR, ...) from the login shell


def resolve(cmd: List[str]) -> List[str]:
    return [shutil.which(cmd[0], path=LOGIN_PATH) or cmd[0], *cmd[1:]]


def run(cmd: List[str], cwd: Optional[str] = None,
        input: Optional[str] = None, check: bool = True) -> str:
    cmd = resolve(cmd)
    try:
        p = subprocess.run(cmd, cwd=cwd, input=input, capture_output=True, text=True)
    except FileNotFoundError:
        raise HandoffError(f"`{cmd[0]}` not found on PATH")
    if check and p.returncode != 0:
        detail = (p.stderr or p.stdout).strip()
        raise HandoffError(f"`{shlex.join(cmd)}` failed ({p.returncode}): {detail}")
    return p.stdout


def ok(cmd: List[str], cwd: Optional[str] = None) -> bool:
    try:
        return subprocess.run(resolve(cmd), cwd=cwd, capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def have(binary: str) -> bool:
    return shutil.which(binary, path=LOGIN_PATH) is not None


def git(repo: str, *args: str, index_file: Optional[str] = None, check: bool = True) -> str:
    prefix = ["env", f"GIT_INDEX_FILE={index_file}"] if index_file else []
    return run([*prefix, "git", "-C", repo, *args], check=check).strip()


def hc_json(*args: str) -> dict:
    """One hc command. JSON is the default when stdout is a pipe; --json makes
    `run` and `read` return JSON too (those two print text otherwise)."""
    if args and "--json" not in args:
        args = (args[0], "--json", *args[1:])
    out = run(["hc", *args]).strip()
    if not out:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"text": out}
    return data if isinstance(data, dict) else {"result": data}


def hc_get(addr: str) -> dict:
    return hc_json("get", addr)


def hc_server_up() -> bool:
    return ok(["hc", "ls", "--json"])


def require_hc() -> None:
    """hc must be installed and its server reachable."""
    if not have("hc"):
        raise HandoffError("missing on target: hc")
    if not hc_server_up():
        raise HandoffError("hc server not reachable on target (run `hc server start` there first)")


# hc `ls` labels agents with these words. Shells and other programs are not agents.
HC_KINDS = {"claude-code": "claude", "claude": "claude", "codex": "codex",
            "omp": "omp", "pi": "pi", "grok": "grok", "opencode": "opencode"}
SHELLS = {"sh", "bash", "zsh", "fish", "nu", "pwsh", "powershell", "cmd", "dash", "elvish"}


def status_state(info: dict) -> Optional[str]:
    status = info.get("status")
    if isinstance(status, dict):
        return status.get("state")
    return status or None


def status_app(info: dict) -> Optional[str]:
    status = info.get("status")
    return status.get("app") if isinstance(status, dict) else None


def fg_argv0(info: dict) -> str:
    fg = info.get("foreground") or {}
    argv = fg.get("argv") or []
    return os.path.basename(argv[0]) if argv else ""


def app_is(info: dict, binary: str) -> bool:
    app = status_app(info) or ""
    return bool(binary) and (app == binary or HC_KINDS.get(app) == binary)


def agent_in_foreground(info: dict, binary: str) -> bool:
    if not binary:
        return False
    name = (info.get("foreground") or {}).get("name") or ""
    base = fg_argv0(info)
    return base == binary or name == binary or name.startswith(binary + "-") or name.startswith(binary + ".")


def shell_back(info: dict, binary: str = "") -> bool:
    """The terminal's program is a shell again (the agent has quit)."""
    if info.get("state") == "finished":
        return True
    if binary and (agent_in_foreground(info, binary) or app_is(info, binary)):
        return False
    fg = info.get("foreground") or {}
    if info.get("pid") and fg.get("pid") == info.get("pid"):
        return True
    name = fg.get("name") or ""
    return fg_argv0(info) in SHELLS or name in SHELLS


def kind_of_hc(row: dict) -> Optional[str]:
    for label in (row.get("agent") or "", status_app(row) or "", fg_argv0(row)):
        if label in HC_KINDS:
            return HC_KINDS[label]
        if label in ADAPTERS:
            return label
    return None


def hc_version() -> str:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", run(["hc", "--version"], check=False))
    return ".".join(m.groups()) if m else "unknown"


def log(msg: str) -> None:
    print(f"agent-handoff: {msg}", file=sys.stderr, flush=True)


CONFIG_VARS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR", "GROK_HOME",
               "OPENCODE_DATA_DIR", "OPENCODE_DB", "XDG_DATA_HOME")


def adopt_login_path() -> None:
    """Non-interactive ssh gets a bare environment. Read the login shell's PATH
    (so hc, claude, codex, omp in /opt/homebrew/bin or ~/.local/bin resolve)
    and the agents' config-dir variables. Uses `env` so it works in any shell;
    only the variables named here are kept."""
    global LOGIN_PATH
    shell = os.environ.get("SHELL") or "/bin/sh"
    try:
        out = subprocess.run([shell, "-ilc", "echo __AH_BEGIN__; env"],
                             capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    login: Dict[str, str] = {}
    for line in out.split("__AH_BEGIN__")[-1].splitlines():
        key, sep, val = line.partition("=")
        if sep and key in ("PATH", *CONFIG_VARS):
            login[key] = val
    LOGIN_ENV.update({k: v for k, v in login.items() if k != "PATH"})
    parts = login.get("PATH", "").split(":") + os.environ.get("PATH", "").split(":")
    parts += ["/opt/homebrew/bin", "/usr/local/bin", str(HOME / ".local/bin")]
    seen: List[str] = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    LOGIN_PATH = ":".join(seen)


# ---------------------------------------------------------------- git helpers


def normalize_remote(url: str) -> str:
    """git@github.com:O/R.git, https://github.com/O/R, ssh://git@github.com:22/O/R
    all become github.com/o/r so the same repo matches across machines."""
    u = url.strip()
    if "://" in u:
        u = u.split("://", 1)[1]
        u = u.split("@", 1)[1] if "@" in u.split("/", 1)[0] else u
        host, _, path = u.partition("/")
        host = host.split(":", 1)[0]
    elif re.match(r"^[^/]+:", u):  # scp-like user@host:path
        u = u.split("@", 1)[1] if "@" in u.split(":", 1)[0] else u
        host, _, path = u.partition(":")
    else:  # local path remote
        return os.path.realpath(u)
    path = re.sub(r"\.git$", "", path.strip("/"))
    return f"{host.lower()}/{path.lower()}"


def remotes_of(repo: str) -> List[str]:
    out = git(repo, "config", "--get-regexp", r"^remote\..*\.url$", check=False)
    return sorted({normalize_remote(line.split(None, 1)[1]) for line in out.splitlines() if " " in line})


def is_dirty(repo: str, untracked: bool = True) -> bool:
    """Untracked files are not changes to the checkout. A handoff can resume
    there without touching them; tracked edits still have to be left alone."""
    args = ["status", "--porcelain"]
    if not untracked:
        args.append("--untracked-files=no")
    return bool(git(repo, *args))


def worktrees(repo: str) -> List[dict]:
    out, cur = [], {}
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if not line:
            if cur:
                out.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val or True
    if cur:
        out.append(cur)
    return out


def is_ancestor(repo: str, a: str, b: str) -> bool:
    return ok(["git", "-C", repo, "merge-base", "--is-ancestor", a, b])


def ref_exists(repo: str, ref: str) -> bool:
    return ok(["git", "-C", repo, "rev-parse", "--verify", "-q", ref])


# ---------------------------------------------------------------- adapters
#
# An adapter answers, for one agent kind: where is the transcript (locate), where
# does it go on the target and what path-like strings inside it must change
# (place), and how to resume it (resume_args). Add an agent by adding a class.


def claude_project_dir(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def omp_session_dir(cwd: str, home: str) -> str:
    rel = os.path.relpath(cwd, home)
    if rel == ".":
        return "-"
    if not rel.startswith(".."):
        return "-" + rel.replace("/", "-")
    return "--" + cwd.strip("/").replace("/", "-") + "--"


class Adapter:
    kind = ""
    binary = ""
    exit_text = "/exit"
    # Flags the resumed process needs so its work stays in the hc terminal.
    launch_flags: Tuple[str, ...] = ()
    # Where the agent keeps sessions: $<config_var> if set, else ~/<default_root>.
    config_var: Optional[str] = None
    default_root = ""
    # First-run "do you trust this folder?" screen, and the option that says yes.
    trust_marker: Optional[str] = None
    trust_yes: Optional[str] = None
    # Screens that are the user's call (never answered by this tool), e.g. Codex
    # asking to review hooks.
    user_screens: Dict[str, str] = {}
    # Files/dirs holding snapshots of the user's own files: copied without rewriting.
    verbatim: Tuple[str, ...] = ()

    def config_root(self, env: Dict[str, str]) -> Path:
        custom = env.get(self.config_var) if self.config_var else None
        return Path(custom).expanduser() if custom else HOME / self.default_root

    def source_roots(self) -> List[Path]:
        """Where to look on this machine: the configured root, then the default."""
        roots = [self.config_root({k: os.environ.get(k, "") for k in CONFIG_VARS}),
                 HOME / self.default_root]
        return [r for i, r in enumerate(roots) if r not in roots[:i]]

    def locate(self, handle: dict, src_cwd: str) -> Tuple[Path, List[Path]]:
        """(primary transcript, extra files/dirs to carry with it)."""
        raise NotImplementedError

    def guess_handle(self, src_cwd: str, started_at: float) -> Optional[dict]:
        """Find the session from the transcript on disk. None if it can't."""
        return None

    def place(self, primary: Path, dst_cwd: str, dst_home: str, dst_root: str
              ) -> Tuple[Dict[str, str], List[Tuple[str, str]], str]:
        """({src abs path: dst abs path}, extra rewrites, dst handle)."""
        raise NotImplementedError

    def resume_args(self, dst_handle: str) -> List[str]:
        raise NotImplementedError

    def capture(self, handle: dict, src_cwd: str, primary: Path, extras: List[Path]
                ) -> Tuple[Path, List[Path]]:
        """The files to copy, after the agent has quit. Default: what locate found."""
        return primary, extras

    def prelude(self, dst_handle: str, dst_cwd: str, mapping: Dict[str, str]) -> List[dict]:
        """Commands to run in the target shell before the agent. Each is
        {"cmd": line, "expect": regex that must show up}, expect optional."""
        return []


def newest_since(files, started_at: float) -> Optional[Path]:
    hits = [(f.stat().st_mtime, f) for f in files if f.stat().st_mtime >= started_at]
    return max(hits)[1] if hits else None


class Claude(Adapter):
    kind = binary = "claude"
    config_var, default_root = "CLAUDE_CONFIG_DIR", ".claude"
    verbatim = ("file-history",)
    # A machine where claude was never set up or isn't logged in. hc reports
    # these as idle, so they must be recognised by their text.
    user_screens = {"first_run_setup": r"Let's get started|Choose the text style",
                    "login": r"Select login method|Not logged in"}
    trust_marker = r"trust this folder"
    trust_yes = r"Yes, I trust"

    def locate(self, handle, src_cwd):
        sid = handle["value"] if handle["kind"] == "id" else Path(handle["value"]).stem
        for root in self.source_roots():
            preferred = root / "projects" / claude_project_dir(src_cwd) / f"{sid}.jsonl"
            hits = [preferred] if preferred.exists() else sorted(root.glob(f"projects/*/{sid}.jsonl"))
            if hits:
                primary = hits[0]
                extras = [p for p in (primary.with_suffix(""), root / "file-history" / sid) if p.exists()]
                return primary, extras
        raise HandoffError(f"no Claude transcript for session {sid} under "
                           f"{', '.join(str(r) for r in self.source_roots())}")

    def guess_handle(self, src_cwd, started_at):
        # The newest transcript for this cwd written since the agent started.
        for root in self.source_roots():
            f = newest_since((root / "projects" / claude_project_dir(src_cwd)).glob("*.jsonl"), started_at)
            if f:
                return {"kind": "id", "value": f.stem}
        return None

    def place(self, primary, dst_cwd, dst_home, dst_root):
        sid, src_root = primary.stem, primary.parents[2]
        src_proj, dst_proj = primary.parent.name, claude_project_dir(dst_cwd)
        mapping = {str(primary): f"{dst_root}/projects/{dst_proj}/{sid}.jsonl",
                   str(primary.with_suffix("")): f"{dst_root}/projects/{dst_proj}/{sid}",
                   str(src_root / "file-history" / sid): f"{dst_root}/file-history/{sid}"}
        return mapping, [(f"{src_root}/", f"{dst_root}/"),
                         (f"projects/{src_proj}/", f"projects/{dst_proj}/")], sid

    def resume_args(self, dst_handle):
        return ["--resume", dst_handle]


CODEX_ID = r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"


class Codex(Adapter):
    kind = binary = "codex"
    exit_text = "/quit"
    launch_flags = ("--no-daemon",)  # the daemon runs commands outside the terminal
    config_var, default_root = "CODEX_HOME", ".codex"
    trust_marker = r"Trust this folder\?"
    trust_yes = r"Trust and continue"
    user_screens = {"hook_review": r"Hooks need review"}

    def locate(self, handle, src_cwd):
        if handle["kind"] == "path":
            return Path(handle["value"]), []
        sid = handle["value"]
        for root in self.source_roots():
            for sub in ("sessions", "archived_sessions"):
                hits = sorted((root / sub).glob(f"**/rollout-*{sid}.jsonl"))
                if hits:
                    return hits[-1], []
        raise HandoffError(f"no Codex rollout for session {sid} under "
                           f"{', '.join(str(r) for r in self.source_roots())}")

    def guess_handle(self, src_cwd, started_at):
        # hc doesn't record the thread id, and codex writes through a shared
        # daemon, so match the newest rollout for this cwd written after the
        # agent process started.
        best = None
        for root in self.source_roots():
            for f in (root / "sessions").glob("**/rollout-*.jsonl"):
                mtime = f.stat().st_mtime
                if mtime < started_at or (best and mtime <= best[0]):
                    continue
                with open(f) as fh:
                    meta = json.loads(fh.readline() or "{}")
                if meta.get("type") == "session_meta" and meta["payload"].get("cwd") == src_cwd:
                    best = (mtime, f)
        return {"kind": "path", "value": str(best[1])} if best else None

    def place(self, primary, dst_cwd, dst_home, dst_root):
        sid = re.search(CODEX_ID + r"\.jsonl$", primary.name)
        if not sid:
            raise HandoffError(f"can't read a session id from {primary.name}")
        parts = primary.parts
        sub = max(i for i, part in enumerate(parts) if part in ("sessions", "archived_sessions"))
        src_root = str(Path(*parts[:sub]))
        return ({str(primary): str(Path(dst_root, *parts[sub:]))},
                [(f"{src_root}/", f"{dst_root}/")], sid.group(1))

    def resume_args(self, dst_handle):
        m = re.search(CODEX_ID + r"(?:\.jsonl)?$", dst_handle)
        return ["resume", m.group(1) if m else dst_handle]


class Omp(Adapter):
    """One jsonl plus a sibling directory per session, under sessions/<cwd-dir>/.
    `omp --resume` takes that path or an id prefix. Quit is `/quit`."""
    kind = binary = "omp"
    exit_text = "/quit"
    # omp reads the same variable pi does; unset, its sessions are ~/.omp/agent.
    config_var, default_root = "PI_CODING_AGENT_DIR", ".omp/agent"

    def locate(self, handle, src_cwd):
        if handle["kind"] == "path":
            primary = Path(handle["value"])
        else:  # id prefix, as `omp --resume` accepts
            hits = [f for root in self.source_roots()
                    for f in sorted((root / "sessions").glob(f"*/*{handle['value']}*.jsonl"))]
            if not hits:
                raise HandoffError(f"no omp session matching {handle['value']}")
            in_cwd = [f for f in hits if f.parent.name == omp_session_dir(src_cwd, str(HOME))]
            primary = (in_cwd or hits)[-1]
        if not primary.exists():
            raise HandoffError(f"omp session file {primary} does not exist")
        sibling = primary.with_suffix("")
        return primary, [sibling] if sibling.exists() else []

    def guess_handle(self, src_cwd, started_at):
        for root in self.source_roots():
            f = newest_since((root / "sessions" / omp_session_dir(src_cwd, str(HOME))).glob("*.jsonl"),
                             started_at)
            if f:
                return {"kind": "path", "value": str(f)}
        return None

    def place(self, primary, dst_cwd, dst_home, dst_root):
        src_dir, dst_dir = primary.parent.name, omp_session_dir(dst_cwd, dst_home)
        dst = f"{dst_root}/sessions/{dst_dir}"
        mapping = {str(primary): f"{dst}/{primary.name}",
                   str(primary.with_suffix("")): f"{dst}/{primary.stem}"}
        return mapping, [(f"{primary.parents[2]}/", f"{dst_root}/"),
                         (f"sessions/{src_dir}/", f"sessions/{dst_dir}/")], f"{dst}/{primary.name}"

    def resume_args(self, dst_handle):
        return ["--resume", dst_handle]


def pi_session_dir(cwd: str) -> str:
    """pi's sessions/<dir> for a cwd (session-manager.js getDefaultSessionDirPath)."""
    return "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", cwd)) + "--"


class Pi(Adapter):
    kind = binary = "pi"
    exit_text = "/quit"
    config_var, default_root = "PI_CODING_AGENT_DIR", ".pi/agent"

    def locate(self, handle, src_cwd):
        if handle["kind"] == "path":
            primary = Path(handle["value"])
        else:  # partial UUID, as `pi --session` accepts
            hits = [f for root in self.source_roots()
                    for f in sorted((root / "sessions").glob(f"*/*{handle['value']}*.jsonl"))]
            if not hits:
                raise HandoffError(f"no pi session matching {handle['value']}")
            primary = hits[0]
        if not primary.exists():
            raise HandoffError(f"pi session file {primary} does not exist")
        sibling = primary.with_suffix("")
        return primary, [sibling] if sibling.exists() else []

    def guess_handle(self, src_cwd, started_at):
        for root in self.source_roots():
            f = newest_since((root / "sessions" / pi_session_dir(src_cwd)).glob("*.jsonl"), started_at)
            if f:
                return {"kind": "path", "value": str(f)}
        return None

    def place(self, primary, dst_cwd, dst_home, dst_root):
        src_dir, dst_dir = primary.parent.name, pi_session_dir(dst_cwd)
        dst = f"{dst_root}/sessions/{dst_dir}"
        mapping = {str(primary): f"{dst}/{primary.name}",
                   str(primary.with_suffix("")): f"{dst}/{primary.stem}"}
        return mapping, [(f"{primary.parents[2]}/", f"{dst_root}/"),
                         (f"sessions/{src_dir}/", f"sessions/{dst_dir}/")], f"{dst}/{primary.name}"

    def resume_args(self, dst_handle):
        return ["--session", dst_handle]


def grok_session_dir(cwd: str) -> str:
    return urllib.parse.quote(cwd, safe="")


class Grok(Adapter):
    """Each session is a directory: <GROK_HOME>/sessions/<url-encoded cwd>/<uuid>/.
    A directory grok has not seen asks to trust its contents; `y` accepts."""
    kind = binary = "grok"
    config_var, default_root = "GROK_HOME", ".grok"
    verbatim = ("rewind_points.jsonl",)  # snapshots of the user's files
    trust_marker = r"trust the contents of this directory"
    trust_yes = r"Yes, proceed"

    def locate(self, handle, src_cwd):
        sid = handle["value"] if handle["kind"] == "id" else Path(handle["value"]).name
        for root in self.source_roots():
            preferred = root / "sessions" / grok_session_dir(src_cwd) / sid
            hits = [preferred] if preferred.is_dir() else [
                d for d in sorted((root / "sessions").glob(f"*/{sid}")) if d.is_dir()]
            if hits:
                return hits[0], []
        raise HandoffError(f"no grok session {sid} under "
                           f"{', '.join(str(r) for r in self.source_roots())}")

    def guess_handle(self, src_cwd, started_at):
        for root in self.source_roots():
            f = newest_since((root / "sessions" / grok_session_dir(src_cwd)).glob("*/chat_history.jsonl"),
                             started_at)
            if f:
                return {"kind": "id", "value": f.parent.name}
        return None

    def place(self, primary, dst_cwd, dst_home, dst_root):
        src_root, src_dir, dst_dir = primary.parents[2], primary.parent.name, grok_session_dir(dst_cwd)
        return ({str(primary): f"{dst_root}/sessions/{dst_dir}/{primary.name}"},
                [(f"{src_root}/", f"{dst_root}/"), (f"sessions/{src_dir}/", f"sessions/{dst_dir}/")],
                primary.name)

    def resume_args(self, dst_handle):
        return ["--resume", dst_handle]


def epoch_ms(value) -> float:
    """Session timestamps are milliseconds. A value small enough to be seconds
    (older files) is scaled up."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if 0 < n < 1_000_000_000_000:
        n *= 1000
    return n


def same_directory(a: str, b: str) -> bool:
    return bool(a and b and os.path.realpath(a) == os.path.realpath(b))


def json_object(text: str) -> str:
    """The first JSON object in text. `opencode session export` has printed a
    status line ahead of the object in some versions."""
    start = text.find("{")
    if start < 0:
        raise HandoffError("opencode export did not return a JSON session")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as e:
        raise HandoffError(f"opencode export did not return a JSON session: {e}")
    if not isinstance(obj, dict):
        raise HandoffError("opencode export did not return a JSON session")
    return json.dumps(obj)


class OpenCode(Adapter):
    """Sessions live in sqlite (`session_v2` in opencode.db), keyed by directory.
    After the agent quits, `opencode session export` writes one JSON file; the
    target imports it, then resumes with `--standalone` so tools stay in the terminal.
    A comma-separated OPENCODE_DATA_DIR is several roots; the first is where the
    export file is written on the target."""
    kind = binary = "opencode"
    config_var, default_root = "OPENCODE_DATA_DIR", ".local/share/opencode"
    launch_flags = ("--standalone",)

    def config_root(self, env):
        custom = (env.get(self.config_var) or "").split(",")[0].strip()
        if custom:
            return Path(custom).expanduser()
        xdg = (env.get("XDG_DATA_HOME") or "").strip()
        if xdg:
            return Path(xdg).expanduser() / "opencode"
        return HOME / self.default_root

    def _env(self, key: str) -> str:
        return os.environ.get(key) or LOGIN_ENV.get(key) or ""

    def source_roots(self):
        # An explicit data dir is the only place this opencode looks. Otherwise
        # try the usual directories; the first one with the session wins.
        custom = self._env("OPENCODE_DATA_DIR").strip()
        if custom:
            return [Path(part.strip()).expanduser() for part in custom.split(",") if part.strip()]
        roots = [self.config_root({k: os.environ.get(k, "") for k in CONFIG_VARS}),
                 HOME / "Library/Application Support/opencode"]
        return [r for i, r in enumerate(roots) if r not in roots[:i]]

    def _databases(self, roots: List[Path]) -> List[Path]:
        db = self._env("OPENCODE_DB").strip()
        if db == ":memory:":
            return []
        if db:
            return [Path(db).expanduser()]
        found: List[Path] = []
        for root in roots:
            found.append(root / "opencode.db")
            if root.is_dir():
                found.extend(sorted(root.glob("opencode-*.db")))
        return found

    def _session_rows(self, roots: Optional[List[Path]] = None) -> List[Tuple[str, str, float]]:
        """(id, directory, updated ms) from sqlite and from legacy session JSON."""
        roots = self.source_roots() if roots is None else roots
        rows: List[Tuple[str, str, float]] = []
        seen = set()

        def add(sid: str, directory: str, updated, parent) -> None:
            if not sid or parent or sid in seen:
                return
            seen.add(sid)
            rows.append((sid, directory or "", epoch_ms(updated)))

        for db in self._databases(roots):
            if not db.is_file():
                continue
            try:
                con = sqlite3.connect("file:" + urllib.parse.quote(str(db)) + "?mode=ro", uri=True)
            except sqlite3.Error:
                continue
            try:
                tables = {r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ("session_v2", "session"):
                    if table not in tables:
                        continue
                    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                    if not {"id", "directory"} <= cols:
                        continue
                    updated = "time_updated" if "time_updated" in cols else "0"
                    parent = "parent_id" if "parent_id" in cols else "NULL"
                    for sid, directory, stamp, parent_id in con.execute(
                            f"SELECT id, directory, {updated}, {parent} FROM {table}"):
                        add(sid, directory, stamp, parent_id)
            except sqlite3.Error:
                continue
            finally:
                con.close()
        # OPENCODE_DB is the whole store; the JSON files next to it are not a second copy.
        if not self._env("OPENCODE_DB").strip():
            for root in roots:
                base = root / "storage" / "session"
                files = list(base.glob("*/*.json")) if base.is_dir() else []
                project = root / "project"
                if project.is_dir():
                    files.extend(project.glob("*/storage/session/*/*.json"))
                for path in files:
                    try:
                        data = json.loads(path.read_text())
                    except (OSError, json.JSONDecodeError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    updated = (data.get("time") or {}).get("updated")
                    if not updated:
                        updated = path.stat().st_mtime
                    add(data.get("id") or path.stem, data.get("directory") or "", updated,
                        data.get("parentID") or data.get("parent_id"))
        return rows

    def _resolve_id(self, value: str) -> str:
        rows = self._session_rows()
        exact = [sid for sid, _, _ in rows if sid == value]
        if exact:
            return exact[0]
        hits = [sid for sid, _, _ in rows if sid.startswith(value)]
        if len(hits) == 1:
            return hits[0]
        where = ", ".join(str(r) for r in self.source_roots())
        if not hits:
            raise HandoffError(f"no OpenCode session matching {value} under {where}")
        raise HandoffError(f"{len(hits)} OpenCode sessions match {value}; "
                           f"pass --session with the full id")

    def locate(self, handle, src_cwd):
        sid = self._resolve_id(handle["value"] if handle["kind"] == "id" else Path(handle["value"]).stem)
        # capture() replaces this with the export after the agent quits.
        return self.config_root({k: os.environ.get(k, "") for k in CONFIG_VARS}) / "handoff" / f"{sid}.json", []

    def _best_session(self, rows, src_cwd: str, started_ms: float) -> Optional[dict]:
        best = None
        for sid, directory, updated in rows:
            if updated < started_ms or not same_directory(directory, src_cwd):
                continue
            if best is None or updated >= best[0]:
                best = (updated, sid)
        return {"kind": "id", "value": best[1]} if best else None

    def guess_handle(self, src_cwd, started_at):
        started_ms = started_at * 1000
        # The database override replaces every data dir.
        if self._env("OPENCODE_DB").strip() not in ("", ":memory:"):
            return self._best_session(self._session_rows(), src_cwd, started_ms)
        for root in self.source_roots():
            found = self._best_session(self._session_rows([root]), src_cwd, started_ms)
            if found:
                return found
        return None

    def capture(self, handle, src_cwd, primary, extras):
        if not have("opencode"):
            raise HandoffError("opencode is not installed here, so its session can't be exported")
        sid = primary.stem
        out_dir = Path(tempfile.mkdtemp(prefix="agent-handoff-opencode-"))
        out = out_dir / f"{sid}.json"
        try:
            text = run(["opencode", "session", "export", sid], cwd=src_cwd)
            out.write_text(json_object(text))
        except Exception:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
        return out, []

    def place(self, primary, dst_cwd, dst_home, dst_root):
        return ({str(primary): f"{dst_root}/handoff/{primary.name}"}, [], primary.stem)

    def resume_args(self, dst_handle):
        return ["--session", dst_handle]

    def prelude(self, dst_handle, dst_cwd, mapping):
        dest = next(iter(mapping.values()))
        cmd = shlex.join([self.binary, "--standalone", "session", "import", dest,
                          "--directory", dst_cwd])
        return [{"cmd": cmd, "expect": r"Imported session:|Session already exists"}]


ADAPTERS: Dict[str, Adapter] = {a.kind: a for a in (Claude(), Codex(), Omp(), Pi(), Grok(), OpenCode())}


def adapter_for(kind: str) -> Adapter:
    if kind not in ADAPTERS:
        raise HandoffError(f"agent kind '{kind}' is not supported yet (supported: {', '.join(ADAPTERS)})")
    return ADAPTERS[kind]


# ---------------------------------------------------------------- hc terminals


def hc_agents() -> List[dict]:
    """Live coding agents on this machine. `pane_id` is the terminal address (`t7`)."""
    rows = []
    for t in hc_json("ls").get("terminals") or []:
        addr = t.get("address") or ""
        if not addr or "/" in addr or t.get("state") not in (None, "live"):
            continue  # another machine's terminals are not sessions on this host
        kind = kind_of_hc(t)
        if not kind:
            continue
        status = t.get("status")
        if isinstance(status, dict):
            status = status.get("state")
        rows.append({"pane_id": addr, "agent": kind, "name": t.get("name"),
                     "cwd": t.get("cwd"), "agent_status": status or "idle",
                     "agent_session": None})
    return rows


def hydrate_hc(agent: dict) -> dict:
    """`hc ls` reports the launch folder. The process's own folder (where the
    session actually is) comes from `hc get`."""
    info = hc_get(agent["pane_id"])
    fg = info.get("foreground") or {}
    if fg.get("cwd"):
        agent["cwd"] = fg["cwd"]
    state = status_state(info)
    if state:
        agent["agent_status"] = state
    return agent


def session_of(agent: dict) -> Optional[dict]:
    if agent.get("agent_session"):
        return agent["agent_session"]
    if agent.get("agent") not in ADAPTERS:
        return None
    try:
        proc, _, _ = foreground(agent["pane_id"], ADAPTERS[agent["agent"]].binary)
    except HandoffError:
        return None
    if not proc or not proc.get("pid"):
        return None
    cwd = proc.get("cwd") or agent.get("cwd") or ""
    return ADAPTERS[agent["agent"]].guess_handle(cwd, process_started_at(proc["pid"]))


def find_agent(target: str) -> dict:
    agents = hc_agents()
    hits = [a for a in agents if target in (a.get("name"), a["pane_id"])]
    if not hits:
        for agent in agents:
            sess = session_of(agent)
            if sess and str(sess.get("value") or "").startswith(target):
                agent["agent_session"] = sess
                hits.append(agent)
    if not hits and re.fullmatch(r"t[0-9a-z]+", target):
        # `hc ls` labels some programs with a word we don't map. The address
        # still resolves through the process `hc get` reports.
        try:
            info = hc_get(target)
        except HandoffError:
            info = {}
        kind = kind_of_hc(info)
        if kind and info.get("state") != "finished":
            fg = info.get("foreground") or {}
            hits = [{"pane_id": target, "agent": kind, "name": info.get("name"),
                     "cwd": fg.get("cwd") or info.get("cwd"),
                     "agent_status": status_state(info) or "idle",
                     "agent_session": None}]
    if len(hits) != 1:
        names = ", ".join(f"{a.get('name') or a['pane_id']} ({a['agent']})" for a in agents)
        raise HandoffError(f"'{target}' matches {len(hits)} agents; live agents: {names or 'none'}")
    return hydrate_hc(hits[0])


def foreground(pane: str, binary: str = "") -> Tuple[Optional[dict], int, int]:
    """The agent's own process in the foreground (not a child it spawned, like
    caffeinate), plus the foreground pid and the terminal's shell pid."""
    info = hc_get(pane)
    fg = info.get("foreground") or {}
    argv = list(fg.get("argv") or [])
    mine = None
    if agent_in_foreground(info, binary):
        mine = {"pid": fg.get("pid"), "cwd": fg.get("cwd"), "argv": argv,
                "argv0": fg_argv0(info) or binary}
    return mine, int(fg.get("pid") or 0), int(info.get("pid") or 0)


def process_started_at(pid: int) -> float:
    """Epoch seconds when pid started, from ps's [[dd-]hh:]mm:ss elapsed time."""
    etime = run(["ps", "-o", "etime=", "-p", str(pid)]).strip()
    days, _, clock = etime.rpartition("-")
    secs = 0
    for part in clock.split(":"):
        secs = secs * 60 + int(part)
    return time.time() - secs - int(days or 0) * 86400 - 5


def wait_for_shell(pane: str, timeout: float, binary: str = "") -> None:
    end = time.time() + timeout
    while time.time() < end:
        if shell_back(hc_get(pane), binary):
            return
        time.sleep(0.5)
    raise HandoffError(f"terminal {pane} did not return to its shell prompt within {timeout:.0f}s")


def press_key(pane: str, key: str) -> None:
    hc_json("keys", pane, key)


def read_screen(pane: str) -> str:
    return hc_json("read", "--lines", "0", pane).get("text") or ""


def exit_agent(pane: str, adapter: Adapter) -> None:
    # `hc send` pastes the line and presses Enter. It refuses while the
    # program is blocked, which is the ctrl+c path below.
    try:
        hc_json("send", pane, adapter.exit_text)
    except HandoffError:
        pass
    else:
        try:
            wait_for_shell(pane, 20, adapter.binary)
            return
        except HandoffError:
            pass
    # Not at a prompt that takes /exit (e.g. a first-run or login screen). TUIs
    # quit on a second ctrl+c within about a second, and a screen change can eat
    # one, so send quick pairs a few times.
    for _ in range(3):
        press_key(pane, "ctrl+c")
        time.sleep(0.3)
        press_key(pane, "ctrl+c")
        try:
            wait_for_shell(pane, 1.5, adapter.binary)
            return
        except HandoffError:
            continue
    raise HandoffError(f"{adapter.kind} in {pane} didn't quit (tried {adapter.exit_text} and ctrl+c); "
                       f"it may be showing a screen that needs the user")


def wait_until_idle(pane: str, timeout: float, binary: str) -> None:
    """Block until an hc agent is idle or done. `hc wait` also returns on
    blocked, error and exit, so those are checked rather than treated as ready."""
    deadline = time.time() + timeout
    while True:
        info = hc_get(pane)
        state = status_state(info)
        if state in ("idle", "done"):
            return
        if info.get("state") == "finished" or (state is None and not agent_in_foreground(info, binary)):
            raise HandoffError(f"{binary} in {pane} exited while waiting; retry the handoff")
        if time.time() >= deadline:
            raise HandoffError(f"terminal {pane} stayed {state or 'busy'} for {timeout:.0f}s")
        if state == "working":
            remaining = max(1, int(deadline - time.time()))
            result = hc_json("wait", pane, "--timeout", str(remaining))
            if not result.get("ended") or result.get("reason") == "timeout":
                raise HandoffError(f"terminal {pane} stayed working for {timeout:.0f}s")
        else:
            time.sleep(0.5)


def answer_trust(pane: str, adapter: Adapter, accept: bool) -> str:
    """Returns 'none' (no prompt), 'accepted', or 'pending' (left for the user).

    A trust screen is either a menu (a cursor on one row, Enter confirms) or a
    single key named at the end of the yes row, such as grok's `y`."""
    if not adapter.trust_marker:
        return "none"
    screen = ""
    previous = None
    stable = 0
    for _ in range(20):
        screen = read_screen(pane)
        if re.search(adapter.trust_marker, screen):
            break
        # The prompt shows up after the TUI draws. A screen that has settled on
        # something else will not become it; a blank one might.
        text = "\n".join(ln.strip() for ln in screen.splitlines() if ln.strip())
        if text and text == previous:
            stable += 1
            if stable >= 2:
                return "none"
        else:
            previous = text
            stable = 0
        time.sleep(0.5)
    else:
        return "none"
    if not accept:
        return "pending"
    lines = screen.splitlines()
    cursor = next((i for i, l in enumerate(lines) if re.match(r"\s*[❯›>]\s", l)), None)
    yes_at = next((i for i, l in enumerate(lines) if re.search(adapter.trust_yes or "", l)), None)
    yes_line = lines[yes_at] if yes_at is not None else ""
    key = re.search(r"(?:^|\s)([a-zA-Z])\s*$", yes_line)
    if cursor is not None and yes_at is not None:
        keys = ["down" if yes_at > cursor else "up"] * abs(yes_at - cursor) + ["enter"]
    elif key:
        keys = [key.group(1).lower()]
    else:
        return "pending"
    for k in keys:
        press_key(pane, k)
        time.sleep(0.15)
    for _ in range(10):
        time.sleep(0.5)
        screen = read_screen(pane)
        if not re.search(adapter.trust_marker, screen):
            return "accepted"
    return "pending"


# ---------------------------------------------------------------- remote plumbing


LOCAL = "local"  # target for --here: same machine, no ssh


def remote(target: str, step: str, payload: dict) -> dict:
    if target == LOCAL:
        return REMOTE_STEPS[step](payload)
    source = Path(__file__).read_text()
    cmd = ["ssh", "-o", "BatchMode=yes", target,
           f"python3 - --remote {shlex.quote(step)} {shlex.quote(json.dumps(payload))}"]
    p = subprocess.run(cmd, input=source, capture_output=True, text=True)
    for line in reversed(p.stdout.splitlines()):
        if line.startswith(RESULT_MARK):
            res = json.loads(line[len(RESULT_MARK):])
            if not res["ok"]:
                raise HandoffError(f"[{target}] {res['error']}")
            return res["data"]
    if p.returncode == 255:
        raise HandoffError(ssh_failure(target, p.stderr))
    raise HandoffError(f"[{target}] step '{step}' failed: {(p.stderr or p.stdout).strip()[-2000:]}")


def this_machine() -> str:
    """How other machines reach this one: $AGENT_HANDOFF_SELF, else user@short-hostname
    (which is what Tailscale MagicDNS and most LANs resolve)."""
    return os.environ.get("AGENT_HANDOFF_SELF") or f"{getpass.getuser()}@{socket.gethostname().split('.')[0]}"


def network_sandboxed() -> bool:
    """A hint, not proof: Codex sets this in its sessions, and commands the user
    approves to run outside the sandbox still inherit it. So it only shapes the
    message after ssh has actually failed."""
    return os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1"


def ssh_failure(target: str, stderr: str) -> str:
    if network_sandboxed():
        return (f"can't ssh to {target}: {stderr.strip()[-300:].rstrip('.')}. This is probably "
                f"Codex's sandbox blocking network access, not a problem with {target}: ask the user "
                f"to approve running this command outside the sandbox, then run it again. If it was "
                f"already outside the sandbox, the user has to fix ssh to {target}; agent-handoff "
                f"never changes ssh keys or config.")
    return (f"can't ssh from {socket.gethostname().split('.')[0]} to {target}: {stderr.strip()[-300:].rstrip('.')}. "
            f"agent-handoff needs ssh that works without prompts (`ssh -o BatchMode=yes {target} true`). "
            f"If {target} is the wrong name for that machine, pass the right one; otherwise the user has "
            f"to set up key-based ssh. agent-handoff never changes ssh keys or config. "
            f"If you're running inside an agent sandbox, it may be blocking the network; ask the "
            f"user to run this outside it.")


def remote_main(step: str, payload: dict) -> None:
    adopt_login_path()
    try:
        data = REMOTE_STEPS[step](payload)
        print(RESULT_MARK + json.dumps({"ok": True, "data": data}))
    except HandoffError as e:
        print(RESULT_MARK + json.dumps({"ok": False, "error": str(e)}))


# ---------------------------------------------------------------- repo discovery (target)


def scan_roots() -> List[Tuple[Path, int]]:
    env = os.environ.get("AGENT_HANDOFF_ROOTS")
    cfg = CONFIG_DIR / "config.json"
    if env:
        return [(Path(p).expanduser(), 5) for p in env.split(":") if p]
    if cfg.exists():
        return [(Path(p).expanduser(), 5) for p in json.loads(cfg.read_text()).get("roots", [])]
    names = ["github.com", "gitlab.com", "src", "code", "dev", "Developer", "projects",
             "repos", "work", "git", "workspace"]
    return [(HOME / n, 4) for n in names] + [(HOME, 2)]


SKIP_DIRS = {"node_modules", "Library", "Applications", "Pictures", "Music", "Movies",
             "Downloads", "vendor", "target", "build", "dist", "_work"}


def iter_repos(root: Path, depth: int):
    if not root.is_dir():
        return
    base = len(root.parts)
    for dirpath, dirnames, _ in os.walk(root):
        if ".git" in dirnames or os.path.isfile(os.path.join(dirpath, ".git")):
            if os.path.isdir(os.path.join(dirpath, ".git")):
                yield dirpath
            dirnames[:] = []
            continue
        if len(Path(dirpath).parts) - base >= depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]


def load_cache() -> Dict[str, str]:
    f = CONFIG_DIR / "repos.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_cache(cache: Dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "repos.json").write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


def is_checkout(path: str) -> bool:
    git_dir = os.path.join(path, ".git")
    return os.path.isdir(git_dir) or os.path.isfile(git_dir)


def find_repo(remotes: List[str], branch: Optional[str], override: Optional[str],
              exclude: Tuple[str, ...] = ()) -> Tuple[str, List[str]]:
    if override:
        path = os.path.realpath(os.path.expanduser(override))
        if not os.path.isdir(path):
            raise HandoffError(f"{path} is not a directory")
        # A named checkout has to be this repo. A plain directory, such as a
        # Herdr space whose folder is ~, is not one: look the repo up instead.
        if is_checkout(path):
            if not set(remotes) & set(remotes_of(path)):
                raise HandoffError(f"{path} has no remote matching {remotes}")
            return path, []
    cache = load_cache()
    cached = [cache[r] for r in remotes
              if r in cache and os.path.isdir(cache[r]) and cache[r] not in exclude]
    matches = [p for p in cached if set(remotes) & set(remotes_of(p))]
    if not matches:
        seen = set()
        for root, depth in scan_roots():
            for repo in iter_repos(root, depth):
                real = os.path.realpath(repo)
                if real in seen or real in exclude:
                    continue
                seen.add(real)
                if set(remotes) & set(remotes_of(real)):
                    matches.append(real)
    if not matches:
        raise HandoffError(f"no checkout of {remotes[0]} found on this machine "
                           f"(clone it, or pass --dir; searched {[str(r) for r, _ in scan_roots()]})")

    def rank(path: str):
        has_branch = branch is not None and any(
            w.get("branch") == f"refs/heads/{branch}" for w in worktrees(path))
        mtime = max((os.path.getmtime(os.path.join(path, ".git", f))
                     for f in ("HEAD", "index", "FETCH_HEAD") if os.path.exists(os.path.join(path, ".git", f))),
                    default=0)
        return (not has_branch, -mtime)

    matches.sort(key=rank)
    # A directory that is not itself the checkout fell through to discovery.
    cache = load_cache()
    for r in remotes:
        cache[r] = matches[0]
    save_cache(cache)
    return matches[0], matches[1:]


# ---------------------------------------------------------------- remote steps


MIN_HC = (0, 1, 13)  # --no-parent and --outlive-parent


def check_hc_version(where: str, version: str) -> None:
    if version != "unknown" and tuple(int(x) for x in version.split(".")) < MIN_HC:
        raise HandoffError(f"hc {version} on {where} is older than "
                           f"{'.'.join(map(str, MIN_HC))}; run `hc update` there")


def step_probe(p: dict) -> dict:
    missing = [b for b in ("git", p["binary"]) if not have(b)]
    if missing:
        raise HandoffError(f"missing on target: {', '.join(missing)}")
    require_hc()
    repo, others = find_repo(p["remotes"], p["branch"], p.get("dir"))
    return {"home": str(HOME), "repo": repo, "other_checkouts": others,
            "config_root": str(ADAPTERS[p["kind"]].config_root(LOGIN_ENV)),
            "receive_pack": shutil.which("git-receive-pack", path=LOGIN_PATH),
            "hc_version": hc_version()}


def step_prepare(p: dict) -> dict:
    repo, sid8, src_branch = p["repo"], p["sid8"], p["branch"]
    head_ref, wip_ref = f"refs/handoff/{sid8}/head", f"refs/handoff/{sid8}/wip"
    head = git(repo, "rev-parse", head_ref)
    branch = src_branch if (src_branch and not p.get("new_branch")) else f"handoff/{sid8}"
    if branch.startswith("handoff/"):  # a fresh branch: don't reuse one an earlier handoff made
        base, n = branch, 2
        while ref_exists(repo, f"refs/heads/{branch}"):
            branch, n = f"{base}-{n}", n + 1
    wts = worktrees(repo)
    current = [w for w in wts if w.get("branch") == f"refs/heads/{branch}"]
    created = False
    if current:
        path = current[0]["worktree"]
        if is_dirty(path, untracked=False):
            raise HandoffError(f"{path} has '{branch}' checked out with uncommitted changes; "
                               f"commit/stash there, or rerun with --new-branch")
        if git(path, "rev-parse", "HEAD") != head:
            if not is_ancestor(path, "HEAD", head):
                raise HandoffError(f"'{branch}' in {path} has commits the source doesn't; "
                                   f"rerun with --new-branch")
            git(path, "merge", "--ff-only", "-q", head)
    else:
        if ref_exists(repo, f"refs/heads/{branch}"):
            if not is_ancestor(repo, f"refs/heads/{branch}", head):
                raise HandoffError(f"local '{branch}' in {repo} has commits the source doesn't; "
                                   f"rerun with --new-branch")
            git(repo, "branch", "-f", branch, head)
        else:
            git(repo, "branch", branch, head)
        slug = re.sub(r"[^A-Za-z0-9._-]", "-", branch)
        main = Path(wts[0]["worktree"])  # worktrees sit beside the main checkout, never nested
        path = str(main.parent / f"{main.name}.worktrees" / slug)
        git(repo, "worktree", "add", "-q", path, branch)
        created = True
    if ref_exists(repo, f"refs/remotes/origin/{branch}"):
        git(path, "branch", "-q", "--set-upstream-to", f"origin/{branch}", branch, check=False)
    applied = False
    if ref_exists(repo, wip_ref):
        diff = run(["git", "-C", path, "diff", "--binary", "--full-index", head, wip_ref])
        if diff:
            run(["git", "-C", path, "apply", "--whitespace=nowarn", "-"], input=diff)
            applied = True
    for ref in (head_ref, wip_ref):
        git(repo, "update-ref", "-d", ref, check=False)
    return {"worktree": os.path.realpath(path), "branch": branch, "created_worktree": created,
            "applied_changes": applied}


def launch_line(kind: str, args: List[str]) -> str:
    argv = [ADAPTERS[kind].binary, *args]
    for flag in ADAPTERS[kind].launch_flags:
        if flag not in argv:
            argv.insert(1, flag)
    return shlex.join(argv)


def agent_name_for(requested: Optional[str], source_name: Optional[str], kind: str, src_root: str) -> str:
    name = requested or source_name or re.sub(
        r"[^a-z0-9_-]", "-", f"{Path(src_root).name}-{kind}".lower()).strip("-")[:32]
    if not name or not re.match(r"^[a-z]", name):
        name = f"a{name}"[:32]
    return name[:32]


def unique_terminal_name(name: str) -> str:
    # Names are unique on one machine. `hc ls` also lists other machines, and
    # those terminals may use the same name.
    live = {t.get("name") for t in (hc_json("ls").get("terminals") or [])
            if t.get("name") and "/" not in (t.get("address") or "")}
    base, n = name, 2
    while name in live:
        name, n = f"{base}-{n}"[:32], n + 1
    return name


def herdr_app(*args: str) -> Optional[dict]:
    """One herdr-app command, or None when the app isn't there or doesn't answer."""
    if not have("herdr-app"):
        return None
    try:
        out = run(["herdr-app", *args], check=False).strip()
    except HandoffError:
        return None
    if not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def show_in_app(addr: str, name: str, cwd: str, space: Optional[str] = None) -> Optional[dict]:
    """Give a core terminal a pane in the Herdr window. None when the app isn't
    running: the terminal still exists, and `hc attach` reaches it."""
    try:
        return _show_in_app(addr, name, cwd, space)
    except HandoffError:
        return None


def _show_in_app(addr: str, name: str, cwd: str, space_id: Optional[str] = None) -> Optional[dict]:
    listing = herdr_app("ls")
    if not listing or "spaces" not in listing:
        return None
    cwd_real = os.path.realpath(cwd)
    bare = space_id.split("/", 1)[1] if space_id and "/" in space_id else space_id
    picked = next((s for s in listing["spaces"]
                   if space_id and not s.get("machine") and s.get("id") in (space_id, bare)), None)
    space = picked or next((s for s in listing["spaces"]
                  if not s.get("machine") and s.get("folder")
                  and os.path.realpath(os.path.expanduser(s["folder"])) == cwd_real), None)
    shell_tab = None
    if space:
        selected = herdr_app("do", "space.select", "--id", space["id"])
        if not selected or selected.get("error"):
            return None
        space_id, space_name = space["id"], space.get("name") or name
    else:
        created = herdr_app("do", "space.new", "--name", name, "--folder", cwd,
                            "--machine", "", "--focus", "true")
        if not created or created.get("error") or "space" not in created:
            return None
        space_id, space_name, shell_tab = created["space"], name, created.get("tab")
    kept = herdr_app("do", "terminal.keep", "--terminal", addr, "--place", "tab")
    if not kept or kept.get("error") or not kept.get("tab"):
        if shell_tab:
            herdr_app("do", "space.close", "--id", space_id, "--confirmed", "true")
        return None
    if shell_tab:
        herdr_app("do", "tab.close", "--id", shell_tab, "--confirmed", "true")
    return {"space": space_name, "tab": kept["tab"]}


def hc_attach(target: str, address: Optional[str], name: Optional[str]) -> str:
    ident = name or address or ""
    if target == LOCAL:
        return f"hc attach {ident}".rstrip()
    machine = None
    try:
        for row in hc_json("machine", "ls").get("machines") or []:
            if row.get("target") == target or row.get("name") == target or row.get("host") == target.split("@")[-1]:
                machine = row.get("name")
                break
    except HandoffError:
        machine = None
    if machine:
        return f"hc attach {machine}/{ident}".rstrip("/")
    return f"ssh -t {target} hc attach {ident}".rstrip()


def schedule_rm(addr: str) -> None:
    """Remove a terminal after this process exits and it has finished."""
    if not addr:
        return
    subprocess.Popen(
        ["/bin/sh", "-c",
         "i=0; while [ \"$i\" -lt 10 ]; do hc rm " + shlex.quote(addr)
         + " >/dev/null 2>&1 && exit 0; i=$((i+1)); sleep 1; done"],
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def step_start(p: dict) -> dict:
    """Open a shell terminal and start the agent in it, so quitting the agent
    later leaves the shell."""
    kind, name = p["kind"], unique_terminal_name(p["name"])
    created = hc_json("run", "--bg", "--name", name, "--cwd", p["cwd"],
                      "--no-parent", "--outlive-parent", "--keep")
    addr = created["address"]
    try:
        wait_for_shell(addr, 20)
        for step in p.get("prelude") or []:
            hc_json("send", addr, step["cmd"])
            wait_for_shell(addr, 90)
            expect = step.get("expect")
            screen = read_screen(addr)
            if expect and not re.search(expect, screen):
                tail = "\n".join(line for line in screen.splitlines() if line.strip())[-1200:]
                raise HandoffError(f"setup on the target did not finish as expected; its last output:\n{tail}")
        hc_json("send", addr, launch_line(kind, list(p["args"])))
        # The shell is still in front for a moment after the command is sent.
        # Wait until the agent itself is in front, rather than treating that
        # gap as the agent having already exited.
        binary = ADAPTERS[kind].binary
        deadline = time.time() + 25
        info = hc_get(addr)
        while time.time() < deadline and not (
                agent_in_foreground(info, binary) or app_is(info, binary)):
            if info.get("state") == "finished":
                break
            time.sleep(0.4)
            info = hc_get(addr)
        if not agent_in_foreground(info, binary) and not app_is(info, binary):
            screen = read_screen(addr)
            tail = "\n".join(line for line in screen.splitlines() if line.strip())[-1200:]
            raise HandoffError(f"{kind} exited right after starting on the target; its last output:\n{tail}")
    except HandoffError:
        discard_terminal(addr)
        raise
    adapter = ADAPTERS[kind]
    trust = answer_trust(addr, adapter, p.get("trust", True))
    time.sleep(1)
    screen = read_screen(addr)
    waiting = [k for k, rx in adapter.user_screens.items() if re.search(rx, screen)]
    info = hc_get(addr)
    state = status_state(info)
    if info.get("state") == "finished" or (
            not agent_in_foreground(info, adapter.binary) and state not in ("idle", "working", "done", "blocked", "error")):
        tail = "\n".join(line for line in screen.splitlines() if line.strip())[-1200:]
        raise HandoffError(f"{kind} exited right after starting on the target; its last output:\n{tail}")
    if state == "blocked" and not waiting:
        waiting = ["unrecognized_prompt"]
    return {"name": name, "pane_id": addr, "trust_prompt": trust, "waiting_for_user": waiting,
            "app": show_in_app(addr, name, p["cwd"], p.get("space"))}


def discard_terminal(addr: str) -> None:
    try:
        hc_json("kill", addr)
    except HandoffError:
        pass
    try:
        hc_json("rm", addr)
    except HandoffError:
        pass


def restart_in_terminal(pane: str, kind: str, args: List[str]) -> None:
    wait_for_shell(pane, 15, ADAPTERS[kind].binary)
    hc_json("send", pane, launch_line(kind, args))


def step_cleanup_refs(p: dict) -> dict:
    for ref in (f"refs/handoff/{p['sid8']}/head", f"refs/handoff/{p['sid8']}/wip"):
        git(p["repo"], "update-ref", "-d", ref, check=False)
    return {}


REMOTE_STEPS = {"probe": step_probe, "prepare": step_prepare, "start": step_start,
                "cleanup-refs": step_cleanup_refs}


# ---------------------------------------------------------------- source side


def identity(repo: str) -> List[str]:
    """`-c` flags giving git a placeholder author for this tool's internal
    commits (the WIP snapshot, the source stash) on machines with no git
    identity, e.g. a fresh server. Empty when the user has one configured."""
    if git(repo, "config", "user.email", check=False) and git(repo, "config", "user.name", check=False):
        return []
    return ["-c", "user.name=agent-handoff", "-c", "user.email=agent-handoff@localhost"]


def make_wip(repo: str) -> Optional[str]:
    """Snapshot tracked + untracked (non-ignored) changes as a commit object
    without touching the branch, index or working tree."""
    index = git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_index = os.path.join(tmp, "index")
        if os.path.exists(index):
            Path(tmp_index).write_bytes(Path(index).read_bytes())
        git(repo, "add", "-A", index_file=tmp_index)
        tree = git(repo, "write-tree", index_file=tmp_index)
    if tree == git(repo, "rev-parse", "HEAD^{tree}"):
        return None
    return run(["git", *identity(repo), "-C", repo, "commit-tree", tree, "-p", "HEAD",
                "-m", "agent-handoff wip"]).strip()


def path_rewriter(pairs: List[Tuple[str, str]]):
    """One-pass replacement of path strings. Sequential str.replace would
    re-rewrite its own output (the /tmp alias matching inside a fresh
    /private/tmp/...) and would hit prefixes (/x/repo inside /x/repo-old), so
    match all sources at once, longest first, and only at a path-component end."""
    table: Dict[str, str] = {}
    for a, b in pairs:
        if a and a != b:
            table.setdefault(a, b)
    if not table:
        return lambda text: text
    alts = sorted(table, key=len, reverse=True)
    rx = re.compile("|".join(re.escape(a) + ("" if a.endswith("/") else r"(?![A-Za-z0-9._-])")
                             for a in alts))
    return lambda text: rx.sub(lambda m: table[m.group(0)], text)


def rewrite_tree(src: Path, dst: Path, pairs: List[Tuple[str, str]],
                 verbatim: Tuple[str, ...] = ()) -> None:
    rewrite = path_rewriter(pairs)
    items = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
    for item in items:
        out = dst if src.is_file() else dst / item.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = item.read_bytes()
        if item.suffix in TEXT_SUFFIXES and not set(item.parts) & set(verbatim):
            try:
                data = rewrite(data.decode("utf-8")).encode("utf-8")
            except UnicodeDecodeError:
                pass  # binary file with a text-ish suffix: copy unchanged
        out.write_bytes(data)


def send_files(target: str, mapping: Dict[str, str], pairs: List[Tuple[str, str]],
               verbatim: Tuple[str, ...] = ()) -> List[str]:
    """Copy {src abs path: dst abs path} to the target, rewriting paths inside."""
    if target == LOCAL:
        sent = []
        for src_abs, dst_abs in mapping.items():
            if Path(src_abs).exists():
                rewrite_tree(Path(src_abs), Path(dst_abs), pairs, verbatim)
                sent.append(dst_abs)
        return sent
    sent = []
    with tempfile.TemporaryDirectory() as stage:
        for src_abs, dst_abs in mapping.items():
            src, rel = Path(src_abs), dst_abs.lstrip("/")
            if src.exists():
                rewrite_tree(src, Path(stage) / rel, pairs, verbatim)
                sent.append(rel)
        archive = Path(stage).with_suffix(".tgz")
        with tarfile.open(archive, "w:gz") as tar:
            for rel in sent:
                tar.add(Path(stage) / rel, arcname=rel)
        with open(archive, "rb") as fh:
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", target, "tar -C / -xzf -"],
                               stdin=fh, capture_output=True, text=True)
        if p.returncode == 255:
            raise HandoffError(ssh_failure(target, p.stderr))
        archive.unlink()
        if p.returncode != 0:
            raise HandoffError(f"copying transcript to {target} failed: {p.stderr.strip()}")
    return sent


def ssh_git_url(target: str, path: str) -> str:
    return path if target == LOCAL else f"ssh://{target}{path}"


def cmd_list(_args) -> None:
    me = os.environ.get("HC_TERMINAL_ID")
    rows = []
    for a in hc_agents():
        sess = a.get("agent_session") or {}
        rows.append({"target": a.get("name") or a["pane_id"], "kind": a["agent"],
                     "status": a["agent_status"], "cwd": a["cwd"],
                     "session": sess.get("value"),
                     "supported": a["agent"] in ADAPTERS,
                     "this_pane": a["pane_id"] == me})
    print(json.dumps(rows, indent=2))


def run_from(host: str, argv: List[str]) -> None:
    """Run this command on `host` (where the agent lives) instead of here: copy
    this file there and run it, streaming its output back."""
    tmp = f"/tmp/agent-handoff-{os.getpid()}.py"
    remote_cmd = (f"cat > {tmp} && AGENT_HANDOFF_VIA_SSH=1 python3 {tmp} "
                  f"{shlex.join(argv)}; rc=$?; rm -f {tmp}; exit $rc")
    with open(__file__, "rb") as fh:
        rc = subprocess.run(["ssh", "-o", "BatchMode=yes", host, remote_cmd], stdin=fh).returncode
    sys.exit(rc)


def defer_payload(args, agent: dict, proc: Optional[dict], handle: dict,
                  target_repo: str, src_root: str, stage: Path) -> dict:
    script = stage / "agent_handoff.py"
    shutil.copyfile(__file__, script)
    local_worktree = args.target == LOCAL and os.path.realpath(target_repo) == os.path.realpath(src_root)
    return dict(vars(args), agent=agent["pane_id"], session=handle["value"],
                dir=None if local_worktree else target_repo,
                new_worktree=args.new_worktree or local_worktree,
                wait=True, self_handoff=False, dry_run=False, deferred_helper=True,
                expected_kind=agent["agent"], expected_pid=(proc or {}).get("pid"),
                expected_session=agent.get("agent_session"))


def defer_send(args, agent: dict, proc: Optional[dict], src_cwd: str,
               handle: dict, target_repo: str, src_root: str) -> dict:
    """A terminal with no parent, so it keeps running after the source agent quits."""
    stage = Path(tempfile.mkdtemp(prefix="agent-handoff-"))
    try:
        deferred = defer_payload(args, agent, proc, handle, target_repo, src_root, stage)
        name = unique_terminal_name("handoff-helper")
        payload = stage / "send.json"
        script = stage / "agent_handoff.py"
        # Written before the terminal starts: the helper reads it as soon as it runs.
        payload.write_text(json.dumps(deferred))
        command = shlex.join([sys.executable, str(script), "--deferred", str(payload)])
        # Stay at a shell when the move fails, so the error is still on screen.
        wrapper = command + " || exec \"${SHELL:-/bin/sh}\""
        argv = ["/bin/sh", "-c", wrapper]
        exports = [f"{key}={os.environ.get(key) or LOGIN_ENV.get(key)}"
                   for key in CONFIG_VARS if os.environ.get(key) or LOGIN_ENV.get(key)]
        if exports:
            argv = ["env", *exports, *argv]
        created = hc_json("run", "--bg", "--name", name, "--cwd", src_cwd,
                          "--no-parent", "--outlive-parent", "--no-notify", "--", *argv)
        addr = created["address"]
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    intended = agent_name_for(args.name, agent.get("name"), agent["agent"], src_root)
    return {"ok": True, "deferred": True, "pane": addr, "target": args.target,
            "attach": hc_attach(args.target, None, intended)}


def run_deferred(payload: Path) -> None:
    args = argparse.Namespace(**json.loads(payload.read_text()))
    try:
        cmd_send(args)
    finally:
        # The shell keeps the error output even after this temporary copy is removed.
        shutil.rmtree(payload.parent)
    # This process is the helper terminal, so its own id is the one to remove.
    schedule_rm(os.environ.get("HC_TERMINAL_ID") or "")


def cmd_send(args) -> None:
    agent = find_agent(args.agent)
    pane, kind = agent["pane_id"], agent["agent"]
    this_pane = pane == os.environ.get("HC_TERMINAL_ID")
    if this_pane and not args.self_handoff and not args.dry_run:
        raise HandoffError("refusing to hand off the agent running this command; "
                           "pass --self to move it after this turn ends")
    deferred = bool(getattr(args, "deferred_helper", False))
    if not args.self_handoff and not args.dry_run and (deferred or agent["agent_status"] in ("working", "blocked")):
        if not args.wait:
            raise HandoffError(f"{kind} in {pane} is {agent['agent_status']}; let it finish "
                               f"(or rerun with --wait)")
        log(f"waiting up to {args.wait_timeout}s for {pane} to go idle")
        wait_until_idle(pane, args.wait_timeout, ADAPTERS[kind].binary if kind in ADAPTERS else kind)
        agent = find_agent(pane)
        if agent["agent"] != kind:
            raise HandoffError(f"the agent in {pane} changed while waiting; retry the handoff")
        if agent["agent_status"] not in ("idle", "done"):
            raise HandoffError(f"{kind} in {pane} is no longer idle; retry the handoff")
    adapter = adapter_for(kind)
    handle = agent.get("agent_session")
    if args.session:
        handle = {"kind": "path" if "/" in args.session else "id", "value": args.session}
    proc, _, _ = foreground(pane, adapter.binary)
    if deferred and (kind != args.expected_kind
                     or (args.expected_pid is not None and (proc or {}).get("pid") != args.expected_pid)
                     or (args.expected_session and agent.get("agent_session") != args.expected_session)):
        raise HandoffError(f"the agent in {pane} changed while waiting; retry the handoff")
    src_cwd = (proc or {}).get("cwd") or agent["cwd"]
    if not handle and proc:
        # `hc ls` reports the launch folder; the session follows the live one.
        peers = [hydrate_hc(dict(a)) for a in hc_agents()]
        same_dir = [a for a in peers if a["agent"] == kind and a["cwd"] == src_cwd]
        if len(same_dir) > 1:
            raise HandoffError(f"{len(same_dir)} {kind} agents share {agent['cwd']} and hc has no "
                               f"session id for {pane}; pass --session <id-or-path>")
        handle = adapter.guess_handle(src_cwd, process_started_at(proc["pid"]))
    if not handle:
        raise HandoffError(f"couldn't find {kind}'s transcript for {pane} "
                           f"(hc doesn't record session ids); pass --session <id-or-path>")

    src_root = git(src_cwd, "rev-parse", "--show-toplevel")
    rel = os.path.relpath(os.path.realpath(src_cwd), os.path.realpath(src_root))
    remotes = remotes_of(src_root)
    if not remotes:
        raise HandoffError(f"{src_root} has no git remotes, so it can't be matched on the target")
    branch = git(src_root, "symbolic-ref", "--short", "-q", "HEAD", check=False) or None
    primary, extras = adapter.locate(handle, src_cwd)
    sid8 = re.sub(r"[^A-Za-z0-9]", "", primary.stem)[-8:]

    where = "this machine" if args.target == LOCAL else args.target
    target_dir, new_branch = args.dir, args.new_branch
    if args.target == LOCAL:
        # Another checkout of the repo here, else a fresh worktree of this one.
        src_real = os.path.realpath(src_root)
        if target_dir and os.path.realpath(os.path.expanduser(target_dir)) == src_real:
            raise HandoffError(f"the agent is already in {src_root}; pick another checkout, "
                               f"or leave out --dir to get a new worktree")
        if args.new_worktree:
            target_dir = src_root
        elif not target_dir:
            try:
                target_dir = find_repo(remotes, branch, None, exclude=(src_real,))[0]
            except HandoffError:
                target_dir = src_root
        common = lambda d: os.path.realpath(git(d, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        if common(target_dir) == common(src_root):
            new_branch = True  # same repo: its branch is checked out here, so use handoff/<id>
    probe = remote(args.target, "probe", {"remotes": remotes, "branch": branch,
                                          "dir": target_dir, "binary": adapter.binary, "kind": kind})
    check_hc_version("this machine", hc_version())
    check_hc_version(where, probe.get("hc_version", "unknown"))
    plan = {"agent": agent.get("name") or pane, "kind": kind, "session": handle["value"],
            "transcript": str(primary), "source_repo": src_root,
            "branch": branch or "(detached)", "dirty": is_dirty(src_root), "target": args.target,
            "target_repo": probe["repo"], "other_target_checkouts": probe["other_checkouts"],
            "source_argv": (proc or {}).get("argv")}
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return
    if args.self_handoff:
        print(json.dumps(defer_send(args, agent, proc, src_cwd, handle, probe["repo"], src_root), indent=2))
        return

    log(f"exiting {kind} in {pane}")
    exit_agent(pane, adapter)
    src_args = adapter.resume_args(handle["value"] if handle["kind"] == "id" else str(primary))
    owned: Optional[Path] = None
    try:
        captured, extras = adapter.capture(handle, src_cwd, primary, extras)
        if captured != primary:
            owned = captured.parent
            primary = captured
        wip = make_wip(src_root)
        refspecs = [f"+HEAD:refs/handoff/{sid8}/head"]
        if wip:
            refspecs.append(f"+{wip}:refs/handoff/{sid8}/wip")
        log(f"pushing {branch or 'HEAD'}{' + uncommitted changes' if wip else ''} to {where}")
        receive = [f"--receive-pack={probe['receive_pack']}"] if probe.get("receive_pack") else []
        run(["git", "-C", src_root, "push", "-q", "--no-verify", *receive,
             ssh_git_url(args.target, probe["repo"]), *refspecs])
        prep = remote(args.target, "prepare", {"repo": probe["repo"], "sid8": sid8, "branch": branch,
                                               "new_branch": new_branch})
        dst_cwd = os.path.normpath(os.path.join(prep["worktree"], rel))
        mapping, extra_pairs, dst_handle = adapter.place(primary, dst_cwd, probe["home"],
                                                         probe["config_root"])
        # The repo root as git and the agent spell it (they differ across
        # symlinks, e.g. macOS /tmp -> /private/tmp); rewrite every spelling.
        roots = {os.path.realpath(src_root), src_root}
        for cwd in (src_cwd, agent["cwd"]):
            roots.add(cwd if rel == "." else cwd[: -len(rel) - 1])
        roots |= {r[len("/private"):] for r in roots if re.match(r"^/private/(tmp|var|etc)/", r)}
        pairs = [(r, prep["worktree"]) for r in sorted(roots, key=len, reverse=True)]
        pairs += extra_pairs + [(str(HOME) + "/", probe["home"].rstrip("/") + "/")]
        sent = send_files(args.target, mapping, [(a, b) for a, b in pairs if a != b], adapter.verbatim)
        log(f"copied {len(sent)} session file(s)")
        if owned:
            shutil.rmtree(owned, ignore_errors=True)
            owned = None
        name = agent_name_for(args.name, agent.get("name"), kind, src_root)
        started = remote(args.target, "start", {
            "kind": kind, "name": name, "cwd": dst_cwd, "trust": not args.no_trust,
            "space": args.space,
            "prelude": adapter.prelude(dst_handle, dst_cwd, mapping),
            "args": adapter.resume_args(dst_handle) + list(args.agent_args)})
    except Exception:
        if owned:
            shutil.rmtree(owned, ignore_errors=True)
        log(f"handoff failed; restarting {kind} in {pane}")
        try:
            remote(args.target, "cleanup-refs", {"repo": probe["repo"], "sid8": sid8})
        except HandoffError as e:
            log(f"could not clean up refs on target: {e}")
        try:
            restart_in_terminal(pane, kind, src_args)
        except HandoffError as e:
            log(f"could not restart it; run `{adapter.binary} {shlex.join(src_args)}` in {pane}: {e}")
        raise

    stashed = False
    if wip and not args.keep_source_changes:
        run(["git", *identity(src_root), "-C", src_root, "stash", "push", "-q", "--include-untracked",
            "-m", f"agent-handoff: {sid8} moved to {where}" + (f" ({prep['worktree']})" if args.target == LOCAL else "")])
        stashed = True
    print(json.dumps({
        "ok": True, **plan, "target_worktree": prep["worktree"], "target_branch": prep["branch"],
        "target_cwd": dst_cwd, "created_worktree": prep["created_worktree"],
        "applied_uncommitted_changes": prep["applied_changes"],
        "target_agent": started["name"], "target_pane": started["pane_id"],
        "target_trust_prompt": started["trust_prompt"],
        "target_waiting_for_user": started["waiting_for_user"],
        "target_app": started.get("app"),
        "source_changes_stashed": stashed, "source_pane_now_at_shell": pane,
        "attach": hc_attach(args.target, started["pane_id"], started.get("name")),
    }, indent=2))


def cmd_doctor(args) -> None:
    report = {"version": VERSION,
              "hc_env": bool(os.environ.get("HC_TERMINAL_ID")),
              "hc_version": hc_version() if have("hc") else None,
              "hc_server": hc_server_up() if have("hc") else False,
              "config_roots": {k: [str(r) for r in a.source_roots()] for k, a in ADAPTERS.items()},
              "tools": {b: have(b) for b in ("git", "hc", "ssh", *ADAPTERS)}}
    if args.target:
        try:
            report["target"] = remote(args.target, "doctor", {})
        except HandoffError as e:
            report["target"] = {"error": str(e)}
    print(json.dumps(report, indent=2))


def step_doctor(_p: dict) -> dict:
    return {"home": str(HOME), "python": sys.version.split()[0],
            "hc_version": hc_version() if have("hc") else None,
            "hc_server": hc_server_up() if have("hc") else False,
            "git_receive_pack": shutil.which("git-receive-pack", path=LOGIN_PATH),
            "config_roots": {k: str(a.config_root(LOGIN_ENV)) for k, a in ADAPTERS.items()},
            "tools": {b: have(b) for b in ("git", "hc", *ADAPTERS)}}


REMOTE_STEPS["doctor"] = step_doctor


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--deferred":
        try:
            run_deferred(Path(sys.argv[2]))
        except HandoffError as e:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        return
    if len(sys.argv) == 4 and sys.argv[1] == "--remote":
        remote_main(sys.argv[2], json.loads(sys.argv[3]))
        return
    ap = argparse.ArgumentParser(prog="agent_handoff.py",
                                 description="Move an hc agent session to another machine.")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list hc agents and whether they can be handed off")
    d = sub.add_parser("doctor", help="check prerequisites here and (optionally) on a target")
    d.add_argument("target", nargs="?")
    s = sub.add_parser("send", help="hand an agent off to a target machine")
    s.add_argument("agent", help="terminal name, address (t7), or session id prefix")
    s.add_argument("target", nargs="?",
                   help="ssh target, e.g. vega@vega (with --from, defaults to this machine)")
    s.add_argument("--here", action="store_true",
                   help="hand off on this machine (no ssh): to --dir, another checkout of the repo, "
                        "or a new worktree")
    s.add_argument("--new-worktree", action="store_true",
                   help="with --here: move the session into a new worktree of the same repo")
    s.add_argument("--dir", help="checkout to use on the target. A directory that is not "
                   "itself a checkout does not block the move; the repo is found on the machine")
    s.add_argument("--space", help="Herdr space id to show the resumed terminal in")
    s.add_argument("--name", help="agent name on the target")
    s.add_argument("--session", help="session id or path, when the transcript guess is ambiguous")
    s.add_argument("--new-branch", action="store_true",
                   help="use a fresh handoff/<id> branch on the target instead of the source branch")
    s.add_argument("--keep-source-changes", action="store_true",
                   help="don't stash the source checkout's uncommitted changes after handoff")
    s.add_argument("--wait", action="store_true", help="wait for a working agent to go idle first")
    s.add_argument("--self", dest="self_handoff", action="store_true",
                   help="move the agent running this command via a helper terminal after its turn ends")
    s.add_argument("--wait-timeout", type=int, default=1800, metavar="SECONDS",
                   help="maximum time to wait for idle with --wait or --self (default: 1800 seconds)")
    s.add_argument("--no-trust", action="store_true",
                   help="leave the agent's 'trust this folder?' prompt on the target for the user")
    s.add_argument("--dry-run", action="store_true", help="show the plan without changing anything")
    for sp in (sub.choices["list"], d, s):
        sp.add_argument("--from", dest="from_host", metavar="HOST",
                        help="run on HOST (where the agent is) over ssh, e.g. to bring a session back")
    s.epilog = "Arguments after -- are passed to the agent on the target, e.g. -- --model opus"
    argv = sys.argv[1:]
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    args = ap.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    args.agent_args = extra
    if args.cmd == "send" and args.wait_timeout <= 0:
        ap.error("--wait-timeout must be a positive number of seconds")
    if args.cmd == "send" and args.here:
        if args.target or args.from_host:
            ap.error("--here hands off on this machine; don't also give a target or --from")
        args.target = LOCAL
    if args.cmd == "send" and args.new_worktree and not args.here:
        ap.error("--new-worktree goes with --here")
    if args.cmd == "send" and not args.target:
        if not args.from_host:
            ap.error("send needs a target, --here, or --from HOST to bring an agent here")
        args.target = this_machine()
        i = argv.index(args.agent)
        argv = argv[:i + 1] + [args.target] + argv[i + 1:]
    if args.from_host and args.cmd == "send" and args.target == args.from_host:
        ap.error(f"--from and the target are both {args.target}; the target is where the agent should go")
    if args.from_host:
        rest, skip = [], 0
        for a in argv:  # drop --from HOST / --from=HOST, keep everything else
            if skip:
                skip -= 1
            elif a == "--from":
                skip = 1
            elif not a.startswith("--from="):
                rest.append(a)
        run_from(args.from_host, rest)
    if os.environ.get("AGENT_HANDOFF_VIA_SSH"):
        adopt_login_path()
    try:
        {"list": cmd_list, "send": cmd_send, "doctor": cmd_doctor}[args.cmd](args)
    except HandoffError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
