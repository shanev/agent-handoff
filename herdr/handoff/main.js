// handoff: move the focused agent to another machine.
// Click the button, pick a machine, then confirm. https://github.com/shanev/agent-handoff
import { View, div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { agents, checking, closeOverlay, exec, focused, hc, openOverlay, props, theme } from "herdr";

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

export default class Handoff extends View {
  init(_props, cx) {
    this.detail = Boolean(props()?.detail);
    this.machines = null;
    this.picked = null;
    this.busy = false;
    this.error = null;
    this.output = "";
    this.fromSsh = null;
    if (checking() || !this.detail) return;
    this.load(cx);
  }

  load(cx) {
    cx.spawn(async (cx) => {
      try {
        const r = await hc(["machine", "ls"]);
        if (r.code !== 0) throw new Error((r.stderr || r.stdout || `exit ${r.code}`).trim());
        const rows = JSON.parse(r.stdout).machines || [];
        const address = props().address || "";
        const slash = address.indexOf("/");
        const source = slash === -1 ? null : address.slice(0, slash);
        const known = rows.filter((m) => m && m.target);
        this.fromSsh = source ? (known.find((m) => m.name === source)?.target || null) : null;
        const dests = [];
        if (source) dests.push({ name: "This computer", here: true, ssh: null });
        for (const m of known) {
          if (m.name === source) continue;
          dests.push({ name: m.name, here: false, ssh: m.target });
        }
        this.machines = dests;
        this.error = source && !this.fromSsh ? `No SSH target for ${source}. Add it with hc machine add.` : null;
      } catch (e) {
        this.machines = [];
        this.error = String(e?.message ?? e);
      }
      cx.notify();
    });
  }

  move(cx) {
    if (this.busy || !this.picked) return;
    const p = props();
    const dest = this.picked;
    const slash = (p.address || "").indexOf("/");
    let args;
    if (slash === -1) {
      if (!dest.ssh) return;
      args = ["send", p.address, dest.ssh];
    } else if (!this.fromSsh) {
      return;
    } else if (dest.here) {
      args = ["send", p.address.slice(slash + 1), "--from", this.fromSsh];
    } else {
      args = ["send", p.address.slice(slash + 1), dest.ssh, "--from", this.fromSsh];
    }
    this.busy = true;
    this.error = null;
    this.output = "";
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await exec("/bin/sh", ["-c", FIND, "agent-handoff", ...args], { timeout: 600000 });
        const text = `${r.stdout || ""}${r.stderr || ""}`.trim();
        this.output = text;
        if (r.code !== 0) this.error = text.split("\n").filter(Boolean).pop() || `exit ${r.code}`;
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
      .bg(t.chrome).border_t(1).border_color(t.border).text_size(12).text_color(t.muted);
    if (!a) return row.child(div().child("Hand off"));
    const label = labelOf(a);
    const where = a.space_name || a.tab_name || a.address;
    return row.child(
      div().id("handoff-open").px(8).py(3).rounded(6).text_color(t.foreground)
        .hover((s) => s.bg(t.hover))
        .on_click(() => openOverlay({
          mod: "handoff",
          props: { detail: true, address: a.address, label, where },
          kind: "popup",
          width: 380,
          height: 320,
          at: "pointer",
        }))
        .child(`Hand off ${label} · ${where}`));
  }

  renderDetail(cx) {
    const t = theme();
    const p = props();
    const box = v_flex().id("handoff-detail").size_full().p(14).gap(10).bg(t.popover)
      .text_size(12.5).text_color(t.foreground).overflow_y_scrollbar();
    box.child(div().font_semibold().child(`Hand off ${p.label || "agent"}`));
    box.child(div().text_size(11.5).text_color(t.muted)
      .child(`${p.where || p.address}. This quits it and resumes the same session on the machine you pick.`));
    if (this.machines == null) {
      box.child(div().text_color(t.muted).child("Looking up machines…"));
    } else if (!this.machines.length) {
      box.child(div().text_color(t.muted).child("No other machines. Add one with hc machine add."));
    } else {
      for (const m of this.machines) {
        const on = this.picked && this.picked.name === m.name;
        box.child(
          h_flex().id(`dest-${m.name}`).gap(8).items_center().px(8).py(6).rounded(6)
            .bg(on ? t.selected : t.surface)
            .hover((s) => s.bg(on ? t.selected : t.hover))
            .on_click((_e, cx) => { this.picked = m; cx.notify(); })
            .child(div().flex_1().child(m.name))
            .child(div().text_size(11).text_color(t.dim).child(m.here ? "bring it here" : m.ssh)));
      }
    }
    if (this.error) box.child(div().text_size(11.5).text_color(t.ansi1 ?? t.blocked).child(this.error));
    if (this.output && !this.error) box.child(div().text_size(11.5).text_color(t.muted).child("Moved."));
    const ready = this.picked && !this.busy && !this.output;
    return box.child(h_flex().gap(12).items_center()
      .child(div().flex_1())
      .child(div().id("cancel").text_color(t.muted).hover((s) => s.text_color(t.foreground))
        .on_click(() => closeOverlay(p.overlay)).child("Close"))
      .child(div().id("move").px(10).py(4).rounded(6)
        .bg(ready ? t.solid : t.border)
        .text_color(ready ? t.solid_foreground : t.dim)
        .on_click((_e, cx) => { if (ready) this.move(cx); })
        .child(this.busy ? "Moving…" : this.output ? "Done" : this.picked ? `Move to ${this.picked.name}` : "Pick a machine")));
  }
}
