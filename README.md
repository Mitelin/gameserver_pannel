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

## Lokální vývoj (Windows i Linux)

### 1. Instalace

**Windows:**

```powershell
git clone <repo-url> gameserver_panel
cd gameserver_panel

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Linux:**

```bash
git clone <repo-url> gameserver_panel
cd gameserver_panel

python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

### 2. `.env`

Zkopíruj `.env.example` na `.env` a pro lokální běh nastav minimálně:

```dotenv
SECRET_KEY=dev-secret-change-me
DEBUG=true
ALLOWED_HOSTS=localhost 127.0.0.1
USE_SQLITE=true
USE_INMEMORY_CHANNEL_LAYER=true
```

Tohle je výchozí lokální režim bez PostgreSQL a bez Redis.

### 3. Databáze

**Windows:**

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
```

**Linux:**

```bash
./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py createsuperuser
```

### 4. Start skripty

Repo obsahuje jednotný launcher `scripts/dev.py` a dva wrappery:

| Platforma | Wrapper |
|---|---|
| Windows | `scripts/start-dev.ps1` |
| Linux | `scripts/start-dev.sh` |

Podporované režimy:

| Režim | Co dělá |
|---|---|
| `check` | spustí `manage.py check` |
| `migrate` | spustí migrace |
| `web` | spustí ASGI development server |
| `worker` | spustí unified runtime worker |
| `all` | spustí web a pokud je nakonfigurovaný Redis, tak i worker |

**Windows:**

```powershell
# základní kontrola
.\scripts\start-dev.ps1 check

# běžný lokální start
.\scripts\start-dev.ps1 all

# jen web na jiném portu
.\scripts\start-dev.ps1 web --port 8001

# worker zvlášť
.\scripts\start-dev.ps1 worker
```

**Linux:**

```bash
# základní kontrola
bash ./scripts/start-dev.sh check

# běžný lokální start
bash ./scripts/start-dev.sh all

# jen web na jiném portu
bash ./scripts/start-dev.sh web --port 8001

# worker zvlášť
bash ./scripts/start-dev.sh worker
```

### 5. Jak ten start funguje

- `web` používá `python manage.py runserver`, ale v tomhle projektu je to správně ASGI běh, protože je nainstalované a zaregistrované `daphne`.
- `all` před startem spustí `manage.py check`.
- Pokud není nastavený Redis, `all` worker přeskočí. Tím se vyhne rozbitému lokálnímu stavu, kdy by web a worker běžely v oddělených procesech nad in-memory channel layer.
- Pokud běží lokální embedded runtime bez Redis (`USE_INMEMORY_CHANNEL_LAYER=true`), launcher automaticky přidá `--noreload`, aby se po editaci kódu neztratily live subprocess handly konzole a ovládání serveru.
- Pro plný lokální multi-process režim nastav `REDIS_URL` a `USE_INMEMORY_CHANNEL_LAYER=false`.

Panel: **http://127.0.0.1:8000/**

---

## Linux / Debian (produkce)

### 1. Systémové závislosti

```bash
sudo apt update
sudo apt install python3.11 python3.11-venv python3-pip git redis-server nginx -y
```

### 2. Instalace aplikace

```bash
sudo mkdir -p /srv/gameserver_panel
sudo chown "$USER" /srv/gameserver_panel

git clone <repo-url> /srv/gameserver_panel
cd /srv/gameserver_panel

python3.11 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

### 3. Produkční `.env`

Použij `.env.example` jako základ a nastav alespoň:

```dotenv
SECRET_KEY=zmen-na-silny-nahodny-retezec
DEBUG=false
ALLOWED_HOSTS=tvoje.domena.cz tvoje-ip
DB_NAME=gameserver_panel
DB_USER=mcpanel
DB_PASSWORD=silne-heslo
DB_HOST=127.0.0.1
DB_PORT=5432
REDIS_URL=redis://127.0.0.1:6379/0
USE_SQLITE=false
USE_INMEMORY_CHANNEL_LAYER=false
```

### 4. Databáze

```bash
./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py createsuperuser
./.venv/bin/python manage.py collectstatic --noinput
```

