# Grace Apartments guest TV

A welcome screen on each apartment's TV, a remote on the guest's phone, and a password-protected
dashboard for staff. Built on [guest-tv](README.md) (MIT licence, see `LICENSE`), with a layer for
running several apartments from one computer: `stays.py`, `access.py`, `tvlink.py`, `content.py`,
`host.py`, `concierge.py`, `store.py`.

## Try it in two minutes (demo mode)

```bash
sh scripts/grace.sh demo
```

Then open, on the same computer:

| Page | Address |
|---|---|
| Host dashboard | `http://localhost:5411/host/` (password `grace-demo`) |
| TV, arrival day | `http://localhost:5411/tv/?screen=apartment-101-living-room-tv` |
| TV, mid-stay | `http://localhost:5411/tv/?screen=apartment-204-living-room-tv` |
| TV, check-out day | `http://localhost:5411/tv/?screen=apartment-305-living-room-tv` |
| TV, between bookings | `http://localhost:5411/tv/?screen=penthouse-501-living-room-tv` |
| Guest remote | In the dashboard: an apartment, Bookings, **Show** next to a booking |

Demo mode uses pretend guests and simulated TVs, keeps its data in `demo-data/` (never in `data/`),
and pins the time of day at 10:30 so every moment of a stay is on show whenever you look. It never
talks to a real TV. If the weather service cannot be reached, demo mode shows a made-up forecast
marked "(sample)"; the real system shows no forecast instead.

Arrow keys, Enter and Escape work on the TV page like a TV remote. Open a TV page and a Guest
remote side by side: the phone's buttons move the TV page.

## Running it for real

On a computer that stays on, on the same network as the TVs (Linux, a Raspberry Pi or a Mac;
Python 3.10 or newer):

```bash
sh scripts/grace.sh start      # first time: makes the Python environment and asks for a dashboard password
sh scripts/grace.sh stop
sh scripts/grace.sh restart
sh scripts/grace.sh status     # running or not, and what each apartment shows now
```

`start` prints the dashboard's address (`http://<this computer>:5410/host/`). The first run makes
one starter apartment, "Apartment 101", with a sample guide, sample images and a simulated TV. In
the dashboard:

1. **Settings**: the property name and the reception contact.
2. **Add apartment** for each apartment, then per apartment:
   - **Details and Wi-Fi**: name, check-in and check-out times, guest Wi-Fi, reception contact,
     streaming shortcuts, and (optional) the Airbnb calendar link.
   - **Photographs**: upload your own. Until then the screens show drawings labelled
     "SAMPLE IMAGE". A landscape photo at least 1920 wide; 3840 wide for a 4K TV.
   - **Guide** and **Nearby places**: edit the sample text. Entries that start "Sample" are
     placeholders to replace.
   - **TVs**: one row per TV. See "Connecting a real TV" below.
   - **Bookings**: guest first name, arrival and departure dates.

All stay times are Asia/Karachi (`"timezone"` in `data/settings.json`), whatever the computer's or
the TV's own clock zone is. The weather is Lahore (`"weather"` in the same file).

### Start automatically after a restart

```bash
sh scripts/grace.sh install      # Linux: a systemd service (asks for sudo). Mac: a launchd agent.
sh scripts/grace.sh uninstall    # undo
```

On Linux check it with `sudo systemctl status guest-tv` and read its log with
`journalctl -u guest-tv`. The computer must not sleep. Give it a fixed address on the router (a DHCP
reservation), because the TVs and the QR codes use that address.

### Backups

Everything staff enter lives in `data/`: apartments (`data/homes/`), bookings, TVs and sync status
(`data/guesttv.db`), photographs (`data/photos/`), and private values such as Wi-Fi passwords, the
calendar links and the password hash (`data/private.json`).

```bash
sh scripts/grace.sh backup                    # writes backups/guest-tv-backup-<date>.zip
.venv/bin/python manage.py backup /media/usb  # or to another folder
.venv/bin/python manage.py restore backups/guest-tv-backup-20261115-1030.zip   # stop guest-tv first
```

