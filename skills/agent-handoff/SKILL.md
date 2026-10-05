---
name: agent-handoff
description: "Move a running coding-agent session (Claude Code, Codex, omp, pi, grok) from one machine to another over SSH, with its transcript, branch and uncommitted changes, and resume it in herdr on the target. Use when the user asks to hand off, move, transfer, or continue an agent session on another machine/host (e.g. 'move the hark claude to vega'), or on this machine into another checkout or its own worktree. Requires herdr on both machines."
version: 0.4.6
author: Shane Vitarana
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [Coding-Agent, herdr, Handoff, Claude, Codex, omp, pi, grok, SSH, Tailscale]
    related_skills: [herdr, claude-code, codex]
---

# Agent handoff

Moves a coding agent that is running in a herdr pane on this machine to another machine, so the user can carry on there (typically: laptop → always-on box on the tailnet, and back).

One handoff does all of this:

1. Finds the agent in herdr and its session: herdr's agent integration records the id. Without it, the script uses the newest transcript for that folder written since the agent started.
2. Checks the target over ssh: tools present, herdr server up, and **a checkout of the same repo** — matched by git remote, so it may live at a different path.
3. Quits the agent cleanly (`/exit`), so nothing else writes to the transcript.
4. Pushes `HEAD` plus a snapshot of uncommitted changes **straight to the target over ssh** (`refs/handoff/*`, removed afterwards). Nothing is pushed to GitHub, and no branch on either side is rewritten.
5. On the target: fast-forwards the branch where it is already checked out, or else makes a worktree next to the repo (`<repo>.worktrees/<branch>`), then applies the uncommitted changes.
6. Copies the transcript into the target agent's session store (honouring `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `PI_CODING_AGENT_DIR` and `GROK_HOME` on either side), replacing the old repo path, home directory and config folder with the new ones inside it.
7. Installs the herdr integration for that agent on the target if it's missing (so the next handoff back knows the session id), opens a herdr workspace there and resumes the agent. If the agent asks whether to trust the folder, the script answers yes: the user was already working in this repo on the source. Pass `--no-trust` to leave that question for the user.
8. Stashes the uncommitted changes in the source checkout (`git stash list` shows them), so a later handoff back applies cleanly.

If anything fails after step 3, including the agent exiting straight after it starts on the target, the script restarts the agent where it was and reports why.

## How to run it

