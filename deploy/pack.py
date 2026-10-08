#!/usr/bin/env python3
"""Pack the app and its data into two files you copy to the server.   Run on your PC, from the project folder:

    .venv\\Scripts\\python deploy\\pack.py              # both:  deploy/out/app.tar.gz  and  deploy/out/data.tar.gz
    .venv\\Scripts\\python deploy\\pack.py --app        # only the code (for updates)

app.tar.gz   the code as last COMMITTED to git (so commit first: it is the same thing the GitHub backup holds), without data/
data.tar.gz  consistent copies of the three databases (made with SQLite's backup call, so it is safe while the server runs), your themes,
             the logins (users.json, secret.key) and every person's saved settings (userprefs/). Logs, caches and old backups are left out.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy" / "out"
DATA = ROOT / "data"
DBS = ["setupdesk.db", "market.db", "history.db"]
FILES = ["themes.json", "users.json", "secret.key", "nifty100.txt"]


def size(p):
    return f"{Path(p).stat().st_size / 1e6:.0f} MB"


def pack_app():
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if dirty:
        print("Note: you have uncommitted changes; they are NOT in app.tar.gz. Commit them first if you want them on the server:\n" + dirty + "\n")
    out = OUT / "app.tar.gz"
    subprocess.run(["git", "archive", "--format=tar.gz", "-o", str(out), "HEAD"], cwd=ROOT, check=True)
    print(f"app.tar.gz   {size(out)}")


def pack_data():
    out = OUT / "data.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name in DBS:
            src = DATA / name
            if not src.exists():
                print(f"  skipped {name}: not found")
                continue
            a, b = sqlite3.connect(src), sqlite3.connect(tmp / name)
            a.backup(b)                                          # a consistent snapshot even while the app is writing
            a.close(); b.close()
            print(f"  {name}  {size(tmp / name)}")
        for name in FILES:
            if (DATA / name).exists():
                shutil.copy2(DATA / name, tmp / name)
        if (DATA / "userprefs").is_dir():
            shutil.copytree(DATA / "userprefs", tmp / "userprefs")
        with tarfile.open(out, "w:gz", compresslevel=6) as tar:
            for item in sorted(tmp.iterdir()):
                tar.add(item, arcname=item.name)
    print(f"data.tar.gz  {size(out)}")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    pack_app()
    if "--app" not in sys.argv:
        pack_data()
    print(f"\nFiles are in {OUT}")
