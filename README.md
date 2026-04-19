# Gameserver Panel

Webový panel pro správu herních serverů (Minecraft, Terraria, Factorio…)
běžící na stejném stroji jako herní server.

---

## Co panel umí

| Funkce | Popis |
|---|---|
| **Dashboard** | Přehled všech serverů, CPU/RAM/disk statistiky, bulk akce |
| **Live konzole** | WebSocket konzole v reálném čase, odesílání příkazů |
| **Metriky** | Grafy CPU, RAM, počtu hráčů – historická data |
| **Správa serverů** | Start / Stop / Restart / Force-stop přes tmux |
| **File manager** | Procházení, editace, upload, download, mazání souborů |
| **Automatické zálohy** | tar.gz zálohy s rotací (max. počet / stáří) |
| **Plánované restarty** | Cron výrazy (např. `0 4 * * *` = každý den ve 4:00) |
| **Whitelist / Ban** | Minecraft whitelist a ban list (RCON nebo JSON soubory) |
| **Email alerty** | Upozornění na crash, CPU/RAM překročení, vzory v logu |
| **Audit log** | Historie všech akcí na serveru |
| **Start profily** | JVM profily pro Minecraft (Xms, Xmx, GC flagy) |
| **RCON** | Vzdálené příkazy přes RCON protokol |
| **Setup wizard** | Průvodce prvním spuštěním – admin účet + základní konfigurace |

---

## Stack

| Vrstva | Technologie |
|---|---|
| Web framework | Django 4.2 |
| Realtime WS | Django Channels 4 + Redis |
| Databáze | PostgreSQL |
| Process mgmt | tmux |
| Metriky | psutil |
| ASGI server | uvicorn |
| Reverse proxy | nginx |
| Grafy | Chart.js 4 |

---

## Nasazení na produkční server (Ubuntu 22.04 / 24.04)

### 1. Systémové závislosti

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev \
    postgresql postgresql-client redis-server tmux nginx git
```

### 2. Systémový uživatel

```bash
sudo useradd -m -s /bin/bash mcpanel
sudo su - mcpanel
```

### 3. Stažení projektu

```bash
git clone <URL_REPOZITARE> /srv/gameserver_panel
cd /srv/gameserver_panel
```

### 4. Python prostředí

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 5. Konfigurace prostředí

```bash
cp .env.example .env
nano .env
```

Vyplň tyto hodnoty v `.env`:

```env
SECRET_KEY=<vygeneruj nahodny retezec, min 50 znaku>
DEBUG=false
ALLOWED_HOSTS=tvoje.domena.cz 1.2.3.4

DB_NAME=gameserver_panel
DB_USER=mcpanel
DB_PASSWORD=<silne heslo>
DB_HOST=127.0.0.1
DB_PORT=5432

REDIS_URL=redis://127.0.0.1:6379/0
```

> Vygenerování `SECRET_KEY`: `python -c "import secrets; print(secrets.token_urlsafe(50))"`

### 6. PostgreSQL databáze

```bash
sudo -u postgres psql -c "CREATE USER mcpanel WITH PASSWORD 'silne-heslo';"
sudo -u postgres psql -c "CREATE DATABASE gameserver_panel OWNER mcpanel;"
```

### 7. Migrace databáze

```bash
cd /srv/gameserver_panel
source .venv/bin/activate
python manage.py migrate
python manage.py collectstatic --noinput
```

> **Nevytvárej superusera ručně** – o to se postará setup wizard při prvním spuštění.

### 8. Systemd služby

Zkopíruj obsah `deploy/systemd/services.conf` do `/etc/systemd/system/`:

```bash
# Zkopíruj každou sekci do vlastního souboru:
sudo nano /etc/systemd/system/gameserver-panel.target
sudo nano /etc/systemd/system/gameserver-panel-web.service
sudo nano /etc/systemd/system/gameserver-panel-worker.service
```

Pak aktivuj:

```bash
sudo systemctl daemon-reload
sudo systemctl enable gameserver-panel.target
sudo systemctl enable gameserver-panel-web.service
sudo systemctl enable gameserver-panel-worker.service
sudo systemctl start gameserver-panel.target
```

Zkontroluj logy:

```bash
journalctl -u gameserver-panel-web.service -f
journalctl -u gameserver-panel-worker.service -f
```

Worker vypíše: `Setup wizard nebyl dokončen – workri cekaji...`
To je normální – čeká na dokončení wizardu.

### 9. nginx

```bash
sudo cp /srv/gameserver_panel/deploy/nginx/gameserver-panel.conf \
        /etc/nginx/sites-available/gameserver-panel
sudo ln -s /etc/nginx/sites-available/gameserver-panel \
           /etc/nginx/sites-enabled/gameserver-panel
sudo nano /etc/nginx/sites-available/gameserver-panel
# Uprav: server_name tvoje.domena.cz;
sudo nginx -t && sudo systemctl reload nginx
```

### 10. HTTPS (Let's Encrypt)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d tvoje.domena.cz
```

