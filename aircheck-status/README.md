# aircheck-status

A live status board for the radio, in a browser tab. It renders the same
readouts the terminal `dashboard` shows (services, transmitter, what's
playing, recent anomalies), fed over plain HTTP so anyone on the network can
watch without an SSH session.

## Pieces

- `logserver.py` runs next to the player and exposes what already exists:
  `GET /logs` streams the service journal (chunked, follows live);
  `GET /status.json` snapshots the systemd unit states plus the TX and log
  status files. Read-only, no control surface. `GET /dash/` serves the page
  itself so it can ride the same host as the audio stream.
- `devserver.py` is the local counterpart: it serves the page and relays the
  two endpoints from a running station, keeping a single upstream connection
  no matter how many tabs are open.
- `dashboard/` is the page. It parses the raw journal lines the same way the
  watchdog does and repaints the board once a second; playback of the
  station's own stream is built in.
- `aircheck-dash.service` is a user unit for `logserver.py`.

## Run

On the station:

    python3 logserver.py --port 8775

Locally against a running station:

    python3 devserver.py --pi http://<host>:8775
    # then open http://localhost:8901/
