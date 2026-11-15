"""Bookings for the managed apartments: who is staying where and when.

Staff type bookings in the host dashboard (manual). An apartment may also follow its Airbnb calendar
link (dates only, Airbnb leaves the name out); the guest's first name is then typed by staff or read
from the booking confirmation email (bookings.py). All dates and times are Asia/Karachi by default.

Each booking carries its own guest remote token (access.py). A booking is never deleted by a sync:
it is marked cancelled, with the reason, so staff can see what happened.
"""
import datetime as dt, re, secrets, urllib.request
from zoneinfo import ZoneInfo

import store

TZ = ZoneInfo("Asia/Karachi")   # server.py sets this from settings.json "timezone"
NAME_MAX = 40


class Invalid(ValueError):
    """A booking the host typed that cannot be saved; the message is shown to them."""


CLOCK = None   # demo mode and the tests may pin the time: a function that returns "now"


def now():
    return CLOCK() if CLOCK else dt.datetime.now(TZ)


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def at(day, hhmm, default="12:00"):
    try:
        hh, mm = (int(x) for x in (hhmm or default).split(":")[:2])
    except ValueError:
        hh, mm = (int(x) for x in default.split(":"))
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def clean_name(s):
    """A first name as it will show on a TV: letters, spaces and a few marks, nothing that is markup."""
    s = re.sub(r"[\x00-\x1f<>{}\\]", "", str(s or "")).strip()
    return re.sub(r"\s+", " ", s)[:NAME_MAX]


def _date(s, what):
    try:
        return dt.date.fromisoformat(str(s or "")[:10])
    except ValueError:
        raise Invalid("The %s date looks like 2026-11-20" % what)


def _check(home, arrive, depart, skip_id=None):
    a, d = _date(arrive, "arrival"), _date(depart, "departure")
    if d <= a:
        raise Invalid("The departure date must be after the arrival date")
    if (d - a).days > 365:
        raise Invalid("A stay can be at most a year long")
    for b in store.rows("SELECT * FROM bookings WHERE home = ? AND status = 'confirmed'", (home,)):
        if b["id"] != skip_id and b["arrive"] < d.isoformat() and a.isoformat() < b["depart"]:
            raise Invalid("These dates overlap another booking in this apartment (%s to %s%s)" % (
                b["arrive"], b["depart"], ", " + b["guest"] if b["guest"] else ""))
    return a.isoformat(), d.isoformat()


def new_token():
    return secrets.token_urlsafe(18)


def add(home, guest, arrive, depart, guests=0, note="", source="manual", uid="", code=""):
    a, d = _check(home, arrive, depart)
    bid = "b_" + secrets.token_hex(6)
    name = clean_name(guest)
    store.run("INSERT INTO bookings (id, home, guest, guests, arrive, depart, source, uid, code, name_from, token, note, created, updated) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (bid, home, name, _int(guests), a, d, source, uid, code, "staff" if name and source == "manual" else "",
               new_token(), str(note or "")[:300], stamp(), stamp()))
    return get(bid)


def _int(v):
    try:
        return max(0, min(50, int(v or 0)))
    except (TypeError, ValueError):
        return 0


def get(bid):
    return store.row("SELECT * FROM bookings WHERE id = ?", (bid,))


def update(bid, guest=None, arrive=None, depart=None, guests=None, note=None):
    b = get(bid)
    if not b:
        raise Invalid("No such booking")
    a, d = b["arrive"], b["depart"]
    if arrive is not None or depart is not None:
        a, d = _check(b["home"], arrive or a, depart or d, skip_id=bid)
    name, name_from = b["guest"], b["name_from"]
    if guest is not None:
        name = clean_name(guest)
        name_from = "staff" if name else ""   # a name staff type always wins over the email's
    store.run("UPDATE bookings SET guest=?, name_from=?, arrive=?, depart=?, guests=?, note=?, updated=? WHERE id=?",
              (name, name_from, a, d, _int(guests) if guests is not None else b["guests"],
               str(note)[:300] if note is not None else b["note"], stamp(), bid))
    return get(bid)


def cancel(bid, why="Cancelled by staff"):
    """Cancelled bookings keep their row (so the list shows what happened) but lose the guest link."""
    store.run("UPDATE bookings SET status='cancelled', why=?, revoked=1, updated=? WHERE id=?", (why[:120], stamp(), bid))
    return get(bid)


def restore(bid):
    b = get(bid)
    if not b:
        raise Invalid("No such booking")
    _check(b["home"], b["arrive"], b["depart"], skip_id=bid)
    store.run("UPDATE bookings SET status='confirmed', why='', revoked=0, token=?, updated=? WHERE id=?", (new_token(), stamp(), bid))
    return get(bid)


def remove(bid):
    store.run("DELETE FROM bookings WHERE id = ?", (bid,))


