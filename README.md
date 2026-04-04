# Gameserver Panel – Kompletní dokumentace

Webový panel pro správu game serverů (Minecraft, Terraria, Factorio…)
běžící na stejném stroji jako server.

---

## Stack

| Vrstva           | Technologie                        |
|------------------|------------------------------------|
| Web framework    | Django 4.2                         |
| Realtime WS      | Django Channels 4 + Redis          |
| Databáze         | PostgreSQL                         |
| Process mgmt     | tmux                               |
| Metriky          | psutil                             |
| ASGI server      | uvicorn                            |
| Reverse proxy    | nginx                              |
| Grafy            | Chart.js 4                         |

---

## Quickstart

```bash
# 1. Systémové závislosti
sudo apt install python3.11 python3.11-venv postgresql redis-server tmux nginx

# 2. Systémový uživatel
sudo useradd -m -s /bin/bash mcpanel && sudo su - mcpanel

# 3. Projekt
git clone <repo> /srv/gameserver_panel && cd /srv/gameserver_panel
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 4. Konfigurace
cp .env.example .env && nano .env

# 5. Databáze
sudo -u postgres psql -c "CREATE USER mcpanel WITH PASSWORD 'heslo';"
sudo -u postgres psql -c "CREATE DATABASE gameserver_panel OWNER mcpanel;"
python manage.py migrate
python manage.py createsuperuser

# 6. Spuštění (vývoj – 4 terminály)
uvicorn config.asgi:application --port 8000
python manage.py run_console_tail
python manage.py run_metrics_collector
python manage.py run_server_watchdog
```

Otevři: http://localhost:8000

---

## Přidání serveru

Admin → Servers → Add server:

| Pole               | Příklad                                   |
|--------------------|-------------------------------------------|
| Name               | Minecraft Prod                            |
| Slug               | mc-prod                                   |
| Game type          | minecraft_java                            |
| Working directory  | /srv/gameservers/mc-prod                  |
| Start command      | java -Xmx4G -jar server.jar nogui        |
| Stop command       | stop                                      |
| tmux session name  | mc-prod                                   |
| Log file path      | /srv/gameservers/mc-prod/logs/latest.log  |

---

## Architektura

```
Browser
  │ HTTP REST + WebSocket
  ▼
Django + Channels (ASGI)
  ├── /                    → Server list dashboard
  ├── /servers/<slug>/     → Server detail (konzole, grafy, akce)
  ├── /servers/<slug>/audit/    → Audit log
  ├── /servers/<slug>/players/  → Historie hráčů
  ├── /servers/<slug>/metrics/  → JSON API pro grafy
  ├── /servers/<slug>/rcon/     → RCON příkazy
  ├── /health/             → Health check
  └── /ws/servers/<slug>/ → WebSocket (konzole + status + metriky)

Background workers (systemd):
  ├── run_console_tail      → log tailer, WS push, startup detection,
  │                           player tracking, log pattern alerts
  ├── run_metrics_collector → psutil metriky, MetricSample, agregace
  └── run_server_watchdog   → stavový automat, crash detection,
                              webhook + alert engine
```

---

## WebSocket zprávy

```
Klient → Server:
  { "type": "console.command", "command": "say hello" }
  { "type": "ping" }

Server → Klient:
  { "type": "console.line",    "line": "...",   "timestamp": "...", "replay": true }
  { "type": "server.status",   "status": "ONLINE" }
  { "type": "metrics.snapshot","cpu_percent": 23.1, "ram_bytes": 2048000000, ... }
  { "type": "audit.event",     "event_type": "...", "severity": "info", ... }
  { "type": "command.update",  "ok": true, "message": "Příkaz odeslán" }
  { "type": "error",           "message": "..." }
```

---

## Stavový automat serveru

```
OFFLINE ──[start]──► STARTING ──[startup pattern]──► ONLINE
                         │                               │
                    [timeout]                       [stop cmd]
                         ▼                               ▼
                      CRASHED              STOPPING ──[gone]──► OFFLINE
                                               │
                                       [pending_restart]──► STARTING
ONLINE ──[log silence 120s]──► UNKNOWN ──[PID+tmux OK]──► ONLINE
                                   └──[3× selhal]──► CRASHED
```

---

## Game adaptery

| game_type          | Startup detection        | Player events | TPS |
|--------------------|--------------------------|---------------|-----|
| minecraft_java     | `Done (X.XXXs)!`         | ✓             | ✓   |
| minecraft_bedrock  | `Server started.`        | ✓             | –   |
| terraria           | `Server started`         | ✓             | –   |
| factorio           | `CreatingGame→InGame`    | ✓             | –   |
| other              | –                        | –             | –   |

