"""Login for TechnoFunda Desk.

    python -m backend.auth add NAME [--admin]    # create a user (asks for the password; nothing is echoed or stored in plain text)
    python -m backend.auth passwd NAME           # change a password
    python -m backend.auth remove NAME
    python -m backend.auth list

Until the first user exists the app is open, exactly as before. From the moment one exists, every page and every /api call needs a login,
on this PC, on a phone and through any tunnel. The first user is created on the PC itself at http://localhost:8000/setup.

How it is kept safe:
  * passwords are stored only as salted scrypt hashes, in data/users.json (never committed to GitHub: data/ is ignored)
  * a login gives a signed cookie (HMAC-SHA256 with a random key in data/secret.key) that lasts 30 days, is HttpOnly and SameSite=Lax,
    and is marked Secure when the page is served over https
  * five wrong passwords from one address lock that address out for a minute, and every wrong attempt takes a moment
"""
import base64
import getpass
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
USERS = DATA / "users.json"
KEY = DATA / "secret.key"
COOKIE = "td_session"
SESSION_DAYS = 30
_cache = {"mtime": None, "users": {}}
_fails = {}                                              # address -> [count, locked_until]


def _users():
    try:
        m = USERS.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _cache["mtime"] != m:
        try:
            _cache.update(mtime=m, users=json.loads(USERS.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            return _cache["users"]
    return _cache["users"]


def _save(users):
    DATA.mkdir(exist_ok=True)
    tmp = USERS.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, indent=1), encoding="utf-8")
    os.replace(tmp, USERS)


def enabled():
    return bool(_users())


def exists(name):
    return name in _users()


def is_admin(name):
    return bool(_users().get(name, {}).get("admin"))


def hash_password(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def _verify(pw, stored):
    try:
        _, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got.hex(), h)
    except Exception:
        return False


def add_user(name, pw, admin=False):
    name = name.strip()
    if not name or len(name) > 40 or not all(c.isalnum() or c in "._-@" for c in name):
        raise ValueError("a user name is 1-40 letters, digits, . _ - or @")
    if len(pw) < 8:
        raise ValueError("a password needs at least 8 characters")
    users = dict(_users())
    users[name] = {"hash": hash_password(pw), "admin": bool(admin) or (not users and True), "created": time.strftime("%Y-%m-%d %H:%M")}
    _save(users)
    return name


def remove_user(name):
    users = dict(_users())
    if users.pop(name, None) is None:
        raise ValueError("no such user")
    _save(users)


def check(name, pw):
    """True if the name and password are right. Always does the hashing work, so a wrong name takes as long as a wrong password."""
    u = _users().get(name.strip())
    ok = _verify(pw, u["hash"] if u else "scrypt$00$00")
    return bool(u) and ok


def locked(addr):
    f = _fails.get(addr)
    return bool(f and f[1] > time.time())


def failed(addr):
    f = _fails.setdefault(addr, [0, 0])
    f[0] += 1
    if f[0] >= 5:
        f[0], f[1] = 0, time.time() + 60
    time.sleep(0.4)


def succeeded(addr):
    _fails.pop(addr, None)


def _secret():
    if not KEY.exists():
        DATA.mkdir(exist_ok=True)
        KEY.write_bytes(os.urandom(32))
    return KEY.read_bytes()


def make_token(user):
    exp = int(time.time()) + SESSION_DAYS * 86400
    body = base64.urlsafe_b64encode(f"{user}|{exp}".encode("utf-8")).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(tok):
    """-> the user name in a valid, unexpired cookie, else None."""
    try:
        body, sig = (tok or "").split(".")
        if not hmac.compare_digest(hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest(), sig):
            return None
        user, exp = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8").rsplit("|", 1)
        return user if int(exp) > time.time() and exists(user) else None
    except Exception:
        return None


def _cli(argv):
    if not argv:
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":
        for n, u in _users().items():
            print(f"{n:<20} {'admin' if u.get('admin') else 'user ':<6} created {u.get('created', '')}")
        return
    if not rest:
        sys.exit("give a user name")
    name = rest[0]
    if cmd in ("add", "passwd"):
        if cmd == "add" and exists(name):
            sys.exit("that user already exists; use passwd to change the password")
        if cmd == "passwd" and not exists(name):
            sys.exit("no such user")
        pw = getpass.getpass("Password (at least 8 characters): ")
        if pw != getpass.getpass("Again: "):
            sys.exit("the two passwords differ; nothing changed")
        keep_admin = _users().get(name, {}).get("admin", False)
        add_user(name, pw, admin="--admin" in rest or keep_admin)
        print("saved")
    elif cmd == "remove":
        remove_user(name)
        print("removed")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    _cli(sys.argv[1:])