def listing(home=None):
    if home:
        return store.rows("SELECT * FROM bookings WHERE home = ? ORDER BY arrive", (home,))
    return store.rows("SELECT * FROM bookings ORDER BY arrive")


def window(b, checkin, checkout):
    a, d = dt.date.fromisoformat(b["arrive"]), dt.date.fromisoformat(b["depart"])
    return a, d, at(a, checkin, "14:00"), at(d, checkout, "12:00")


def current(home, checkin, checkout, t=None):
    """The booking this apartment's screens are about right now, with the moment of the stay:
    before (the day before arrival: nothing private yet), arrival (arrival day, before check-in),
    during, lastday (check-out day, until the check-out time). None between bookings.
    The earliest booking that has not checked out yet wins, so on a same-day changeover the leaving
    guest keeps the screen until check-out and the arriving guest gets it straight after."""
    t = t or now()
    today = t.date()
    for b in store.rows("SELECT * FROM bookings WHERE home = ? AND status = 'confirmed' ORDER BY arrive", (home,)):
        a, d, cin, cout = window(b, checkin, checkout)
        if a - dt.timedelta(days=1) <= today and t < cout:
            nights = max(1, (d - a).days)
            phase = ("arrival" if t < cin and today == a else "before" if t < cin
                     else "lastday" if today == d else "during")
            active = phase in ("arrival", "during", "lastday")
            return {"booking": b["id"], "phase": phase, "active": active,
                    "guest": b["guest"] if active else "", "guests": b["guests"] if active else 0,
                    "from": b["arrive"], "to": b["depart"], "checkin": cin.isoformat(), "checkout": cout.isoformat(),
                    "nights": nights, "night": min(nights, max(1, (today - a).days + 1))}
    return None


def state_of(b, checkin, checkout, t=None):
    """For the host's list: upcoming, arriving, staying, leaving, past or cancelled."""
    if b["status"] != "confirmed":
        return "cancelled"
    t = t or now()
    a, d, cin, cout = window(b, checkin, checkout)
    if t >= cout:
        return "past"
    if t.date() < a:
        return "upcoming"
    if t < cin:
        return "arriving"
    return "leaving" if t.date() == d else "staying"


def forget_old(days=30):
    """Guest names are not kept for ever: a month after check-out the name and note are wiped."""
    cut = (now().date() - dt.timedelta(days=max(1, int(days)))).isoformat()
    return store.run("UPDATE bookings SET guest='', guests=0, note='', name_from='' WHERE depart < ? AND (guest != '' OR note != '')", (cut,))


# ---------------------------------------------------------------- Airbnb calendar link (dates only)

CODE = re.compile(r"\bHM[A-Z0-9]{6,10}\b")


def parse_ical(text):
    """One VEVENT per booking: DTSTART and DTEND as dates. The host's own blocked days are left out.
    Raises ValueError when the text is not a calendar (an error page must never cancel bookings)."""
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("That link did not give a calendar")
    text = re.sub(r"\r?\n[ \t]", "", text)   # unfold long lines
    out = []
    for ev in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        f = dict(re.findall(r"^([A-Z-]+)(?:;[^:\n]*)?:(.*)$", ev, re.M))
        s, e = f.get("DTSTART", "")[:8], f.get("DTEND", "")[:8]
        if not (s.isdigit() and e.isdigit() and len(s) == 8 and len(e) == 8):
            continue
        summary = f.get("SUMMARY", "").strip().lower()
        if "not available" in summary or "blocked" in summary or "unavailable" in summary:
            continue
        try:
            a, d = dt.date(int(s[:4]), int(s[4:6]), int(s[6:])), dt.date(int(e[:4]), int(e[4:6]), int(e[6:]))
        except ValueError:
            continue
        if d <= a:
            continue
        code = CODE.search(f.get("DESCRIPTION", "") + " " + f.get("SUMMARY", ""))
        uid = f.get("UID", "").strip()[:200] or "%s:%s" % (a.isoformat(), d.isoformat())
        out.append({"uid": uid, "arrive": a.isoformat(), "depart": d.isoformat(), "code": code.group(0) if code else ""})
    return out


def fetch(url):
    if not re.match(r"https?://", url or ""):
        raise ValueError("The calendar link must start with https://")
    req = urllib.request.Request(url, headers={"User-Agent": "guest-tv"})
    return urllib.request.urlopen(req, timeout=15).read(2_000_000).decode("utf-8", "replace")


