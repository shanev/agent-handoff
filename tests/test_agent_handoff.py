"""Tests for agent_handoff.py. Stdlib only: python3 -m unittest discover -s tests

Pure logic is tested directly. The git steps run against real temporary repos.
Anything that would drive herdr or ssh is replaced with a fake.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "agent-handoff" / "scripts" / "agent_handoff.py"
spec = importlib.util.spec_from_file_location("agent_handoff", SCRIPT)
ah = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ah)

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(os.path.realpath(self._tmp.name))
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmp.cleanup()


# ---------------------------------------------------------------- pure helpers


class NormalizeRemote(unittest.TestCase):
    def test_equivalent_forms_match(self):
        forms = ["git@github.com:Tensor-Systems/Hark.git",
                 "https://github.com/tensor-systems/hark",
                 "https://github.com/tensor-systems/hark.git/",
                 "ssh://git@github.com:22/tensor-systems/hark.git",
                 "https://user:token@github.com/tensor-systems/hark.git"]
        self.assertEqual({ah.normalize_remote(f) for f in forms}, {"github.com/tensor-systems/hark"})

    def test_hosts_stay_distinct(self):
        self.assertNotEqual(ah.normalize_remote("git@gitlab.com:o/r.git"),
                            ah.normalize_remote("git@github.com:o/r.git"))

    def test_local_path_remote(self):
        self.assertEqual(ah.normalize_remote("/tmp/../tmp/repo"), os.path.realpath("/tmp/repo"))


class SessionDirNames(unittest.TestCase):
    def test_claude_replaces_every_non_alphanumeric(self):
        self.assertEqual(ah.claude_project_dir("/Users/a/github.com/o/my_repo"),
                         "-Users-a-github-com-o-my-repo")

    def test_omp_inside_home(self):
        self.assertEqual(ah.omp_session_dir("/Users/a/github.com/o/r", "/Users/a"), "-github.com-o-r")
        self.assertEqual(ah.omp_session_dir("/Users/a", "/Users/a"), "-")

    def test_omp_outside_home(self):
        # matches what omp itself writes for /private/tmp/...
        self.assertEqual(ah.omp_session_dir("/private/tmp/x/y", "/Users/a"), "--private-tmp-x-y--")


class Adapters(unittest.TestCase):
    def test_claude_place_moves_project_dir_and_config_root(self):
        mapping, pairs, handle = ah.ADAPTERS["claude"].place(
            Path("/Users/a/.claude/projects/-Users-a-r/abc.jsonl"), "/home/b/code/r", "/home/b", "/home/b/cfg")
        self.assertEqual(mapping["/Users/a/.claude/projects/-Users-a-r/abc.jsonl"],
                         "/home/b/cfg/projects/-home-b-code-r/abc.jsonl")
        self.assertEqual(mapping["/Users/a/.claude/file-history/abc"], "/home/b/cfg/file-history/abc")
        self.assertIn(("/Users/a/.claude/", "/home/b/cfg/"), pairs)
        self.assertEqual(handle, "abc")
        self.assertEqual(ah.ADAPTERS["claude"].resume_args(handle), ["--resume", "abc"])

    def test_codex_place_keeps_date_layout(self):
        sid = "01a10cfe-f8f7-70e1-9c1d-8085ccccea24"
        src = Path(f"/Users/a/.codex/sessions/2026/10/05/rollout-2026-10-05T12-56-38-{sid}.jsonl")
        mapping, pairs, handle = ah.ADAPTERS["codex"].place(src, "/home/b/r", "/home/b", "/home/b/.codex")
        self.assertEqual(mapping[str(src)], f"/home/b/.codex/sessions/2026/10/05/{src.name}")
        self.assertEqual(handle, sid)
        self.assertEqual(ah.ADAPTERS["codex"].resume_args(str(src)), ["resume", sid])

    def test_omp_place_uses_target_cwd(self):
        src = Path("/Users/a/.omp/agent/sessions/-src-r/2026_x.jsonl")
        mapping, pairs, handle = ah.ADAPTERS["omp"].place(src, "/home/b/code/r", "/home/b", "/home/b/.omp/agent")
        self.assertEqual(handle, "/home/b/.omp/agent/sessions/-code-r/2026_x.jsonl")
        self.assertIn(("sessions/-src-r/", "sessions/-code-r/"), pairs)
        self.assertEqual(ah.ADAPTERS["omp"].resume_args(handle), ["--resume", handle])

    def test_config_root_env_override(self):
        claude = ah.ADAPTERS["claude"]
        self.assertEqual(claude.config_root({"CLAUDE_CONFIG_DIR": "/x/claude"}), Path("/x/claude"))
        self.assertEqual(claude.config_root({}), ah.HOME / ".claude")

    def test_unsupported_kind(self):
        with self.assertRaisesRegex(ah.HandoffError, "not supported"):
            ah.adapter_for("hermes")


class HerdrVersion(unittest.TestCase):
    def test_too_old(self):
        with self.assertRaisesRegex(ah.HandoffError, "older than"):
            ah.check_herdr_version("vega", "0.7.9")

    def test_ok_and_unknown(self):
        ah.check_herdr_version("vega", "0.8.0")
        ah.check_herdr_version("vega", "1.2.3")
        ah.check_herdr_version("vega", "unknown")


# ---------------------------------------------------------------- transcripts on disk


class SessionLookup(TempDirTest):
    def test_claude_locate_and_guess_in_custom_config_dir(self):
        cfg, cwd = self.tmp / "claude-cfg", "/work/r"
        proj = cfg / "projects" / ah.claude_project_dir(cwd)
        proj.mkdir(parents=True)
        old, new = proj / "old.jsonl", proj / "new.jsonl"
        old.write_text("{}\n")
        os.utime(old, (time.time() - 3600,) * 2)
        new.write_text("{}\n")
        (proj / "new").mkdir()  # subagent dir travels with it
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(cfg)}):
            claude = ah.ADAPTERS["claude"]
            self.assertEqual(claude.guess_handle(cwd, time.time() - 60), {"kind": "id", "value": "new"})
            primary, extras = claude.locate({"kind": "id", "value": "new"}, cwd)
        self.assertEqual(primary, new)
        self.assertEqual(extras, [proj / "new"])

    def test_codex_guess_matches_cwd_and_start_time(self):
        root = self.tmp / "codex"
        day = root / "sessions" / "2026" / "10" / "05"
        day.mkdir(parents=True)

        def rollout(name, cwd, age):
            f = day / f"rollout-{name}.jsonl"
            f.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n")
            os.utime(f, (time.time() - age,) * 2)
            return f

        rollout("a-00000000-0000-0000-0000-000000000001", "/work/other", 1)
        rollout("b-00000000-0000-0000-0000-000000000002", "/work/r", 7200)  # before agent start
        mine = rollout("c-00000000-0000-0000-0000-000000000003", "/work/r", 5)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(root)}):
            got = ah.ADAPTERS["codex"].guess_handle("/work/r", time.time() - 60)
        self.assertEqual(got, {"kind": "path", "value": str(mine)})

    def test_rewrite_tree_rewrites_text_and_leaves_binary(self):
        src = self.tmp / "src"
        src.mkdir()
        (src / "t.jsonl").write_text('{"cwd":"/Users/a/repo","p":"/Users/a/.claude/x"}\n')
        (src / "b.bin").write_bytes(b"\xff\xfe/Users/a/repo")
        ah.rewrite_tree(src, self.tmp / "out", [("/Users/a/repo", "/home/b/r"), ("/Users/a/", "/home/b/")])
        self.assertEqual((self.tmp / "out" / "t.jsonl").read_text(),
                         '{"cwd":"/home/b/r","p":"/home/b/.claude/x"}\n')
        self.assertEqual((self.tmp / "out" / "b.bin").read_bytes(), b"\xff\xfe/Users/a/repo")


# ---------------------------------------------------------------- git transport


class GitTransport(TempDirTest):
    """make_wip on a source, push its refs into a target clone the way `send`
    does (over a path instead of ssh), then run the target's prepare step."""

    def setUp(self):
        super().setUp()
        self.origin = self.tmp / "origin.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.tmp / "seed"
        sh("git", "clone", "-q", str(self.origin), str(seed))
        (seed / "a.txt").write_text("one\n")
        sh("git", "add", "-A", cwd=seed)
        sh("git", "commit", "-qm", "init", cwd=seed)
        sh("git", "push", "-q", "origin", "main", cwd=seed)
        self.src = self.tmp / "laptop" / "repo"
        self.dst = self.tmp / "mini" / "elsewhere" / "repo"
        sh("git", "clone", "-q", str(self.origin), str(self.src))
        sh("git", "clone", "-q", str(self.origin), str(self.dst))

    def handoff_refs(self, sid8="abcd1234"):
        wip = ah.make_wip(str(self.src))
        refspecs = [f"+HEAD:refs/handoff/{sid8}/head"] + ([f"+{wip}:refs/handoff/{sid8}/wip"] if wip else [])
        sh("git", "push", "-q", str(self.dst), *refspecs, cwd=self.src)
        return wip

    def prepare(self, **kw):
        p = {"repo": str(self.dst), "sid8": "abcd1234", "branch": "main", "new_branch": False, **kw}
        return ah.step_prepare(p)

    def test_wip_snapshot_leaves_source_untouched(self):
        (self.src / "a.txt").write_text("two\n")
        (self.src / "new.txt").write_text("untracked\n")
        sh("git", "add", "a.txt", cwd=self.src)  # one staged change
        before = sh("git", "status", "--porcelain", cwd=self.src)
        wip = ah.make_wip(str(self.src))
        self.assertTrue(wip)
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.src), before)
        self.assertEqual(sh("git", "show", f"{wip}:new.txt", cwd=self.src), "untracked")

    def test_clean_tree_has_no_wip(self):
        self.assertIsNone(ah.make_wip(str(self.src)))

    def test_fast_forwards_in_place_and_applies_changes(self):
        (self.src / "b.txt").write_text("committed\n")
        sh("git", "add", "-A", cwd=self.src)
        sh("git", "commit", "-qm", "local commit", cwd=self.src)
        (self.src / "a.txt").write_text("edited\n")
        (self.src / "untracked.txt").write_text("u\n")
        (self.src / "a.txt").chmod(0o644)
        self.handoff_refs()
        out = self.prepare()
        self.assertEqual(out["worktree"], os.path.realpath(self.dst))
        self.assertFalse(out["created_worktree"])
        self.assertTrue(out["applied_changes"])
        self.assertEqual((self.dst / "b.txt").read_text(), "committed\n")
        self.assertEqual((self.dst / "a.txt").read_text(), "edited\n")
        self.assertEqual((self.dst / "untracked.txt").read_text(), "u\n")
        self.assertEqual(sh("git", "for-each-ref", "refs/handoff", cwd=self.dst), "")

    def test_deleted_file_is_deleted_on_target(self):
        (self.src / "a.txt").unlink()
        self.handoff_refs()
        self.prepare()
        self.assertFalse((self.dst / "a.txt").exists())

    def test_branch_not_checked_out_gets_worktree(self):
        sh("git", "checkout", "-qb", "feature", cwd=self.src)
        (self.src / "f.txt").write_text("f\n")
        self.handoff_refs()
        out = self.prepare(branch="feature")
        self.assertTrue(out["created_worktree"])
        self.assertEqual(Path(out["worktree"]), Path(os.path.realpath(self.dst)).parent / "repo.worktrees" / "feature")
        self.assertEqual((Path(out["worktree"]) / "f.txt").read_text(), "f\n")
        self.assertEqual(sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=out["worktree"]), "feature")

    def test_dirty_target_is_refused(self):
        (self.dst / "a.txt").write_text("someone else's edit\n")
        self.handoff_refs()
        with self.assertRaisesRegex(ah.HandoffError, "uncommitted changes"):
            self.prepare()

    def test_diverged_target_is_refused_unless_new_branch(self):
        (self.dst / "c.txt").write_text("only on target\n")
        sh("git", "add", "-A", cwd=self.dst)
        sh("git", "commit", "-qm", "target-only", cwd=self.dst)
        self.handoff_refs()
        with self.assertRaisesRegex(ah.HandoffError, "commits the source doesn't"):
            self.prepare()
        out = self.prepare(new_branch=True)
        self.assertEqual(out["branch"], "handoff/abcd1234")

    def test_detached_source_uses_handoff_branch(self):
        sh("git", "checkout", "-q", "--detach", cwd=self.src)
        self.handoff_refs()
        out = self.prepare(branch=None)
        self.assertEqual(out["branch"], "handoff/abcd1234")
        self.assertTrue(out["created_worktree"])


