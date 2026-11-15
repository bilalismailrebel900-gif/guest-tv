"""Content for the managed apartments: each apartment's details, photographs, Wi-Fi, reception
contact, house guide and nearby places, and the settings for the whole property.

An apartment is a folder, data/homes/<id>/ (home.json and guide.json), the way guest-tv has always
kept a home, with "managed": true. What must stay on this computer (the Wi-Fi password, the Airbnb
calendar link) is in data/private.json. Uploaded photographs go to data/photos/. Everything a
host types is cleaned and length-limited here before it is saved, because it ends up on a TV.
"""
import base64, io, os, re, shutil, time

import store

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.join(HERE, "seed")
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
PHOTOS = ("hero", "inside", "area")
SAMPLE_HEROES = ["sample-1", "sample-2", "sample-3", "sample-4"]


class Invalid(ValueError):
    pass


def homes_dir():
    return os.path.join(store.DATA, "homes")


def photos_dir():
    return os.path.join(store.DATA, "photos")


def text(v, n=200):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b-\x1f<>]", "", str(v or ""))).strip()[:n]


def para(v, n=1200):
    return re.sub(r"[ \t]+", " ", re.sub(r"[\x00-\x08\x0b-\x1f<>]", "", str(v or ""))).strip()[:n]


def url(v):
    v = str(v or "").strip()[:500]
    return v if re.fullmatch(r"https?://[^\s<>\"']+", v) else ""


def phone(v):
    return re.sub(r"[^0-9+ ()-]", "", str(v or "")).strip()[:30]


def ids():
    d = homes_dir()
    if not os.path.isdir(d):
        return []
    return sorted(x for x in os.listdir(d) if (store.jread(os.path.join(d, x, "home.json"), {}) or {}).get("managed"))


def home_file(hid):
    return os.path.join(homes_dir(), hid, "home.json")


def guide_file(hid):
    return os.path.join(homes_dir(), hid, "guide.json")


def read(hid):
    """Everything the dashboard edits for one apartment (so: for a signed-in host only)."""
    if hid not in ids():
        return None
    h = store.jread(home_file(hid), {}) or {}
    p = (store.private().get("homes") or {}).get(hid, {})
    return {"id": hid, "name": h.get("name", ""), "label": h.get("kind", ""), "checkin": h.get("checkin", "14:00"),
            "checkout": h.get("checkout", "12:00"), "photos": h.get("photos", {}),
            "sample_photos": {k: str(v).startswith("sample-") for k, v in (h.get("photos") or {}).items()},
            "guest_apps": h.get("guest_apps", []), "reception": h.get("reception", {}),
            "wifi": p.get("wifi") or {"ssid": "", "password": ""}, "ical_url": p.get("ical_url", ""),
            "guide": store.jread(guide_file(hid), {}) or {}}


def create(name):
    name = text(name, 60)
    if not name:
        raise Invalid("Give the apartment a name")
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "apartment"
    hid, n = base, 2
    while os.path.exists(os.path.join(homes_dir(), hid)):
        hid, n = "%s-%d" % (base, n), n + 1
    h = store.jread(os.path.join(SEED, "home.json"), {}) or {}
    h.update(id=hid, name=name, managed=True)
    h["photos"] = dict(h.get("photos") or {}, hero=SAMPLE_HEROES[len(ids()) % len(SAMPLE_HEROES)])
    store.jwrite(home_file(hid), h)
    store.jwrite(guide_file(hid), store.jread(os.path.join(SEED, "guide.json"), {}) or {})
    return hid