The script is `scripts/agent_handoff.py` in the folder that contains this SKILL.md (your agent may call it the skill's base directory). It needs only `python3`.

Run it with a shell that has network access and may run `ssh` and write to other agents' session folders (`~/.claude`, `~/.codex`, ...). If your agent runs commands in a sandbox that blocks these, ask the user to approve running the script outside it; don't work around the sandbox.

```bash
S="<skill_dir>/scripts/agent_handoff.py"
python3 "$S" list                         # agents here, their status and session ids
python3 "$S" doctor <ssh-target>          # prerequisites here and on the target
python3 "$S" send <agent> <ssh-target> --dry-run
python3 "$S" send <agent> <ssh-target>
python3 "$S" list --from <host>           # agents on another machine
python3 "$S" send <agent> --from <host>   # bring an agent on <host> back to this machine
python3 "$S" send <agent> --here --new-worktree   # same machine: into a new worktree of the repo
python3 "$S" send <agent> --here --dir <checkout> # same machine: into another checkout
```

- `<agent>`: a herdr agent name, pane id (e.g. `w1Y:p7`), or session id prefix, taken from `list`.
- `<ssh-target>`: whatever `ssh` accepts non-interactively (e.g. `vega@vega`, a tailnet name, or an SSH config alias).

Every command prints JSON. `send` ends with `"ok": true` and an `attach` command, or `"ok": false` and an `error`.

## Procedure

The handoff always runs on the machine where the agent is. To bring a session *back* from another machine (for example "bring the hark session on vega back here"), use `list --from <that machine>`, then `send <agent> --from <that machine>` with **no target**. The script fills in this machine's ssh name (`$AGENT_HANDOFF_SELF`, else `user@hostname`). Only pass a target with `--from` if the user names a third machine. Never pass the `--from` host as the target.

To move a session **on this machine**, use `--here` (no ssh). Use `--here --new-worktree` when the user wants the session in its own worktree (it gets a `handoff/<id>` branch), and `--here --dir <path>` for a checkout they name. Plain `--here` uses another checkout of the repo on this machine if there is one, else a new worktree; the dry run shows which (`target_repo`).

If this is the first handoff to a machine, run `doctor <ssh-target>` first. Report anything missing (herdr not running, the agent CLI not installed, ssh failing), with the fix.

1. Run `list`. Pick the agent the user means: match on name, repo (`cwd`) or what it is working on. If more than one fits, ask. Never pick a row with `"this_pane": true`; that is you.
2. If the user didn't name the target machine, ask for it. Don't guess. Users often say a nickname ("the mini", "my laptop"). If it doesn't match a name the user has given before, ask which ssh host it means.
3. Run `send … --dry-run` and check the plan:
   - `target_repo` is the checkout you expected. If `other_target_checkouts` lists others and it's not obvious, ask, then pass `--dir <path>`.
   - `source_argv` shows how the agent was started. If it had flags the user will want kept (model, permission mode), pass them after `--`, e.g. `send hark vega@vega -- --model opus`.
4. If the agent is `working`, tell the user, and only pass `--wait` if they want to wait for it to finish its turn.
5. Run `send`. Report the target worktree, branch, whether uncommitted changes moved, and the `attach` line (`herdr --remote <target>`).
6. If `target_waiting_for_user` is not empty, the agent on the target is showing a screen the user must answer, and the script never answers these. Tell the user what it is, and don't send the agent prompts until they've dealt with it (a prompt's Enter would land on that screen):
   - `hook_review`: Codex wants approval for a new or changed hook, usually the herdr hook the handoff just installed. Approving it lets herdr track Codex's state on that machine. Escape skips it, and hooks then don't run.
   - `first_run_setup` / `login`: the agent has never been set up, or isn't logged in, on the target. The session is copied over and will resume once the user attaches, finishes setup and logs in.
   - `unrecognized_prompt`: the agent stopped on a screen this tool doesn't know (often after an agent update reworded one). The user should attach and look.
7. If `send` fails, quote its `error`. It ends with what to do (`--new-branch`, `--dir`, install something, wait for the agent).

## Never fix access or setup yourself

If a step fails because of ssh (permission denied, host key verification, unknown host), a missing tool, herdr not running, or a logged-out agent, **stop and tell the user**. Give them the error and the fix, and let them do it. Do not:

- add, copy or generate SSH keys, or edit any SSH file (the authorized-keys and known-hosts files, or the client config), on any machine;
- try other usernames or hostnames until something connects. Use the names the user gave, or ask;
- install software, or log agents in, on either machine;
- change git config, discard changes, or delete stashes or worktrees.

Running `doctor` to diagnose is fine. Retrying with a corrected name the user gave you is fine.

## Options

| Flag | Use |
|---|---|
| `--dir PATH` | Use this checkout on the target (skips discovery). |
| `--new-branch` | Put the work on `handoff/<id>` on the target. Use when the target's copy of the branch is dirty or has diverged; the error message says when. |
| `--keep-source-changes` | Don't stash the source checkout afterwards. |
| `--name NAME` | Agent name on the target (defaults to the source name, or `<repo>-<kind>`). |
| `--session ID_OR_PATH` | Session to move, if herdr didn't record one (integration installed after the agent started). |
| `--no-focus` | Don't focus the new workspace on the target. |
| `--wait` | Wait for a `working` agent to go idle first. |
| `--no-trust` | Don't answer the agent's "trust this folder?" screen on the target; leave it for the user. |
| `--here` | Hand off on this machine, no ssh. Add `--new-worktree` or `--dir`. |
| `--new-worktree` | With `--here`: move the session into a new worktree of the same repo. |
| `--from HOST` | Run on HOST, where the agent is, over ssh. Works with `list`, `doctor` and `send`. |

## Repo discovery on the target

Checkouts are matched by any git remote (`git@github.com:o/r.git` and `https://github.com/o/r` are the same). The search covers `~/github.com`, `~/src`, `~/code`, `~/dev`, `~/Developer`, `~/projects`, `~/repos`, `~/work`, `~/git`, `~/workspace` and the top of `~`. Results are cached in `~/.config/agent-handoff/repos.json`. To change the search roots, set `AGENT_HANDOFF_ROOTS=dir1:dir2` or write `{"roots": [...]}` to `~/.config/agent-handoff/config.json`.

## Limits

- Supported agents: `claude`, `codex`, `omp`, `pi`, `grok`. Others (including `hermes`) fail with a clear error. Adding one means adding an adapter class to the script.
- The repo must have at least one git remote, so it can be matched on the target.
- The agent must be at rest (idle or done), not mid-turn or waiting on an approval.
- herdr's Codex integration doesn't record a session id. The script picks the newest Codex transcript for the agent's folder written since the agent started. If two Codex agents share a folder it stops and asks for `--session`.
- Uncommitted changes leave the source checkout (they're stashed). Don't hand off an agent in a repo where you are editing files by hand at the same time.
- Needs herdr 0.8.0 or newer on both machines, and ssh between them that works without prompts (`ssh -o BatchMode=yes <host> true`).
- The source pane is left at a shell prompt. Worktrees the handoff creates are left in place; remove them with `git worktree remove` when you're done.
