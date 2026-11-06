# system-dashboard

A small live system monitor: a Python server (`server.py`) streams CPU, memory,
network, disk, sensor and process stats to a single-page UI (`index.html`).

## Run

```bash
pip install psutil
python3 server.py
```

Then open http://localhost:8080.

## Configuration

| Variable          | Default   | Meaning                                              |
|-------------------|-----------|------------------------------------------------------|
| `DASHBOARD_PORT`  | `8080`    | Port to listen on                                    |
| `DASHBOARD_BIND`  | `0.0.0.0` | Address to bind. Use `127.0.0.1` to keep it local.   |

The dashboard has no authentication and exposes process and network details,
so bind to `127.0.0.1` unless you want it reachable from your LAN.