def sync_home(home, url, fetcher=fetch):
    """Bring one apartment's bookings in line with its Airbnb calendar. Returns the status that is
    also saved for the dashboard. A failed fetch changes nothing."""
    st = {"home": home, "at": stamp(), "ok": 0, "message": "", "seen": 0, "added": 0, "changed": 0, "cancelled": 0}
    try:
        events = parse_ical(fetcher(url))
    except Exception as e:
        st["message"] = "Could not read the calendar: %s" % (str(e)[:120] or type(e).__name__)
        _save_sync(st)
        return st
    today = now().date().isoformat()
    st["seen"] = len(events)
    mine = store.rows("SELECT * FROM bookings WHERE home = ?", (home,))
    seen_ids, clashes = set(), 0
    for ev in events:
        b = (next((b for b in mine if b["uid"] and b["uid"] == ev["uid"]), None)
             or next((b for b in mine if ev["code"] and b["code"] == ev["code"]), None)
             or next((b for b in mine if b["status"] == "confirmed" and b["arrive"] == ev["arrive"] and b["depart"] == ev["depart"]), None))
        if b:
            seen_ids.add(b["id"])
            moved = (b["arrive"], b["depart"]) != (ev["arrive"], ev["depart"])
            back = b["status"] == "cancelled" and b["why"].startswith("Gone from")
            if moved or back or b["uid"] != ev["uid"] or (ev["code"] and not b["code"]):
                try:
                    if moved or back:
                        _check(home, ev["arrive"], ev["depart"], skip_id=b["id"])
                except Invalid:
                    clashes += 1
                    continue
                store.run("UPDATE bookings SET arrive=?, depart=?, uid=?, code=?, status=?, why=?, revoked=?, updated=? WHERE id=?",
                          (ev["arrive"], ev["depart"], ev["uid"], ev["code"] or b["code"],
                           "confirmed" if back else b["status"], "" if back else b["why"], 0 if back else b["revoked"], stamp(), b["id"]))
                st["changed"] += int(moved or back)
            continue
        if ev["depart"] <= today:
            continue   # a stay that is already over: nothing to show for it
        try:
            nb = add(home, "", ev["arrive"], ev["depart"], source="airbnb", uid=ev["uid"], code=ev["code"])
            seen_ids.add(nb["id"])
            st["added"] += 1
        except Invalid:
            clashes += 1
    for b in mine:   # Airbnb drops a cancelled reservation from the link
        if b["source"] == "airbnb" and b["status"] == "confirmed" and b["id"] not in seen_ids and b["depart"] > today:
            cancel(b["id"], "Gone from the Airbnb calendar")
            st["cancelled"] += 1
    st["ok"] = 1
    st["message"] = "In step with Airbnb" + (". %d calendar booking%s overlap a booking typed here: check the dates" % (clashes, "" if clashes == 1 else "s") if clashes else "")
    _save_sync(st)
    return st


def _save_sync(st):
    store.run("INSERT INTO sync (home, at, ok, message, seen, added, changed, cancelled) VALUES (?,?,?,?,?,?,?,?) "
              "ON CONFLICT(home) DO UPDATE SET at=excluded.at, ok=excluded.ok, message=excluded.message, seen=excluded.seen, "
              "added=excluded.added, changed=excluded.changed, cancelled=excluded.cancelled",
              (st["home"], st["at"], st["ok"], st["message"], st["seen"], st["added"], st["changed"], st["cancelled"]))


def sync_status(home=None):
    if home:
        return store.row("SELECT * FROM sync WHERE home = ?", (home,))
    return {r["home"]: r for r in store.rows("SELECT * FROM sync")}


# ---------------------------------------------------------------- names from booking emails (optional)

def apply_emails(found):
    """found: bookings.collect()'s list, oldest first. A confirmation gives an Airbnb booking its
    guest's first name (never over a name staff typed); a cancellation cancels it. Matched by the
    reservation code, else by the arrival day when exactly one unnamed Airbnb booking arrives then."""
    named = cancelled = unmatched = 0
    for e in found:
        rows = store.rows("SELECT * FROM bookings WHERE source = 'airbnb'")
        hit = [b for b in rows if e.get("code") and b["code"] == e["code"]]
        if not hit and e.get("arrives"):
            hit = [b for b in rows if b["status"] == "confirmed" and not b["code"]
                   and (int(b["arrive"][5:7]), int(b["arrive"][8:10])) == tuple(e["arrives"])]
        if len(hit) != 1:
            unmatched += 1
            continue
        b = hit[0]
        if e["kind"] == "cancelled":
            if b["status"] == "confirmed":
                cancel(b["id"], "Airbnb cancellation email")
                cancelled += 1
        elif e.get("guest") and b["name_from"] != "staff":
            name = clean_name(e["guest"])
            if name != b["guest"] or (e.get("guests") or 0) != b["guests"]:
                store.run("UPDATE bookings SET guest=?, guests=?, name_from='email', code=?, updated=? WHERE id=?",
                          (name, _int(e.get("guests")), e.get("code") or b["code"], stamp(), b["id"]))
                named += 1
    return {"named": named, "cancelled": cancelled, "unmatched": unmatched}
