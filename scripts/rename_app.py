"""Rename the app everywhere its name is shown.

    python scripts/rename_app.py "New Name"

The current name is kept in appname.txt. Page titles, the menu's monogram, the server title, the batch file, the README and the scheduler
descriptions are updated. Browser settings keep their old internal keys ("setupdesk.*") so nothing you saved is lost.
Re-register the scheduled tasks (scripts/register_nightly.ps1) if you want their descriptions to show the new name.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["setup-desk.html", "screener.html", "breadth.html", "industries.html", "start-desk.bat", "README.md", "build_data.py",
         "backend/app.py", "scripts/allow_local_network.ps1", "scripts/register_nightly.ps1", "scripts/register_intraday.ps1"]


def main(new):
    new = new.strip()
    if not new:
        sys.exit("give the new name")
    old = (ROOT / "appname.txt").read_text(encoding="utf-8").strip()
    mono = "".join(w[0] for w in new.split()[:2]).upper() or new[:2].upper()
    n = 0
    for f in FILES:
        p = ROOT / f
        t = p.read_text(encoding="utf-8")
        u = t.replace(old, new)
        u = re.sub(r'(<div class="brand" title=")[^"]*(">)[^<]*(</div>)', lambda m: f"{m.group(1)}{new}{m.group(2)}{mono}{m.group(3)}", u)
        if u != t:
            p.write_text(u, encoding="utf-8")
            n += 1
    (ROOT / "appname.txt").write_text(new, encoding="utf-8")
    print(f"{old} -> {new}: {n} files updated (monogram {mono})")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]))