The dashboard's Settings page has **Download a backup** too. A backup holds Wi-Fi passwords and
guest names: keep it somewhere private. To back up daily on Linux, add to `crontab -e`:
`15 3 * * * cd /path/to/guest-tv && sh scripts/grace.sh backup`. `restore` keeps the folder as it
was in `data.before-restore/`.

## What guests see

- **TV**: the apartment name, the guest's first name, stay dates, check-in and check-out times, the
  clock, Lahore weather, the Wi-Fi with a scan-to-connect code, the house guide, the reception
  contact, check-out instructions, nearby places, and shortcuts for the streaming apps staff
  ticked. A second code opens the Guest remote. Laid out for 16:9 at 1080p and 4K (everything
  scales with the screen width, and body text is 23 px or more at 1080p), driven by the TV remote's arrows,
  OK and Back, with a white outline on whatever has the focus.
- **The moments of a stay**: arrival day (from midnight until check-in time: "Welcome, Ayesha",
  check-in time), during the stay ("Night 2 of 3"), check-out day (check-out steps first), and
  between bookings (welcome, weather, nearby places, reception; no name, no Wi-Fi, no codes). The
  screen changes by itself at check-in and check-out time and within 15 seconds of a change in the
  dashboard. On a same-day changeover the leaving guest keeps the screen until check-out and the
  arriving guest gets it straight after.
- **Guest remote** (phone browser, no app): arrows, OK, Back, Home, play/pause, volume, mute, the
  app shortcuts, an honest status line for the TV, plus the stay, Wi-Fi, apartment guide,
  reception (call and WhatsApp buttons), nearby places with map links, and an Ask box. It controls
  the TV only.

Streaming subscriptions are not part of this: guests sign in to Netflix, YouTube or Prime Video
with their own accounts.

## Bookings

- **Manual** (always works): staff type the first name and dates. Overlapping dates in one
  apartment are refused; a same-day changeover is fine. Edit, cancel, restore and delete are in the
  list.
- **Airbnb calendar (optional)**: paste the listing's calendar export link in Details. Every 15
  minutes (and on **Read now**) reservation dates come in, moved reservations follow, and a
  reservation Airbnb removed is marked cancelled with the reason. If the link cannot be read, or
  returns something that is not a calendar, nothing is changed and the dashboard shows the problem.
  Airbnb's link carries no guest names: type the name on the booking.
- **Booking emails (optional)**: save Airbnb confirmation emails as `.eml` files into
  `data/bookings-inbox/`, or set up a read-only mailbox (README, "Airbnb bookings"). A confirmation
  gives the matching Airbnb booking its first name; a cancellation email cancels it. A name staff
  typed always wins. Not yet checked against a real Airbnb email: compare the first one by eye.
- A month after check-out (`"forget_guest_names_after_days"`), the guest's name and note are wiped
  from the booking record.

## Guest access

Each booking has its own random link (`/r/?home=...&c=<token>&tv=...`), shown as the TV's "Guest
remote" code and under **Show** in the dashboard. The server checks every request:

- the token belongs to a confirmed booking that staff have not revoked,
- the booking is for the apartment in the request, and the TV in the request belongs to that
  apartment,
- the booking is that apartment's current stay (arrival day until the check-out time).

Otherwise the answer is 403. **Revoke** stops a link at once and takes the code off the TV;
**New link** issues a fresh one and kills the old. Five wrong codes from one address in a minute
get 429 for the rest of that minute. The dashboard password is stored as a salted scrypt hash;
sessions are HttpOnly, SameSite=Strict cookies that last 12 hours; eight wrong passwords lock
sign-in for five minutes. Calendar links, the API key and the password hash are never sent to a
TV or a guest.

Keep in mind: the Guest remote and the TV pages work on the local network only. Do not forward a
port to this computer. Anyone on the same network who knows a TV's page address can read what that
TV shows (as they could by looking at the TV), so put guests on a guest Wi-Fi that cannot reach
other apartments' TVs if your router allows it.

