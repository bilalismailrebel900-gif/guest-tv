#!/usr/bin/env python3
"""Housekeeping for the managed apartments, run on the host computer.

    .venv/bin/python manage.py set-password         set or change the host dashboard password
    .venv/bin/python manage.py backup [folder]      a dated zip of everything in data/ (default: backups/)
    .venv/bin/python manage.py restore <zip>        put a backup back (stop guest-tv first)
    .venv/bin/python manage.py status               apartments, today's stays, TVs
"""
import getpass, os, shutil, sqlite3, sys, zipfile

import content, host, stays, store, tvlink


def set_password():
    a = os.environ.get("GUEST_TV_NEW_PASSWORD") or getpass.getpass("New dashboard password (8 characters or more): ")
    if not os.environ.get("GUEST_TV_NEW_PASSWORD") and a != getpass.getpass("Again: "):
        sys.exit("They did not match. Nothing was changed.")
    try:
        host.set_password(a)
    except ValueError as e:
        sys.exit(str(e))
    print("Saved. Everyone signed in to the dashboard must sign in again.")


def backup(folder):
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, "guest-tv-backup-%s.zip" % stays.now().strftime("%Y%m%d-%H%M%S"))
    with open(out, "wb") as f:
        f.write(host.backup_zip())
    os.chmod(out, 0o600)
    print("Backup written:", out)
    print("It holds Wi-Fi passwords and guest names: keep it somewhere private.")


def restore(zpath):
    if not os.path.isfile(zpath):
        sys.exit("No such file: " + zpath)
    keep = store.DATA.rstrip("/") + ".before-restore"
    if os.path.exists(keep):
        shutil.rmtree(keep)
    if os.path.isdir(store.DATA):
        shutil.copytree(store.DATA, keep)
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            if n.startswith("/") or ".." in n.split("/"):
                sys.exit("That zip has an unsafe path in it: " + n)
        z.extractall(store.DATA)
    sql = os.path.join(store.DATA, "guesttv.sql")
    if os.path.isfile(sql):
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(store.path() + ext):
                os.remove(store.path() + ext)
        con = sqlite3.connect(store.path())
        con.executescript(open(sql, encoding="utf-8").read())
        con.close()
        os.remove(sql)
    print("Restored into %s. The folder as it was is kept at %s." % (store.DATA, keep))


def status():
    s = __import__("server")
    s.start_managed.__doc__   # (server is only read here, never started)
    print("Data folder:", store.DATA)
    for hid in content.ids():
        h = s.load_home(hid)
        cur = s.stay_of(h)
        print("%-18s %-9s %s" % (h.get("name"), cur["phase"], ("%s, %s to %s" % (cur.get("guest") or "(no name)", cur.get("from"), cur.get("to"))) if cur.get("from") else ""))
        for t in tvlink.listing(hid):
            print("    TV %-22s %s %s  %s" % (t["name"], t["kind"], t["ip"] or "(no address)", "REAL commands" if tvlink.is_live(t) else "simulated"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "set-password":
        set_password()
    elif cmd == "backup":
        backup(sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups"))
    elif cmd == "restore" and len(sys.argv) > 2:
        restore(sys.argv[2])
    elif cmd == "status":
        status()
    else:
        sys.exit(__doc__)
