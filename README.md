# GameServer Panel

Webový panel pro správu herních serverů (Minecraft, GTNH a další).

**Funkce:** živá konzole · file manager · metriky (CPU/RAM/disk/hráči) · plánované restarty · start profily · Java auto-detekce · audit log · file browser · whitelist/OP manager

---

## Požadavky

| Komponenta | Verze | Poznámka |
|---|---|---|
| Python | 3.11+ | |
| Django | 4.2 | |
| Daphne | 4.x | ASGI server, nutný pro WebSocket |
| croniter | 6.x | plánované restarty |
| psutil | 7.x | metriky procesů |
| SQLite | — | výchozí, pro produkci PostgreSQL |
| Redis | 7.x | pouze pro produkci (více workerů) |

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

> **Důležité:** Používej `daphne`, ne `runserver` — WebSocket nefunguje bez ASGI serveru.

```powershell
# Terminál 1 – webový server (povinné)
.venv\Scripts\activate
daphne -b 0.0.0.0 -p 8000 config.asgi:application

# Terminál 2 – worker (pro grafy, hráče, plánované restarty)
.venv\Scripts\activate
python manage.py run_runtime_worker
```

Panel: **http://127.0.0.1:8000/**  
Přihlášení: admin / (heslo z createsuperuser)

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

# Redis pro WebSocket (nutné pro produkci)
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
Description=GameServer Panel (Daphne)
After=network.target redis.service

[Service]
User=www-data
WorkingDirectory=/opt/gameserver_panel
ExecStart=/opt/gameserver_panel/.venv/bin/daphne -b 0.0.0.0 -p 8000 config.asgi:application
Restart=always
RestartSec=5
Environment=DJANGO_SETTINGS_MODULE=config.settings

[Install]
WantedBy=multi-user.target
```

**Worker** `/etc/systemd/system/gameserver-worker.service`:

```ini
[Unit]
Description=GameServer Panel Worker (metriky, watchdog, scheduler)
After=network.target redis.service gameserver-panel.service

[Service]
User=www-data
WorkingDirectory=/opt/gameserver_panel
ExecStart=/opt/gameserver_panel/.venv/bin/python manage.py run_runtime_worker
Restart=always
RestartSec=10
Environment=DJANGO_SETTINGS_MODULE=config.settings

[Install]
WantedBy=multi-user.target
```

**Aktivace:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable gameserver-panel gameserver-worker
sudo systemctl start gameserver-panel gameserver-worker
sudo systemctl status gameserver-panel
sudo systemctl status gameserver-worker
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
2. Vyplnit:
   - **Název** — libovolný
   - **Slug** — URL identifikátor (malá písmena, čísla, pomlčky)
   - **Typ hry** — Minecraft Java / Bedrock / jiný
   - **Working directory** — absolutní cesta ke složce serveru
   - **Start command** — příkaz pro spuštění
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

**GTNH 2.8.0 (lwjgl3ify, Java 17–25):**
```
java -Xms6G -Xmx6G -Dfml.readTimeout=180 @java9args.txt -jar lwjgl3ify-forgePatches.jar nogui
```

---

## Funkce panelu

### Konzole
- Živá konzole přes WebSocket — výstup serveru v reálném čase
- Příkazy odesílané přímo do stdin procesu
- Po restartu panelu se konzole automaticky reconnectne (file tailer)
- Historie posledních 200 řádků při připojení

### Metriky (status bar)
| Položka | Zdroj |
|---|---|
| CPU % | psutil — součet celého process tree |
| RAM | psutil — skutečná paměť Java procesu |
| Hráči | parsování logů (runtime worker) |
| Vlákna | psutil |
| PID | Java proces (ne cmd.exe wrapper) |
| Disk | psutil — využití disku working directory |

> Grafy (historické metriky) vyžadují běžící `run_runtime_worker`.

### Plánované restarty

Správa: **Konfigurace → ⏰ Restarty** (dostupné adminům)

Formát: standardní 5-polní cron výraz.

| Cron | Kdy |
|---|---|
| `0 4 * * *` | každý den ve 4:00 |
| `0 */6 * * *` | každých 6 hodin |
| `0 4 * * 1` | každé pondělí ve 4:00 |

**Varování před restartem** (nastavitelné v minutách):
- ≥ 10 min → varování v N min, 5 min a 1 min předem
- 5–9 min → varování v N min a 1 min předem
- 1–4 min → jedno varování

Zprávy se odesílají příkazem `say` přímo do konzole serveru.

### Automatická detekce Java

V **Start profilu** klikni **🔍 Najít Java** — panel prohledá:
- Registry (Windows)
- `C:\Program Files\Java\*`, Eclipse Adoptium, Microsoft, Amazon Corretto…
- `JAVA_HOME`, `PATH`
- Prism Launcher bundled Java
- `/usr/lib/jvm/*` (Linux), `update-alternatives` (Linux)

### File manager
- Windows Explorer styl — ikony, seznam, dvojklik, pravé tlačítko
- Drag & drop upload
- Editace textových souborů přímo v prohlížeči
- Stahování souborů

### Whitelist / OP / Bany
Dostupné v **⚔ Whitelist / Bany** — přidávání a odebírání hráčů přímo z panelu.

---

## GTNH specifika

### World složka
GTNH potřebuje existující world složku — nejde generovat od nuly (zasekne se).  
Zkopíruj `World/` ze stávajícího serveru nebo ze singleplayer save.

### Singleplayer save jako server world
```
# Prism Launcher save → server world
C:\Users\<jmeno>\AppData\Roaming\PrismLauncher\instances\<instance>\.minecraft\saves\<world>\
→ zkopírovat do: D:\gtnh-server\World\
```

### Doporučené nastavení server.properties
```properties
level-type=DEFAULT        # ne rwg při novém světě
online-mode=false         # pro lokální testování
white-list=false          # při testování
view-distance=8           # GTNH je náročný
```

### Plánovaný restart
GTNH server má nastaven automatický restart každý den ve **4:00**.  
Hráči dostanou varování 5 minut a 1 minutu předem.

---

## Přesun z Windows na Linux

1. Zkopíruj složky herních serverů na Linux (např. `/srv/gtnh-server`)
2. Přidej servery znovu v panelu — změň Working directory a cestu k Javě
3. Start command zůstane stejný (pouze cesta k Javě se změní)
4. Spusť systemd služby (viz výše)

---

## Časté problémy

| Problém | Příčina | Řešení |
|---|---|---|
| WebSocket 404 | `runserver` místo `daphne` | Použij `daphne -b 0.0.0.0 -p 8000 config.asgi:application` |
| Konzole prázdná po restartu panelu | File tailer se znovu připojí | Obnov stránku v prohlížeči |
| CPU/RAM = – | Daphne nenačetla nový kód | Restartuj daphne, obnov stránku |
| Grafy prázdné | Runtime worker neběží | Spusť `python manage.py run_runtime_worker` |
| Hráči se nepočítají | Runtime worker neběží | Spusť `python manage.py run_runtime_worker` |
| Plánované restarty nefungují | Runtime worker neběží | Spusť `python manage.py run_runtime_worker` |
| Port 8000 obsazený | Starý process běží | `taskkill /F /IM daphne.exe` (Win) / `pkill daphne` (Linux) |
| Server se nezastaví | Proces neodpovídá | Použij **✕ Force** v panelu |
| GTNH se zasekne při startu | Chybí world složka nebo RWG bug | Zkopíruj world složku ze stávajícího serveru |
