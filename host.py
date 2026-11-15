"""The host dashboard's server side: sign-in, and the API the dashboard page (/host/) calls.

One password for staff, kept only as a salted scrypt hash in data/private.json. Signing in gives
the browser a session cookie (HttpOnly, SameSite=Strict); every /api/host/ call but the sign-in
ones needs it. Calendar links, Wi-Fi passwords, guest links and the API key state are only ever
sent to a signed-in host, and the API key itself is never sent to anyone.

The password is set on the host computer: `python manage.py set-password`, or in a browser on
that computer itself the first time (never from another device).
"""
import datetime as dt, hashlib, hmac, io, os, secrets, threading, time, zipfile

import access, concierge, content, stays, store, tvctl, tvlink

COOKIE = "gtv_host"
SESSION_HOURS = 12
DEMO_PASSWORD = "grace-demo"
_fails, _fail_lock = [], threading.Lock()


def demo():
    return os.environ.get("GUEST_TV_DEMO") == "1"


# ---------------------------------------------------------------- password and sessions

def _hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def has_password():
    return bool((store.private().get("host_password") or {}).get("hash"))


def set_password(password):
    if len(password or "") < 8:
        raise ValueError("Use at least 8 characters")
    salt = secrets.token_bytes(16)
    pv = store.private()
    pv["host_password"] = {"salt": salt.hex(), "hash": _hash(password, salt).hex()}
    store.save_private(pv)
    store.run("DELETE FROM sessions")   # everyone signs in again


def check_password(password):
    p = store.private().get("host_password") or {}
    if not p.get("hash"):
        return False
    try:
        return hmac.compare_digest(_hash(str(password or ""), bytes.fromhex(p["salt"])).hex(), p["hash"])
    except ValueError:
        return False


def locked():
    """Eight wrong passwords in five minutes lock sign-in for the rest of those five minutes."""
    with _fail_lock:
        t = time.monotonic()
        _fails[:] = [x for x in _fails if t - x < 300]
        return len(_fails) >= 8


