#!/usr/bin/env python3
"""agent-handoff: move a herdr-managed coding agent session to another machine.

Runs on the machine that has the session. Everything that must happen on the
target is done by piping this same file to `python3 -` over ssh, so the target
needs only python3, git, herdr and the agent CLI (no install of this tool).

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
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VERSION = "0.4.3"
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


def herdr(*args: str) -> dict:
    out = run(["herdr", *args]).strip()
    return json.loads(out).get("result", {}) if out else {}  # send-text/send-keys print nothing


def log(msg: str) -> None:
    print(f"agent-handoff: {msg}", file=sys.stderr, flush=True)


CONFIG_VARS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR", "GROK_HOME")


def adopt_login_path() -> None:
    """Non-interactive ssh gets a bare environment. Read the login shell's PATH
    (so herdr, claude, codex, omp in /opt/homebrew/bin or ~/.local/bin resolve)
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


def is_dirty(repo: str) -> bool:
    return bool(git(repo, "status", "--porcelain"))


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
    # Where the agent keeps sessions: $<config_var> if set, else ~/<default_root>.
    config_var: Optional[str] = None
    default_root = ""
    # First-run "do you trust this folder?" screen, and the option that says yes.
    trust_marker: Optional[str] = None
    trust_yes: Optional[str] = None
    # Screens that are the user's call (never answered by this tool), e.g. codex
    # asking to trust the hook `herdr integration install` just added.
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
        """Find the session when herdr didn't record one. None if it can't."""
        return None

    def place(self, primary: Path, dst_cwd: str, dst_home: str, dst_root: str
              ) -> Tuple[Dict[str, str], List[Tuple[str, str]], str]:
        """({src abs path: dst abs path}, extra rewrites, dst handle)."""
        raise NotImplementedError

    def resume_args(self, dst_handle: str) -> List[str]:
        raise NotImplementedError


def newest_since(files, started_at: float) -> Optional[Path]:
    hits = [(f.stat().st_mtime, f) for f in files if f.stat().st_mtime >= started_at]
    return max(hits)[1] if hits else None


class Claude(Adapter):
    kind = binary = "claude"
    config_var, default_root = "CLAUDE_CONFIG_DIR", ".claude"
    verbatim = ("file-history",)
    # A machine where claude was never set up or isn't logged in. herdr reports
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
        # Without herdr's claude integration: the newest transcript for this cwd
        # written since the agent started.
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
        # herdr's codex integration doesn't report the thread id, and codex writes
        # through a shared daemon, so match the newest rollout for this cwd that
        # was written after the agent process started.
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
    kind = binary = "omp"
    default_root = ".omp/agent"

    def locate(self, handle, src_cwd):
        if handle["kind"] != "path":
            raise HandoffError("herdr reported an omp session id, not a path; pass --session <path>")
        primary = Path(handle["value"])
        if not primary.exists():
            raise HandoffError(f"omp session file {primary} does not exist")
        sibling = primary.with_suffix("")
        return primary, [sibling] if sibling.exists() else []

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
    grok has no folder-trust screen (its trust is opt-in, for hooks only)."""
    kind = binary = "grok"
    config_var, default_root = "GROK_HOME", ".grok"
    verbatim = ("rewind_points.jsonl",)  # snapshots of the user's files

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


ADAPTERS: Dict[str, Adapter] = {a.kind: a for a in (Claude(), Codex(), Omp(), Pi(), Grok())}


def adapter_for(kind: str) -> Adapter:
    if kind not in ADAPTERS:
        raise HandoffError(f"agent kind '{kind}' is not supported yet (supported: {', '.join(ADAPTERS)})")
    return ADAPTERS[kind]


# ---------------------------------------------------------------- herdr helpers


def find_agent(target: str) -> dict:
    agents = herdr("agent", "list")["agents"]
    hits = [a for a in agents if target in (a.get("name"), a["pane_id"])]
    if not hits:
        hits = [a for a in agents if (a.get("agent_session") or {}).get("value", "").startswith(target)]
    if len(hits) != 1:
        names = ", ".join(f"{a.get('name') or a['pane_id']} ({a['agent']})" for a in agents)
        raise HandoffError(f"'{target}' matches {len(hits)} agents; live agents: {names}")
    return hits[0]


def foreground(pane: str, binary: str = "") -> Tuple[Optional[dict], int, int]:
    """The agent's own process in the pane's foreground group (not a child it
    spawned, like caffeinate), plus the group id and shell pid."""
    info = herdr("pane", "process-info", "--pane", pane)["process_info"]
    procs = info.get("foreground_processes") or []
    mine = [p for p in procs if binary and os.path.basename(p.get("argv0") or "") == binary]
    return (mine[0] if mine else None), info["foreground_process_group_id"], info["shell_pid"]


def process_started_at(pid: int) -> float:
    """Epoch seconds when pid started, from ps's [[dd-]hh:]mm:ss elapsed time."""
    etime = run(["ps", "-o", "etime=", "-p", str(pid)]).strip()
    days, _, clock = etime.rpartition("-")
    secs = 0
    for part in clock.split(":"):
        secs = secs * 60 + int(part)
    return time.time() - secs - int(days or 0) * 86400 - 5