class RepoDiscovery(TempDirTest):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        for name, attr in (("HOME", self.home), ("CONFIG_DIR", self.home / ".config" / "agent-handoff")):
            p = mock.patch.object(ah, name, attr)
            p.start()
            self.addCleanup(p.stop)

    def make_repo(self, rel, remote):
        path = self.home / rel
        path.mkdir(parents=True)
        sh("git", "init", "-q", str(path))
        sh("git", "-C", str(path), "remote", "add", "origin", remote)
        return os.path.realpath(path)

    def test_finds_checkout_by_remote_at_any_path_and_caches_it(self):
        want = self.make_repo("src/deep/hark", "https://github.com/tensor-systems/hark.git")
        self.make_repo("src/other", "git@github.com:o/other.git")
        repo, others = ah.find_repo(["github.com/tensor-systems/hark"], "main", None)
        self.assertEqual((repo, others), (want, []))
        self.assertEqual(ah.load_cache()["github.com/tensor-systems/hark"], want)

    def test_skips_ci_runner_and_dependency_dirs(self):
        # both sit inside a search root (~/src, ~/code) at a depth the scan reaches
        self.make_repo("src/actions-runner/_work/hark/hark", "git@github.com:tensor-systems/hark.git")
        self.make_repo("code/app/node_modules/hark", "git@github.com:tensor-systems/hark.git")
        with self.assertRaisesRegex(ah.HandoffError, "no checkout"):
            ah.find_repo(["github.com/tensor-systems/hark"], "main", None)

    def test_dir_override_must_match_and_is_not_cached(self):
        repo = self.make_repo("x/hark", "git@github.com:tensor-systems/hark.git")
        self.assertEqual(ah.find_repo(["github.com/tensor-systems/hark"], None, repo)[0], repo)
        self.assertEqual(ah.load_cache(), {})
        with self.assertRaisesRegex(ah.HandoffError, "no remote matching"):
            ah.find_repo(["github.com/o/elsewhere"], None, repo)

    def test_roots_env_override(self):
        repo = self.make_repo("odd/place/hark", "git@github.com:tensor-systems/hark.git")
        with mock.patch.dict(os.environ, {"AGENT_HANDOFF_ROOTS": str(self.home / "odd")}):
            self.assertEqual(ah.find_repo(["github.com/tensor-systems/hark"], None, None)[0], repo)


