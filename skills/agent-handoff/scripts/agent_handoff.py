#!/usr/bin/env python3
"""agent-handoff: move a herdr-managed coding agent session to another machine.

Runs on the machine that has the session. Everything that must happen on the
target is done by piping this same file to `python3 -` over ssh, so the target
needs only python3, git, herdr and the agent CLI (no install of this tool).

Stdlib only, Python 3.9+ (macOS ships 3.9).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VERSION = "0.1.0"
RESULT_MARK = "AGENT_HANDOFF_RESULT:"
HOME = Path.home()
CONFIG_DIR = HOME / ".config" / "agent-handoff"
TEXT_SUFFIXES = {".jsonl", ".json", ".md", ".txt", ".log", ""}


class HandoffError(Exception):
    pass


# ---------------------------------------------------------------- process helpers


LOGIN_PATH: Optional[str] = None  # set by adopt_login_path() on the target


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


def adopt_login_path() -> None:
    """Non-interactive ssh gets a bare PATH; resolve commands against the login
    shell's PATH so herdr, claude, codex, omp (often in /opt/homebrew/bin or
    ~/.local/bin) are found."""
    global LOGIN_PATH
    shell = os.environ.get("SHELL") or "/bin/sh"
    try:
        out = subprocess.run([shell, "-ilc", 'printf "\\n__AH_PATH__%s\\n" "$PATH"'],
                             capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    found = re.findall(r"__AH_PATH__(.*)", out)
    parts = (found[-1].split(":") if found else []) + os.environ.get("PATH", "").split(":")
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
    # First-run "do you trust this folder?" screen, and the option that says yes.
    trust_marker: Optional[str] = None
    trust_yes: Optional[str] = None
    # Screens that are the user's call (never answered by this tool), e.g. codex
    # asking to trust the hook `herdr integration install` just added.
    user_screens: Dict[str, str] = {}

    def locate(self, handle: dict, src_cwd: str) -> Tuple[Path, List[Path]]:
        """(primary transcript, extra files/dirs). All must live under $HOME."""
        raise NotImplementedError

    def guess_handle(self, src_cwd: str, started_at: float) -> Optional[dict]:
        """Find the session when herdr didn't record one. None if it can't."""
        return None

    def place(self, primary: Path, src_cwd: str, dst_cwd: str, dst_home: str
              ) -> Tuple[Dict[str, str], List[Tuple[str, str]], str]:
        """({src path rel to home: dst path rel to home}, extra rewrites, dst handle)."""
        raise NotImplementedError

    def resume_args(self, dst_handle: str) -> List[str]:
        raise NotImplementedError


class Claude(Adapter):
    kind = binary = "claude"
    trust_marker = r"trust this folder"
    trust_yes = r"Yes, I trust"

    def locate(self, handle, src_cwd):
        sid = handle["value"] if handle["kind"] == "id" else Path(handle["value"]).stem
        projects = HOME / ".claude" / "projects"
        preferred = projects / claude_project_dir(src_cwd) / f"{sid}.jsonl"
        hits = [preferred] if preferred.exists() else sorted(projects.glob(f"*/{sid}.jsonl"))
        if not hits:
            raise HandoffError(f"no Claude transcript for session {sid} under {projects}")
        primary = hits[0]
        extras = [p for p in (primary.with_suffix(""), HOME / ".claude" / "file-history" / sid)
                  if p.exists()]
        return primary, extras

    def place(self, primary, src_cwd, dst_cwd, dst_home):
        sid = primary.stem
        src_proj, dst_proj = primary.parent.name, claude_project_dir(dst_cwd)
        mapping = {}
        for rel in (f".claude/projects/{src_proj}/{sid}.jsonl", f".claude/projects/{src_proj}/{sid}"):
            mapping[rel] = rel.replace(f"/{src_proj}/", f"/{dst_proj}/")
        mapping[f".claude/file-history/{sid}"] = f".claude/file-history/{sid}"
        return mapping, [(f"projects/{src_proj}/", f"projects/{dst_proj}/")], sid

    def resume_args(self, dst_handle):
        return ["--resume", dst_handle]


class Codex(Adapter):
    kind = binary = "codex"
    exit_text = "/quit"
    trust_marker = r"Trust this folder\?"
    trust_yes = r"Trust and continue"
    user_screens = {"hook_review": r"Hooks need review"}

    def locate(self, handle, src_cwd):
        if handle["kind"] == "path":
            return Path(handle["value"]), []
        sid = handle["value"]
        for sub in ("sessions", "archived_sessions"):
            hits = sorted((HOME / ".codex" / sub).glob(f"**/rollout-*{sid}.jsonl"))
            if hits:
                return hits[-1], []
        raise HandoffError(f"no Codex rollout for session {sid} under ~/.codex/sessions")

    def guess_handle(self, src_cwd, started_at):
        # herdr's codex integration doesn't report the thread id, and codex writes
        # through a shared daemon, so match the newest rollout for this cwd that
        # was written after the agent process started.
        best = None
        for f in (HOME / ".codex" / "sessions").glob("**/rollout-*.jsonl"):
            mtime = f.stat().st_mtime
            if mtime < started_at or (best and mtime <= best[0]):
                continue
            with open(f) as fh:
                meta = json.loads(fh.readline() or "{}")
            if meta.get("type") == "session_meta" and meta["payload"].get("cwd") == src_cwd:
                best = (mtime, f)
        return {"kind": "path", "value": str(best[1])} if best else None

    def place(self, primary, src_cwd, dst_cwd, dst_home):
        rel = str(primary.relative_to(HOME))
        sid = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})\.jsonl$", primary.name)
        if not sid:
            raise HandoffError(f"can't read a session id from {primary.name}")
        return {rel: rel}, [], sid.group(1)

    def resume_args(self, dst_handle):
        m = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})(?:\.jsonl)?$", dst_handle)
        return ["resume", m.group(1) if m else dst_handle]


class Omp(Adapter):
    kind = binary = "omp"

    def locate(self, handle, src_cwd):
        if handle["kind"] != "path":
            raise HandoffError("herdr reported an omp session id, not a path; pass --session <path>")
        primary = Path(handle["value"])
        if not primary.exists():
            raise HandoffError(f"omp session file {primary} does not exist")
        sibling = primary.with_suffix("")
        return primary, [sibling] if sibling.exists() else []

    def place(self, primary, src_cwd, dst_cwd, dst_home):
        src_dir, dst_dir = primary.parent.name, omp_session_dir(dst_cwd, dst_home)
        base = f".omp/agent/sessions"
        mapping = {f"{base}/{src_dir}/{primary.name}": f"{base}/{dst_dir}/{primary.name}",
                   f"{base}/{src_dir}/{primary.stem}": f"{base}/{dst_dir}/{primary.stem}"}
        dst_handle = f"{dst_home}/{base}/{dst_dir}/{primary.name}"
        return mapping, [(f"sessions/{src_dir}/", f"sessions/{dst_dir}/")], dst_handle

    def resume_args(self, dst_handle):
        return ["--resume", dst_handle]


ADAPTERS: Dict[str, Adapter] = {a.kind: a for a in (Claude(), Codex(), Omp())}


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
    except HandoffError:
        herdr("pane", "send-keys", pane, "ctrl+c")
        time.sleep(0.3)
        herdr("pane", "send-keys", pane, "ctrl+c")
        wait_for_shell(pane, 10)


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


def remote(target: str, step: str, payload: dict) -> dict:
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
    raise HandoffError(f"[{target}] step '{step}' failed: {(p.stderr or p.stdout).strip()[-2000:]}")


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


def find_repo(remotes: List[str], branch: Optional[str], override: Optional[str]) -> Tuple[str, List[str]]:
    if override:
        path = os.path.realpath(os.path.expanduser(override))
        if not set(remotes) & set(remotes_of(path)):
            raise HandoffError(f"{path} has no remote matching {remotes}")
        matches = [path]
    else:
        cache = load_cache()
        cached = [cache[r] for r in remotes if r in cache and os.path.isdir(cache[r])]
        matches = [p for p in cached if set(remotes) & set(remotes_of(p))]
        if not matches:
            seen = set()
            for root, depth in scan_roots():
                for repo in iter_repos(root, depth):
                    real = os.path.realpath(repo)
                    if real in seen:
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
            "integration": integration_state(p["kind"])}


def step_prepare(p: dict) -> dict:
    repo, sid8, src_branch = p["repo"], p["sid8"], p["branch"]
    head_ref, wip_ref = f"refs/handoff/{sid8}/head", f"refs/handoff/{sid8}/wip"
    head = git(repo, "rev-parse", head_ref)
    branch = src_branch if (src_branch and not p.get("new_branch")) else f"handoff/{sid8}"
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
        path = str(Path(repo).parent / f"{Path(repo).name}.worktrees" / slug)
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
    waiting = []
    if adapter.user_screens:
        time.sleep(1)
        screen = run(["herdr", "pane", "read", pane, "--source", "visible", "--lines", "80"])
        waiting = [k for k, rx in adapter.user_screens.items() if re.search(rx, screen)]
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
    return git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "agent-handoff wip")


def rewrite_tree(src: Path, dst: Path, pairs: List[Tuple[str, str]]) -> None:
    items = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
    for item in items:
        out = dst if src.is_file() else dst / item.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = item.read_bytes()
        if item.suffix in TEXT_SUFFIXES:
            try:
                text = data.decode("utf-8")
                for a, b in pairs:
                    text = text.replace(a, b)
                data = text.encode("utf-8")
            except UnicodeDecodeError:
                pass  # binary file with a text-ish suffix: copy unchanged
        out.write_bytes(data)


def send_files(target: str, mapping: Dict[str, str], pairs: List[Tuple[str, str]]) -> List[str]:
    sent = []
    with tempfile.TemporaryDirectory() as stage:
        for src_rel, dst_rel in mapping.items():
            src = HOME / src_rel
            if src.exists():
                # file-history holds backups of the user's own files: copy them verbatim
                rewrite_tree(src, Path(stage) / dst_rel, [] if "file-history" in src_rel else pairs)
                sent.append(dst_rel)
        archive = Path(stage).with_suffix(".tgz")
        with tarfile.open(archive, "w:gz") as tar:
            for rel in sent:
                tar.add(Path(stage) / rel, arcname=rel)
        with open(archive, "rb") as fh:
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", target, 'tar -C "$HOME" -xzf -'],
                               stdin=fh, capture_output=True, text=True)
        archive.unlink()
        if p.returncode != 0:
            raise HandoffError(f"copying transcript to {target} failed: {p.stderr.strip()}")
    return sent


def ssh_git_url(target: str, path: str) -> str:
    return f"ssh://{target}{path}"


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
        raise HandoffError(f"{src_root} has no git remotes, so it can't be matched on {args.target}")
    branch = git(src_root, "symbolic-ref", "--short", "-q", "HEAD", check=False) or None
    primary, extras = adapter.locate(handle, src_cwd)
    sid8 = re.sub(r"[^A-Za-z0-9]", "", primary.stem)[-8:]

    probe = remote(args.target, "probe", {"remotes": remotes, "branch": branch,
                                          "dir": args.dir, "binary": adapter.binary, "kind": kind})
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
        log(f"pushing {branch or 'HEAD'}{' + uncommitted changes' if wip else ''} to {args.target}")
        run(["git", "-C", src_root, "push", "-q", "--no-verify",
             ssh_git_url(args.target, probe["repo"]), *refspecs])
        prep = remote(args.target, "prepare", {"repo": probe["repo"], "sid8": sid8, "branch": branch,
                                               "new_branch": args.new_branch})
        dst_cwd = os.path.normpath(os.path.join(prep["worktree"], rel))
        mapping, extra_pairs, dst_handle = adapter.place(primary, src_cwd, dst_cwd, probe["home"])
        # The repo root as git, the agent and herdr spell it (they differ across
        # symlinks, e.g. macOS /tmp -> /private/tmp); rewrite every spelling.
        roots = {os.path.realpath(src_root), src_root}
        for cwd in (src_cwd, agent["cwd"]):
            roots.add(cwd if rel == "." else cwd[: -len(rel) - 1])
        roots |= {r[len("/private"):] for r in roots if re.match(r"^/private/(tmp|var|etc)/", r)}
        pairs = [(r, prep["worktree"]) for r in sorted(roots, key=len, reverse=True)]
        pairs += extra_pairs + [(str(HOME) + "/", probe["home"].rstrip("/") + "/")]
        sent = send_files(args.target, mapping, [(a, b) for a, b in pairs if a != b])
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
        git(src_root, "stash", "push", "-q", "--include-untracked",
            "-m", f"agent-handoff: {sid8} moved to {args.target}")
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
        "attach": f"herdr --remote {args.target}",
    }, indent=2))


def cmd_doctor(args) -> None:
    report = {"version": VERSION, "herdr_env": os.environ.get("HERDR_ENV") == "1",
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
    s.add_argument("target", help="ssh target, e.g. vega@vega")
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
    s.add_argument("agent_args", nargs="*", help="extra agent CLI args, after --")
    args = ap.parse_args()
    try:
        {"list": cmd_list, "send": cmd_send, "doctor": cmd_doctor}[args.cmd](args)
    except HandoffError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