def wait_for_shell(pane: str, timeout: float) -> None:
    end = time.time() + timeout
    while time.time() < end:
        _, pgid, shell_pid = foreground(pane)
        if pgid == shell_pid:
            return
        time.sleep(0.5)
    raise HandoffError(f"pane {pane} did not return to its shell prompt within {timeout:.0f}s")


def exit_agent(pane: str, adapter: Adapter) -> None:
    herdr("pane", "send-text", pane, adapter.exit_text)
    time.sleep(0.4)  # let slash-command autocomplete settle before Enter
    herdr("pane", "send-keys", pane, "enter")
    try:
        wait_for_shell(pane, 20)
        return
    except HandoffError:
        pass
    # Not at a prompt that takes /exit (e.g. a first-run or login screen). TUIs
    # quit on a second ctrl+c within about a second, and a screen change can eat
    # one, so send quick pairs a few times.
    for _ in range(3):
        herdr("pane", "send-keys", pane, "ctrl+c")
        time.sleep(0.3)
        herdr("pane", "send-keys", pane, "ctrl+c")
        try:
            wait_for_shell(pane, 1.5)
            return
        except HandoffError:
            continue
    raise HandoffError(f"{adapter.kind} in {pane} didn't quit (tried {adapter.exit_text} and ctrl+c); "
                       f"it may be showing a screen that needs the user")


def start_agent(name: str, kind: str, pane: str, args: List[str]) -> dict:
    last = None
    for _ in range(10):  # a fresh pane's shell can take a moment to reach its prompt
        try:
            return herdr("agent", "start", name, "--kind", kind, "--pane", pane,
                         "--timeout", "90000", "--", *args)
        except HandoffError as e:
            # The agent may be up but stopped at a first-run screen (herdr reports
            # that as blocked and fails the start); that still counts as started.
            occupant = next((a for a in herdr("agent", "list")["agents"] if a["pane_id"] == pane), None)
            if occupant and occupant["agent"] == kind:
                return {"agent": occupant}
            last = e
            if not re.search(r"agent_pane_busy|available|prompt", str(e)):
                raise
            time.sleep(1)
    raise last  # type: ignore[misc]


def answer_trust(pane: str, adapter: Adapter, accept: bool) -> str:
    """Returns 'none' (no prompt), 'accepted', or 'pending' (left for the user)."""
    if not adapter.trust_marker:
        return "none"
    for _ in range(8):
        screen = run(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "80"])
        if re.search(adapter.trust_marker, screen):
            break
        time.sleep(0.5)
    else:
        return "none"
    if not accept:
        return "pending"
    lines = screen.splitlines()
    cursor = next((i for i, l in enumerate(lines) if re.match(r"\s*[❯›>]\s", l)), None)
    yes = next((i for i, l in enumerate(lines) if re.search(adapter.trust_yes, l)), None)
    if cursor is None or yes is None:
        return "pending"
    keys = ["down" if yes > cursor else "up"] * abs(yes - cursor) + ["enter"]
    for k in keys:
        herdr("pane", "send-keys", pane, k)
        time.sleep(0.15)
    for _ in range(10):
        time.sleep(0.5)
        screen = run(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "80"])
        if not re.search(adapter.trust_marker, screen):
            return "accepted"
    return "pending"


