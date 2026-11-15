"""TV connections for the managed apartments: which TV belongs to which apartment, and whether a
guest's button press goes to a real TV or to a simulated one.

Every TV is one row (store.tvs) tied to exactly one apartment. A TV starts SIMULATED: the Guest
remote works, and its keys move the welcome screen in whatever browser shows /tv/?screen=<id>, but
nothing is sent to any hardware. Real commands start only after staff, standing at the TV:
  1. fill in the TV's kind and address,
  2. run "Check connection" (reads only: the TV's app list or power state; an Android TV asks
     "Allow USB debugging?" the first time, others pair with tools/tv_app.py pair), and it passes,
  3. switch "Send real commands" on.
The status a guest sees is always the honest one: simulated, connected, or not answering.

The drivers are guest-tv's own (tvctl.TV over ADB, tvbrands.KINDS for every other TV). Nothing
here scans the network: a TV is only ever reached at the address staff typed.
"""
import ipaddress, os, re, secrets, threading, time

import store, tvctl

ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
KIND_NAMES = {"android": "Google TV / Android TV / Fire TV (Fire OS)", "samsung": "Samsung (Tizen)", "lg": "LG (webOS)",
              "roku": "Roku", "vidaa": "Hisense VIDAA", "titan": "Philips Titan OS", "saphi": "Philips Saphi",
              "panasonic": "Panasonic Viera", "vizio": "VIZIO", "appletv": "Apple TV", "vega": "Fire TV (Vega OS)"}


class Invalid(ValueError):
    pass


class SimTV:
    """A stand-in with the same calls as tvctl.TV. It keeps the last few presses and hands the
    navigation keys to the TV page (a long poll, the way guest-tv's Fire TV Vega app gets them)."""
    simulated = True
    KEY_FRESH = 8.0

    def __init__(self, tv_id):
        self.id = tv_id
        self._cv = threading.Condition()
        self._n = 0
        self._keys = []
        self.page_seen = 0.0
        self.log = []

    def note(self, what):
        self.log = (self.log + [time.strftime("%H:%M:%S ") + what])[-12:]

    def installed(self):
        return list(tvctl.APPS)

    def front(self):
        return None

    def key(self, name, times=1):
        if name not in tvctl.KEYS:
            raise KeyError(name)
        for _ in range(max(1, min(int(times), 10))):
            self._push("key:" + name)
        self.note("key " + name)

    def open_app(self, app_id):
        a = tvctl.BY_ID[app_id]   # KeyError for anything not in the table
        self._push("app:" + a["name"])
        self.note("open " + a["id"])

    def open_link(self, uri, app_id):
        self.open_app(app_id)
        return "app"

    def spotify(self, query):
        self.open_app("spotify")
        return False

    def playing(self, package):
        return False

    def _push(self, item):
        with self._cv:
            self._n += 1
            self._keys = (self._keys + [(self._n, item, time.monotonic())])[-50:]
            self._cv.notify_all()

    def watching(self):
        """True when a welcome screen asked for this TV's keys in the last minute."""
        return time.monotonic() - self.page_seen < 60 if self.page_seen else False

    def page_keys(self, after, timeout=20.0):
        try:
            after = int(after)
        except (TypeError, ValueError):
            after = 0
        end = time.monotonic() + max(0.0, min(float(timeout), 30.0))
        self.page_seen = time.monotonic()
        with self._cv:
            while True:
                t = time.monotonic()
                if after > self._n:
                    after = 0
                new = [item for n, item, when in self._keys if n > after and t - when <= self.KEY_FRESH]
                if new or t >= end:
                    self.page_seen = time.monotonic()
                    return {"n": self._n, "keys": new, "simulated": True}
                self._cv.wait(min(0.25, max(0.01, end - t)))


_sims, _sim_lock = {}, threading.Lock()
_status = {}   # tv id -> (when, ok, message): the last real answer, kept a few seconds


def sim(tv_id):
    with _sim_lock:
        if tv_id not in _sims:
            _sims[tv_id] = SimTV(tv_id)
        return _sims[tv_id]


# ---------------------------------------------------------------- the registry

def listing(home=None):
    if home:
        return store.rows("SELECT * FROM tvs WHERE home = ? ORDER BY name, id", (home,))
    return store.rows("SELECT * FROM tvs ORDER BY home, name, id")


def get(tv_id):
    return store.row("SELECT * FROM tvs WHERE id = ?", (tv_id,))


def pick(home, tv_id=None):
    """The TV a request may use: it must belong to this apartment. With no id, the apartment's
    first TV. None when the id is another apartment's (or nobody's)."""
    tvs = listing(home)
    if tv_id:
        return next((t for t in tvs if t["id"] == tv_id), None)
    return tvs[0] if tvs else None


def _clean(name, kind, ip):
    name = re.sub(r"[\x00-\x1f<>]", "", str(name or "")).strip()[:50] or "TV"
    kind = kind if kind in KIND_NAMES else "android"
    ip = str(ip or "").strip()
    if ip:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            raise Invalid("The TV's address is a number like 192.168.1.50 (TV Settings, Network, About), not a name")
        if a.is_loopback or a.is_multicast or a.is_unspecified:
            raise Invalid("That is not a TV's address")
    return name, kind, ip


