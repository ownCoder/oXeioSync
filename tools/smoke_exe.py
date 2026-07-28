"""Acceptance checks for the built executable.

    python tools/smoke_exe.py [--exe PATH] [--no-engine] [--cold] [--keep]

Runs the frozen build in an isolated data folder and checks the things that only
a bundle can get wrong: whether QtWebEngine initialises at all, whether the child
engine starts, whether logging survives having no console, and whether a second
launch defers to the first.

The data folder starts empty, which is the check that matters most for a release
build: a bundle that ships its own engine must reach a running, answering engine
from nothing, with no dialog and nothing fetched. ``--no-engine`` is for builds
made without one — the engine is then seeded from this machine's own copy so the
rest of the checks can still run.

``--cold`` exercises the first-run download instead, on a build that has no
bundled engine. It is **interactive**: the application asks for consent before
downloading, so a dialog appears and the run blocks until someone answers it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXE = ROOT / "dist" / "oXeioSync" / "oXeioSync.exe"
GUI_PORT = 19400
API_KEY = "smoke-test-key-0123456789ab"
ENGINE_NAME = "syncthing.exe" if os.name == "nt" else "syncthing"

FAILURES: list[str] = []
PASSES = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global PASSES
    if ok:
        PASSES += 1
    else:
        FAILURES.append(f"{label}: {detail}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
    return ok


def wait_for(predicate, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def descendants(root_pid: int) -> set[int]:
    """Every live process descended from root_pid, by name-agnostic walk."""
    out = subprocess.run(
        ["wmic", "process", "get", "ProcessId,ParentProcessId", "/format:csv"],
        capture_output=True, text=True,
    ).stdout
    pairs = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            pairs.append((int(parts[2]), int(parts[1])))
    found, frontier = set(), {root_pid}
    while frontier:
        nxt = set()
        for pid, ppid in pairs:
            if ppid in frontier and pid not in found:
                found.add(pid)
                nxt.add(pid)
        frontier = nxt
    return found


def alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
    ).stdout
    return str(pid) in out


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def bundled_engine(app_dir: Path) -> Path | None:
    """The engine shipped inside the bundle, wherever the packager put it."""
    for candidate in (app_dir / ENGINE_NAME, app_dir / "_internal" / ENGINE_NAME):
        if candidate.is_file():
            return candidate
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument(
        "--no-engine",
        action="store_true",
        help="the build has no bundled engine; seed one instead of expecting it to ship",
    )
    parser.add_argument(
        "--cold",
        action="store_true",
        help="exercise the engine download (INTERACTIVE: prompts for consent on screen)",
    )
    parser.add_argument("--keep", action="store_true", help="leave the sandbox behind")
    args = parser.parse_args(argv[1:])

    exe: Path = args.exe.resolve()
    if not exe.is_file():
        print(f"no executable at {exe}; build it first", file=sys.stderr)
        return 1

    sandbox = ROOT / "build" / "smoke-sandbox"
    shutil.rmtree(sandbox, ignore_errors=True)
    data = sandbox / "oXeioSync"
    data.mkdir(parents=True, exist_ok=True)

    (data / "oxeiosync.json").write_text(
        json.dumps(
            {"api_key": API_KEY, "gui_address": f"127.0.0.1:{GUI_PORT}"}, indent=2
        ),
        encoding="utf-8",
    )

    # The standalone claim, checked before anything is launched: an installed
    # copy must carry its own engine. Everything below then starts from an empty
    # data folder, so a pass means it really did run on what it shipped with.
    shipped = bundled_engine(exe.parent)
    if not args.cold:
        check(
            "bundle ships its own sync engine",
            args.no_engine or shipped is not None,
            f"no {ENGINE_NAME} beside {exe.name} or in its _internal folder",
        )
        if shipped is not None:
            print(f"  (bundled engine: {shipped.relative_to(exe.parent)})")

    if not args.cold and shipped is None:
        source = Path(os.environ["LOCALAPPDATA"]) / "oXeioSync" / "bin" / ENGINE_NAME
        if source.is_file():
            (data / "bin").mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, data / "bin" / ENGINE_NAME)
            print(f"seeded engine from {source}")
        else:
            print("no engine to seed; the run will need the network")

    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(sandbox)

    log_file = data / "logs" / "oxeiosync.log"
    print(f"\n=== launching {exe.name}")
    started = time.monotonic()
    proc = subprocess.Popen([str(exe)], env=env, cwd=str(exe.parent))

    try:
        # 1. It stays up.
        check("process survives launch", wait_for(lambda: proc.poll() is None, 5)
              and proc.poll() is None, f"exited {proc.poll()}")

        # 2. Logging works with no console attached.
        got_log = wait_for(lambda: log_file.is_file() and log_file.stat().st_size > 0, 60)
        check("writes its log without a console", got_log, f"nothing at {log_file}")
        if got_log:
            text = log_file.read_text(encoding="utf-8", errors="replace")
            check("logs its own start-up line", "Starting oXeioSync" in text, text[:200])

        # 3. The child engine comes up and answers.
        import urllib.error
        import urllib.request

        def api_ok() -> bool:
            request = urllib.request.Request(
                f"http://127.0.0.1:{GUI_PORT}/rest/system/ping",
                headers={"X-API-Key": API_KEY},
            )
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    return response.status == 200
            except (urllib.error.URLError, OSError):
                return False

        engine_up = wait_for(api_ok, 180 if args.cold else 90)
        check("engine child answers its API", engine_up, "no response")
        print(f"  (ready {time.monotonic() - started:.0f}s after launch)")

        # 4. The bundle really did carry QtWebEngine.
        kids = descendants(proc.pid)
        names = subprocess.run(
            ["wmic", "process", "where", f"ParentProcessId={proc.pid}", "get", "Name"],
            capture_output=True, text=True,
        ).stdout
        check("QtWebEngine helper process started", "QtWebEngineProcess" in names,
              f"children: {names.strip()[:200]}")
        check("engine process started", "syncthing" in names.lower(),
              f"children: {names.strip()[:200]}")

        # 5. A second launch must defer to the first.
        print("\n=== second launch")
        second = subprocess.Popen([str(exe)], env=env, cwd=str(exe.parent))
        exited = wait_for(lambda: second.poll() is not None, 45)
        check("second instance exits by itself", exited, "still running after 45s")
        if exited:
            check("second instance exits cleanly", second.returncode == 0,
                  f"code {second.returncode}")
        else:
            kill_tree(second.pid)
        check("first instance survived it", proc.poll() is None, f"exited {proc.poll()}")

        if got_log:
            text = log_file.read_text(encoding="utf-8", errors="replace")
            check("log records the hand-off", "already running" in text.lower(),
                  text[-300:])
            check("no tracebacks in the log", "Traceback" not in text,
                  text[text.find("Traceback"):][:300] if "Traceback" in text else "")

        # 6. Shutting down takes the whole tree with it.
        print("\n=== shutdown")
        kill_tree(proc.pid)
        time.sleep(4)
        survivors = {pid for pid in kids if alive(pid)}
        check("no orphaned child processes", not survivors, f"survivors={sorted(survivors)}")

    finally:
        kill_tree(proc.pid)
        for name in ("syncthing.exe",):
            subprocess.run(
                ["taskkill", "/F", "/FI", f"IMAGENAME eq {name}",
                 "/FI", f"WINDOWTITLE eq {sandbox.name}*"],
                capture_output=True,
            )
        if not args.keep:
            time.sleep(2)
            shutil.rmtree(sandbox, ignore_errors=True)

    print(f"\n{PASSES} passed, {len(FAILURES)} failed")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
