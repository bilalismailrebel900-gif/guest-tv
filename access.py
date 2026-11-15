"""Guest access: one remote link per booking, checked on the server for every request.

A link works only when all of these hold:
  - its token belongs to a confirmed booking that staff have not revoked,
  - that booking is for the apartment named in the request (a link never opens another apartment),
  - that booking is the apartment's current stay: from arrival day until the check-out time.
After check-out, or after staff revoke it, the same link answers 403. Tokens are random (not
derived from anything guessable) and are never written to a log.
"""
import hmac

import stays, store


def booking_for(token):
    if not token or len(token) > 80:
        return None
    return store.row("SELECT * FROM bookings WHERE token = ?", (token,))


def verify(home, token, checkin, checkout, t=None):
    """The booking this token opens in this apartment right now, or None."""
    b = booking_for(token)
    if not b or b["home"] != home or b["status"] != "confirmed" or b["revoked"]:
        return None
    cur = stays.current(home, checkin, checkout, t)
    if not cur or not cur["active"] or cur["booking"] != b["id"]:
        return None
    return b if hmac.compare_digest(b["token"], token) else None


def live_token(home, checkin, checkout, t=None):
    """The token the TV may put in its Guest remote code right now (None: no code on screen)."""
    cur = stays.current(home, checkin, checkout, t)
    if not cur or not cur["active"]:
        return None
    b = stays.get(cur["booking"])
    return None if not b or b["revoked"] else b["token"]


def revoke(bid):
    """Staff: stop this booking's link now. The TV stops showing a Guest remote code for it."""
    store.run("UPDATE bookings SET revoked = 1, updated = ? WHERE id = ?", (stays.stamp(), bid))
    return stays.get(bid)


def reissue(bid):
    """Staff: a fresh link for this booking. The old one stops working at once."""
    store.run("UPDATE bookings SET token = ?, revoked = 0, updated = ? WHERE id = ? AND status = 'confirmed'",
              (stays.new_token(), stays.stamp(), bid))
    return stays.get(bid)
