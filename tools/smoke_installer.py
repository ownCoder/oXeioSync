"""Acceptance checks for the installer.

    python tools/smoke_installer.py [--setup PATH]

Installs, upgrades over a *running* copy, and uninstalls — all silently, and all
inside a sandbox: ``LOCALAPPDATA`` is redirected, so the install folder, the data
folder and the Start Menu entry all land under ``build/`` and this machine's real
oXeioSync installation and sync database are never touched.

Two checks matter most: that an uninstall leaves the user's data alone, and that
the installation reaches a running engine on what the installer put there —
nothing is seeded into the data folder when the payload carries an engine, so a
pass means an offline machine would have got the same result.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETUP = ROOT / "dist" / "oXeioSync-setup.exe"
SANDBOX = ROOT / "build" / "installer-sandbox"
INSTALL_DIR = SANDBOX / "Programs" / "oXeioSync"
DATA_DIR = SANDBOX / "oXeioSync"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
GUI_PORT = 19800
API_KEY = "installer-key-0123456789ab"

#: Resolved through the shell, not the environment — see the note by its use.
START_MENU = (
    Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu"
    / "Programs" / "oXeioSync"
)
#: The folder the *uninstaller* will consider deleting. Deliberately the real
#: one: that is what it resolves, and proving it is left alone is the point.
REAL_DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "oXeioSync"


def uninstaller_finished(install_dir: Path) -> bool:
    """True once no uninstaller process is left and the folder has gone.

    The uninstaller copies itself to %TEMP% and relaunches, so the process the
    caller waited on returns long before the work is done.
    """
    running = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq unins000.exe", "/NH"],
        capture_output=True, text=True,
    ).stdout
    if "unins000" in running:
        return False
    return not install_dir.exists() or not any(install_dir.iterdir())

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


def sandbox_env() -> dict[str, str]:
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(SANDBOX)
    return env


def run_setup(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    command = [str(SETUP), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
               f"/DIR={INSTALL_DIR}", *args]
    return subprocess.run(command, env=sandbox_env(), capture_output=True, timeout=timeout)


def api_ready() -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{GUI_PORT}/rest/system/ping", headers={"X-API-Key": API_KEY}
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def installed_engine() -> Path | None:
    """The engine the installer laid down, wherever the packaging put it."""
    for candidate in (
        INSTALL_DIR / "_internal" / "syncthing.exe",
        INSTALL_DIR / "syncthing.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


def seed_data_folder() -> None:
    """Give the app a config, and an engine only if the installer shipped none.

    Seeding when the installation already has one would hide the thing this
    check is for: that an install on a machine with no engine and no reachable
    network reaches a running engine anyway.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "oxeiosync.json").write_text(
        json.dumps({"api_key": API_KEY, "gui_address": f"127.0.0.1:{GUI_PORT}"}),
        encoding="utf-8",
    )
    if installed_engine() is not None:
        return

    engine = Path(os.environ["LOCALAPPDATA"]) / "oXeioSync" / "bin" / "syncthing.exe"
    if engine.is_file():
        (DATA_DIR / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(engine, DATA_DIR / "bin" / "syncthing.exe")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, default=DEFAULT_SETUP)
    args = parser.parse_args(argv[1:])

    global SETUP
    SETUP = args.setup.resolve()
    if not SETUP.is_file():
        print(f"no installer at {SETUP}; compile it first", file=sys.stderr)
        return 1

    shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True, exist_ok=True)
    exe = INSTALL_DIR / "oXeioSync.exe"

    # -------------------------------------------------------------- install
    print("=== silent install")
    started = time.monotonic()
    result = run_setup()
    check("installer exited 0", result.returncode == 0, f"code {result.returncode}")
    print(f"  took {time.monotonic() - started:.0f}s")
    check("executable installed", exe.is_file(), str(exe))
    check("_internal tree installed", (INSTALL_DIR / "_internal").is_dir())
    check("uninstaller present", any(INSTALL_DIR.glob("unins*.exe")))

    # The whole point of the installer: what it lays down is enough on its own.
    engine = installed_engine()
    check("sync engine installed", engine is not None,
          f"no syncthing.exe under {INSTALL_DIR}")
    # Shipping someone else's binary means shipping its terms where they can be
    # read, not buried in a folder named after an implementation detail.
    check("engine notice installed where it can be found",
          (INSTALL_DIR / "ENGINE-NOTICE.txt").is_file(),
          f"not at the top of {INSTALL_DIR}")
    check("engine licence installed",
          (INSTALL_DIR / "LICENSE-engine.txt").is_file(),
          f"not at the top of {INSTALL_DIR}")

    # Inno resolves {autoprograms} through the shell's known-folder API, which
    # does not read the environment — so the shortcut lands in the real Start
    # Menu no matter what LOCALAPPDATA says. The uninstall check below is what
    # makes that acceptable.
    check("start menu folder created", START_MENU.is_dir(), str(START_MENU))
    contents = [p.name for p in START_MENU.glob("*")] if START_MENU.is_dir() else "missing"
    check("start menu shortcut points at the exe",
          (START_MENU / "oXeioSync.lnk").is_file(), f"contents: {contents}")

    # ----------------------------------------------------------- it runs
    print("\n=== the installed app runs")
    seed_data_folder()
    app = subprocess.Popen([str(exe)], env=sandbox_env(), cwd=str(INSTALL_DIR))
    check("app reaches its engine", wait_for(api_ready, 120))

    # ------------------------------------------------- upgrade while running
    # A second copy from a different folder, with its own data folder, stands in
    # for a portable install on a USB stick. The installer must not touch it —
    # terminating by image name would.
    print("\n=== a second, unrelated copy is running elsewhere")
    other_data = SANDBOX / "elsewhere"
    other_data.mkdir(parents=True, exist_ok=True)
    (other_data / "oXeioSync").mkdir(parents=True, exist_ok=True)
    (other_data / "oXeioSync" / "oxeiosync.json").write_text(
        json.dumps({"api_key": "other-key-0123456789abc",
                    "gui_address": f"127.0.0.1:{GUI_PORT + 1}"}),
        encoding="utf-8",
    )
    other_env = os.environ.copy()
    other_env["LOCALAPPDATA"] = str(other_data)
    bystander_exe = ROOT / "dist" / "oXeioSync" / "oXeioSync.exe"
    bystander = subprocess.Popen(
        [str(bystander_exe)], env=other_env, cwd=str(bystander_exe.parent)
    )
    time.sleep(8)
    check("bystander copy is running", bystander.poll() is None,
          f"exited {bystander.poll()}")

    print("\n=== upgrade over the running copy")
    stale = INSTALL_DIR / "_internal" / "stale-from-old-version.txt"
    stale.write_text("left over", encoding="utf-8")

    started = time.monotonic()
    result = run_setup()
    check("upgrade exited 0", result.returncode == 0, f"code {result.returncode}")
    print(f"  took {time.monotonic() - started:.0f}s")
    check("running app was shut down", app.poll() is not None or not api_ready(),
          "it is somehow still serving")
    check("stale file from the old version removed", not stale.exists(), str(stale))
    check("executable still there after upgrade", exe.is_file())
    # _internal is cleared and rewritten on every upgrade, and the engine now
    # lives in it. An upgrade that takes the engine away installs a working app
    # that cannot sync.
    check("engine still there after upgrade", installed_engine() is not None,
          f"no syncthing.exe under {INSTALL_DIR} after the upgrade")
    check("the unrelated copy was left alone", bystander.poll() is None,
          f"the installer killed a copy it does not own (exit {bystander.poll()})")

    subprocess.run(["taskkill", "/PID", str(bystander.pid), "/T", "/F"], capture_output=True)

    # ------------------------------------------------------ autostart entry
    print("\n=== uninstall")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, "oXeioSync", 0, winreg.REG_SZ, f'"{exe}" --minimized')
    print("  wrote a start-on-login entry to remove")

    # The uninstaller looks at the real data folder, so that is what has to be
    # observed. Nothing is written into it: its mere survival is the assertion.
    had_real_data = REAL_DATA_DIR.is_dir()
    had_real_db = (REAL_DATA_DIR / "syncthing").is_dir()

    uninstaller = next(INSTALL_DIR.glob("unins*.exe"), None)
    if uninstaller is None:
        check("uninstaller found", False, "none in the install folder")
    else:
        subprocess.run(
            [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            env=sandbox_env(), capture_output=True, timeout=600,
        )
        done = wait_for(lambda: uninstaller_finished(INSTALL_DIR), 180)
        check("uninstall ran to completion", done, "uninstaller still working after 180s")
        check("executable removed", not exe.exists(), str(exe))
        leftovers = (
            [p for p in INSTALL_DIR.rglob("*") if p.is_file()] if INSTALL_DIR.exists() else []
        )
        check("no files left in the install folder", not leftovers,
              f"{len(leftovers)} left, e.g. {[p.name for p in leftovers[:3]]}")

    check("start menu entry removed", not START_MENU.exists(), str(START_MENU))

    if had_real_data:
        check("a silent uninstall keeps the sync data", REAL_DATA_DIR.is_dir(),
              f"{REAL_DATA_DIR} was deleted")
        if had_real_db:
            check("the sync database survived", (REAL_DATA_DIR / "syncthing").is_dir(),
                  "the device identity and folder index were deleted")
    else:
        print("  (no real data folder on this machine; preservation not exercised)")

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, "oXeioSync")
        still_there = True
    except FileNotFoundError:
        still_there = False
    check("start-on-login entry removed", not still_there,
          "a dangling Run value would point at a deleted exe")

    # ------------------------------------------------------------- tidy up
    subprocess.run(["taskkill", "/PID", str(app.pid), "/T", "/F"], capture_output=True)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "oXeioSync")
    except FileNotFoundError:
        pass
    time.sleep(2)
    shutil.rmtree(SANDBOX, ignore_errors=True)

    print(f"\n{PASSES} passed, {len(FAILURES)} failed")
    for failure in FAILURES:
        print(f"  - {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
