#!/usr/bin/env python3
"""guest-tv server: the TV page, the phone page and the little API behind them.

    .venv/bin/python server.py            # http://<your-computer>.local:5410/tv/?home=lake

Homes live in data/homes/<id>/ (home.json + guide.json). Private values (the real Wi-Fi password,
door codes, the guest's name, the aircon address) live in data/private.json, which never leaves this
computer. The couch-tv TV app can show this page: point its bridge at this server.
"""
import asyncio, datetime as dt, hashlib, hmac, ipaddress, json, os, re, secrets, socket, sys, threading, time, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import access, content, stays, store, tvlink

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
DATA = store.DATA   # data/, or GUEST_TV_DATA (demo mode, tests)
TZ = dt.timezone(dt.timedelta(hours=11))   # replaced from the weather answer (utc_offset_seconds), unless "timezone" is set


def jread(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jwrite(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def private():
    p = os.path.join(DATA, "private.json")
    pv = jread(p)
    if pv is None:   # first run: make one with a fresh host key
        pv = (jread(os.path.join(DATA, "private.example.json"), {}) or {}) if not settings().get("managed") else {}
        pv["host_key"] = secrets.token_urlsafe(9)
        jwrite(p, pv)
    return pv


def settings():
    """settings.json, with what the host changed in the dashboard (data/property.json) laid over it."""
    base = jread(os.path.join(DATA, "settings.json")) or jread(os.path.join(HERE, "data", "settings.json"), {}) or {}
    return merge(base, store.prop())


def merge(a, b):
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def home_ids():
    d = os.path.join(DATA, "homes")
    return sorted(x for x in os.listdir(d) if os.path.isfile(os.path.join(d, x, "home.json")))


def load_home(hid):
    if hid not in home_ids():
        return None
    base = jread(os.path.join(DATA, "homes", hid, "home.json"), {})
    h = merge(base, private().get("homes", {}).get(hid, {}))
    h["guide"] = jread(os.path.join(DATA, "homes", hid, "guide.json"), {}) or {}
    if h.get("managed"):   # an apartment run from the host dashboard: the property's reception is the fallback
        s = settings()
        rc = {k: v for k, v in (h.get("reception") or {}).items() if v}
        h["reception"] = merge(s.get("reception") or {}, rc)
        h["city"] = (s.get("weather") or {}).get("place", "")
        h.setdefault("id", hid)
    film = settings().get("film") and jread(os.path.join(DATA, "homes", hid, "film.json"))
    if film:   # film mode: demo host, Wi-Fi and picks over the real ones, so nothing private is on camera
        h = merge(h, {k: v for k, v in film.items() if not k.startswith("_")})
    return h


def now():
    return stays.CLOCK() if stays.CLOCK else dt.datetime.now(TZ)


def at(day, hhmm):
    hh, mm = (int(x) for x in (hhmm or "10:00").split(":"))
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


# ---------------------------------------------------------------- the stay

ical_cache = {}


def ical_stays(url):
    """Airbnb's calendar link: one VEVENT per booking, DTSTART/DTEND as dates, SUMMARY "Reserved"."""
    c = ical_cache.get(url)
    if c and time.time() - c[0] < 900:
        return c[1]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "guest-tv"})
        text = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace")
    except Exception:
        return c[1] if c else []
    text = re.sub(r"\r?\n[ \t]", "", text)   # unfold long lines
    out = []
    for ev in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        f = dict(re.findall(r"^([A-Z-]+)(?:;[^:\n]*)?:(.*)$", ev, re.M))
        s, e = f.get("DTSTART", "")[:8], f.get("DTEND", "")[:8]
        if not (s.isdigit() and e.isdigit()):
            continue
        summary = f.get("SUMMARY", "").strip()
        if "not available" in summary.lower() or "blocked" in summary.lower():
            continue   # the host's own blocked days, not a guest
        code = re.search(r"\bHM[A-Z0-9]{6,10}\b", f.get("DESCRIPTION", ""))   # in the reservation link
        out.append({"from": dt.date(int(s[:4]), int(s[4:6]), int(s[6:])),
                    "to": dt.date(int(e[:4]), int(e[4:6]), int(e[6:])), "code": code.group(0) if code else ""})
    ical_cache[url] = (time.time(), out)
    return out


def managed_stay(h):
    return stays.current(h["id"], h.get("checkin"), h.get("checkout"), now()) or {"phase": "between", "guest": "", "active": False}


def stay_of(h):
    """Where we are in a stay: before, arrival (today, before check-in), during, lastday, between."""
    if h.get("managed"):   # bookings from the host dashboard and the Airbnb calendar sync (stays.py)
        return managed_stay(h)
    t = now()
    today = t.date()
    stays = []
    if h.get("demo") and h.get("demo_stay"):
        d = h["demo_stay"]
        f = today + dt.timedelta(days=d.get("from", 0))
        stays = [{"from": f, "to": f + dt.timedelta(days=d.get("nights", 2)), "guest": d.get("guest", "")}]
    elif h.get("calendar_url"):
        names = h.get("guest_names", {})   # {"2026-11-02": "Sarah"}, typed by the host (wins)
        auto = h.get("booking_names", {})  # {"2026-11-02": {"guest": "Sarah", "guests": 2}}, read from the booking email
        stays = [dict(s, guest=names.get(k) or auto.get(k, {}).get("guest", ""), guests=auto.get(k, {}).get("guests", 0))
                 for s in ical_stays(h["calendar_url"]) for k in [s["from"].isoformat()]]
    elif (h.get("stay") or {}).get("from"):
        s = h["stay"]
        try:
            stays = [{"from": dt.date.fromisoformat(s["from"]), "to": dt.date.fromisoformat(s["to"]), "guest": s.get("guest", "")}]
        except (ValueError, KeyError):
            stays = []
    elif (h.get("stay") or {}).get("guest"):
        return {"phase": "open", "guest": h["stay"]["guest"], "active": True}

    for s in sorted(stays, key=lambda s: s["from"]):
        cin, cout = at(s["from"], h.get("checkin")), at(s["to"], h.get("checkout"))
        if s["from"] - dt.timedelta(days=1) <= today and t < cout:
            nights = max(1, (s["to"] - s["from"]).days)
            night = min(nights, max(1, (today - s["from"]).days + 1))
            phase = ("arrival" if t < cin and today == s["from"] else "before" if t < cin
                     else "lastday" if today == s["to"] else "during")
            return {"phase": phase, "guest": s.get("guest", ""), "guests": s.get("guests", 0), "active": phase in ("arrival", "during", "lastday"),
                    "from": s["from"].isoformat(), "to": s["to"].isoformat(), "checkin": cin.isoformat(),
                    "checkout": cout.isoformat(), "nights": nights, "night": night}
    if h.get("mode") == "guest":
        return {"phase": "open", "guest": "", "active": True}
    return {"phase": "between", "guest": "", "active": False}


booking_state = {"checked": None, "found": 0, "matched": {}, "errors": []}   # shown to the host at /api/bookings


def read_bookings():
    """Booking emails -> guest names for each home with an Airbnb calendar link (bookings.py)."""
    import bookings
    pv = private()
    found, errors = bookings.collect(settings(), pv)
    changed = False
    for hid in home_ids():
        h = load_home(hid)
        if not h.get("calendar_url") or h.get("demo"):
            continue
        names = bookings.match(found, ical_stays(h["calendar_url"]))
        booking_state["matched"][hid] = names
        hp = pv.setdefault("homes", {}).setdefault(hid, {})
        if hp.get("booking_names") != names:
            hp["booking_names"] = names
            changed = True
    if changed:
        jwrite(os.path.join(DATA, "private.json"), pv)
    booking_state.update(checked=now().isoformat(timespec="minutes"), found=len(found), errors=errors)


def booking_loop():
    while True:
        try:
            read_bookings()
        except Exception as e:
            booking_state["errors"] = [str(e)]
        time.sleep(60 * int((settings().get("bookings") or {}).get("every_min", 15)))


# ---------------------------------------------------------------- weather (Open-Meteo, free, no key)

wx_cache, wx_fail = {}, {}


