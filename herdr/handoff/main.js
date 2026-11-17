// Move the agent you right-clicked into a space on another machine.
// The agents list opens this popup; herdr/handoff/wire.py adds that right-click.
// https://github.com/shanev/agent-handoff
import { View, div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { agents, checking, closeOverlay, exec, focused, hc, machines, openOverlay, props, spaces, theme } from "herdr";

// Finds the skill's script on the machine where Herdr is running. AGENT_HANDOFF overrides it.
const FIND = `
script=
for p in "$AGENT_HANDOFF" \\
  "$HOME/.agents/skills/agent-handoff/scripts/agent_handoff.py" \\
  "$HOME/.agents/skills/agent-handoff/skills/agent-handoff/scripts/agent_handoff.py" \\
  "$HOME/.hermes/skills/agent-handoff/scripts/agent_handoff.py" \\
  "$HOME/.codex/skills/agent-handoff/scripts/agent_handoff.py" \\
  "$HOME/github.com/shanev/agent-handoff/skills/agent-handoff/scripts/agent_handoff.py"
do
  [ -n "$p" ] && [ -f "$p" ] && script=$p && break
done
[ -n "$script" ] || { echo "agent-handoff is not installed on this machine" >&2; exit 127; }
exec python3 "$script" "$@"
`;

function subject() {
  const f = focused();
  const list = agents();
  if (f?.terminal) {
    const hit = list.find((a) => a.address === f.terminal);
    if (hit) return hit;
  }
  if (f?.agent) {
    const hit = list.find((a) => a.address === f.agent);
    if (hit) return hit;
  }
  return list.find((a) => a.focused) || null;
}

function labelOf(a) {
  return a.program?.name || a.app || "agent";
}

// Spaces the agent can move to: other machines, or this computer when it is already remote.
function destinations(address, currentSpace) {
  const source = address.includes("/") ? address.split("/")[0] : null;
  const groups = [];
  const add = (key, title, space) => {
    let g = groups.find((x) => x.key === key);
    if (!g) groups.push(g = { key, title, spaces: [] });
    g.spaces.push(space);
  };
  for (const s of spaces()) {
    if (!s.folder || s.id === currentSpace) continue;
    if (source == null) {
      if (s.machine) add(s.machine, s.machine, s);
    } else if (s.machine == null) {
      add("", "This computer", s);
    } else if (s.machine !== source) {
      add(s.machine, s.machine, s);
    }
  }
  return groups;
}

function sendArgs(address, space, rows) {
  const dir = space.folder;
  const slash = address.indexOf("/");
  if (slash === -1) {
    const dest = rows.find((m) => m.name === space.machine);
    if (!dest?.target) throw new Error(`No SSH target for ${space.machine}. Add it with hc machine add.`);
    return ["send", address, dest.target, "--dir", dir];
  }
  const source = rows.find((m) => m.name === address.slice(0, slash));
  if (!source?.target) throw new Error(`No SSH target for ${address.slice(0, slash)}.`);
  const id = address.slice(slash + 1);
  if (!space.machine) return ["send", id, "--from", source.target, "--dir", dir];
  const dest = rows.find((m) => m.name === space.machine);
  if (!dest?.target) throw new Error(`No SSH target for ${space.machine}.`);
  return ["send", id, dest.target, "--from", source.target, "--dir", dir];
}

export default class Handoff extends View {
  init(_props, cx) {
    this.detail = Boolean(props()?.detail);
    this.rows = null;
    this.picked = null;
    this.busy = false;
    this.error = null;
    this.done = false;
    if (checking() || !this.detail) return;
    this.load(cx);
  }

  load(cx) {
    cx.spawn(async (cx) => {
      try {
        const r = await hc(["machine", "ls"]);
        if (r.code !== 0) throw new Error((r.stderr || r.stdout || `exit ${r.code}`).trim());
        this.rows = JSON.parse(r.stdout).machines || [];
      } catch (e) {
        this.rows = [];
        this.error = String(e?.message ?? e);
      }
      cx.notify();
    });
  }

  move(cx) {
    if (this.busy || !this.picked || this.done) return;
    let args;
    try {
      args = sendArgs(props().address, this.picked, this.rows || []);
    } catch (e) {
      this.error = String(e?.message ?? e);
      cx.notify();
      return;
    }
    this.busy = true;
    this.error = null;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await exec("/bin/sh", ["-c", FIND, "agent-handoff", ...args], { timeout: 600000 });
        const text = `${r.stdout || ""}${r.stderr || ""}`.trim();
        if (r.code !== 0) this.error = text.split("\n").filter(Boolean).pop() || `exit ${r.code}`;
        else this.done = true;
      } catch (e) {
        this.error = String(e?.message ?? e);
      }
      this.busy = false;
      cx.notify();
    });
  }

  render(cx) {
    return this.detail ? this.renderDetail(cx) : this.renderStrip();
  }

  renderStrip() {
    const t = theme();
    const a = subject();
    const row = h_flex().id("handoff").size_full().px(12).gap(8).items_center().justify_end()
      .bg(t.chrome).text_size(12).text_color(t.muted);
    if (!a) return row.child(div().child("Hand off"));
    const label = labelOf(a);
    const where = a.space_name || a.title || a.address;
    return row.child(
      div().id("handoff-open").px(8).py(3).rounded(6).text_color(t.foreground)
        .hover((s) => s.bg(t.hover))
        .on_click(() => openOverlay({
          mod: "handoff",
          props: { detail: true, address: a.address, label, where, space: a.space },
          kind: "popup",
          width: 420,
          height: 380,
          at: "pointer",
        }))
        .child(`Hand off ${label} · ${where}`));
  }

  renderDetail(cx) {
    const t = theme();
    const p = props();
    const groups = this.rows == null ? [] : destinations(p.address || "", p.space);
    const waiting = machines().filter((m) => m.name && m.layout === "loading");
    const box = v_flex().id("handoff-detail").size_full().p(12).gap(8).bg(t.popover)
      .text_size(12.5).text_color(t.foreground).overflow_y_scrollbar();
    box.child(div().font_semibold().child(`Move ${p.label || "agent"}`));
    box.child(div().text_size(11.5).text_color(t.muted)
      .child("Pick a space on another machine. This quits the agent and resumes it in that folder."));
    if (this.rows == null) box.child(div().text_color(t.muted).child("Looking up machines…"));
    else if (!groups.length) box.child(div().text_color(t.muted).child(
      waiting.length ? "Waiting for the other machine…" : "No spaces on another machine yet."));
    let n = 0;
    for (const g of groups) {
      box.child(div().pt(4).text_size(11).font_medium().text_color(t.dim).child(g.title));
      for (const s of g.spaces) {
        const on = this.picked && this.picked.id === s.id;
        const i = n++;
        box.child(
          h_flex().id(`dest-${i}`).gap(8).items_center().px(8).py(5).rounded(6)
            .bg(on ? t.selected : t.surface)
            .hover((el) => el.bg(on ? t.selected : t.hover))
            .on_click((_e, cx) => { this.picked = s; cx.notify(); })
            .child(div().w(140).truncate().child(s.name))
            .child(div().flex_1().min_w(0).truncate().text_size(11).text_color(t.dim).child(s.folder)));
      }
    }
    if (this.error) box.child(div().text_size(11.5).text_color(t.ansi1 ?? t.blocked).child(this.error));
    if (this.done) box.child(div().text_size(11.5).text_color(t.muted).child("Moved."));
    const ready = this.picked && !this.busy && !this.done;
    const where = this.picked ? `${this.picked.machine || "this computer"} / ${this.picked.name}` : "";
    return box.child(h_flex().gap(12).items_center().pt(4)
      .child(div().flex_1())
      .child(div().id("cancel").text_color(t.muted).hover((el) => el.text_color(t.foreground))
        .on_click(() => closeOverlay(p.overlay)).child("Close"))
      .child(div().id("move").px(10).py(4).rounded(6)
        .bg(ready ? t.solid : t.border)
        .text_color(ready ? t.solid_foreground : t.dim)
        .on_click((_e, cx) => { if (ready) this.move(cx); })
        .child(this.busy ? "Moving…" : this.done ? "Done" : this.picked ? `Move to ${where}` : "Pick a space")));
  }
}