def save(home, name, kind="android", ip="", tv_id=None):
    """Add a TV to an apartment, or change one. Moving a TV to another apartment, or changing its
    kind or address, switches real commands off again until it passes a new check."""
    name, kind, ip = _clean(name, kind, ip)
    old = get(tv_id) if tv_id else None
    if tv_id and not old:
        raise Invalid("No such TV")
    if old:
        same = (old["home"], old["kind"], old["ip"]) == (home, kind, ip)
        store.run("UPDATE tvs SET home=?, name=?, kind=?, ip=?, live=?, check_ok=?, checked=?, check_msg=?, updated=? WHERE id=?",
                  (home, name, kind, ip, old["live"] if same else 0, old["check_ok"] if same else 0,
                   old["checked"] if same else "", old["check_msg"] if same else "", _now(), tv_id))
        _status.pop(tv_id, None)
        return get(tv_id)
    base = re.sub(r"[^a-z0-9]+", "-", ("%s-%s" % (home, name)).lower()).strip("-")[:32] or "tv"
    tid = base
    while get(tid):
        tid = "%s-%s" % (base[:28], secrets.token_hex(2))
    store.run("INSERT INTO tvs (id, home, name, kind, ip, updated) VALUES (?,?,?,?,?,?)", (tid, home, name, kind, ip, _now()))
    return get(tid)


def remove(tv_id):
    store.run("DELETE FROM tvs WHERE id = ?", (tv_id,))
    _status.pop(tv_id, None)


def _now():
    import stays
    return stays.stamp()


# ---------------------------------------------------------------- real or simulated

def is_live(tv):
    return bool(tv and tv["live"] and tv["check_ok"] and tv["ip"]) and os.environ.get("GUEST_TV_DEMO") != "1"


def driver(tv):
    """What guest commands for this TV go to: the real TV's driver once it is live, else the stand-in."""
    if is_live(tv):
        return tvctl.tv_for(tv["ip"], tv["kind"])
    return sim(tv["id"])


def _probe(d):
    """One read from the real TV: is it there, and which guest apps does it have."""
    if hasattr(d, "is_on") and not d.is_on():
        raise RuntimeError("The TV is off or not answering")
    return [a["id"] for a in d.installed()]


def check(tv_id):
    """Staff pressed "Check connection", standing at the TV. Reads only; nothing is opened or changed."""
    tv = get(tv_id)
    if not tv:
        raise Invalid("No such TV")
    if not tv["ip"]:
        raise Invalid("Type the TV's address first")
    if os.environ.get("GUEST_TV_DEMO") == "1":
        raise Invalid("Demo mode never talks to a real TV. Start guest-tv normally to connect one.")
    ok, msg = False, ""
    try:
        if tv["kind"] == "android":
            _adb_key()
            d = tvctl.TV(tv["ip"], auth_timeout=45)   # time to press Allow on the TV
            apps = _probe(d)
            tvctl._tvs[("android", tv["ip"])] = d
        else:
            apps = _probe(tvctl.tv_for(tv["ip"], tv["kind"]))
        ok, msg = True, "The TV answered. Guest apps found on it: %s" % (", ".join(apps) or "none of the supported ones")
    except Exception as e:
        msg = _why(tv, e)
    store.run("UPDATE tvs SET checked=?, check_ok=?, check_msg=?, live=CASE WHEN ? THEN live ELSE 0 END, updated=? WHERE id=?",
              (_now(), int(ok), msg[:300], int(ok), _now(), tv_id))
    _status.pop(tv_id, None)
    return get(tv_id)


def _adb_key():
    key = os.path.expanduser("~/.android/adbkey")
    if not os.path.isfile(key):
        from adb_shell.auth.keygen import keygen
        os.makedirs(os.path.dirname(key), exist_ok=True)
        keygen(key)


def _why(tv, e):
    if tv["kind"] == "android":
        return ("The TV did not answer (%s). On the TV: Settings, System, Developer options, switch USB (or Network) debugging on; "
                "check the address; then press Check again and choose Always allow on the TV." % type(e).__name__)
    return ("The TV did not answer (%s). Is it on, and paired? Pair it once on this computer: "
            "TV_KIND=%s TV_IP=%s .venv/bin/python tools/tv_app.py pair" % (type(e).__name__, tv["kind"], tv["ip"]))


def set_live(tv_id, on):
    tv = get(tv_id)
    if not tv:
        raise Invalid("No such TV")
    if on and not (tv["check_ok"] and tv["ip"]):
        raise Invalid("Run Check connection at the TV first. Real commands start only after it passes.")
    store.run("UPDATE tvs SET live=?, updated=? WHERE id=?", (int(bool(on)), _now(), tv_id))
    _status.pop(tv_id, None)
    return get(tv_id)


def status(tv, fresh=False):
    """What the dashboard and the Guest remote show. mode: none | simulated | connected | offline."""
    if not tv:
        return {"mode": "none", "label": "No TV is set up for this apartment yet", "real": False}
    if not is_live(tv):
        s = sim(tv["id"])
        why = ("Demo mode" if os.environ.get("GUEST_TV_DEMO") == "1" else "Not connected to the real TV yet" if not tv["check_ok"]
               else "Connection checked, real commands still switched off")
        return {"mode": "simulated", "real": False, "watching": s.watching(),
                "label": "Simulated TV: buttons move the preview screen only", "why": why}
    c = _status.get(tv["id"])
    if fresh or not c or time.monotonic() - c[0] > 20:
        try:
            _probe(driver(tv))
            c = (time.monotonic(), True, "")
        except Exception as e:
            c = (time.monotonic(), False, type(e).__name__)
        _status[tv["id"]] = c
    if c[1]:
        return {"mode": "connected", "real": True, "label": "Connected to %s" % tv["name"]}
    return {"mode": "offline", "real": True, "label": "%s is not answering. Is it switched on?" % tv["name"]}


def sent(tv, ok):
    """A real command just worked or failed: the status follows at once."""
    if is_live(tv):
        _status[tv["id"]] = (time.monotonic(), bool(ok), "" if ok else "command failed")