def remove(hid):
    """The apartment's folder moves to data/archive/ (nothing is destroyed); its private values,
    bookings and TVs go."""
    if hid not in ids():
        raise Invalid("No such apartment")
    arch = os.path.join(store.DATA, "archive")
    os.makedirs(arch, exist_ok=True)
    shutil.move(os.path.join(homes_dir(), hid), os.path.join(arch, "%s-%d" % (hid, int(time.time()))))
    pv = store.private()
    (pv.get("homes") or {}).pop(hid, None)
    store.save_private(pv)
    store.run("DELETE FROM bookings WHERE home = ?", (hid,))
    store.run("DELETE FROM tvs WHERE home = ?", (hid,))
    store.run("DELETE FROM sync WHERE home = ?", (hid,))


def save(hid, body, app_ids=()):
    """Details from the dashboard. Only the fields that are sent are changed."""
    if hid not in ids():
        raise Invalid("No such apartment")
    h = store.jread(home_file(hid), {}) or {}
    pv = store.private()
    hp = pv.setdefault("homes", {}).setdefault(hid, {})
    if "name" in body:
        if not text(body["name"], 60):
            raise Invalid("The apartment needs a name")
        h["name"] = text(body["name"], 60)
    if "label" in body:
        h["kind"] = text(body["label"], 60)
    for k in ("checkin", "checkout"):
        if k in body:
            if not TIME_RE.fullmatch(str(body[k] or "")):
                raise Invalid("Times look like 14:00")
            h[k] = body[k]
    if "guest_apps" in body:
        h["guest_apps"] = [a for a in body["guest_apps"] or [] if a in app_ids][:8]
    if "reception" in body:
        r = body["reception"] or {}
        h["reception"] = {"name": text(r.get("name"), 60), "phone": phone(r.get("phone")), "whatsapp": phone(r.get("whatsapp")),
                          "hours": text(r.get("hours"), 80), "note": text(r.get("note"), 200)}
    if "wifi" in body:
        w = body["wifi"] or {}
        hp["wifi"] = {"ssid": re.sub(r"[\x00-\x1f]", "", str(w.get("ssid") or ""))[:32],
                      "password": re.sub(r"[\x00-\x1f]", "", str(w.get("password") or ""))[:63]}
    if "ical_url" in body:
        u = str(body["ical_url"] or "").strip()
        if u and not url(u):
            raise Invalid("The Airbnb calendar link starts with https://")
        hp["ical_url"] = url(u)
    store.jwrite(home_file(hid), h)
    store.save_private(pv)
    return read(hid)


def _items(v, n=30):
    out = []
    for x in (v or [])[:n]:
        if isinstance(x, dict) and text(x.get("title"), 80):
            out.append({"title": text(x.get("title"), 80), "body": para(x.get("body"))})
    return out


def save_guide(hid, body):
    """The house guide, check-out steps, emergency numbers and nearby places."""
    if hid not in ids():
        raise Invalid("No such apartment")
    g = store.jread(guide_file(hid), {}) or {}
    if "welcome" in body:
        g["welcome"] = para(body["welcome"], 600)
    for k in ("rules", "howto"):
        if k in body:
            g[k] = _items(body[k])
    if "checkout_steps" in body:
        g["checkout"] = {"steps": [text(s, 200) for s in (body["checkout_steps"] or [])[:15] if text(s, 200)]}
    if "emergency" in body:
        g["emergency"] = [{"name": text(e.get("name"), 80), "phone": phone(e.get("phone"))}
                          for e in (body["emergency"] or [])[:12] if isinstance(e, dict) and text(e.get("name"), 80)]
    if "local" in body:
        cats = []
        for c in (body["local"] or [])[:10]:
            if not isinstance(c, dict) or not text(c.get("category"), 40):
                continue
            items = []
            for it in (c.get("items") or [])[:25]:
                if isinstance(it, dict) and text(it.get("name"), 80):
                    p = {"name": text(it.get("name"), 80), "note": para(it.get("note"), 300), "distance": text(it.get("distance"), 40)}
                    if url(it.get("book")):
                        p["book"] = url(it.get("book"))   # the place's own booking page
                    if phone(it.get("phone")):
                        p["phone"] = phone(it.get("phone"))
                    items.append(p)
            cats.append({"category": text(c.get("category"), 40), "items": items})
        g["local"] = cats
    store.jwrite(guide_file(hid), g)
    return g