# ---------------------------------------------------------------- herdr-facing logic (faked)


class TrustScreen(unittest.TestCase):
    CLAUDE_SCREEN = """ Quick safety check: Is this a project you created or one you trust?
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel"""
    CODEX_SCREEN = """  Trust this folder? Codex can read, edit, and run files here.
› 1. Trust and continue
  2. Quit"""

    def run_trust(self, kind, screen, accept=True):
        screens = [screen] * 3 + ["❯ "]  # the screen goes away after the keys
        keys = []
        with mock.patch.object(ah, "run", side_effect=lambda cmd, **kw: screens.pop(0) if len(screens) > 1 else screens[0]), \
             mock.patch.object(ah, "herdr", side_effect=lambda *a: keys.append(a[-1]) or {}), \
             mock.patch.object(ah.time, "sleep"):
            result = ah.answer_trust("w1:p1", ah.ADAPTERS[kind], accept)
        return result, keys

    def test_claude_moves_down_to_yes(self):
        self.assertEqual(self.run_trust("claude", self.CLAUDE_SCREEN), ("accepted", ["down", "enter"]))

    def test_codex_yes_is_already_selected(self):
        self.assertEqual(self.run_trust("codex", self.CODEX_SCREEN), ("accepted", ["enter"]))

    def test_no_trust_leaves_it_pending(self):
        self.assertEqual(self.run_trust("claude", self.CLAUDE_SCREEN, accept=False), ("pending", []))

    def test_no_screen(self):
        with mock.patch.object(ah, "run", return_value="❯ "), mock.patch.object(ah.time, "sleep"):
            self.assertEqual(ah.answer_trust("w1:p1", ah.ADAPTERS["claude"], True), "none")


