#!/usr/bin/env python3
"""Runs the TechnoFunda Desk server and its daily jobs on a Mac, using launchd (the Mac's own scheduler: no extra software).

    .venv/bin/python scripts/mac_services.py install                 # the server (starts at login) + all scheduled jobs
    .venv/bin/python scripts/mac_services.py install --keep-awake    # also stop the Mac from idle-sleeping while the server runs (uses more battery)
    .venv/bin/python scripts/mac_services.py install --no-server     # only the scheduled jobs (you start the app with start-desk.command)
    .venv/bin/python scripts/mac_services.py status
    .venv/bin/python scripts/mac_services.py remove

Jobs (the same as scripts/register_nightly.ps1, register_intraday.ps1 and register_backup.ps1 on Windows; times are this Mac's local time, so set the Mac to India time):
  nightly    Mon-Fri 18:45   prices for the whole market (about 6 minutes)
  topup      Mon-Fri 20:15   delivery file, desk bars and saved screens again, in case NSE's file came late
  weekly     Sat 09:00       the nightly job plus fundamentals (about an hour)
  intraday   Mon-Fri every hour 09:30-16:30   market-wide breadth (the job itself does nothing outside NSE hours)
  backup     every day 22:30 commits and pushes the code to GitHub

If the Mac is asleep at a scheduled time, launchd runs the job when it wakes up. If it is switched off, the run is skipped (the nightly job catches up on its own next time).
Logs: data/launchd-<job>.log
"""
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"
AGENTS = Path.home() / "Library" / "LaunchAgents"
PREFIX = "com.technofunda."
WEEKDAYS = range(1, 6)                                    # launchd: 1 = Monday ... 5 = Friday, 6 = Saturday


def at(days, hour, minute):
    return [{"Weekday": d, "Hour": hour, "Minute": minute} for d in days]


JOBS = {
    "nightly": ([str(PY), "-m", "backend.nightly"], at(WEEKDAYS, 18, 45)),
    "topup": ([str(PY), "-m", "backend.nightly", "--only", "extras,desk,screens"], at(WEEKDAYS, 20, 15)),
    "weekly": ([str(PY), "-m", "backend.nightly", "--weekly"], at([6], 9, 0)),
    "intraday": ([str(PY), "-m", "backend.live"], [x for h in range(9, 17) for x in at(WEEKDAYS, h, 30)]),
    "backup": (["/bin/bash", str(ROOT / "scripts" / "backup_github.sh")], [{"Hour": 22, "Minute": 30}]),
}


def plist_for(name, argv, calendar=None, server=False, keep_awake=False):
    if keep_awake and server:
        argv = ["/usr/bin/caffeinate", "-i"] + argv
    d = {
        "Label": PREFIX + name,
        "ProgramArguments": argv,
        "WorkingDirectory": str(ROOT),
        "StandardOutPath": str(ROOT / "data" / f"launchd-{name}.log"),
        "StandardErrorPath": str(ROOT / "data" / f"launchd-{name}.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"},
        "ProcessType": "Background",
    }
    if server:
        d["RunAtLoad"] = True
        d["KeepAlive"] = True
        d["ThrottleInterval"] = 15
    else:
        d["StartCalendarInterval"] = calendar
        d["ExitTimeOut"] = 60
    return d


def build(keep_awake=False, with_server=True):
    out = {}
    if with_server:
        out["server"] = plist_for("server", [str(PY), "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"], server=True, keep_awake=keep_awake)
    for name, (argv, cal) in JOBS.items():
        out[name] = plist_for(name, argv, calendar=cal)
    return out


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def domain():
    return f"gui/{os.getuid()}"


def install(args):
    if not args.dry_run:
        if sys.platform != "darwin":
            sys.exit("This installs launchd agents, so it only works on a Mac. (Use --dry-run DIR to just write the files.)")
        if not PY.exists():
            sys.exit(f"{PY} not found: run  bash scripts/setup_mac.sh  first.")
    target = Path(args.dry_run) if args.dry_run else AGENTS
    target.mkdir(parents=True, exist_ok=True)
    (ROOT / "data").mkdir(exist_ok=True)
    for name, d in build(args.keep_awake, not args.no_server).items():
        path = target / f"{PREFIX}{name}.plist"
        with open(path, "wb") as f:
            plistlib.dump(d, f)
        if args.dry_run:
            print("wrote", path)
            continue
        launchctl("bootout", f"{domain()}/{PREFIX}{name}")          # replace an older copy if there is one
        r = launchctl("bootstrap", domain(), str(path))
        print(("installed " if r.returncode == 0 else "FAILED    ") + PREFIX + name + ("" if r.returncode == 0 else "  " + r.stderr.strip()))
    if not args.dry_run:
        print("\nDone. Check any time with:  .venv/bin/python scripts/mac_services.py status")
        if args.no_server:
            print("The server is not scheduled: start the app with ./start-desk.command")
        else:
            print("The server starts at login and restarts if it stops: open http://localhost:8000/")
        tz = os.path.realpath("/etc/localtime")
        if "Kolkata" not in tz and "Calcutta" not in tz:
            print(f"\nNote: this Mac's time zone is {tz.split('zoneinfo/')[-1]}. The jobs run at the times above in THAT time zone; set India time in System Settings > General > Date & Time.")


def remove(args):
    for name in ["server", *JOBS]:
        path = AGENTS / f"{PREFIX}{name}.plist"
        launchctl("bootout", f"{domain()}/{PREFIX}{name}")
        if path.exists():
            path.unlink()
            print("removed", PREFIX + name)


def status(args):
    for name in ["server", *JOBS]:
        path = AGENTS / f"{PREFIX}{name}.plist"
        if not path.exists():
            print(f"{name:9} not installed")
            continue
        r = launchctl("print", f"{domain()}/{PREFIX}{name}")
        state = "loaded" if r.returncode == 0 else "NOT loaded"
        last = ""
        for line in r.stdout.splitlines():
            if "last exit code" in line or line.strip().startswith("state ="):
                last += " " + line.strip()
        print(f"{name:9} {state}{last}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--keep-awake", action="store_true")
    i.add_argument("--no-server", action="store_true")
    i.add_argument("--dry-run", metavar="DIR", help="only write the plist files into DIR (for checking)")
    i.set_defaults(fn=install)
    sub.add_parser("remove").set_defaults(fn=remove)
    sub.add_parser("status").set_defaults(fn=status)
    a = ap.parse_args()
    a.fn(a)