# ---------------------------------------------------------------- photographs

def photo_path(name):
    """Where a photo file is: an uploaded one in data/photos/, else one that came with the code."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or "") or ".." in name:
        return None
    for d in (photos_dir(), os.path.join(HERE, "web", "homes")):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def set_photo(hid, which, data_b64):
    """An uploaded photograph: turned upright, cut to 16:9 from the middle, saved at 4K when the
    original is big enough (else 1080p), with a frosted copy for the glass clock."""
    if hid not in ids() or which not in PHOTOS:
        raise Invalid("No such apartment or photo")
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    try:
        raw = base64.b64decode(str(data_b64).split(",")[-1], validate=False)
        if len(raw) > 30_000_000:
            raise Invalid("That photo is over 30 MB. Export a smaller copy.")
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Invalid:
        raise
    except Exception:
        raise Invalid("That file is not a photo this can read. Use a JPG or PNG.")
    if img.width < 1280:
        raise Invalid("That photo is small (%d pixels wide). Use one at least 1920 wide; 3840 for a 4K TV." % img.width)
    size = (3840, 2160) if img.width >= 3200 and which == "hero" else (1920, 1080)
    img = ImageOps.fit(img, size, Image.LANCZOS)
    os.makedirs(photos_dir(), exist_ok=True)
    name = "%s-%s-%d" % (hid, which, int(time.time()))
    img.save(os.path.join(photos_dir(), name + ".jpg"), quality=88, optimize=True, progressive=True)
    small = img.resize((1920, 1080), Image.LANCZOS) if size[0] > 1920 else img
    glass = ImageEnhance.Brightness(small.filter(ImageFilter.GaussianBlur(16))).enhance(1.3)
    ImageEnhance.Color(glass).enhance(1.25).save(os.path.join(photos_dir(), name + "-glass.jpg"), quality=80)
    h = store.jread(home_file(hid), {}) or {}
    old = (h.get("photos") or {}).get(which, "")
    h.setdefault("photos", {})[which] = name
    store.jwrite(home_file(hid), h)
    if old.startswith(hid + "-"):   # the upload this one replaces
        for suf in (".jpg", "-glass.jpg"):
            try:
                os.remove(os.path.join(photos_dir(), old + suf))
            except OSError:
                pass
    return name


def sample_photo(hid, which):
    """Back to the labelled sample image."""
    if hid not in ids() or which not in PHOTOS:
        raise Invalid("No such apartment or photo")
    h = store.jread(home_file(hid), {}) or {}
    h.setdefault("photos", {})[which] = {"hero": SAMPLE_HEROES[ids().index(hid) % len(SAMPLE_HEROES)],
                                         "inside": "sample-inside", "area": "sample-area"}[which]
    store.jwrite(home_file(hid), h)


# ---------------------------------------------------------------- the property, and first run

def save_property(body):
    p = store.prop()
    if "name" in body:
        p["property"] = text(body["name"], 60) or "Grace Apartments"
    if "reception" in body:
        r = body["reception"] or {}
        p["reception"] = {"name": text(r.get("name"), 60), "phone": phone(r.get("phone")), "whatsapp": phone(r.get("whatsapp")),
                          "hours": text(r.get("hours"), 80), "note": text(r.get("note"), 200)}
    if "concierge" in body:
        p["concierge"] = {"enabled": bool((body["concierge"] or {}).get("enabled"))}
    store.save_prop(p)
    return p


def seed():
    """First run: one starter apartment with a sample guide and a simulated TV, so the screens have
    something to show. Does nothing once any managed apartment exists."""
    if ids():
        return None
    import tvlink
    hid = create("Apartment 101")
    tvlink.save(hid, "Living room TV")
    return hid
