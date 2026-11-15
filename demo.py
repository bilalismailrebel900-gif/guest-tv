"""Demo mode: pretend apartments, guests and TVs for trying guest-tv out and for development.

    sh scripts/grace.sh demo        (GUEST_TV_DEMO=1, data in demo-data/, never in data/)

Four apartments, one at each moment of a stay (arrival day, mid-stay, check-out day, between
bookings), each with a simulated TV. Nothing in demo mode ever reaches a real TV, and the real
data folder is not touched. The dashboard password is "grace-demo" and the sign-in page says so.
Every name, Wi-Fi password and phone number here is made up.
"""
import datetime as dt

import content, host, stays, store, tvlink

APARTMENTS = [   # name, moment today, guest, wifi
    ("Apartment 101", "arrival", "Ayesha", "Grace-101", "welcome-lahore-101"),
    ("Apartment 204", "during", "Daniel", "Grace-204", "welcome-lahore-204"),
    ("Apartment 305", "lastday", "Fatima", "Grace-305", "welcome-lahore-305"),
    ("Penthouse 501", "between", "", "Grace-501", "welcome-lahore-501"),
]
RECEPTION = {"name": "Reception (demo)", "phone": "+92 300 0000000", "whatsapp": "+92 300 0000000",
             "hours": "Open 24 hours", "note": "Demo number. Put the real one in the dashboard."}


def seed():
    today = stays.now().date()
    pv = store.private()
    if not content.ids():
        for name, _, _, ssid, pw in APARTMENTS:
            hid = content.create(name)
            content.save(hid, {"wifi": {"ssid": ssid, "password": pw}})
            tvlink.save(hid, "Living room TV")
        content.save_property({"name": "Grace Apartments", "reception": RECEPTION})
        host.set_password(host.DEMO_PASSWORD)
        pv = store.private()
    if pv.get("demo_seeded") == today.isoformat():
        return
    store.run("DELETE FROM bookings")   # the pretend stays move with the calendar
    day = lambda n: (today + dt.timedelta(days=n)).isoformat()
    for hid, (_, moment, guest, _, _) in zip(content.ids(), APARTMENTS):
        if moment == "arrival":
            stays.add(hid, guest, day(0), day(3), 2)
            stays.add(hid, "Omar", day(6), day(9), 1)
        elif moment == "during":
            stays.add(hid, guest, day(-1), day(2), 2)
            b = stays.add(hid, "Sana", day(5), day(7), 3)
            stays.cancel(b["id"], "Cancelled by staff")
        elif moment == "lastday":
            stays.add(hid, guest, day(-3), day(0), 4)
            stays.add(hid, "", day(1), day(4), 0, note="Name to follow")
        else:
            stays.add(hid, "Bilal", day(-6), day(-2), 2)
            stays.add(hid, "Hina", day(2), day(5), 2)
    pv = store.private()
    pv["demo_seeded"] = today.isoformat()
    store.save_private(pv)