### 11. Setup wizard

Otevři prohlížeč a přejdi na `http://tvoje.domena.cz` (nebo IP serveru).
**Automaticky tě přesměruje na setup wizard** kde:

1. Vytvoříš admin účet
2. Nastavíš základní konfiguraci panelu

Po dokončení wizardu se **runtime worker automaticky probudí** a začne pracovat.

---

## Přidání herního serveru

Po dokončení wizardu přejdi na hlavní stránku → **+ Přidat server**.

| Pole | Příklad |
|---|---|
| Name | Minecraft Survival |
| Slug | mc-survival |
| Game type | minecraft_java |
| Working directory | /srv/gameservers/mc-survival |
| Start command | `java -Xmx4G -jar server.jar nogui` |
| Stop command | `stop` |
| tmux session name | mc-survival |
| Log file path | `/srv/gameservers/mc-survival/logs/latest.log` |

> tmux session musí mít **unikátní název** pro každý server.

---

## Oprávnění pro herní servery

Worker běží jako uživatel `mcpanel`. Herní servery musí být přístupné tomuto uživateli:

```bash
sudo chown -R mcpanel:mcpanel /srv/gameservers/
```

---

## Automatické zálohy

Nastav v konfiguraci serveru:
- **Backup directory** – kam ukládat zálohy (např. `/srv/backups/mc-survival`)
- **Backup keep count** – kolik posledních záloh zachovat (výchozí: 7)
- **Backup max age hours** – maximální stáří zálohy v hodinách

Worker automaticky spustí zálohu pokud je záloha starší než nastavený limit.
Zálohy lze spustit i ručně z detailu serveru.

---

## Plánované restarty

Detail serveru → **Restarty** → Přidat plán.

Příklady cron výrazů:

| Výraz | Kdy |
|---|---|
| `0 4 * * *` | Každý den ve 4:00 |
| `0 */6 * * *` | Každých 6 hodin |
| `0 4 * * 1` | Každé pondělí ve 4:00 |
| `30 3 * * 0,6` | Víkendy ve 3:30 |

---

## Správa Minecraft hráčů

Detail serveru → **Whitelist / Bany**

Panel nejprve zkusí RCON (pokud je povoleno v konfiguraci serveru),
jinak přímo edituje `whitelist.json` a `banned-players.json` v pracovním adresáři serveru.

---

## Email alerty

Nastav SMTP v **Nastavení → SMTP konfigurace**.
Pak přidej pravidla v **detailu serveru → Alerty**.

| Typ | Popis |
|---|---|
| `status_change` | Server crashnul nebo je nedostupný |
| `cpu_threshold` | CPU překročilo X % |
| `ram_threshold` | RAM překročila X MB |
| `no_players` | Žádní hráči po X minutách |
| `log_pattern` | Regex vzor v logu (např. `OutOfMemoryError`) |

---

## Diagnostika

```bash
# Worker logy
journalctl -u gameserver-panel-worker.service -f

# Web logy
journalctl -u gameserver-panel-web.service -f

# Stav runtime workerů (v prohlížeči, jen pro adminy)
http://tvoje.domena.cz/runtime/

# Health check
curl http://tvoje.domena.cz/health/
```

---

## Lokální vývoj (bez PostgreSQL a Redis)

```bash
git clone <repo> && cd gameserver_panel
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

USE_SQLITE=true USE_INMEMORY_CHANNEL_LAYER=true python manage.py migrate
USE_SQLITE=true USE_INMEMORY_CHANNEL_LAYER=true python manage.py runserver
# V druhém terminálu:
USE_SQLITE=true USE_INMEMORY_CHANNEL_LAYER=true python manage.py run_runtime_worker
```

Otevři `http://localhost:8000` → projdi setup wizard → hotovo.

---

## Struktura projektu

```
gameserver_panel/
├── config/              – settings, urls, asgi
├── apps/
│   ├── setup/           – setup wizard, SystemSettings, BootstrapMiddleware
│   ├── servers/         – Server model, adaptery, zálohy, profily, plánované restarty
│   ├── console/         – ConsoleLine, WebSocket consumer
│   ├── control/         – tmux backend, akce, mcadmin (whitelist/ban), runtime worker
│   ├── filemanager/     – file browser, editor, upload/download
│   ├── metrics/         – MetricSample/Minute/Hour, agregátor, JSON API
│   ├── audit/           – AuditEvent, audit log
│   ├── alerts/          – AlertRule, engine, email/webhook
│   ├── dashboard/       – přehledové views
│   └── users/           – správa přístupu
├── templates/           – HTML šablony
├── deploy/
│   ├── systemd/         – systemd unit soubory
│   └── nginx/           – nginx konfigurace
├── .env.example         – vzor konfigurace prostředí
└── requirements.txt
```