def sample_weather(w):
    """No forecast could be fetched. Normally the screens then simply show no weather. In demo mode
    only, a made-up forecast stands in, and the place is marked "(sample)" so nobody reads it as real."""
    if not demo_mode():
        return {"place": w["place"], "ok": False}
    today = now().date()
    codes, hi, lo = [1, 2, 0, 3, 61, 1, 0], [24, 25, 26, 23, 21, 24, 25], [11, 12, 12, 13, 12, 10, 11]
    return {"ok": True, "sample": True, "place": w["place"] + " (sample)", "now": {"temp": 22, "code": 1, "day": 1},
            "days": [{"date": (today + dt.timedelta(days=i)).isoformat(), "code": codes[i], "hi": hi[i], "lo": lo[i], "rain": 60 if codes[i] > 50 else 5,
                      "sunrise": "06:30", "sunset": "17:05"} for i in range(7)]}


def weather(days=7):
    global TZ
    w = settings().get("weather") or {"place": "Byron Bay", "lat": -28.6474, "lon": 153.602, "tz": "Australia/Sydney"}
    key = (w["lat"], w["lon"])
    c = wx_cache.get(key)
    if c and time.time() - c[0] < 1800:
        return c[1]
    q = urllib.parse.urlencode({
        "latitude": w["lat"], "longitude": w["lon"], "timezone": w.get("tz", "auto"), "forecast_days": days,
        "current": "temperature_2m,weather_code,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"})
    if wx_fail.get(key, 0) > time.time():   # it failed a moment ago: do not make every screen wait again
        return c[1] if c else sample_weather(w)
    try:
        d = json.loads(urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + q, timeout=10).read())
    except Exception:
        wx_fail[key] = time.time() + 300
        return c[1] if c else sample_weather(w)
    if not settings().get("timezone"):
        TZ = dt.timezone(dt.timedelta(seconds=d.get("utc_offset_seconds", 39600)))
    dl = d["daily"]
    out = {"ok": True, "place": w["place"],
           "now": {"temp": round(d["current"]["temperature_2m"]), "code": d["current"]["weather_code"], "day": d["current"]["is_day"]},
           "days": [{"date": dl["time"][i], "code": dl["weather_code"][i], "hi": round(dl["temperature_2m_max"][i]),
                     "lo": round(dl["temperature_2m_min"][i]), "rain": dl["precipitation_probability_max"][i],
                     "sunrise": dl["sunrise"][i][-5:], "sunset": dl["sunset"][i][-5:]} for i in range(len(dl["time"]))]}
    wx_cache[key] = (time.time(), out)
    return out


# ---------------------------------------------------------------- aircon

sim = {}            # demo homes: a pretend system per home, so the screens can be tried safely
sim_lock = threading.Lock()


def sim_state(h):
    with sim_lock:
        s = sim.get(h["id"])
        if not s:
            names = (h.get("climate") or {}).get("zones", [])
            s = {"connected": True, "simulated": True, "power": "off", "mode": "cool", "can_mode": False,
                 "zones": [{"id": i, "name": n, "on": False, "temp": 24.5 - i * 0.4, "setpoint": 22, "can_set": True}
                           for i, n in enumerate(names)], "at": time.time()}
            sim[h["id"]] = s
        # rooms drift toward their setting while on, back toward 25 while off
        dtm = min(600, time.time() - s["at"]) / 60
        for z in s["zones"]:
            goal = z["setpoint"] if (z["on"] and s["power"] == "on") else 25.0
            z["temp"] = round(z["temp"] + (goal - z["temp"]) * min(1, 0.08 * dtm), 1)
        s["at"] = time.time()
        return json.loads(json.dumps(s))


def sim_set(h, zone, on=None, setpoint=None):
    sim_state(h)
    with sim_lock:
        s = sim[h["id"]]
        z = next((z for z in s["zones"] if z["id"] == zone), None)
        if z is None:
            return {"ok": False, "error": "no such room"}
        if setpoint is not None:
            z["setpoint"] = max(16, min(30, round(float(setpoint))))
        if on is not None:
            z["on"] = bool(on)
            if on and s["power"] == "off" and (h.get("climate") or {}).get("unit_on", True):
                s["power"] = "on"
            if not any(x["on"] for x in s["zones"]):
                s["power"] = "off"
    return {"ok": True}


ac_lock = threading.Lock()
ac_cache = {"at": 0, "data": None}
pending = {}   # preview mode: what the guest asked for, shown over the real reading


def ac_ip(cfg, new=None):
    """The console's address: settings, then the one we found last time, then the other bridge's file."""
    found = os.path.join(DATA, "aircon-ip.json")
    if new:
        jwrite(found, {"ip": new})
        return new
    if cfg.get("ip"):
        return cfg["ip"]
    ip = (jread(found, {}) or {}).get("ip")
    f = cfg.get("ip_file")
    return ip or ((jread(os.path.join(HERE, f), {}) or {}).get("ip") if f else None)


async def _discover():
    from airtouch5py.discovery import AirtouchDiscovery
    d = AirtouchDiscovery()
    await d.establish_server()
    try:
        found = await asyncio.wait_for(d.discover(), 6)
    finally:
        await d.close()
    return found[0].ip if found else None


def _name(e):
    return str(e).split(".")[-1].lower()


async def _at_session(ip, action=None):
    from airtouch5py.airtouch5_simple_client import Airtouch5SimpleClient
    c = Airtouch5SimpleClient(ip)
    try:
        await asyncio.wait_for(c.connect_and_stay_connected(), 12)
        if action:
            await action(c)
            await asyncio.sleep(1.5)   # the console answers a change with a fresh status
        st = c.latest_ac_status.get(0) or next(iter(c.latest_ac_status.values()))
        names = {z.zone_number: z.zone_name for z in c.zones}
        zones = []
        for n, z in sorted(c.latest_zone_status.items()):
            if not names.get(n) or names[n].lower().startswith("zone"):
                continue
            zones.append({"id": n, "name": names[n], "on": _name(z.zone_power_state) in ("on", "turbo"),
                          "temp": round(z.temperature, 1) if isinstance(z.temperature, (int, float)) else None,
                          "setpoint": z.set_point if isinstance(z.set_point, (int, float)) else None,
                          "can_set": _name(z.control_method) == "temperature_control" and z.set_point is not None})
        return {"connected": True, "power": "on" if _name(st.ac_power_state) in ("on", "away_on") else "off",
                "mode": _name(st.ac_mode).replace("auto_", ""), "unit_setpoint": st.ac_setpoint, "zones": zones}
    finally:
        try:
            await c.disconnect()
        except Exception:
            pass


def real_state(cfg, fresh=False):
    with ac_lock:
        if not fresh and ac_cache["data"] and time.time() - ac_cache["at"] < 30:
            return json.loads(json.dumps(ac_cache["data"]))
        ip = ac_ip(cfg)
        try:
            try:
                if not ip:
                    raise OSError("no address yet")
                data = asyncio.run(_at_session(ip))
            except Exception:
                ip = asyncio.run(_discover())   # the console's address moves when the router hands out a new one
                if not ip:
                    raise
                ac_ip(cfg, ip)
                data = asyncio.run(_at_session(ip))
            ac_cache.update(at=time.time(), data=data)
        except Exception as e:
            if ac_cache["data"] and time.time() - ac_cache["at"] < 600:
                return json.loads(json.dumps(ac_cache["data"]))
            return {"connected": False, "error": type(e).__name__}
        return json.loads(json.dumps(data))