def integration_state(kind: str) -> str:
    for line in run(["herdr", "integration", "status"]).splitlines():
        if line.startswith(f"{kind}:"):
            return line.split(":", 1)[1].strip().split(" ")[0]
    return "unknown"


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


def ssh_failure(target: str, stderr: str) -> str:
    return (f"can't ssh from {socket.gethostname().split('.')[0]} to {target}: {stderr.strip()[-300:].rstrip('.')}. "
            f"agent-handoff needs ssh that works without prompts (`ssh -o BatchMode=yes {target} true`). "
            f"If {target} is the wrong name for that machine, pass the right one; otherwise the user has "
            f"to set up key-based ssh. agent-handoff never changes ssh keys or config.")


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


def find_repo(remotes: List[str], branch: Optional[str], override: Optional[str],
              exclude: Tuple[str, ...] = ()) -> Tuple[str, List[str]]:
    if override:
        path = os.path.realpath(os.path.expanduser(override))
        if not set(remotes) & set(remotes_of(path)):
            raise HandoffError(f"{path} has no remote matching {remotes}")
        matches = [path]
    else:
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
    if not override:  # remember discoveries, not one-off --dir choices
        cache = load_cache()
        for r in remotes:
            cache[r] = matches[0]
        save_cache(cache)
    return matches[0], matches[1:]


# ---------------------------------------------------------------- remote steps


MIN_HERDR = (0, 8, 0)  # oldest version this has been tested with


def herdr_version() -> str:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", run(["herdr", "--version"], check=False))
    return ".".join(m.groups()) if m else "unknown"


def check_herdr_version(where: str, version: str) -> None:
    if version != "unknown" and tuple(int(x) for x in version.split(".")) < MIN_HERDR:
        raise HandoffError(f"herdr {version} on {where} is older than "
                           f"{'.'.join(map(str, MIN_HERDR))}; run `herdr update` there")


def step_probe(p: dict) -> dict:
    missing = [b for b in ("git", "herdr", p["binary"]) if not have(b)]
    if missing:
        raise HandoffError(f"missing on target: {', '.join(missing)}")
    try:
        herdr("workspace", "list")
    except HandoffError as e:
        raise HandoffError(f"herdr server not reachable on target (start herdr there first): {e}")
    repo, others = find_repo(p["remotes"], p["branch"], p.get("dir"))
    return {"home": str(HOME), "repo": repo, "other_checkouts": others,
            "integration": integration_state(p["kind"]),
            "config_root": str(ADAPTERS[p["kind"]].config_root(LOGIN_ENV)),
            "receive_pack": shutil.which("git-receive-pack", path=LOGIN_PATH),
            "herdr_version": herdr_version()}


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
        if is_dirty(path):
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


def step_start(p: dict) -> dict:
    kind, name = p["kind"], p["name"]
    installed = False
    if integration_state(kind) != "current":
        run(["herdr", "integration", "install", kind])
        installed = True
    live = {a.get("name") for a in herdr("agent", "list")["agents"]}
    base, n = name, 2
    while name in live:
        name, n = f"{base}-{n}"[:32], n + 1
    args = ["workspace", "create", "--cwd", p["cwd"], "--label", p["label"]]
    args.append("--focus" if p.get("focus") else "--no-focus")
    ws = herdr(*args)
    pane = ws["root_pane"]["pane_id"]
    start_agent(name, kind, pane, p["args"])
    adapter = ADAPTERS[kind]
    trust = answer_trust(pane, adapter, p.get("trust", True))
    time.sleep(1)
    screen = run(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "80"])
    waiting = [k for k, rx in adapter.user_screens.items() if re.search(rx, screen)]
    status = next((a["agent_status"] for a in herdr("agent", "list")["agents"] if a["pane_id"] == pane), None)
    if status is None:  # it started, then exited (bad flag, auth, daemon error, ...)
        tail = "\n".join(l for l in screen.splitlines() if l.strip())[-1200:]
        raise HandoffError(f"{kind} exited right after starting on the target; its last output:\n{tail}")
    if status == "blocked" and not waiting:
        waiting = ["unrecognized_prompt"]  # e.g. a screen whose wording changed in a new agent version
    return {"name": name, "pane_id": pane, "workspace_id": ws["workspace"]["workspace_id"],
            "installed_integration": installed, "trust_prompt": trust,
            "waiting_for_user": waiting}


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
    rows = []
    for a in herdr("agent", "list")["agents"]:
        sess = a.get("agent_session") or {}
        rows.append({"target": a.get("name") or a["pane_id"], "kind": a["agent"],
                     "status": a["agent_status"], "cwd": a["cwd"],
                     "session": sess.get("value"),
                     "supported": a["agent"] in ADAPTERS,
                     "this_pane": a["pane_id"] == os.environ.get("HERDR_PANE_ID")})
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