Vlastní adapter:
```python
from apps.servers.adapters import BaseGameAdapter, register_adapter

class MyGameAdapter(BaseGameAdapter):
    def startup_patterns(self): return [r"Server ready"]
    def shutdown_patterns(self): return [r"Shutting down"]
    def parse_console_line(self, line): return {"raw": line, "level": "info"}
    def extract_player_events(self, line): return None

register_adapter("my_game", MyGameAdapter)
```

---

## RCON

Povolení v admin: Server → RCON enabled ✓, vyplň host/port/password.

Použití přes API:
```bash
curl -X POST /servers/mc-prod/rcon/ \
  -H "X-CSRFToken: ..." \
  -d '{"command": "list"}'
# {"ok": true, "response": "There are 2 of 20 players online: Steve, Alex"}
```

---

## Alerting

Admin → Alert Rules → Add:

| Podmínka     | Příklad                        |
|--------------|--------------------------------|
| status_change| Status values: `CRASHED,UNKNOWN`|
| cpu_threshold| Threshold: `90` (%)            |
| ram_threshold| Threshold: `8192` (MB)         |
| no_players   | Duration minutes: `60`         |
| log_pattern  | Log pattern: `OutOfMemoryError`|

Zpráva supports: `{server_name}`, `{status}`, `{value}`, `{condition}`, `{details}`

---

## Metriky a retention

| Tabulka       | Granularita | Retention |
|---------------|-------------|-----------|
| MetricSample  | ~12 sekund  | 7 dní     |
| MetricMinute  | 1 minuta    | 60 dní    |
| MetricHour    | 1 hodina    | navždy    |
| ConsoleLine   | realtime    | 14 dní    |
| AuditEvent    | –           | navždy    |

```bash
# Ruční cleanup (preview)
python manage.py run_retention_cleanup --dry-run

# Skutečné smazání s custom retention
python manage.py run_retention_cleanup --raw-days=3 --console-days=7
```

---

## Management commands

```bash
python manage.py run_console_tail        # log tailer
python manage.py run_metrics_collector   # metrics + agregace
python manage.py run_server_watchdog     # watchdog + alerty
python manage.py run_retention_cleanup   # cleanup starých dat
python manage.py run_metrics_aggregator --all  # ruční agregace
```

---

## Health check

```
GET /health/          → {"status":"ok","checks":{"database":...,"redis":...,"tmux":...}}
GET /health/?backups=1 → přidá backup status všech serverů
```

HTTP 200 = vše OK, HTTP 503 = degraded.

---

## Produkční deployment (systemd)

Zkopíruj unit soubory z `deploy/systemd/services.conf` a:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gameserver-panel.target
```

### nginx konfigurace

```nginx
server {
    listen 80;
    server_name tvoje.domena.cz;

    location /static/ { alias /srv/gameserver_panel/staticfiles/; }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
```

---

## Struktura projektu

```
gameserver_panel/
├── config/           – settings, urls, asgi, wsgi
├── apps/
│   ├── servers/      – Server model, adapters, validators,
│   │                   player_tracker, player_session, backup
│   ├── console/      – ConsoleLine, CommandHistory,
│   │                   WS consumer, log tailer
│   ├── control/      – service, tmux backend, RCON backend,
│   │                   metrics collector, watchdog
│   ├── metrics/      – MetricSample/Minute/Hour, aggregator,
│   │                   JSON API pro grafy
│   ├── audit/        – AuditEvent, audit log stránka
│   ├── alerts/       – AlertRule, AlertFire, engine, RCON view
│   ├── dashboard/    – views, player history
│   └── common/       – health check, rate limiter, retention cleanup
├── templates/
│   ├── base.html
│   ├── dashboard/    – server_list, server_detail, player_history
│   ├── audit/        – audit_log
│   └── registration/ – login
└── deploy/systemd/   – service unit soubory
```

---

## Fáze vývoje – co bylo implementováno

| Fáze | Co přibylo |
|------|-----------|
| 1    | Server model, tmux backend, WebSocket konzole, start/stop/restart, audit, watchdog základní, log tailer |
| 2    | MetricSample/Minute/Hour, agregátor, retention, JSON API, Chart.js grafy v UI |
| 3    | Rate limiting, config validace, přepracovaný watchdog s timeouty, graceful shutdown, audit log stránka, retention command |
| 4    | Kompletní game adaptery (MC Java/Bedrock, Terraria, Factorio), RCON backend, alert system (5 typů podmínek), multi-server dashboard, player tracking |
| 5    | PlayerSession (join/leave historie), player history stránka, backup status checker, rozšířený health endpoint |
