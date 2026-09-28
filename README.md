# 🚀 Website Monitoring Bot with Discord Alerts & Interactive Bot

Bot monitoring website otomatis untuk memantau status satu atau banyak website (`TARGET_URLS`) secara realtime dan mengirim notifikasi instan ke server Discord via **Discord Webhook** atau **Discord Bot Token**, serta dilengkapi mode **Interactive Discord Bot** (`discord.py`) dengan dukungan Slash Commands & Mention Prefix (`@Bot <command>`).

---

## ✨ Fitur Utama

- 🌐 **Multi-Website Monitoring**: Memantau banyak target website sekaligus secara paralel/konkuren dengan state machine independen per website.
- 🤖 **Interactive Discord Bot**: Bot dapat online di Discord, merespons mention/tag `@Bot status`, `@Bot check`, `@Bot add`, `@Bot remove`, `@Bot list`, dan Slash Commands (`/status`, dll).
- 🔑 **Fleksibilitas Autentikasi**: Mendukung **Discord Webhook** ataupun **Discord Bot Token** + Channel ID.
- 🔴 **Instant DOWN Alert**: Mendeteksi HTTP 4xx/5xx, connection timeout, DNS error, SSL/TLS certificate error, dan connection refused.
- 🟢 **RECOVERED Alert**: Notifikasi otomatis saat website kembali normal beserta informasi total durasi downtime.
- 🟡 **Degraded / High Latency Alert**: Peringatan otomatis jika response time website lambat (misal > 3000ms).
- 💬 **Rich Discord Embeds**: Tampilan embed interaktif lengkap dengan status, latensi, kode HTTP, dan timestamp.
- 🔔 **Custom Mention**: Mendukung mention (`@everyone`, `<@&ROLE_ID>`, atau `<@USER_ID>`) saat down alert terpicu.
- 🛡️ **Anti-Flapping**: Konfigurasi `FAILURE_THRESHOLD` agar tidak memicu false alarm akibat glitch sesaat.
- 🛑 **Graceful Shutdown**: Penanganan `SIGINT` (Ctrl+C) dan `SIGTERM` yang aman.

---

## 📋 Struktur File

```
bot-monitoring/
├── config.py             # Parser dan validator konfigurasi environment & multi-URL
├── monitor.py            # Engine monitoring, state machine (UP/DOWN/RECOVERED)
├── notifier.py           # Formatter embed & pengirim notifikasi (Webhook & Bot Token REST API)
├── bot.py                # Interactive Discord Bot client (@mention prefix & Slash commands)
├── main.py               # CLI entrypoint & continuous concurrent monitoring loop
├── requirements.txt      # Daftar dependencies Python
├── .env.example          # Template konfigurasi environment
├── systemd/
│   └── bot-monitoring.service # File service untuk running 24/7 di server
└── tests/
    ├── test_config.py    # Unit test parsing URL dan konfigurasi
    ├── test_monitor.py   # Unit test monitoring logic & state transition
    ├── test_notifier.py  # Unit test webhook & bot token REST payload
    └── test_bot.py       # Unit test interactive bot commands & state
```

---

## 🛠️ Panduan Konfigurasi (`.env`)

Salin file `.env.example` menjadi `.env`:
```bash
cp .env.example .env
```

Buka `.env` dan atur konfigurasi:
```ini
# ==========================================
# 1. Target Website (Pisahkan dengan koma)
# ==========================================
TARGET_URLS=https://deploy.fedora.biz.id/, https://google.com, https://github.com

# ==========================================
# 2. Opsi Notifikasi Discord (Pilih salah satu atau keduanya)
# ==========================================
# METODE A: DISCORD WEBHOOK
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxx/yyyy

# METODE B: DISCORD BOT TOKEN (Untuk Bot Interaktif / Notifikasi Bot)
DISCORD_BOT_TOKEN=MTAxMjM0NTY3ODkw...
DISCORD_CHANNEL_ID=123456789012345678

# ==========================================
# 3. Pengaturan Monitoring
# ==========================================
CHECK_INTERVAL_SECONDS=60
REQUEST_TIMEOUT_SECONDS=10
FAILURE_THRESHOLD=1
ALERT_MENTION=
DEGRADED_LATENCY_THRESHOLD_MS=3000
```

---

## 🎮 Cara Menjalankan

### Mode 1: Interactive Discord Bot (Rekomendasi)
Bot akan online di Discord, memantau di background, mengirim alert ke channel, dan merespons perintah user:
```bash
source .venv/bin/activate
python bot.py
# atau
python main.py --bot
```

#### 📌 Perintah Bot di Discord (Prefix `s!`, Tag/Mention Bot, atau Slash Command):
| Prefix `s!` / Mention | Slash Command | Deskripsi |
| :--- | :--- | :--- |
| `s!status` / `@Bot status` | `/status` | Menampilkan ringkasan status realtime seluruh website |
| `s!check [url]` / `@Bot check [url]` | `/check [url]` | Menjalankan live diagnostic check langsung |
| `s!add <url>` / `@Bot add <url>` | `/add <url>` | Menambahkan website baru ke monitoring secara realtime |
| `s!remove <url>` / `@Bot remove <url>` | `/remove <url>` | Menghapus website dari monitoring |
| `s!list` / `@Bot list` | `/list` | Menampilkan daftar website yang dipantau |
| `s!ping` / `@Bot ping` | `/ping` | Mengecek latency bot Discord |
| `s!help` / `@Bot help` | - | Menampilkan panduan bantuan perintah |

---

### Mode 2: Standalone Background Monitoring (Tanpa Bot Interaktif)
Menjalankan loop monitoring di server console (mengirim alert via Webhook atau Bot Token):
```bash
source .venv/bin/activate
python main.py
```

#### Diagnostic Check Sekali Jalan (CLI):
```bash
python main.py --check-only
# atau spesifik URL tertentu:
python main.py --check-only --target "https://google.com, https://github.com"
```

#### Test Notifikasi Discord:
```bash
python main.py --test-discord
```

---

### 4. Menjalankan Menggunakan Docker 🐳

#### A. Menggunakan Docker Compose (Paling Praktis)
```bash
# Build dan jalankan container di background
docker compose up -d

# Cek logs container
docker compose logs -f

# Hentikan container
docker compose down
```

#### B. Menggunakan Docker CLI Standar
```bash
# 1. Build image Docker
docker build -t bot-monitoring:latest .

# 2. Jalankan container dengan file .env
docker run -d --name bot-monitoring --restart unless-stopped --env-file .env bot-monitoring:latest

# 3. Cek live logs
docker logs -f bot-monitoring
```

---

### 5. Menjalankan 24/7 di Server (Systemd Service)

1. Salin file service:
   ```bash
   sudo cp systemd/bot-monitoring.service /etc/systemd/system/
   ```
2. Reload daemon dan jalankan service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now bot-monitoring
   ```
3. Cek live logs:
   ```bash
   journalctl -u bot-monitoring -f
   ```

---

### 6. Menjalankan Unit Tests
```bash
source .venv/bin/activate
pytest -v
```
