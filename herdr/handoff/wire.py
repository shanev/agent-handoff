#!/usr/bin/env python3
"""Point Herdr's agent list at a copy that right-clicks into the handoff menu.

Herdr's own agents mod has no menu hook. This duplicates it as agents-handoff,
adds one right-click, and shows that copy from the layout that is open now.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
APP = HOME / ".config" / "herdr-app"
MODS = APP / "mods"
LAYOUTS = APP / "layouts"
HERE = Path(__file__).resolve().parent
MARKER = "agent-handoff:right-click"
CLICK = '.on_click(() => run("pane.focus", { pane: a.pane }))'
RIGHT = '''.on_mouse_down("right", (_e, cx) => {
            cx.stop_propagation();
            openOverlay({ mod: "handoff", kind: "popup", width: 420, height: 380, at: "pointer",
              props: { detail: true, address: a.address, label: a.program?.name || a.app || "agent",
                where: a.space_name || a.title || a.address, space: a.space } }); // %s
          })
          .on_click(() => run("pane.focus", { pane: a.pane }))''' % MARKER


def app(*args):
    bin = shutil.which("herdr-app") or str(HOME / ".local" / "bin" / "herdr-app")
    out = subprocess.run([bin, *args], check=False, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(out.stderr.strip() or out.stdout.strip() or f"herdr-app {' '.join(args)} failed")
    return out.stdout


def patch(path: Path) -> None:
    text = path.read_text()
    if MARKER in text:
        return
    if CLICK not in text:
        sys.exit(f"{path} has no agent row to attach the right-click to")
    path.write_text(text.replace(CLICK, RIGHT))


def shown_layout() -> str:
    raw = app("status")
    try:
        return json.loads(raw).get("layout") or "main"
    except json.JSONDecodeError:
        return "main"


def retarget(layout: str) -> None:
    path = LAYOUTS / f"{layout}.json"
    text = path.read_text()
    old = '"mod": "agents"'
    new = '"mod": "agents-handoff"'
    if new in text:
        return
    if old not in text:
        sys.exit(f"{path} has no agents block to attach the right-click to")
    path.write_text(text.replace(old, new, 1))


def main() -> None:
    dest = MODS / "handoff"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in ("main.js", "mod.json"):
        shutil.copy(HERE / name, dest / name)
    clone = MODS / "agents-handoff"
    if not (clone / "main.js").exists():
        app("do", "mod.clone", "--name", "agents", "--as", "agents-handoff")
    patch(clone / "main.js")
    app("check", str(dest))
    app("check", str(clone))
    retarget(shown_layout())
    print(f"right-click an agent in {shown_layout()} to move it")


if __name__ == "__main__":
    main()