def real_set(h, cfg, zone, on=None, setpoint=None):
    from airtouch5py.packets.zone_control import ZoneControlZone, ZoneSettingValue, ZoneSettingPower
    from airtouch5py.packets.ac_control import AcControl, SetPowerSetting, SetAcMode, SetAcFanSpeed, SetpointControl
    st = real_state(cfg)
    if not st.get("connected"):
        return {"ok": False, "error": "the aircon is not answering"}
    z = next((z for z in st["zones"] if z["id"] == zone), None)
    if z is None:
        return {"ok": False, "error": "no such room"}
    unit_on = bool(on) and st["power"] == "off" and (h.get("climate") or {}).get("unit_on", True)
    if not cfg.get("write"):
        # Preview: nothing is sent to the aircon. The screen shows what would happen.
        p = pending.setdefault(zone, {})
        pending["at"] = time.time()
        if on is not None:
            p["on"] = bool(on)
        if setpoint is not None and z["can_set"]:
            p["setpoint"] = max(16, min(30, round(float(setpoint))))
        if unit_on:
            pending["unit"] = "on"
        return {"ok": True, "preview": True}

    async def act(c):
        packets = []
        if setpoint is not None and z["can_set"]:
            packets.append(c.data_packet_factory.zone_control([ZoneControlZone(
                zone, ZoneSettingValue.SET_TARGET_SETPOINT, ZoneSettingPower.KEEP_POWER_STATE, max(16, min(30, round(float(setpoint)))))]))
        if on is not None:
            packets.append(c.data_packet_factory.zone_control([ZoneControlZone(
                zone, ZoneSettingValue.KEEP_SETTING_VALUE, ZoneSettingPower.SET_TO_ON if on else ZoneSettingPower.SET_TO_OFF, 0)]))
        if unit_on:   # the house's own mode and fan stay as they are
            packets.append(c.data_packet_factory.ac_control([AcControl(
                SetPowerSetting.SET_TO_ON, 0, SetAcMode.KEEP_AC_MODE, SetAcFanSpeed.KEEP_AC_FAN_SPEED,
                SetpointControl.KEEP_SETPOINT_VALUE, 0)]))
        for p in packets:
            await c.send_packet(p)
            await asyncio.sleep(0.4)

    with ac_lock:
        try:
            data = asyncio.run(_at_session(ac_ip(cfg), act))
            ac_cache.update(at=time.time(), data=data)
        except Exception as e:
            return {"ok": False, "error": type(e).__name__}
    return {"ok": True}


def aircon_for(h):
    cfg = settings().get("aircon") or {}
    clim = h.get("climate") or {}
    if h.get("demo") or not cfg.get("kind"):
        return sim_state(h)
    st = real_state(cfg)
    if not st.get("connected"):
        return st
    allowed = [n.lower() for n in clim.get("zones", [])]
    zones = [z for z in st["zones"] if not allowed or z["name"].lower() in allowed]
    if pending and time.time() - pending.get("at", 0) > 120:
        pending.clear()   # a preview change is shown for two minutes, then the real reading comes back
    for z in zones:
        z.update(pending.get(z["id"], {}))
    if pending.get("unit") == "on" and any(z["on"] for z in zones):
        st["power"] = "on"
    labels = clim.get("labels", {})   # "Bed 2" can show as "Your room"
    for z in zones:
        z["label"] = labels.get(z["name"], z["name"])
    st["zones"] = zones
    st["preview"] = not cfg.get("write")
    st["can_mode"] = False
    return st