### 5. Systemd a Nginx

Použij připravené soubory v `deploy/`:

- `deploy/systemd/services.conf` obsahuje target, web službu a worker službu
- `deploy/nginx/gameserver-panel.conf` obsahuje reverse proxy včetně `/ws/`

Webová služba v produkci běží přes `uvicorn config.asgi:application`, worker přes `python manage.py run_runtime_worker`.

Po zkopírování jednotek a nginx konfigurace:

```bash
sudo systemctl daemon-reload
sudo systemctl enable gameserver-panel.target
sudo systemctl enable gameserver-panel-web.service
sudo systemctl enable gameserver-panel-worker.service
sudo systemctl start gameserver-panel.target
sudo nginx -t
sudo systemctl reload nginx
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
- Po znovuotevření detailu serveru se načte uložená historie z databáze
- U subprocess backendu je konzole čtená přímo ze stdout/stderr procesu, ne z log tailu

### Metriky (status bar)
| Položka | Zdroj |
|---|---|
| CPU % | psutil — součet celého process tree |
| RAM | psutil — skutečná paměť Java procesu |
| Hráči | parsování logů (runtime worker) |
| Vlákna | psutil |
| PID | Java proces (ne cmd.exe wrapper) |
| Disk | psutil — využití disku working directory |

> Grafy, plánované restarty a část runtime automatiky vyžadují běžící `run_runtime_worker`.
> V lokálním multi-process režimu pro živé worker eventy používej Redis, ne in-memory channel layer.

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

### Zálohy

- AUTO i USER serverové zálohy respektují nastavené vyloučené relativní cesty z pracovního adresáře.
- Data vyloučená ze záloh se v těchto archivech neuloží a nepůjdou z nich obnovit.
- Automatické vrstvy jsou oddělené typem souboru i podsložkou v backup adresáři:
   - `hourly/`: `*-HOURLY.tar.gz`, každé 3 hodiny, uchovává 8 souborů.
   - `daily/`: `*-DAILY.tar.gz`, jednou denně po 02:00, uchovává 7 souborů.
   - `weekly/`: `*-WEEKLY.tar.gz`, jednou za 7 dní po 03:00, uchovává 4 soubory.
   - `monthly/`: `*-MONTHLY.tar.gz`, poslední den měsíce po 04:00, uchovává 12 souborů.
- Každá automatická vrstva maže pouze vlastní přebytečné soubory.
- Ruční zálohy jsou v `user/` jako `*-USER.tar.gz` a automatická rotace je nikdy nemaže.
- Starší neoznačené archivy v kořeni backup adresáře se zobrazují jako LEGACY a automatická rotace je nemaže.

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
| WebSocket 404 | start mimo ASGI flow | Použij `scripts/start-dev.ps1 web` nebo `bash ./scripts/start-dev.sh web` |
| Konzole po reloadu neukazuje nové live eventy | web běží bez funkčního websocket připojení | Zkontroluj, že běží `web` režim a stránka ukazuje `Připojeno` |
| Grafy prázdné | Runtime worker neběží | Spusť `scripts/start-dev.ps1 worker` nebo `bash ./scripts/start-dev.sh worker` |
| Hráči se nepočítají | Runtime worker neběží | Spusť `scripts/start-dev.ps1 worker` nebo `bash ./scripts/start-dev.sh worker` |
| Plánované restarty nefungují | Runtime worker neběží | Spusť `scripts/start-dev.ps1 worker` nebo `bash ./scripts/start-dev.sh worker` |
| Worker lokálně běží, ale live eventy se nepropíšou do webu | používá se in-memory channel layer bez Redis | Nastav `REDIS_URL` a `USE_INMEMORY_CHANNEL_LAYER=false`, nebo spusť jen `web` |
| Port 8000 obsazený | starý process běží | Ukonči starý Python/Daphne/Uvicorn process a spusť start skript znovu |
| Server se nezastaví | Proces neodpovídá | Použij **✕ Force** v panelu |
| GTNH se zasekne při startu | Chybí world složka nebo RWG bug | Zkopíruj world složku ze stávajícího serveru |
