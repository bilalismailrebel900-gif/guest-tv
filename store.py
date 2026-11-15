"""Persistent storage for the managed apartments: one SQLite file (data/guesttv.db) for bookings,
TVs, sync status and host sessions, plus the small JSON files guest-tv already used.

Everything lives under DATA (the data/ folder, or GUEST_TV_DATA for demo mode and tests), so a
backup is a copy of that one folder. Nothing here talks to a TV or the network.
"""
import contextlib, json, os, sqlite3, threading

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("GUEST_TV_DATA") or os.path.join(HERE, "data")
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
  id TEXT PRIMARY KEY, home TEXT NOT NULL, guest TEXT NOT NULL DEFAULT '', guests INTEGER NOT NULL DEFAULT 0,
  arrive TEXT NOT NULL, depart TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual',      -- manual | airbnb
  uid TEXT NOT NULL DEFAULT '',               -- the calendar event this came from (airbnb)
  code TEXT NOT NULL DEFAULT '',              -- Airbnb reservation code (HM...), when known
  status TEXT NOT NULL DEFAULT 'confirmed',   -- confirmed | cancelled
  why TEXT NOT NULL DEFAULT '',               -- why it was cancelled
  name_from TEXT NOT NULL DEFAULT '',         -- staff | email | ''
  token TEXT NOT NULL UNIQUE,                 -- the guest remote link for this booking
  revoked INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS bookings_home ON bookings (home, arrive);
CREATE TABLE IF NOT EXISTS tvs (
  id TEXT PRIMARY KEY, home TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'android',
  ip TEXT NOT NULL DEFAULT '',
  live INTEGER NOT NULL DEFAULT 0,            -- 1: guest commands go to the real TV (only after a passed check)
  checked TEXT NOT NULL DEFAULT '', check_ok INTEGER NOT NULL DEFAULT 0, check_msg TEXT NOT NULL DEFAULT '',
  updated TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sync (
  home TEXT PRIMARY KEY, at TEXT, ok INTEGER, message TEXT, seen INTEGER DEFAULT 0, added INTEGER DEFAULT 0,
  changed INTEGER DEFAULT 0, cancelled INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS log (at TEXT NOT NULL, what TEXT NOT NULL);
"""


def path():
    return os.path.join(DATA, "guesttv.db")


@contextlib.contextmanager
def db():
    """One short transaction. Commits on success, rolls back on an error."""
    with _lock:
        os.makedirs(DATA, exist_ok=True)
        con = sqlite3.connect(path(), timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


def rows(sql, args=()):
    with db() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def row(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def run(sql, args=()):
    with db() as con:
        return con.execute(sql, args).rowcount


def note(what):
    """A short line in the host's activity log (never a Wi-Fi password, key or link)."""
    import datetime as dt
    with db() as con:
        con.execute("INSERT INTO log (at, what) VALUES (?, ?)", (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), str(what)[:300]))
        con.execute("DELETE FROM log WHERE rowid NOT IN (SELECT rowid FROM log ORDER BY rowid DESC LIMIT 500)")


# ---------------------------------------------------------------- the JSON files

def jread(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jwrite(p, obj):
    with _lock:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, p)


def private():
    return jread(os.path.join(DATA, "private.json"), {}) or {}


def save_private(pv):
    jwrite(os.path.join(DATA, "private.json"), pv)
    try:
        os.chmod(os.path.join(DATA, "private.json"), 0o600)
    except OSError:
        pass


def prop():
    """What the host changed in the dashboard about the whole property (data/property.json). It is
    laid over settings.json, which stays as it came with the code."""
    return jread(os.path.join(DATA, "property.json"), {}) or {}


def save_prop(p):
    jwrite(os.path.join(DATA, "property.json"), p)