## Connecting a real TV

Every TV starts **simulated**: the Guest remote works and moves the welcome screen in any browser
showing that TV's page, and nothing is sent to hardware. The remote says "Demo: simulated TV".

To connect one, standing at the TV with it muted:

1. Show the welcome screen on it. Google TV, Android TV or Fire TV: install the Guest TV app
   (README, "The quick way") and give it this computer's address; for Samsung, LG and the others
   see README "Will it work on my TV?" and `docs/`. Each TV's own page address is in the dashboard
   under TVs (`/tv/?screen=<tv id>`); a TV whose address is typed in the dashboard is also
   recognised when it opens the plain `/tv/` address.
2. In the dashboard, TVs: choose the kind, type the TV's address (TV Settings, Network), Save.
3. **Check connection**. It only reads (the TV's app list or power state). A Google TV or Android
   TV needs Developer options with USB or Network debugging on, and asks "Allow?" once: choose
   Always allow. Other kinds are paired once on this computer with
   `TV_KIND=<kind> TV_IP=<address> .venv/bin/python tools/tv_app.py pair`.
4. When the check passes, switch **Send real commands** on. Changing the TV's kind, address or
   apartment switches it off again until a new check passes.
5. Test from a phone: arrows, OK, Back, Home, volume, and each app shortcut.

The status is then "Connected", or "not answering" when the TV is off or unreachable. For a TV that
is not Android, restart guest-tv once after switching real commands on, so it starts opening the
welcome screen when that TV comes on.

## Optional AI concierge

Off by default. The Ask box always works: without AI it searches the apartment's guide and places.
To have Claude write the answers instead:

1. `.venv/bin/pip install anthropic`
2. Put an Anthropic API key on this computer only: `"anthropic_api_key": "sk-ant-..."` in
   `data/private.json` (or `ANTHROPIC_API_KEY` in the service's environment).
3. Dashboard, Settings: **Switch the AI concierge on**.

It answers only from that apartment's guide and approved places, has no tools (it cannot press a
TV button or open a web page), and is limited to 20 questions an hour per guest link. The code adds
the buttons: a map link for each place it names, and **Book a table** when staff gave that place
its own booking page under Nearby places. Web addresses in the model's text are removed. The model
is `claude-opus-5-5` (`"concierge": {"model": ...}` in `data/settings.json`), and a declined
request is re-run on a fallback model by the API. The sample guide has no booking links: add each
restaurant's own page yourself.

## Checks

```bash
sh scripts/grace.sh test
```

`tools/grace_test.py` (79 checks, no TV, no internet, no AI) covers: each moment of a stay, booking
edits and cancellations, expiry at check-out, same-day changeovers, revoking and reissuing links,
separation between two apartments (links, Wi-Fi, TVs, key presses), the dashboard sign-in, the
Airbnb sync with cancellations and failed fetches, email names, the no-AI concierge, and recovery
after a hard stop and restart. `tools/security_test.py` covers the guard rails that came with
guest-tv.

## What works where

| Works on this computer alone | Needs a TV paired and checked | Needs something from outside |
|---|---|---|
| Dashboard, apartments, photographs, guides, places | Real button presses and app shortcuts from the Guest remote | Lahore weather: internet access to api.open-meteo.com (free, no key) |
| Manual bookings and every moment of a stay | The welcome screen on the TV itself (the Guest TV app or the TV's browser) | Airbnb dates: the listing's calendar export link |
| Guest links, expiry, revoking | Opening the welcome screen when a non-Android TV comes on | Guest names from email: saved `.eml` files or a mailbox app password |
| Guest remote with a simulated TV | | AI concierge answers: an Anthropic API key |
| Guide search in the Ask box (no AI) | | Restaurant booking buttons: each place's own booking page, typed by staff |
| Backups, restore, restart recovery | | |