def cmd_send(args) -> None:
    agent = find_agent(args.agent)
    pane, kind = agent["pane_id"], agent["agent"]
    if pane == os.environ.get("HERDR_PANE_ID"):
        raise HandoffError("refusing to hand off the agent running this command; run it from another pane")
    adapter = adapter_for(kind)
    handle = agent.get("agent_session")
    if args.session:
        handle = {"kind": "path" if "/" in args.session else "id", "value": args.session}
    proc, _, _ = foreground(pane, adapter.binary)
    src_cwd = (proc or {}).get("cwd") or agent["cwd"]
    if not handle and proc:
        same_dir = [a for a in herdr("agent", "list")["agents"]
                    if a["agent"] == kind and a["cwd"] == agent["cwd"]]
        if len(same_dir) > 1:
            raise HandoffError(f"{len(same_dir)} {kind} agents share {agent['cwd']} and herdr has no "
                               f"session id for {pane}; pass --session <id-or-path>")
        handle = adapter.guess_handle(src_cwd, process_started_at(proc["pid"]))
    if not handle:
        state = integration_state(kind)
        hint = (f"run `herdr integration install {kind}` and restart the agent"
                if state != "current" else "pass --session <id-or-path>")
        raise HandoffError(f"herdr doesn't know {kind}'s session id in {pane}; {hint}")

    if agent["agent_status"] in ("working", "blocked"):
        if not args.wait:
            raise HandoffError(f"{kind} in {pane} is {agent['agent_status']}; let it finish "
                               f"(or rerun with --wait)")
        log(f"waiting for {pane} to go idle")
        herdr("agent", "wait", pane, "--until", "idle", "--until", "done", "--timeout", "1800000")

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
    check_herdr_version("this machine", herdr_version())
    check_herdr_version(where, probe["herdr_version"])
    plan = {"agent": agent.get("name") or pane, "kind": kind, "session": handle["value"],
            "transcript": str(primary), "source_repo": src_root, "branch": branch or "(detached)",
            "dirty": is_dirty(src_root), "target": args.target, "target_repo": probe["repo"],
            "other_target_checkouts": probe["other_checkouts"],
            "target_integration": probe["integration"], "source_argv": (proc or {}).get("argv")}
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return

    log(f"exiting {kind} in {pane}")
    exit_agent(pane, adapter)
    src_args = adapter.resume_args(handle["value"] if handle["kind"] == "id" else str(primary))
    try:
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
        # The repo root as git, the agent and herdr spell it (they differ across
        # symlinks, e.g. macOS /tmp -> /private/tmp); rewrite every spelling.
        roots = {os.path.realpath(src_root), src_root}
        for cwd in (src_cwd, agent["cwd"]):
            roots.add(cwd if rel == "." else cwd[: -len(rel) - 1])
        roots |= {r[len("/private"):] for r in roots if re.match(r"^/private/(tmp|var|etc)/", r)}
        pairs = [(r, prep["worktree"]) for r in sorted(roots, key=len, reverse=True)]
        pairs += extra_pairs + [(str(HOME) + "/", probe["home"].rstrip("/") + "/")]
        sent = send_files(args.target, mapping, [(a, b) for a, b in pairs if a != b], adapter.verbatim)
        log(f"copied {len(sent)} session file(s)")
        name = args.name or agent.get("name") or re.sub(
            r"[^a-z0-9_-]", "-", f"{Path(src_root).name}-{kind}".lower()).strip("-")[:32]
        if not re.match(r"^[a-z]", name):
            name = f"a{name}"[:32]
        started = remote(args.target, "start", {
            "kind": kind, "name": name, "cwd": dst_cwd, "focus": not args.no_focus,
            "label": f"{Path(prep['worktree']).name} ({kind})", "trust": not args.no_trust,
            "args": adapter.resume_args(dst_handle) + list(args.agent_args)})
    except Exception:
        log(f"handoff failed; restarting {kind} in {pane}")
        try:
            remote(args.target, "cleanup-refs", {"repo": probe["repo"], "sid8": sid8})
        except HandoffError as e:
            log(f"could not clean up refs on target: {e}")
        try:
            start_agent(agent.get("name") or f"{kind}-{sid8}".lower(), kind, pane, src_args)
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
        "installed_target_integration": started["installed_integration"],
        "target_trust_prompt": started["trust_prompt"],
        "target_waiting_for_user": started["waiting_for_user"],
        "source_changes_stashed": stashed, "source_pane_now_at_shell": pane,
        "attach": None if args.target == LOCAL else f"herdr --remote {args.target}",
    }, indent=2))


