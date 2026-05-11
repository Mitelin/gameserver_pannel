# GameServer Panel

Webový panel pro správu herních serverů (Minecraft, GTNH a další).  
Konzole živě, file manager, metriky, start profily, audit log.

---

## Požadavky

| Komponenta | Verze |
|---|---|
| Python | 3.11+ |
| Django | 4.2 |
| Daphne (ASGI) | 4.x |
| SQLite / PostgreSQL | — |
| Redis | pouze pro produkci (více workerů) |

---

## Windows (testování)

### 1. Instalace

```powershell
git clone <repo-url> gameserver_pannel
cd gameserver_pannel

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Databáze

```powershell
python manage.py migrate
python manage.py createsuperuser
```

### 3. Spuštění

```powershell
# Terminál 1 – webový server
daphne -b 0.0.0.0 -p 8000 config.asgi:application

# Terminál 2 – metriky a watchdog (volitelné, pro grafy)
python manage.py run_runtime_worker
```

Panel: **http://127.0.0.1:8000/**

---

## Linux / Debian (produkce)

### 1. Systémové závislosti

```bash
sudo apt update
sudo apt install python3.11 python3.11-venv python3-pip git redis-server -y
```

### 2. Instalace aplikace

```bash
sudo mkdir -p /opt/gameserver_panel
sudo chown $USER /opt/gameserver_panel

git clone <repo-url> /opt/gameserver_panel
cd /opt/gameserver_panel

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Konfigurace

Vytvoř soubor `config/settings_local.py`:

```python
SECRET_KEY = 'změň-na-náhodný-řetězec-min-50-znaků'
DEBUG = False
ALLOWED_HOSTS = ['tvoje-ip', 'tvoje-domena.cz']

# Redis pro WebSocket (více workerů)
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [("127.0.0.1", 6379)]},
    }
}
```

### 4. Databáze

```bash
source .venv/bin/activate
python manage.py migrate
python manage.py createsuperuser
```

### 5. Systemd služby

**Webový server** `/etc/systemd/system/gameserver-panel.service`:

```ini
[Unit]
Description=GameServer Panel
After=network.target redis.service

[Service]
User=www-data
WorkingDirectory=/opt/gameserver_panel
ExecStart=/opt/gameserver_panel/.venv/bin/daphne -b 0.0.0.0 -p 8000 config.asgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Worker** `/etc/systemd/system/gameserver-worker.service`:

```ini
[Unit]
Description=GameServer Panel Worker
After=gameserver-panel.service

[Service]
User=www-data
WorkingDirectory=/opt/gameserver_panel
ExecStart=/opt/gameserver_panel/.venv/bin/python manage.py run_runtime_worker
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Aktivace:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable gameserver-panel gameserver-worker
sudo systemctl start gameserver-panel gameserver-worker
sudo systemctl status gameserver-panel
```

### 6. Nginx (reverse proxy)

```bash
sudo apt install nginx -y
```

`/etc/nginx/sites-available/gameserver-panel`:

```nginx
server {
    listen 80;
    server_name tvoje-domena.cz;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/gameserver-panel /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

## Přidání herního serveru

1. Přihlásit se jako admin → **Nový server**
2. Vyplnit název, slug, typ hry, working directory, start command
3. Uložit → **▶ Start**

### Příklady start command

**Minecraft Paper (Windows):**
```
"C:\Program Files\Java\jdk-25\bin\java.exe" -Xms4G -Xmx4G -jar server.jar nogui
```

**Minecraft Paper (Linux):**
```
/usr/lib/jvm/java-21/bin/java -Xms4G -Xmx4G -jar server.jar nogui
```

**GTNH 2.8.0 (lwjgl3ify):**
```
java -Xms6G -Xmx6G -Dfml.readTimeout=180 @java9args.txt -jar lwjgl3ify-forgePatches.jar nogui
```

---

## Automatická detekce Java

V nastavení Start profilu klikni **🔍 Najít Java** — panel prohledá systém a nabídne nalezené instalace.

---

## Přesun z Windows na Linux

1. Zkopíruj složky herních serverů na Linux (např. `/srv/mc-server`)
2. Přidej servery znovu v panelu — změň jen Working directory a cestu k Javě
3. Start command zůstane stejný (kromě cesty k Javě)

---

## Časté problémy

| Problém | Řešení |
|---|---|
| WebSocket 404 | Použij `daphne`, ne `runserver` |
| Konzole prázdná po restartu | Obnov stránku (file tailer se reconnectne) |
| CPU/RAM = – | Obnov stránku, po 5s se načtou |
| Port 8000 obsazený | `taskkill /F /IM daphne.exe` (Win) / `pkill daphne` (Linux) |
| Server se nezastaví | Použij **✕ Force** v panelu |
| GTNH se zasekne | Potřebuje existující world složku |
