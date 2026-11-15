#!/bin/sh
# Grace Apartments guest TV: start, stop and look after it on the host computer.
#
#   sh scripts/grace.sh start      start it in the background (real data in data/)
#   sh scripts/grace.sh stop       stop it
#   sh scripts/grace.sh restart
#   sh scripts/grace.sh status     is it running, and what each apartment shows now
#   sh scripts/grace.sh demo       demo mode in this window: pretend guests, simulated TVs, data in demo-data/
#   sh scripts/grace.sh password   set or change the host dashboard password
#   sh scripts/grace.sh backup     a dated zip of data/ in backups/
#   sh scripts/grace.sh test       the checks (no TV, no internet, no AI)
#   sh scripts/grace.sh install    start it after every restart of this computer (systemd or launchd)
#   sh scripts/grace.sh uninstall  undo that
set -e
cd "$(dirname "$0")/.."
DIR=$(pwd)
PID="$DIR/logs/guest-tv.pid"
mkdir -p logs

setup() {
  if [ ! -x .venv/bin/python ]; then
    PY=""
    for p in python3.13 python3.12 python3.11 python3.10 python3; do
      if command -v "$p" > /dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2> /dev/null; then PY=$p; break; fi
    done
    [ -n "$PY" ] || { echo "guest-tv needs Python 3.10 or newer (Linux: sudo apt install python3 python3-venv)."; exit 1; }
    echo "Making the Python environment (a minute)"
    "$PY" -m venv .venv
    .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
  fi
}

running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2> /dev/null; }

address() { .venv/bin/python -c 'from server import PORT, lan_ip as ip; print("http://%s:%d" % (ip(), PORT))' 2> /dev/null || echo "http://<this computer>:5410"; }

case "${1:-}" in
  start)
    setup
    if running; then echo "guest-tv is already running (pid $(cat "$PID"))."; exit 0; fi
    if ! .venv/bin/python -c 'import host, sys; sys.exit(0 if host.has_password() else 1)'; then
      echo "First, a password for the host dashboard:"
      .venv/bin/python manage.py set-password
    fi
    nohup .venv/bin/python server.py >> logs/guest-tv.log 2>&1 &
    echo $! > "$PID"
    sleep 2
    if running; then
      A=$(address)
      echo "guest-tv is running."
      echo "  Host dashboard:  $A/host/"
      echo "  TV screens:      the address for each TV is in the dashboard, under TVs"
      echo "  Log:             logs/guest-tv.log     Stop: sh scripts/grace.sh stop"
    else
      echo "It did not start. The last lines of logs/guest-tv.log:"; tail -n 20 logs/guest-tv.log; exit 1
    fi ;;
  stop)
    if running; then kill "$(cat "$PID")"; rm -f "$PID"; echo "guest-tv stopped."; else rm -f "$PID"; echo "guest-tv was not running (if a service runs it: sh scripts/grace.sh uninstall, or sudo systemctl stop guest-tv)."; fi ;;
  restart) "$0" stop; "$0" start ;;
  status)
    setup
    if running; then echo "Running (pid $(cat "$PID")) at $(address)"; else echo "Not started by this script (a service may be running it)."; fi
    .venv/bin/python manage.py status ;;
  demo)
    setup
    echo "Demo mode: pretend guests, simulated TVs, data in demo-data/. Dashboard password: grace-demo. Ctrl+C stops it."
    GUEST_TV_DEMO=1 GUEST_TV_DATA="$DIR/demo-data" PORT="${PORT:-5411}" exec .venv/bin/python server.py ;;
  password) setup; .venv/bin/python manage.py set-password ;;
  backup) setup; .venv/bin/python manage.py backup "${2:-$DIR/backups}" ;;
  test) setup; .venv/bin/python tools/grace_test.py && .venv/bin/python tools/security_test.py | tail -n 1 ;;
  install)
    setup
    if ! .venv/bin/python -c 'import host, sys; sys.exit(0 if host.has_password() else 1)'; then .venv/bin/python manage.py set-password; fi
    "$0" stop > /dev/null 2>&1 || true
    if [ "$(uname)" = Darwin ]; then
      mkdir -p "$HOME/Library/LaunchAgents"
      sed "s|FOLDER|$DIR|g" service/guest-tv.plist > "$HOME/Library/LaunchAgents/guest-tv.plist"
      launchctl bootout "gui/$(id -u)/guest-tv" 2> /dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/guest-tv.plist"
      echo "guest-tv now starts by itself after a restart. Keep this Mac awake: System Settings, Energy, Prevent automatic sleeping."
    else
      sed -e "s|FOLDER|$DIR|g" -e "s|USER|$(id -un)|g" service/guest-tv.service > "/tmp/guest-tv.service.$$"
      sudo cp "/tmp/guest-tv.service.$$" /etc/systemd/system/guest-tv.service; rm -f "/tmp/guest-tv.service.$$"
      sudo systemctl daemon-reload; sudo systemctl enable --now guest-tv
      echo "guest-tv now starts by itself after a restart (sudo systemctl status guest-tv; logs: journalctl -u guest-tv)."
    fi
    echo "Host dashboard: $(address)/host/" ;;
  uninstall)
    if [ "$(uname)" = Darwin ]; then launchctl bootout "gui/$(id -u)/guest-tv" 2> /dev/null || true; rm -f "$HOME/Library/LaunchAgents/guest-tv.plist"
    else sudo systemctl disable --now guest-tv || true; sudo rm -f /etc/systemd/system/guest-tv.service; sudo systemctl daemon-reload; fi
    echo "guest-tv no longer starts by itself." ;;
  *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