def cmd_doctor(args) -> None:
    report = {"version": VERSION, "herdr_env": os.environ.get("HERDR_ENV") == "1",
              "herdr_version": herdr_version() if have("herdr") else None,
              "config_roots": {k: [str(r) for r in a.source_roots()] for k, a in ADAPTERS.items()},
              "tools": {b: have(b) for b in ("git", "herdr", "ssh", *ADAPTERS)},
              "integrations": {k: integration_state(k) for k in ADAPTERS}}
    if args.target:
        try:
            report["target"] = remote(args.target, "doctor", {})
        except HandoffError as e:
            report["target"] = {"error": str(e)}
    print(json.dumps(report, indent=2))


def step_doctor(_p: dict) -> dict:
    return {"home": str(HOME), "python": sys.version.split()[0],
            "herdr_version": herdr_version() if have("herdr") else None,
            "git_receive_pack": shutil.which("git-receive-pack", path=LOGIN_PATH),
            "config_roots": {k: str(a.config_root(LOGIN_ENV)) for k, a in ADAPTERS.items()},
            "tools": {b: have(b) for b in ("git", "herdr", *ADAPTERS)},
            "herdr_server": ok(["herdr", "workspace", "list"]),
            "integrations": {k: integration_state(k) for k in ADAPTERS}
            if have("herdr") else {}}


REMOTE_STEPS["doctor"] = step_doctor


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--remote":
        remote_main(sys.argv[2], json.loads(sys.argv[3]))
        return
    ap = argparse.ArgumentParser(prog="agent_handoff.py",
                                 description="Move a herdr-managed agent session to another machine.")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list herdr agents and whether they can be handed off")
    d = sub.add_parser("doctor", help="check prerequisites here and (optionally) on a target")
    d.add_argument("target", nargs="?")
    s = sub.add_parser("send", help="hand an agent off to a target machine")
    s.add_argument("agent", help="herdr agent name, pane id, or session id prefix")
    s.add_argument("target", nargs="?",
                   help="ssh target, e.g. vega@vega (with --from, defaults to this machine)")
    s.add_argument("--here", action="store_true",
                   help="hand off on this machine (no ssh): to --dir, another checkout of the repo, "
                        "or a new worktree")
    s.add_argument("--new-worktree", action="store_true",
                   help="with --here: move the session into a new worktree of the same repo")
    s.add_argument("--dir", help="checkout to use on the target (skips discovery)")
    s.add_argument("--name", help="agent name on the target")
    s.add_argument("--session", help="session id or path, if herdr doesn't report one")
    s.add_argument("--new-branch", action="store_true",
                   help="use a fresh handoff/<id> branch on the target instead of the source branch")
    s.add_argument("--keep-source-changes", action="store_true",
                   help="don't stash the source checkout's uncommitted changes after handoff")
    s.add_argument("--wait", action="store_true", help="wait for a working agent to go idle first")
    s.add_argument("--no-focus", action="store_true", help="don't focus the new workspace on the target")
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