def _sid(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_session():
    token = secrets.token_urlsafe(32)
    store.run("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    store.run("INSERT INTO sessions (id, expires) VALUES (?, ?)", (_sid(token), time.time() + SESSION_HOURS * 3600))
    return token


def cookie_of(handler):
    for part in (handler.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return ""


def authed(handler):
    token = cookie_of(handler)
    if not token:
        return False
    return bool(store.row("SELECT id FROM sessions WHERE id = ? AND expires > ?", (_sid(token), time.time())))


def set_cookie(token):
    return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (COOKIE, token, SESSION_HOURS * 3600 if token else 0)


# ---------------------------------------------------------------- what the dashboard shows

def booking_view(b, h, srv):
    st = stays.state_of(b, h.get("checkin"), h.get("checkout"))
    tv = tvlink.pick(b["home"])
    live = b["status"] == "confirmed" and not b["revoked"] and st != "past"
    return {"id": b["id"], "home": b["home"], "guest": b["guest"], "guests": b["guests"], "arrive": b["arrive"], "depart": b["depart"],
            "source": b["source"], "code": b["code"], "status": b["status"], "why": b["why"], "name_from": b["name_from"],
            "note": b["note"], "state": st, "revoked": bool(b["revoked"]),
            "link": srv.remote_link(b["home"], b["token"], tv["id"] if tv else None) if live else None,
            "link_state": ("cancelled" if b["status"] != "confirmed" else "expired at check-out" if st == "past"
                           else "revoked by staff" if b["revoked"] else "works now" if st in ("arriving", "staying", "leaving")
                           else "starts on arrival day")}


def tv_view(t, srv):
    st = tvlink.status(t)
    return {"id": t["id"], "home": t["home"], "name": t["name"], "kind": t["kind"], "ip": t["ip"], "live": bool(t["live"]),
            "checked": t["checked"], "check_ok": bool(t["check_ok"]), "check_msg": t["check_msg"], "status": st,
            "screen_url": srv.screen_url(t["id"]), "preview_path": "/tv/?screen=%s" % t["id"]}


def apartment_view(hid, srv):
    a = content.read(hid)
    if not a:
        return None
    h = srv.load_home(hid)
    a["bookings"] = [booking_view(b, h, srv) for b in stays.listing(hid)]
    a["tvs"] = [tv_view(t, srv) for t in tvlink.listing(hid)]
    a["sync"] = stays.sync_status(hid)
    a["now"] = srv.stay_of(h)
    return a


def overview(srv):
    s = srv.settings()
    out = []
    for hid in content.ids():
        h = srv.load_home(hid)
        cur = srv.stay_of(h)
        up = [b for b in stays.listing(hid) if b["status"] == "confirmed" and stays.state_of(b, h.get("checkin"), h.get("checkout")) == "upcoming"]
        out.append({"id": hid, "name": h.get("name"), "phase": cur.get("phase"), "guest": cur.get("guest", ""),
                    "from": cur.get("from"), "to": cur.get("to"), "next": {"arrive": up[0]["arrive"], "guest": up[0]["guest"]} if up else None,
                    "tvs": [tv_view(t, srv) for t in tvlink.listing(hid)], "sync": stays.sync_status(hid),
                    "has_ical": bool(((store.private().get("homes") or {}).get(hid) or {}).get("ical_url")),
                    "sample_photo": str((h.get("photos") or {}).get("hero", "")).startswith("sample-")})
    return {"property": s.get("property") or "Grace Apartments", "reception": s.get("reception") or {},
            "timezone": str(stays.TZ), "now": stays.now().isoformat(timespec="minutes"), "demo": demo(),
            "apartments": out, "concierge": concierge.status(s), "emails": srv.email_state,
            "apps": [{"id": a["id"], "name": a["name"]} for a in tvctl.APPS], "kinds": tvlink.KIND_NAMES,
            "address": srv.base_url(), "log": store.rows("SELECT at, what FROM log ORDER BY rowid DESC LIMIT 40")}


def backup_zip():
    """Everything under the data folder as one zip (apartments, bookings, photographs, private
    values). The database is copied with SQLite's own backup, so it is safe while running."""
    import sqlite3
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(store.DATA):
            dirs[:] = [d for d in dirs if d not in ("archive", "__pycache__")]
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, store.DATA)
                if f.startswith("guesttv.db") or f.endswith(".tmp"):
                    continue
                z.write(p, rel)
        with store.db() as con:
            mem = sqlite3.connect(":memory:")
            con.backup(mem)
            z.writestr("guesttv.sql", "\n".join(mem.iterdump()))
            mem.close()
    return buf.getvalue()


# ---------------------------------------------------------------- the API

def handle(h, method, path, qs, body, srv):
    """h: the request handler. Returns True when the request was answered here."""
    if not path.startswith("/api/host/"):
        return False
    act = path[len("/api/host/"):]
    local = srv._loopback(h.client_address[0])

    if method == "GET" and act == "session":
        h.send(200, {"authed": authed(h), "has_password": has_password(), "demo": demo(),
                     "demo_password": DEMO_PASSWORD if demo() else None, "can_setup": not has_password() and local,
                     "property": srv.settings().get("property") or "Grace Apartments"})
        return True
    if method == "POST" and act == "setup":
        if has_password() or not local:
            h.send(403, {"error": "Set the first password on the host computer itself: python manage.py set-password"})
            return True
        try:
            set_password(str(body.get("password") or ""))
        except ValueError as e:
            h.send(400, {"error": str(e)})
            return True
        store.note("The dashboard password was set")
        h.send(200, {"ok": True}, headers={"Set-Cookie": set_cookie(new_session())})
        return True
    if method == "POST" and act == "login":
        if locked():
            h.send(429, {"error": "Too many wrong passwords. Wait five minutes and try again."})
            return True
        if not check_password(body.get("password")):
            with _fail_lock:
                _fails.append(time.monotonic())
            store.note("A wrong dashboard password was tried")
            h.send(403, {"error": "That password is not right"})
            return True
        h.send(200, {"ok": True}, headers={"Set-Cookie": set_cookie(new_session())})
        return True
    if method == "POST" and act == "logout":
        if cookie_of(h):
            store.run("DELETE FROM sessions WHERE id = ?", (_sid(cookie_of(h)),))
        h.send(200, {"ok": True}, headers={"Set-Cookie": set_cookie("")})
        return True

    if not authed(h):
        h.send(401, {"error": "Sign in to the host dashboard first"})
        return True

    try:
        if method == "GET":
            if act == "overview":
                return h.send(200, overview(srv)) or True
            if act == "apartment":
                a = apartment_view(qs.get("id", ""), srv)
                return h.send(200 if a else 404, a or {"error": "No such apartment"}) or True
            if act == "backup":
                name = "guest-tv-backup-%s.zip" % stays.now().strftime("%Y%m%d-%H%M")
                return h.send(200, backup_zip(), "application/zip", headers={"Content-Disposition": 'attachment; filename="%s"' % name}) or True
            if act == "qr":
                b = stays.get(qs.get("booking", ""))
                home = b and srv.load_home(b["home"])
                v = b and booking_view(b, home, srv)
                if not v or not v["link"]:
                    return h.send(404, {"error": "No working link for that booking"}) or True
                return h.send(200, srv.qr_svg(v["link"]), "image/svg+xml") or True
            return h.send(404, {"error": "not found"}) or True

        hid = str(body.get("home") or body.get("id") or "")
        if act == "password":
            if not check_password(body.get("old")):
                raise ValueError("The current password is not right")
            set_password(str(body.get("new") or ""))
            store.note("The dashboard password was changed")
            return h.send(200, {"ok": True}, headers={"Set-Cookie": set_cookie(new_session())}) or True
        if act == "property":
            content.save_property(body)
            return h.send(200, {"ok": True}) or True
        if act == "apartment/create":
            new = content.create(body.get("name"))
            tvlink.save(new, "Living room TV")
            store.note("Apartment added: %s" % new)
            return h.send(200, {"ok": True, "id": new}) or True
        if act == "apartment/save":
            content.save(hid, body, {a["id"] for a in tvctl.APPS})
            if "ical_url" in body:
                srv.sync_now(hid)
            return h.send(200, {"ok": True, "apartment": apartment_view(hid, srv)}) or True
        if act == "apartment/delete":
            content.remove(hid)
            store.note("Apartment removed: %s" % hid)
            return h.send(200, {"ok": True}) or True
        if act == "guide/save":
            content.save_guide(hid, body)
            return h.send(200, {"ok": True}) or True
        if act == "photo":
            content.set_photo(hid, body.get("which"), body.get("data") or "")
            return h.send(200, {"ok": True, "apartment": apartment_view(hid, srv)}) or True
        if act == "photo/sample":
            content.sample_photo(hid, body.get("which"))
            return h.send(200, {"ok": True, "apartment": apartment_view(hid, srv)}) or True

        if act == "booking/add":
            if hid not in content.ids():
                raise ValueError("Choose an apartment")
            b = stays.add(hid, body.get("guest"), body.get("arrive"), body.get("depart"), body.get("guests"), body.get("note"))
            store.note("Booking added in %s: %s to %s" % (hid, b["arrive"], b["depart"]))
            return h.send(200, {"ok": True, "id": b["id"]}) or True
        if act.startswith("booking/"):
            b = stays.get(str(body.get("id") or ""))
            if not b:
                raise ValueError("No such booking")
            what = act.split("/", 1)[1]
            if what == "update":
                stays.update(b["id"], body.get("guest"), body.get("arrive"), body.get("depart"), body.get("guests"), body.get("note"))
            elif what == "cancel":
                stays.cancel(b["id"])
            elif what == "restore":
                stays.restore(b["id"])
            elif what == "delete":
                stays.remove(b["id"])
            elif what == "revoke":
                access.revoke(b["id"])
            elif what == "reissue":
                access.reissue(b["id"])
            else:
                return h.send(404, {"error": "not found"}) or True
            store.note("Booking %s in %s (%s to %s)" % ({"update": "changed", "cancel": "cancelled", "restore": "restored", "delete": "deleted",
                                                         "revoke": "guest link revoked", "reissue": "new guest link issued"}[what],
                                                        b["home"], b["arrive"], b["depart"]))
            return h.send(200, {"ok": True}) or True

        if act == "tv/save":
            if hid not in content.ids():
                raise ValueError("Choose an apartment")
            t = tvlink.save(hid, body.get("name"), body.get("kind"), body.get("ip"), body.get("tv") or None)
            return h.send(200, {"ok": True, "tv": tv_view(t, srv)}) or True
        if act == "tv/delete":
            tvlink.remove(str(body.get("tv") or ""))
            return h.send(200, {"ok": True}) or True
        if act == "tv/check":
            t = tvlink.check(str(body.get("tv") or ""))
            store.note("TV check for %s: %s" % (t["name"], "passed" if t["check_ok"] else "failed"))
            return h.send(200, {"ok": True, "tv": tv_view(t, srv)}) or True
        if act == "tv/live":
            t = tvlink.set_live(str(body.get("tv") or ""), bool(body.get("on")))
            store.note("Real commands for %s switched %s" % (t["name"], "on" if t["live"] else "off"))
            return h.send(200, {"ok": True, "tv": tv_view(t, srv)}) or True

        if act == "sync":
            return h.send(200, {"ok": True, "sync": srv.sync_now(hid)}) or True
        if act == "emails":
            srv.read_emails()
            return h.send(200, {"ok": True, "emails": srv.email_state}) or True
    except (ValueError, KeyError) as e:   # stays.Invalid, content.Invalid and tvlink.Invalid are ValueErrors
        h.send(400, {"error": str(e) or "That could not be saved"})
        return True
    h.send(404, {"error": "not found"})
    return True