class StartAgent(unittest.TestCase):
    def test_agent_stopped_at_first_run_screen_counts_as_started(self):
        def fake(*args):
            if args[:2] == ("agent", "start"):
                raise ah.HandoffError("agent_blocked")
            return {"agents": [{"pane_id": "w1:p1", "agent": "claude"}]}
        with mock.patch.object(ah, "herdr", side_effect=fake):
            self.assertEqual(ah.start_agent("n", "claude", "w1:p1", [])["agent"]["agent"], "claude")

    def test_busy_pane_is_retried(self):
        calls = []

        def fake(*args):
            if args[:2] == ("agent", "start"):
                calls.append(1)
                if len(calls) < 3:
                    raise ah.HandoffError('{"code":"agent_pane_busy"}')
                return {"agent": "ok"}
            return {"agents": []}
        with mock.patch.object(ah, "herdr", side_effect=fake), mock.patch.object(ah.time, "sleep"):
            self.assertEqual(ah.start_agent("n", "claude", "w1:p1", []), {"agent": "ok"})
        self.assertEqual(len(calls), 3)

    def test_other_errors_raise(self):
        def fake(*args):
            if args[:2] == ("agent", "start"):
                raise ah.HandoffError("agent_name_taken")
            return {"agents": []}
        with mock.patch.object(ah, "herdr", side_effect=fake):
            with self.assertRaisesRegex(ah.HandoffError, "agent_name_taken"):
                ah.start_agent("n", "claude", "w1:p1", [])


