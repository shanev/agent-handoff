# agent-handoff

Move a running coding-agent session to another machine and keep going there.

You're in a Claude Code (or Codex, or omp) session on your laptop and want it running on the always-on Mac mini instead, or the other way round. `agent-handoff` quits the agent, moves its transcript, branch and uncommitted changes over SSH, and resumes it in [herdr](https://herdr.dev) on the other machine. The repo can be checked out at a different path there: checkouts are matched by git remote.

It is packaged as an [agent skill](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills), so you can just tell your agent "hand the hark session off to vega". The script also works on its own.

## Install

As a Hermes skill:

```bash
hermes skills install shanev/agent-handoff/skills/agent-handoff
```

Install it on every machine you want to hand off *from*. The target only needs `python3`, `git`, `herdr` (with its server running) and the agent's CLI. The script sends itself over SSH for the steps that run there.

## Use

```bash
S=~/.hermes/skills/agent-handoff/scripts/agent_handoff.py   # or wherever it's installed
python3 $S list                          # agents in this herdr session
python3 $S doctor vega@vega              # check both machines
python3 $S send hark-claude vega@vega --dry-run
python3 $S send hark-claude vega@vega
herdr --remote vega@vega                 # attach and carry on
```

To hand it back, run the same thing on the other machine with this one as the target.

## What a handoff does

1. Reads the agent's session id from herdr. The target must run herdr; if it lacks the agent integration, the handoff installs it so the session can come back.
2. Finds the same repo on the target by git remote, at whatever path it lives.
3. Quits the agent with `/exit` so the transcript is final.
4. Pushes `HEAD` and a snapshot of uncommitted and untracked changes **directly to the target over SSH**, as temporary `refs/handoff/*` refs. Nothing goes to GitHub, and no branch is force-updated.
5. On the target, fast-forwards the branch where it's checked out, or creates a worktree at `<repo>.worktrees/<branch>`, then applies the changes.
6. Copies the transcript into the target agent's session store, replacing old repo and home paths with the new ones.
7. Opens a herdr workspace on the target and resumes the agent there.
8. Stashes the changes in the source checkout, so handing back applies cleanly.

If something fails after the agent has quit, it is restarted where it was.

## Supported agents

| Agent | Transcript | Resume |
|---|---|---|
| Claude Code | `~/.claude/projects/<cwd>/<id>.jsonl` (+ subagent and file-history dirs) | `claude --resume <id>` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-…-<id>.jsonl` | `codex resume <id>` |
| omp | `~/.omp/agent/sessions/<cwd>/…jsonl` | `omp --resume <path>` |

Each agent is a small adapter class in [`agent_handoff.py`](skills/agent-handoff/scripts/agent_handoff.py) with three methods: where the transcript is, where it goes on the target, and how to resume it. PRs for more agents are welcome.

## Requirements

- macOS or Linux, `python3` ≥ 3.9 (stdlib only), `git`, `ssh`, `tar`.
- herdr on both machines, with the agent integration installed on the source (`herdr integration install claude`).
- Non-interactive SSH between the machines, e.g. over [Tailscale](https://tailscale.com).

## License

MIT