def aircon_set(h, body):
    try:
        zone = int(body.get("zone"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "which room?"}
    on, sp = body.get("on"), body.get("setpoint")
    cfg = settings().get("aircon") or {}
    if h.get("demo") or not cfg.get("kind"):
        return sim_set(h, zone, on, sp)
    allowed = [n.lower() for n in (h.get("climate") or {}).get("zones", [])]
    st = real_state(cfg)
    z = next((z for z in st.get("zones", []) if z["id"] == zone), None)
    if not z or (allowed and z["name"].lower() not in allowed):
        return {"ok": False, "error": "that room is not yours to change"}
    return real_set(h, cfg, zone, on, sp)


# ---------------------------------------------------------------- what the screens get

def preview_stay(h, phase):
    """A pretend stay for looking at one moment of a stay on a managed apartment's screen (the host's
    preview and demo mode only). It is never a real booking: no guest link comes with it."""
    today, cin, cout = now().date(), h.get("checkin"), h.get("checkout")
    if phase == "between":
        return {"phase": "between", "guest": "", "active": False, "preview": True}
    a = today if phase == "arrival" else today - dt.timedelta(days=3 if phase == "lastday" else 1)
    d = a + dt.timedelta(days=3)
    return {"phase": phase, "guest": "Ayesha", "guests": 2, "active": True, "preview": True, "from": a.isoformat(), "to": d.isoformat(),
            "checkin": at(a, cin).isoformat(), "checkout": at(d, cout).isoformat(), "nights": 3,
            "night": 3 if phase == "lastday" else 1 if phase == "arrival" else 2}


def public_state(h, for_host=False, phase=None, tv=None):
    s = stay_of(h)
    if h.get("demo") and phase in ("arrival", "during", "lastday", "between"):   # demo homes: try each moment of a stay
        s.update(phase=phase, active=phase != "between")
        if phase == "lastday":
            s["night"] = s.get("nights", 1)
    elif h.get("managed") and phase in ("arrival", "during", "lastday", "between") and (for_host or demo_mode()):
        s = preview_stay(h, phase)
    s = {k: v for k, v in s.items() if k != "booking"}
    show_private = s["active"] or h.get("mode") == "guest" or for_host
    g = h.get("guide", {})
    guest_room = h.get("mode") == "guest"   # family and friends: no house rules, no check-out
    return {
        "id": h["id"], "mode": h.get("mode", "stay"), "demo": bool(h.get("demo")), "name": h.get("name"),
        "kind": h.get("kind"), "host": h.get("host"), "photos": h.get("photos", {}), "scene": h.get("scene", {}),
        "stay": s, "checkin": h.get("checkin"), "checkout_time": h.get("checkout"),
        "wifi": h.get("wifi") if show_private else None,
        "access": h.get("access", []) if show_private else [],
        "welcome": g.get("welcome", ""), "rules": [] if guest_room else g.get("rules", []), "howto": g.get("howto", []),
        "local": g.get("local", []), "checkout": {} if guest_room else g.get("checkout", {}), "emergency": g.get("emergency", []),
        "notes": h.get("notes", []), "phone_url": phone_url(h["id"]),
        "remote_url": remote_url(h, tv) if show_private and not s.get("preview") else None,
        "homes": [{"id": i, "name": (jread(os.path.join(DATA, "homes", i, "home.json"), {}) or {}).get("name", i)} for i in home_ids()],
        # the managed apartments (host dashboard): reception, the property's clock, and which TV this screen is
        "managed": bool(h.get("managed")), "reception": h.get("reception") or {}, "property": settings().get("property", ""),
        "tz_offset_min": int(now().utcoffset().total_seconds() // 60), "demo_mode": demo_mode(),
        "clock_fixed": now().strftime("%H:%M") if stays.CLOCK else None,
        "screen": {"id": tv["id"], "name": tv["name"], "simulated": not tvlink.is_live(tv)} if tv else None,
        "sample_photos": str((h.get("photos") or {}).get("hero", "")).startswith("sample-"),
    }


def phone_url(hid):
    base = settings().get("public_url") or "http://%s:%d" % (local_name(), PORT)
    return "%s/p/?home=%s" % (base.rstrip("/"), hid)


def lan_ip():
    """This computer's address on the home network (phones and TVs reach it by number, not .local)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))   # nothing is sent; this only picks the outgoing interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return local_name()


# ---------------------------------------------------------------- the Guest remote

def remote_code(h):
    """The code in the Guest remote link. Guest room: stays until the host resets it. Stay homes:
    one per booking, and none between stays, so a past guest's link stops at check-out."""
    if h.get("managed"):   # one random token per booking, revocable by staff (access.py)
        return access.live_token(h["id"], h.get("checkin"), h.get("checkout"), now())
    if h.get("mode") == "guest":
        seed = "%s:room:%s" % (h["id"], h.get("remote_epoch", 0))
    else:
        s = stay_of(h)
        if not s.get("active"):
            return None
        seed = "%s:stay:%s" % (h["id"], s.get("from", ""))
    return hmac.new(private().get("host_key", "-").encode(), seed.encode(), hashlib.sha256).hexdigest()[:12]


def remote_url(h, tv=None):
    code = remote_code(h)
    if code and h.get("managed"):
        return remote_link(h["id"], code, tv["id"] if tv else None)
    base = settings().get("public_url") or "http://%s:%d" % (lan_ip(), PORT)
    return "%s/r/?home=%s&c=%s" % (base.rstrip("/"), h["id"], code) if code else None


def demo_mode():
    return os.environ.get("GUEST_TV_DEMO") == "1"


def base_url():
    """This computer as phones and TVs on the Wi-Fi reach it."""
    return (settings().get("public_url") or "http://%s:%d" % (lan_ip(), PORT)).rstrip("/")


def remote_link(hid, token, tv_id=None):
    return "%s/r/?home=%s&c=%s%s" % (base_url(), urllib.parse.quote(hid), urllib.parse.quote(token),
                                     "&tv=" + urllib.parse.quote(tv_id) if tv_id else "")


def screen_url(tv_id):
    """The address a TV shows for its welcome screen: it names the TV, never a key or a guest link."""
    return "%s/tv/?screen=%s" % ((settings().get("tv_url") or base_url()).rstrip("/"), urllib.parse.quote(tv_id))


# ---------------------------------------------------------------- managed apartments: sync and emails

email_state = {"checked": None, "found": 0, "result": {}, "errors": [], "on": False}


def sync_now(hid):
    """Bring one apartment in line with its Airbnb calendar link now (when it has one)."""
    url = ((store.private().get("homes") or {}).get(hid) or {}).get("ical_url")
    if not url:
        store.run("DELETE FROM sync WHERE home = ?", (hid,))
        return None
    return stays.sync_home(hid, url)


def read_emails():
    """Optional: Airbnb confirmation and cancellation emails give synced bookings their guest's first
    name (bookings.py reads data/bookings-inbox/*.eml and, when set up, the mailbox)."""
    import bookings
    found, errors = bookings.collect(settings(), store.private())
    email_state.update(checked=now().isoformat(timespec="minutes"), found=len(found), errors=errors,
                       result=stays.apply_emails(found) if found else {},
                       on=bool((settings().get("bookings") or {}).get("imap")) or bool(found))


def managed_loop():
    """Every few minutes: the Airbnb calendars, the booking emails, and forgetting old guest names."""
    while True:
        try:
            for hid in content.ids():
                sync_now(hid)
            read_emails()
            stays.forget_old(int(settings().get("forget_guest_names_after_days", 30)))
        except Exception as e:
            email_state["errors"] = [str(e)[:200]]
        time.sleep(60 * max(5, int(settings().get("sync_every_min", 15))))


def managed_remote(h, tv):
    """What a managed apartment's Guest remote shows: this stay, this apartment's guide and places,
    and the one TV it may press buttons on. Only ever sent with this booking's own link."""
    s, g = stay_of(h), h.get("guide", {})
    st = tvlink.status(tv)
    apps = []
    if tv and st["mode"] != "offline":
        try:
            apps = guest_apps(h, tvlink.driver(tv))
        except Exception:
            apps = []
    import concierge
    return {"ok": True, "managed": True, "home": h["id"], "mode": "stay", "place": h.get("name"), "property": h.get("kind"),
            "guest": s.get("guest", ""), "until": s.get("checkout"),
            "tv": {"id": tv["id"], "name": tv["name"], "status": st} if tv else {"status": st},
            "tvs": [{"id": t["id"], "name": t["name"]} for t in tvlink.listing(h["id"])],
            "tv_on": st["mode"] in ("connected", "simulated"), "apps": [{"id": a["id"], "name": a["name"]} for a in apps],
            "rooms": [], "pool": None, "speakers": [], "places": places(h), "level": "locked", "ask_limit": concierge.ASKS_PER_HOUR,
            "concierge": {"ai": concierge.status(settings())["ai"]},
            "stay": {"from": s.get("from"), "to": s.get("to"), "night": s.get("night"), "nights": s.get("nights"),
                     "checkin": h.get("checkin"), "checkout": h.get("checkout"), "wifi": h.get("wifi"), "access": h.get("access", []),
                     "rules": g.get("rules", []), "howto": g.get("howto", []), "checkout_steps": (g.get("checkout") or {}).get("steps", []),
                     "emergency": g.get("emergency", []), "reception": h.get("reception") or {}, "local": g.get("local", []),
                     "welcome": g.get("welcome", "")}}


def tv_ip(h):
    return os.environ.get("GUEST_TV_IP") or h.get("tv") or settings().get("tv") or (settings().get("profiles") or {}).get("tv")


def tv_kind(h):
    """"android" (Google TV, Android TV and Fire OS: ADB), or one of tvbrands.KINDS: "samsung", "lg",
    "roku", "vidaa", "titan", "saphi", "panasonic", "vizio", "appletv", "vega"."""
    return os.environ.get("GUEST_TV_KIND") or h.get("tv_kind") or settings().get("tv_kind") or "android"


def tv_page(hid, kind):
    """guest-tv's TV page as a TV reaches it: by number, never a key in it (couch-tv#110)."""
    base = (settings().get("tv_url") or "http://%s:%d" % (lan_ip(), PORT)).rstrip("/")
    return "%s/tv/?tv=%s&brand=%s" % (base, urllib.parse.quote(hid), urllib.parse.quote(kind))


def start_tv_watchers():
    """Every TV that is not Google TV, Android TV or Fire OS: open guest-tv each time the TV comes on
    (tvbrands.watch). One watcher per TV. "tv_hook": false (per home or in settings.json) leaves the
    TV alone. A TV that cannot show guest-tv (VIZIO) gets no watcher, only the Guest remote."""
    import tvbrands, tvctl
    seen = set()
    for hid in home_ids():
        h = load_home(hid)
        ip, kind = tv_ip(h), tv_kind(h)
        if kind in tvbrands.KINDS and ip and (kind, ip) not in seen and h.get("tv_hook", settings().get("tv_hook", True)) is not False:
            seen.add((kind, ip))
            tv = tvctl.tv_for(ip, kind)
            tv.page = tv_page(hid, kind)
            threading.Thread(target=tvbrands.watch, args=(tv,), daemon=True).start()
    for t in tvlink.listing():   # managed apartments: only TVs that passed their check and were switched to real commands
        if tvlink.is_live(t) and t["kind"] in tvbrands.KINDS and (t["kind"], t["ip"]) not in seen and settings().get("tv_hook", True) is not False:
            seen.add((t["kind"], t["ip"]))
            tv = tvctl.tv_for(t["ip"], t["kind"])
            tv.page = "%s&brand=%s" % (screen_url(t["id"]), urllib.parse.quote(t["kind"]))
            threading.Thread(target=tvbrands.watch, args=(tv,), daemon=True).start()


# ---------------------------------------------------------------- wrong codes (from couch-tv#73)

WRONG, WRONG_LOCK = {}, threading.Lock()   # address -> (window start, set of wrong values tried)
WRONG_MAX, WRONG_WINDOW, WRONG_ADDRESSES = 5, 60.0, 1000


def _loopback(addr):
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def wrong_limited(addr):
    """Five different wrong remote codes or host keys from one address in a minute, then 429 for the
    rest of that minute. This computer itself is never limited; X-Forwarded-For is never trusted."""
    if _loopback(addr):
        return False
    with WRONG_LOCK:
        w = WRONG.get(addr)
        return bool(w) and time.monotonic() - w[0] < WRONG_WINDOW and len(w[1]) >= WRONG_MAX


def note_wrong(addr, value):
    if _loopback(addr):
        return
    with WRONG_LOCK:
        t = time.monotonic()
        w = WRONG.get(addr)
        if not w or t - w[0] >= WRONG_WINDOW:
            if len(WRONG) >= WRONG_ADDRESSES:   # memory stays small: drop the oldest
                WRONG.pop(min(WRONG, key=lambda a: WRONG[a][0]), None)
            w = WRONG[addr] = (t, set())
        w[1].add(hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16])   # the same old code twice counts once


def host_allowed(host):
    """Only this computer's own names may be in the Host header: any IP address, .local names,
    localhost, the host of public_url and tv_url, and "hosts" in settings.json. Anything else is a
    page on some other site reaching this computer through DNS rebinding, so it gets 403 (couch-tv#73).
    A request with no Host passes."""
    if not host:
        return True
    host = host.strip().lower()
    name = host[1:host.find("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.rstrip(".")
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name == "localhost" or name.endswith(".local"):
        return True
    own = {socket.gethostname().lower(), local_name().lower()}
    for u in (settings().get("public_url"), settings().get("tv_url")):
        if u:
            own.add((urllib.parse.urlparse(u).hostname or "").lower())
    own.update(str(x).lower() for x in settings().get("hosts") or [])
    return name in own


def control(h, what):
    """Which rooms a guest may control for one kind of device: "their room" (default), "all", a list
    of room names, or "none". Set per home or in settings.json as "control": {"aircon": ..., "lights": ...}."""
    return (h.get("control") or settings().get("control") or {}).get(what, "their room")


def _pick(names, scope, mine):
    if scope in ("none", False, None):
        return []
    if scope == "all":
        return list(names)
    if isinstance(scope, list):
        low = [str(s).lower() for s in scope]
        return [n for n in names if n.lower() in low]
    if mine:
        return [n for n in names if n.lower() == mine.lower()]
    return list(names)[:1]


def remote_rooms(h):
    """The rooms the guest's remote and agent may change, each with its aircon and lights (or None)."""
    import devices
    clim, rooms = h.get("climate") or {}, {}
    ac = aircon_for(h)
    zones = [z for z in (ac.get("zones") or []) if z.get("can_set", True)] if ac.get("connected") else []
    mine = h.get("room") or (zones[0]["name"] if zones else None)   # the guest's own room
    for name in _pick([z["name"] for z in zones], control(h, "aircon"), mine):
        z = next(z for z in zones if z["name"] == name)
        rooms[name.lower()] = {"id": name, "label": z.get("label") or name, "lights": None, "aircon": {
            "zone": z["id"], "on": bool(z["on"]) and ac.get("power") == "on", "temp": z.get("temp"),
            "setpoint": z.get("setpoint"), "min": int(clim.get("min", 18)), "max": int(clim.get("max", 28))}}
    lcfg = devices.light_cfg(h, settings())
    lr = devices.light_rooms(lcfg)
    for name in _pick(list(lr), control(h, "lights"), mine or next(iter(lr), None)):
        r = rooms.setdefault(name.lower(), {"id": name, "label": (clim.get("labels") or {}).get(name, name), "aircon": None})
        r["lights"] = devices.lights_state(lcfg, private(), lr[name])
    return list(rooms.values())


def room_of(h, rid):
    rooms = remote_rooms(h)
    if rid is None and len(rooms) == 1:
        return rooms[0]
    return next((r for r in rooms if str(rid or "").lower() in (r["id"].lower(), str(r["label"]).lower())), None)


def lights_set(h, rid, on=None, brightness=None):
    import devices
    r = room_of(h, rid)
    if not r or not r.get("lights"):
        return {"ok": False, "error": "no lights you can change there"}
    lcfg = devices.light_cfg(h, settings())
    return devices.lights_set(lcfg, private(), devices.light_rooms(lcfg)[r["id"]], on, brightness)


def remote_speakers(h):
    """The speakers this guest may play and pause, one per room, with what each is doing."""
    import devices
    cfg = devices.speaker_cfg(h, settings())
    sr = devices.speaker_rooms(cfg)
    mine = h.get("room") or next(iter(sr), None)
    return [dict(devices.speaker_state(cfg, private(), sr[n]), id=n, label=n) for n in _pick(list(sr), control(h, "speakers"), mine)]


def speaker_set(h, rid, action, volume=None):
    import devices
    cfg = devices.speaker_cfg(h, settings())
    r = next((s for s in remote_speakers(h) if s["id"].lower() == str(rid or "").lower()), None)
    if not r:
        sp = remote_speakers(h)
        r = sp[0] if rid is None and len(sp) == 1 else None
    if not r:
        return {"ok": False, "error": "no speaker you can use there"}
    return devices.speaker_set(cfg, private(), devices.speaker_rooms(cfg)[r["id"]], action, volume)


def pool_info(h):
    """The pool's water temperature and heater, and whether a guest may switch the heater."""
    import devices
    st = devices.pool_state(devices.pool_cfg(h, settings()), private(), h["id"])
    if st:
        st["can_heat"] = bool(st.get("has_heater")) and control(h, "pool") == "all"
    return st


def pool_set(h, heater):
    import devices
    p = pool_info(h)
    if not p or not p.get("can_heat"):
        return {"ok": False, "error": "the pool heater is not yours to change"}
    return devices.pool_set(devices.pool_cfg(h, settings()), private(), h["id"], heater)


def hub(h):
    """The smart home at a glance: aircon rooms, lights, pool and speakers the guest may use."""
    return {"rooms": remote_rooms(h), "pool": pool_info(h), "speakers": remote_speakers(h)}


def stay_info(h):
    """The stay on the Guest remote (only ever sent with this stay's remote code)."""
    s, g = stay_of(h), h.get("guide", {})
    if h.get("mode") == "guest":
        return {"wifi": h.get("wifi")}
    return {"from": s.get("from"), "to": s.get("to"), "night": s.get("night"), "nights": s.get("nights"),
            "checkin": h.get("checkin"), "checkout": h.get("checkout"), "host": h.get("host"),
            "wifi": h.get("wifi"), "access": h.get("access", []), "rules": g.get("rules", []),
            "checkout_steps": (g.get("checkout") or {}).get("steps", []), "emergency": g.get("emergency", [])}


def agent_cfg():
    """How much the guest agent may do: "locked" (default), "plus" (the host's own actions) or "open"
    (also web search and the MCP servers the host lists, including agentic booking). README, "Agent levels"."""
    a = settings().get("agent") or {}
    lvl = a.get("level") if a.get("level") in ("locked", "plus", "open") else "locked"
    return dict(a, level=lvl)


def places(h):
    """Places in the house guide with their own booking page: what a "Book a table" button may open."""
    out = []
    for c in (h.get("guide") or {}).get("local", []):
        for it in c.get("items", []):
            if re.match(r"https?://", str(it.get("book") or "")):
                out.append({"name": it["name"], "book": it["book"], "category": c.get("category", "")})
    return out


def guest_apps(h, tv):
    """The apps on this TV a guest can use: installed, and in the host's "guest_apps" list when there
    is one (apps that are signed in). An app that is installed but signed out only shows a login wall."""
    if h.get("managed"):   # exactly the shortcuts the host ticked for this apartment
        return [a for a in tv.installed() if a["id"] in (h.get("guest_apps") or [])]
    allow = h.get("guest_apps") or settings().get("guest_apps")
    return [a for a in tv.installed() if not allow or a["id"] in allow]


def remote_ctx(h):
    import tvctl
    tv = tvctl.tv_for(tv_ip(h), tv_kind(h))
    try:
        apps = guest_apps(h, tv)
    except Exception:
        apps = []
    s, g = stay_of(h), h.get("guide", {})
    stay = ""
    if h.get("mode") != "guest" and s.get("active"):
        stay = "%s. Check-in %s, check-out %s. Check-out steps: %s" % (
            ("Guest " + s["guest"]) if s.get("guest") else "Guests", s.get("checkin", "")[:16].replace("T", " "),
            s.get("checkout", "")[:16].replace("T", " "), "; ".join((g.get("checkout") or {}).get("steps", [])))
    import devices
    ag = agent_cfg()
    acts = devices.load_actions(os.path.join(DATA, ag.get("actions_file", "actions.json"))) if ag["level"] != "locked" else []
    book = ag.get("booking") or {}
    return {"tv": tv, "tv_on": bool(apps), "apps": apps, "rooms": remote_rooms(h), "weather": weather(),
            "pool": pool_info(h), "speakers": remote_speakers(h),
            "pool_set": lambda on: pool_set(h, on), "speaker_set": lambda rid, act, vol: speaker_set(h, rid, act, vol),
            "local": g.get("local", []), "howto": g.get("howto", []), "notes": h.get("notes", []), "stay": stay,
            "place": "%s (%s)" % (h.get("name"), h.get("kind") or "a home"), "today": now().strftime("%A %-d %B %Y"),
            "places": places(h), "level": ag["level"], "actions": acts, "open": ag.get("open") or {},
            "booking": book if ag["level"] == "open" and book.get("mcp_file") else None, "data": DATA,
            "aircon_set": lambda body: aircon_set(h, body), "lights_set": lambda rid, on, b: lights_set(h, rid, on, b),
            "run_action": lambda a, p: devices.run_action(a, p, private())}


def local_name():
    n = socket.gethostname()
    return n if n.endswith(".local") else n + ".local"


def qr_svg(text):
    import segno
    import io
    b = io.BytesIO()
    segno.make(text, error="m").save(b, kind="svg", scale=4, border=2, dark="#111", light="#fff", xmldecl=False, svgns=True, omitsize=True)
    return b.getvalue()


def qr_png(text):
    """The same code as a PNG, for the TVs that draw the screen themselves (Roku, Apple TV): no SVG there."""
    import segno
    import io
    b = io.BytesIO()
    segno.make(text, error="m").save(b, kind="png", scale=10, border=2, dark="#111111", light="#ffffff")
    return b.getvalue()


DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _t12(hhmm):
    try:
        hh, mm = (int(x) for x in (hhmm or "").split(":")[:2])
    except ValueError:
        return hhmm or ""
    return "%d%s%s" % (hh % 12 or 12, (":%02d" % mm) if mm else "", "am" if hh < 12 else "pm")


def _short(iso):
    try:
        d = dt.date.fromisoformat((iso or "")[:10])
    except ValueError:
        return ""
    return "%s %d %s" % (DAYS[d.weekday()][:3], d.day, d.strftime("%b"))


def native_screen(h, base):
    """The welcome screen as plain data, for the TVs whose app draws it natively instead of showing
    the /tv/ page: Roku (tv-apps/roku) and Apple TV (tv-apps/tvos). Same rules as the page: Wi-Fi,
    door codes and the Guest remote only while public_state shows them, never a key, never a name
    between stays. `base` is this computer's address as the TV reached it."""
    st = public_state(h)
    s, hid = st["stay"], h["id"]
    guest, part = s.get("guest", ""), datetime_part()
    eyebrow, title, sub = st["name"], "", ""
    if st["mode"] == "guest":
        eyebrow = "Staying with " + st["host"] if st.get("host") else st["name"]
        title = (part + ", " + guest) if guest else "Welcome"
        sub = ("Here until %s. Make yourself at home." % DAYS[dt.date.fromisoformat(s["to"]).weekday()]) if s.get("to") else "Make yourself at home."
    elif s["phase"] in ("between", "before"):
        eyebrow, title, sub = st.get("kind") or st["name"], "Welcome to %s" % st["name"], "Your stay details show here when you arrive."
    elif s["phase"] == "arrival":
        title = "Welcome" + (", " + guest if guest else "")
        sub = "Check-in from %s today. Settle in, everything is here." % _t12(st.get("checkin"))
    elif s["phase"] == "lastday":
        title = part + (", " + guest if guest else "")
        sub = "Check-out today at %s. Thanks for staying." % _t12(st.get("checkout_time"))
    else:
        title = ("Welcome" if s.get("night") == 1 else part) + (", " + guest if guest else "")
        sub = "%s to %s · Night %s of %s" % (_short(s.get("from")), _short(s.get("to")), s.get("night"), s.get("nights"))
    q = "home=" + urllib.parse.quote(hid)
    wx = weather()
    today = (wx.get("days") or [{}])[0] if wx.get("ok") else {}
    hero = (st.get("photos") or {}).get("hero")
    wifi = st["wifi"] if st["wifi"] and st["wifi"].get("ssid") else None
    return {
        "home": hid, "mode": st["mode"], "phase": s["phase"], "active": bool(s.get("active")),
        "eyebrow": eyebrow or "", "title": title, "sub": sub, "welcome": st.get("welcome", ""),
        "photo": "%s/tv/homes/%s.jpg" % (base, hero) if hero else None,
        "wifi": {"ssid": wifi.get("ssid", ""), "password": wifi.get("password", "")} if wifi else None,
        "wifi_qr": "%s/api/qr?%s&what=wifi&fmt=png" % (base, q) if wifi else None,
        "remote_qr": "%s/api/qr?%s&what=%s&fmt=png" % (base, q, "remote" if st["remote_url"] else "phone"),
        "remote_label": "Guest remote" if st["remote_url"] else "House info",
        "access": [{"label": a.get("label", ""), "code": a.get("code", "")} for a in st["access"]],
        "weather": {"place": wx.get("place", ""), "temp": wx["now"]["temp"], "code": wx["now"]["code"],
                    "hi": today.get("hi"), "lo": today.get("lo"), "rain": today.get("rain")} if wx.get("ok") else None,
        "time": now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


def datetime_part():
    hr = now().hour
    return "Good morning" if hr < 12 else "Good afternoon" if hr < 18 else "Good evening"


def wifi_qr(w):
    esc = lambda s: re.sub(r"([\\;,:\"])", r"\\\1", s or "")
    return "WIFI:T:WPA;S:%s;P:%s;;" % (esc(w.get("ssid")), esc(w.get("password")))


# ---------------------------------------------------------------- http

PORT = int(os.environ.get("PORT") or settings().get("port") or 5410)
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".json": "application/json",
         ".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".svg": "image/svg+xml",
         ".woff2": "font/woff2", ".webmanifest": "application/manifest+json"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", cache="no-store", cors=False, headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        if cors:   # the Samsung and LG app asks from its own origin whether the computer is up
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def q(self):
        u = urllib.parse.urlparse(self.path)
        return u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    def home(self, qs):
        """The home a request is about. ?screen=<tv id> (a managed apartment's TV) names the TV, and the
        TV's apartment follows from it; self.tv is then that TV."""
        self.tv = None
        if qs.get("screen"):
            self.tv = tvlink.get(qs["screen"])
            return load_home(self.tv["home"]) if self.tv else None
        hid = qs.get("home") or qs.get("tv_id")
        if not hid:   # a TV that opens the plain address is known by the address staff typed for it in the dashboard
            mine = [t for t in tvlink.listing() if t["ip"] and t["ip"] == self.client_address[0]]
            if mine:
                self.tv = mine[0]
                return load_home(self.tv["home"])
        elif hid not in home_ids() and tvlink.get(hid):   # ?home=<tv id> (a TV app that was given the TV's id)
            self.tv = tvlink.get(hid)
            return load_home(self.tv["home"])
        if not hid:
            managed = content.ids()
            hid = (managed[0] if managed else None) or settings().get("default_home") or home_ids()[0]
        h = load_home(hid)
        if h and h.get("managed"):
            self.tv = tvlink.pick(hid, qs.get("tv") or None)   # None when that TV is another apartment's
            if qs.get("tv") and not self.tv:
                return None
        return h

    def is_host(self, qs):
        import host
        if host.authed(self):   # signed in to the host dashboard
            return True
        key = qs.get("key", "")
        if not key:
            return False
        ok = secrets.compare_digest(key.encode("utf-8", "replace"), private().get("host_key", "-").encode())
        if not ok:
            note_wrong(self.client_address[0], key)
        return ok

    def refused(self):
        """Checks every request goes through first: the Host header, and too many wrong codes."""
        if not host_allowed(self.headers.get("Host")) and os.environ.get("GUEST_TV_PREVIEW") != "1":
            self.send(403, {"error": "unknown host name"})
            return True
        if wrong_limited(self.client_address[0]):
            self.send(429, {"error": "Too many wrong codes. Wait a minute, then scan the code on the TV again."})
            return True
        return False

    def do_GET(self):
        if self.refused():
            return
        path, qs = self.q()
        if path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/tv/")
            return self.end_headers()
        if path == "/api/health":
            return self.send(200, {"ok": True, "app": "guest-tv"}, cors=True)
        if path.startswith("/api/host/"):
            import host
            return host.handle(self, "GET", path, qs, {}, sys.modules[__name__])
        if path == "/api/state":
            h = self.home(qs)
            return self.send(200, public_state(h, self.is_host(qs), qs.get("phase"), self.tv) if h else {"error": "no such home"}, cache="no-store")
        if path == "/api/profiles":   # the guest profile picker: what it saw and did lately
            import profiles
            return self.send(200, {"last_app": profiles.state["last"], "log": profiles.state["seen"]})
        if path == "/api/bookings":   # what the booking-email reader found (names, so host only)
            if not self.is_host(qs):
                return self.send(403, {"error": "host key needed"})
            if qs.get("now"):
                read_bookings()
            return self.send(200, booking_state)
        if path == "/api/r":   # the Guest remote: what this guest's remote can do right now
            h = self.home(qs)
            if not self.remote_ok(h, qs):
                return self.send(403, {"error": "This remote has ended. Scan the code on the TV again."})
            if h.get("managed"):
                return self.send(200, managed_remote(h, self.tv))
            ctx, s = remote_ctx(h), stay_of(h)
            return self.send(200, {
                "ok": True, "home": h["id"], "mode": h.get("mode", "stay"), "place": h.get("name"),
                "guest": s.get("guest", ""), "until": s.get("checkout") if h.get("mode") != "guest" else None,
                "tv_on": ctx["tv_on"], "apps": [{"id": a["id"], "name": a["name"]} for a in ctx["apps"]],
                "rooms": ctx["rooms"], "pool": ctx["pool"], "speakers": ctx["speakers"], "stay": stay_info(h),
                "places": ctx["places"], "level": ctx["level"], "ask_limit": 20})
        if path == "/api/hub":   # the TV page: the smart home tile and sheet
            h = self.home(qs)
            return self.send(200, hub(h) if h else {"error": "no such home"})
        if path == "/api/tv/apps":   # the TV page on a Samsung or LG: the app row (they open through here)
            h = self.home(qs)
            import tvctl
            try:
                if h and h.get("managed"):
                    apps = guest_apps(h, tvlink.driver(self.tv)) if self.tv else [a for a in tvctl.APPS if a["id"] in (h.get("guest_apps") or [])]
                else:
                    apps = guest_apps(h, tvctl.tv_for(tv_ip(h), tv_kind(h))) if h and tv_ip(h) else []
            except Exception:
                apps = []
            return self.send(200, {"apps": [{"id": a["id"], "name": a["name"], "package": a["package"]} for a in apps],
                                   "managed": bool(h and h.get("managed"))})
        if path == "/api/tv/status":   # host: what the TV is doing and what its watcher did
            h = self.home(qs)
            if not self.is_host(qs) or not h:
                return self.send(403, {"error": "host key needed"})
            import tvctl
            tv = tvctl.tv_for(tv_ip(h), tv_kind(h))
            out = {"kind": tv_kind(h), "ip": tv_ip(h)}
            if hasattr(tv, "is_on"):
                on = tv.is_on()
                out.update(on=on, front=tv.front() if on and tv.sees_front else None, log=tv.log,
                           can_open=tv.can_open)
            return self.send(200, out)
        if path == "/api/tv/keys":   # Fire TV on Vega OS: the page asks for the Guest remote's keys (a long poll)
            h = self.home(qs)
            import tvctl
            if h and h.get("managed"):   # a simulated TV's welcome screen collects the Guest remote's keys here
                tv = tvlink.sim(self.tv["id"]) if self.tv and not tvlink.is_live(self.tv) else None
            else:
                tv = tvctl.tv_for(tv_ip(h), tv_kind(h)) if h and tv_ip(h) else None
            if not hasattr(tv, "page_keys"):
                return self.send(404, {"error": "this TV takes its keys directly"})
            try:
                after = int(qs.get("after") or 0)
            except ValueError:
                after = 0
            return self.send(200, tv.page_keys(after, 20.0))
        if path == "/api/weather":
            return self.send(200, weather())
        if path == "/api/aircon":
            h = self.home(qs)
            return self.send(200, aircon_for(h) if h else {"connected": False})
        if path == "/api/tv/native":   # Roku and Apple TV: the welcome screen as data their app draws
            h = self.home(qs)
            if not h:
                return self.send(404, {"error": "no such home"})
            return self.send(200, native_screen(h, "http://" + (self.headers.get("Host") or "%s:%d" % (lan_ip(), PORT))))
        if path == "/api/qr":
            h = self.home(qs)
            png = qs.get("fmt") == "png"   # Roku and Apple TV draw PNGs, the page SVGs
            code = (lambda t: self.send(200, qr_png(t), "image/png")) if png else (lambda t: self.send(200, qr_svg(t), "image/svg+xml"))
            if not h:
                return self.send(404, {"error": "no such home"})
            if qs.get("what") == "wifi":
                st = public_state(h)
                if not st["wifi"]:
                    return self.send(404, {"error": "no wifi now"})
                return code(wifi_qr(st["wifi"]))
            if qs.get("what") == "remote":
                st = public_state(h, tv=self.tv)
                if not st["remote_url"]:
                    return self.send(404, {"error": "no remote now"})
                return code(st["remote_url"])
            return code(phone_url(h["id"]))
        if path in ("/titan", "/titan/"):   # the start page a Philips Titan OS app is given (tv-apps/titan)
            with open(os.path.join(HERE, "tv-apps", "titan", "index.html"), "rb") as fh:
                return self.send(200, fh.read(), TYPES[".html"], "no-cache")
        # files
        rel = {"/tv/": "/index.html", "/p/": "/phone.html", "/r/": "/remote.html", "/host/": "/host.html", "/host": "/host.html"}.get(path, path)
        for pre in ("/tv/", "/p/", "/r/", "/host/"):
            if rel.startswith(pre):
                rel = "/" + rel[len(pre):]
        f = os.path.normpath(os.path.join(WEB, rel.lstrip("/")))
        if rel.startswith("/homes/") and not os.path.isfile(f):   # a photograph the host uploaded (data/photos/)
            f = content.photo_path(os.path.basename(rel)) or f
            if not os.path.isfile(f):
                return self.send(404, {"error": "not found"})
        elif not f.startswith(WEB) or not os.path.isfile(f):
            return self.send(404, {"error": "not found"})
        ext = os.path.splitext(f)[1]
        with open(f, "rb") as fh:
            body = fh.read()
        cache = "max-age=86400" if ext in (".jpg", ".webp", ".woff2", ".png") else "no-cache"
        self.send(200, body, TYPES.get(ext, "application/octet-stream"), cache)

    def remote_ok(self, h, qs):
        given = qs.get("c", "")
        if h and h.get("managed"):   # this booking's own link, this apartment, and only until check-out
            ok = bool(access.verify(h["id"], given, h.get("checkin"), h.get("checkout"), now()))
            if given and not ok:
                note_wrong(self.client_address[0], given)
            return ok
        code = h and remote_code(h)
        ok = bool(code) and secrets.compare_digest(given.encode("utf-8", "replace"), code.encode())
        if given and not ok:
            note_wrong(self.client_address[0], given)
        return ok

    def remote_post(self, path, h, qs, body):
        import guest_agent, tvctl
        if not self.remote_ok(h, qs):
            return self.send(403, {"error": "This remote has ended. Scan the code on the TV again."})
        if h.get("managed"):
            return self.managed_post(path, h, qs, body)
        tv = tvctl.tv_for(tv_ip(h), tv_kind(h))
        try:
            if path == "/api/r/key" and body.get("key") in tvctl.KEYS:
                tv.key(body["key"])
                return self.send(200, {"ok": True})
            if path == "/api/r/app" and body.get("app") in {a["id"] for a in guest_apps(h, tv)}:
                tv.open_app(body["app"])
                return self.send(200, {"ok": True})
            if path == "/api/r/aircon":
                r = room_of(h, body.get("room"))
                ac = r and r.get("aircon")
                if not ac:
                    return self.send(200, {"ok": False, "error": "No aircon you can change there"})
                sp = body.get("setpoint")
                if sp is not None and not (ac["min"] <= float(sp) <= ac["max"]):
                    return self.send(200, {"ok": False, "error": "Between %d° and %d°" % (ac["min"], ac["max"])})
                return self.send(200, aircon_set(h, {"zone": ac["zone"], "on": body.get("on"), "setpoint": sp}))
            if path == "/api/r/lights":
                on = body.get("on") if isinstance(body.get("on"), bool) else None
                b = body.get("brightness")
                return self.send(200, lights_set(h, body.get("room"), on, int(b) if isinstance(b, (int, float)) else None))
            if path == "/api/r/pool":
                return self.send(200, pool_set(h, body.get("heater")))
            if path == "/api/r/speaker":
                return self.send(200, speaker_set(h, body.get("room"), body.get("action"), body.get("volume")))
            if path == "/api/r/ask":
                return self.send(200, guest_agent.ask(body.get("text", ""), remote_ctx(h), qs.get("c", "")))
            if path == "/api/r/confirm":   # the guest tapped Confirm on a booking or a host action the agent offered
                return self.send(200, guest_agent.confirm(body, remote_ctx(h), qs.get("c", "")))
        except Exception as e:
            return self.send(200, {"ok": False, "error": "The TV didn't answer. Is it on?"})
        return self.send(400, {"error": "not allowed"})

    def managed_post(self, path, h, qs, body):
        """Guest remote actions in a managed apartment: the TV only (keys and the app shortcuts), and
        the concierge. The TV is the one in the link, and self.home() already refused another apartment's."""
        import concierge, tvctl
        if path == "/api/r/ask":
            return self.send(200, concierge.ask(body.get("text", ""), h, settings(), qs.get("c", "")))
        if path not in ("/api/r/key", "/api/r/app"):
            return self.send(400, {"error": "not allowed"})   # no aircon, lights or anything else here
        tv = self.tv
        if not tv:
            return self.send(200, {"ok": False, "error": "No TV is set up for this apartment yet"})
        d, real = tvlink.driver(tv), tvlink.is_live(tv)
        try:
            if path == "/api/r/key":
                if body.get("key") not in tvctl.KEYS:
                    return self.send(400, {"error": "not allowed"})
                d.key(body["key"])
            else:
                if body.get("app") not in {a["id"] for a in guest_apps(h, d)}:
                    return self.send(400, {"error": "not allowed"})
                d.open_app(body["app"])
            tvlink.sent(tv, True)
            return self.send(200, {"ok": True, "simulated": not real})
        except Exception:
            tvlink.sent(tv, False)
            return self.send(200, {"ok": False, "error": "The TV didn't answer. Is it on?"})

    def do_POST(self):
        if self.refused():
            return
        path, qs = self.q()
        # JSON only, so a form on some other web page cannot reach a handler (couch-tv#73)
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self.send(415, {"error": "send JSON with Content-Type: application/json"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > (45_000_000 if path == "/api/host/photo" else 200_000):
            return self.send(413, {"error": "too big"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self.send(400, {"error": "bad json"})
        if path.startswith("/api/host/"):
            import host
            return host.handle(self, "POST", path, qs, body, sys.modules[__name__])
        h = self.home(qs)
        if not h:
            return self.send(404, {"error": "no such home"})
        if path == "/api/tv/app":   # the TV page on a Samsung or LG opens an app through the computer
            import tvctl
            if h.get("managed") and not self.tv:
                return self.send(200, {"ok": False, "error": "No TV is set up for this apartment yet"})
            tv = tvlink.driver(self.tv) if h.get("managed") else tvctl.tv_for(tv_ip(h), tv_kind(h))
            try:
                if body.get("app") not in {a["id"] for a in guest_apps(h, tv)}:
                    return self.send(200, {"ok": False, "error": "That app is not on this TV"})
                tv.open_app(body["app"])
                return self.send(200, {"ok": True, "simulated": bool(getattr(tv, "simulated", False))})
            except Exception:
                return self.send(200, {"ok": False, "error": "The TV didn't answer"})
        if path.startswith("/api/r/"):   # Guest remote actions: only with this stay's code
            return self.remote_post(path, h, qs, body)
        if path == "/api/remote/reset":   # host: a new Guest remote link for the guest room (old ones stop)
            if not self.is_host(qs):
                return self.send(403, {"error": "host key needed"})
            pv = private()
            hp = pv.setdefault("homes", {}).setdefault(h["id"], {})
            hp["remote_epoch"] = int(hp.get("remote_epoch", 0)) + 1
            jwrite(os.path.join(DATA, "private.json"), pv)
            return self.send(200, {"ok": True})
        if h.get("managed") and path in ("/api/aircon", "/api/hub", "/api/stay", "/api/remote/reset"):
            return self.send(400, {"error": "not allowed"})   # managed apartments: the TV only, and bookings in the dashboard
        if path == "/api/aircon":
            return self.send(200, aircon_set(h, body))
        if path == "/api/hub":   # the TV page: pool heater, speakers and lights, with the same limits as the remote
            if "heater" in body:
                return self.send(200, pool_set(h, body.get("heater")))
            if body.get("speaker"):
                return self.send(200, speaker_set(h, body["speaker"], body.get("action"), body.get("volume")))
            if body.get("lights"):
                return self.send(200, lights_set(h, body["lights"], body.get("on") if isinstance(body.get("on"), bool) else None))
            return self.send(400, {"error": "nothing to change"})
        if path == "/api/log":   # the TV page reports its own script errors here (TVs have no console to look at)
            os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)   # a fresh clone has no logs/ yet
            with open(os.path.join(HERE, "logs", "tv-errors.log"), "a") as f:
                f.write("%s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), self.client_address[0], json.dumps(body)[:600]))
            return self.send(200, {"ok": True})
        if path == "/api/stay":   # the host sets the guest's name and dates (guest room, or a name for an Airbnb booking)
            if not self.is_host(qs):
                return self.send(403, {"error": "host key needed"})
            pv = private()
            hp = pv.setdefault("homes", {}).setdefault(h["id"], {})
            st = {k: str(body.get(k, "")).strip()[:60] for k in ("guest", "from", "to")}
            for k in ("from", "to"):
                if st[k]:
                    try:
                        dt.date.fromisoformat(st[k])
                    except ValueError:
                        return self.send(400, {"error": "dates look like 2026-11-02"})
            if h.get("calendar_url") and st["from"]:
                hp.setdefault("guest_names", {})[st["from"]] = st["guest"]
            else:
                hp["stay"] = st
            jwrite(os.path.join(DATA, "private.json"), pv)
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


def start_managed():
    """The managed apartments (host dashboard): the clock's time zone, a starter apartment on the
    first run, and the loop that follows Airbnb calendars and booking emails."""
    global TZ
    tz = settings().get("timezone")
    if tz:
        from zoneinfo import ZoneInfo
        TZ = stays.TZ = ZoneInfo(tz)
    if demo_mode():
        import demo
        # Demo mode pins the time of day at 10:30, so every moment of a stay is on show whenever you look
        # (an arrival day before check-in, a check-out day before check-out). The date is the real one.
        stays.CLOCK = lambda: dt.datetime.now(TZ).replace(hour=10, minute=30, second=0, microsecond=0)
        demo.seed()
    elif settings().get("managed", True):
        content.seed()
    threading.Thread(target=managed_loop, daemon=True).start()


if __name__ == "__main__":
    private()
    start_managed()
    import profiles
    profiles.start(settings())   # picks the guest profile in streaming apps on the guest TV
    threading.Thread(target=booking_loop, daemon=True).start()   # guest names from booking emails
    start_tv_watchers()   # every TV that is not Android TV: open guest-tv when the TV comes on
    srv = ThreadingHTTPServer((os.environ.get("HOST", "0.0.0.0"), PORT), H)
    print("guest-tv on http://%s:%d/tv/  (phone: /p/; a TV app that asks wants the number: %s)" % (local_name(), PORT, lan_ip()), flush=True)
    print("Host dashboard: http://%s:%d/host/%s" % (lan_ip(), PORT, "   DEMO MODE: pretend bookings, simulated TVs, password grace-demo" if demo_mode() else ""), flush=True)
    srv.serve_forever()