class CommandLine(unittest.TestCase):
    def main(self, *argv):
        seen = {}
        with mock.patch.object(sys, "argv", ["agent_handoff.py", *argv]), \
             mock.patch.object(ah, "cmd_send", side_effect=lambda a: seen.setdefault("args", a)), \
             mock.patch.object(ah, "run_from", side_effect=lambda h, rest: seen.update(host=h, rest=rest)):
            ah.main()
        return seen

    def test_agent_args_after_double_dash_with_options_first(self):
        args = self.main("send", "hark", "vega@vega", "--dir", "/x", "--", "--model", "opus")["args"]
        self.assertEqual((args.dir, args.agent_args), ("/x", ["--model", "opus"]))

    def test_from_is_stripped_before_running_remotely(self):
        for argv in (["send", "a", "me@laptop", "--from", "vega@vega", "--", "--x"],
                     ["send", "a", "me@laptop", "--from=vega@vega", "--", "--x"]):
            seen = self.main(*argv)
            self.assertEqual(seen["host"], "vega@vega")
            self.assertEqual(seen["rest"], ["send", "a", "me@laptop", "--", "--x"])

    def test_remote_step_reports_errors_as_json(self):
        buf = io.StringIO()
        with mock.patch.object(ah, "adopt_login_path"), redirect_stdout(buf):
            ah.remote_main("cleanup-refs", {"repo": "/nonexistent", "sid8": "x"})
            ah.remote_main("probe", {"remotes": [], "branch": None, "binary": "no-such-binary-xyz", "kind": "claude"})
        ok_line, err_line = [json.loads(l[len(ah.RESULT_MARK):]) for l in buf.getvalue().splitlines()]
        self.assertTrue(ok_line["ok"])
        self.assertFalse(err_line["ok"])
        self.assertIn("missing on target", err_line["error"])


if __name__ == "__main__":
    unittest.main()
