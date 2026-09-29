# Flow Architecture — Website Monitoring Bot

## 1. Ringkasan Sistem

Aplikasi ini adalah bot monitoring website berbasis Python yang:

- Memantau satu atau banyak website secara paralel.
- Menentukan status setiap website berdasarkan hasil HTTP check.
- Mendeteksi website `UP`, `DOWN`, `RECOVERED`, dan `DEGRADED`.
- Mengirim alert ke Discord melalui Webhook, Discord Bot Token REST API, atau keduanya.
- Menyediakan Interactive Discord Bot dengan prefix command, mention command, dan slash command.
- Menyimpan daftar target secara opsional ke `targets.json`.
- Dapat dijalankan sebagai proses Python biasa, Docker container, Docker Compose service, atau systemd service.

Arsitektur utama aplikasi dapat diringkas sebagai berikut:

```mermaid
flowchart LR
    ENV[Environment .env] --> CONFIG[Config Loader\nconfig.py]
    CONFIG --> ENTRY{Entry Point}

    ENTRY --> MAIN[Standalone Runner\nmain.py]
    ENTRY --> IBOT[Interactive Discord Bot\nbot.py]

    MAIN --> MON[Website Monitor\nmonitor.py]
    IBOT --> MON
    IBOT --> CMD[Discord Commands]

    MON --> TARGET[Target Websites]
    MON --> STATE[Per-Target State Machine]
    STATE --> NOTIFY[Discord Notifier\nnotifier.py]
    CMD --> NOTIFY
    NOTIFY --> WEBHOOK[Discord Webhook]
    NOTIFY --> API[Discord Bot REST API]
    NOTIFY --> QUEUE[Pending Alert Queue\noptional local JSON]

    IBOT --> DISCORD[Discord Gateway/API]
```

---

## 2. Komponen dan Tanggung Jawab

| Komponen | File | Tanggung jawab |
|---|---|---|
| Configuration Loader | `config.py` | Membaca environment variable, parsing URL, memberi default value, dan menyediakan konfigurasi terpusat. |
| Standalone Entrypoint | `main.py` | Menentukan mode aplikasi melalui CLI, membuat runner monitoring, menjalankan check sekali atau loop monitoring. |
| Interactive Bot | `bot.py` | Menjalankan Discord Bot, menerima command user, mengelola target secara realtime, dan menjalankan monitoring background. |
| Website Monitor | `monitor.py` | Melakukan HTTP check, mengukur latency, memeriksa SSL, dan menjalankan transisi state per website. |
| Discord Notifier | `notifier.py` | Membentuk payload/embed Discord, mengirim alert, menjalankan retry/failover, circuit breaker, dan queue alert gagal. |
| Tests | `tests/` | Menguji konfigurasi, hasil check, transisi state, notifikasi, command logic, dan persistence target. |
| Container Runtime | `Dockerfile`, `docker-compose.yml` | Menjalankan aplikasi sebagai container non-root dengan restart policy. |
| Server Runtime | `systemd/bot-monitoring.service` | Menjalankan standalone monitoring sebagai service Linux dengan auto-restart. |

---

## 3. Sumber Konfigurasi

### 3.1 Environment Variable

Konfigurasi utama dibaca oleh `Config.from_env()` dari `.env` atau environment sistem.

| Variable | Fungsi | Default / aturan |
|---|---|---|
| `TARGET_URLS` | Daftar target website | Dipisahkan koma atau newline. |
| `TARGET_URL` | Format target tunggal lama/backward compatibility | Dipakai jika `TARGET_URLS` kosong. |
| `DISCORD_WEBHOOK_URL` | Endpoint Discord Webhook | Opsional. |
| `DISCORD_BOT_TOKEN` | Token Discord Bot | Dibutuhkan untuk mode interactive bot. |
| `DISCORD_CHANNEL_ID` | Channel tujuan pesan REST API | Dibutuhkan bersama bot token untuk notifikasi REST. |
| `CHECK_INTERVAL_SECONDS` | Jeda antar siklus monitoring | Minimum efektif 5 detik. Default 60 detik. |
| `REQUEST_TIMEOUT_SECONDS` | Batas waktu HTTP request | Minimum efektif 1 detik. Default 10 detik. |
| `FAILURE_THRESHOLD` | Jumlah kegagalan beruntun sebelum status DOWN | Minimum 1. Default 1. |
| `ALERT_MENTION` | Mention saat alert tertentu | Contoh `@everyone` atau role mention. |
| `DEGRADED_LATENCY_THRESHOLD_MS` | Ambang latency lambat | Default 3000 ms. Nilai 0 menonaktifkan alert degraded. |
| `ENABLE_SSL_CHECK` | Mengaktifkan pemeriksaan expiry SSL | Default aktif. |
| `TARGETS_FILE` | File persistence daftar target interactive bot | Default `targets.json`. |

### 3.2 Normalisasi Konfigurasi

```mermaid
flowchart TD
    START[Program dimulai] --> LOAD[load_dotenv]
    LOAD --> READ[Ambil environment variable]
    READ --> URLS[Parse TARGET_URLS/TARGET_URL]
    URLS --> DEDUP[Trim dan hapus URL duplikat]
    DEDUP --> VALIDATE[Normalisasi nilai numerik\ndan boolean]
    VALIDATE --> CONFIG[Objek Config]
    CONFIG --> ENTRY[Dipakai oleh main.py atau bot.py]
```

Jika tidak ada URL yang dikonfigurasi, aplikasi menggunakan target default yang ditentukan di `config.py`.

---

## 4. Entry Point dan Mode Operasi

Aplikasi memiliki dua entry point utama:

### 4.1 Standalone Monitoring — `main.py`

Digunakan untuk monitoring tanpa command interaktif Discord.

```mermaid
flowchart TD
    START[python main.py] --> ARGS[Parse CLI arguments]
    ARGS --> CONFIG[Config.from_env]
    CONFIG --> OVERRIDE{Ada override CLI?}
    OVERRIDE -->|Ya| APPLY[Override interval/target]
    OVERRIDE -->|Tidak| RUNNER
    APPLY --> RUNNER[MonitoringRunner]
    RUNNER --> MODE{Mode operasi}
    MODE -->|--check-only| ONCE[run_check_once]
    MODE -->|--test-discord| TEST[test_discord_webhook]
    MODE -->|default| LOOP[run_loop]
    MODE -->|--bot| BOT[run_interactive_bot]
```

Command yang tersedia:

- `python main.py` — monitoring berkelanjutan.
- `python main.py --check-only` — satu kali check seluruh target lalu keluar.
- `python main.py --check-only --target "https://a.com,https://b.com"` — check target override.
- `python main.py --test-discord` — menguji koneksi/pengiriman Discord.
- `python main.py --interval 30` — override interval.
- `python main.py --bot` — menjalankan interactive bot.

### 4.2 Interactive Discord Bot — `bot.py`

Mode ini menggabungkan Discord Bot Gateway dengan background monitoring.

```mermaid
flowchart TD
    START[python bot.py / python main.py --bot] --> CONFIG[Config.from_env]
    CONFIG --> TOKEN{DISCORD_BOT_TOKEN tersedia?}
    TOKEN -->|Tidak| STOP[Log error dan berhenti]
    TOKEN -->|Ya| CREATE[Create InteractiveMonitoringBot]
    CREATE --> LOAD[Load targets.json jika dikonfigurasi]
    LOAD --> INIT[Create session, executor, notifier, monitors]
    INIT --> LOGIN[Login ke Discord Gateway]
    LOGIN --> HOOK[setup_hook]
    HOOK --> TASK[Start monitoring_task]
    LOGIN --> READY[on_ready]
    READY --> SYNC[Sync slash commands]
    SYNC --> ONLINE[Bot online]
    ONLINE --> EVENTS[Terima command/event Discord]
    TASK --> MONITOR[Monitoring background]
```

Jika koneksi Discord terputus, `run_interactive_bot()` melakukan retry dengan delay yang meningkat secara bertahap sampai batas maksimum. Login failure menghentikan retry karena token dianggap tidak valid atau revoked.

---

## 5. Arsitektur Monitoring

Setiap URL dibuatkan satu instance `WebsiteMonitor`. State setiap website bersifat independen sehingga kegagalan satu target tidak mengubah status target lain.

```mermaid
flowchart LR
    CONFIG[Config] --> RUNNER[Runner / monitoring_task]
    RUNNER --> EXECUTOR[ThreadPoolExecutor]
    EXECUTOR --> M1[WebsiteMonitor A]
    EXECUTOR --> M2[WebsiteMonitor B]
    EXECUTOR --> MN[WebsiteMonitor N]
    M1 --> HTTPA[HTTP check A]
    M2 --> HTTPB[HTTP check B]
    MN --> HTTPN[HTTP check N]
    HTTPA --> RESULT[CheckResult]
    HTTPB --> RESULT
    HTTPN --> RESULT
    RESULT --> STATE[process_result]
```

### 5.1 Eksekusi Paralel

- `main.py` menggunakan `ThreadPoolExecutor` reusable.
- Setiap target diproses sebagai future terpisah.
- Jumlah worker standalone dibatasi maksimum 20 dan menyesuaikan jumlah target.
- Interactive bot memakai executor untuk menjalankan blocking `requests` tanpa memblokir event loop asyncio.
- Satu target yang lambat/error tidak menghentikan check target lain.

### 5.2 HTTP Check

Method `WebsiteMonitor.check()` melakukan:

1. Memulai pengukuran waktu dengan `time.perf_counter()`.
2. Mengirim HTTP `GET` menggunakan `requests.Session`.
3. Menggunakan `stream=True` agar cukup mengambil header tanpa mengunduh seluruh body.
4. Mengikuti redirect.
5. Menggunakan custom User-Agent.
6. Mengukur response latency.
7. Menutup response.
8. Menghasilkan objek `CheckResult`.

Klasifikasi HTTP:

| Kondisi | Hasil |
|---|---|
| HTTP 200–399 | Healthy / UP |
| HTTP 400–599 | Unhealthy / failure |
| Timeout | Unhealthy dengan pesan timeout |
| SSL error | Unhealthy dengan pesan SSL/TLS |
| Connection/DNS error | Unhealthy dengan pesan koneksi |
| Request exception lain | Unhealthy dengan pesan exception |

### 5.3 Pemeriksaan SSL

Untuk target HTTPS yang healthy:

- Hostname dan port diambil dari URL.
- Sertifikat diperiksa menggunakan modul standar `ssl` dan `socket` Python.
- Sisa masa berlaku dihitung dalam hari.
- Hasil dicache berdasarkan hostname selama 6 jam.
- Jika expiry `<= 14 hari`, dicatat sebagai warning.
- Jika expiry `<= 7 hari`, alert SSL dikirim maksimal satu kali per 24 jam per monitor.
- Kegagalan pemeriksaan SSL tidak membuat HTTP check menjadi unhealthy; nilai SSL dikembalikan sebagai `None`.

---

## 6. State Machine Per Website

State disimpan di instance `WebsiteMonitor`, bukan di database.

### 6.1 State Internal

| Properti | Fungsi |
|---|---|
| `is_currently_down` | Menandakan target sudah dianggap DOWN. |
| `consecutive_failures` | Jumlah failure berturut-turut. |
| `down_since` | Timestamp saat status DOWN dimulai. |
| `last_degraded_alert` | Timestamp alert latency terakhir. |
| `last_ssl_alert` | Timestamp alert SSL terakhir. |

### 6.2 Diagram Transisi

```mermaid
stateDiagram-v2
    [*] --> UP
    UP --> UP: Healthy response
    UP --> UP: Healthy + latency tinggi\n(kirim DEGRADED jika cooldown lewat)
    UP --> UP: Healthy + SSL hampir expired\n(kirim SSL alert sesuai cooldown)
    UP --> FAILING: Failure #1 sampai #threshold-1
    FAILING --> FAILING: Failure berikutnya\nthreshold belum tercapai
    FAILING --> DOWN: Failure >= FAILURE_THRESHOLD\nset down_since + kirim DOWN
    DOWN --> DOWN: Failure berikutnya\nnaikkan consecutive_failures\ntanpa spam alert
    DOWN --> RECOVERED: Healthy response\nhitung downtime + kirim RECOVERED
    RECOVERED --> UP: Reset failure counter\ndan down_since
```

### 6.3 Aturan Anti-Flapping

1. Failure counter bertambah hanya ketika check gagal.
2. Alert DOWN hanya dikirim saat threshold tercapai dan target belum berstatus DOWN.
3. Failure berikutnya ketika target sudah DOWN tidak mengirim alert DOWN tambahan.
4. Satu response healthy mereset failure counter.
5. Jika sebelumnya DOWN, response healthy memicu alert RECOVERED satu kali.
6. Downtime dihitung dari `down_since` sampai waktu recovery.

---

## 7. Flow Siklus Monitoring Standalone

```mermaid
sequenceDiagram
    participant R as MonitoringRunner
    participant M as WebsiteMonitor
    participant W as Target Website
    participant N as DiscordNotifier
    participant D as Discord

    loop Setiap CHECK_INTERVAL_SECONDS
        R->>N: flush_pending_alerts()
        R->>M: check() secara paralel
        M->>W: HTTP GET dengan timeout
        W-->>M: Status/error + latency
        M->>M: Opsional cek expiry SSL
        M-->>R: CheckResult
        R->>M: process_result(CheckResult)
        M->>M: Update state target
        alt DOWN baru terdeteksi
            M->>N: notify_down()
        else RECOVERED
            M->>N: notify_recovered()
        else Latency di atas threshold
            M->>N: notify_degraded()
        else SSL hampir expired
            M->>N: notify_ssl_expiring()
        end
        N->>D: Kirim alert bila diperlukan
    end
```

Loop standalone menunggu interval dalam potongan satu detik, bukan sleep sekali penuh. Tujuannya agar `SIGINT` atau `SIGTERM` dapat diproses lebih cepat.

---

## 8. Flow Background Monitoring Interactive Bot

```mermaid
sequenceDiagram
    participant G as Discord Gateway
    participant B as InteractiveMonitoringBot
    participant E as ThreadPoolExecutor
    participant M as WebsiteMonitor
    participant N as DiscordNotifier

    B->>G: Login dengan DISCORD_BOT_TOKEN
    G-->>B: on_ready
    B->>G: Sync slash commands
    B->>B: Start monitoring_task

    loop Setiap check interval
        B->>E: flush_pending_alerts()
        E-->>B: Queue flush selesai
        B->>E: Submit check untuk setiap monitor
        E->>M: check() + process_result()
        M->>N: Kirim event alert bila state berubah
        E-->>B: Semua target selesai
    end
```

`monitoring_task` memakai `asyncio` sebagai scheduler, tetapi HTTP request blocking dikerjakan oleh thread executor agar Discord Bot tetap responsif terhadap command.

---

## 9. Arsitektur Notifikasi Discord

### 9.1 Sumber Alert

| Event | Method notifier | Warna embed |
|---|---|---|
| Website gagal melewati threshold | `notify_down()` | Merah |
| Website kembali normal | `notify_recovered()` | Hijau |
| Response terlalu lambat | `notify_degraded()` | Kuning |
| SSL mendekati expiry | `notify_ssl_expiring()` | Oranye |
| Tes koneksi manual | `notify_test()` | Biru |

### 9.2 Jalur Pengiriman

```mermaid
flowchart TD
    EVENT[Alert event] --> EMBED[Build Discord embed]
    EMBED --> DEST{Destination configured?}
    DEST -->|Tidak| DROP[Log warning, tidak mengirim]
    DEST -->|Webhook tersedia| WH[POST Webhook]
    DEST -->|Bot Token + Channel tersedia| API[POST Discord REST API]
    WH --> RESULT1{Berhasil?}
    API --> RESULT2{Berhasil?}
    RESULT1 -->|Ya| SENT[Alert terkirim]
    RESULT1 -->|Tidak| FAILOVER[Gunakan Bot Token jika tersedia]
    FAILOVER --> RESULT2
    RESULT2 -->|Ya| SENT
    RESULT2 -->|Tidak| QUEUE[Simpan pending alert]
```

Jika Webhook dan Bot Token sama-sama tersedia, implementasi dapat mengirim melalui kedua jalur ketika Webhook berhasil; Bot Token juga berfungsi sebagai failover ketika Webhook gagal.

### 9.3 Retry dan Circuit Breaker

- Setiap jalur pengiriman mencoba maksimal 3 kali.
- HTTP `429` membaca `retry_after`, lalu menunggu sebelum mencoba kembali.
- Error koneksi mencoba ulang dengan jeda 2 detik.
- HTTP `401`, `403`, atau `404` dianggap error permanen sementara dan menonaktifkan jalur tersebut melalui flag circuit breaker.
- Alert hanya dianggap sukses jika minimal satu destination berhasil mengirim.

### 9.4 Pending Alert Queue

Jika seluruh destination gagal:

1. Payload alert dimasukkan ke `pending_alerts`.
2. Queue dibatasi maksimal 50 item; item terlama dibuang jika melebihi batas.
3. Jika `failed_queue_file` dikonfigurasi, queue disimpan ke file JSON.
4. Pada awal setiap siklus monitoring, `flush_pending_alerts()` mencoba mengirim ulang.
5. Item yang sukses dihapus dari queue; item yang masih gagal dipertahankan.

Catatan implementasi: pada konfigurasi saat ini, `DiscordNotifier` dibuat tanpa `failed_queue_file`, sehingga queue default hanya berada di memory dan tidak bertahan setelah proses mati. Persistence queue baru aktif jika parameter file diberikan.

---

## 10. Flow Interactive Discord Command

### 10.1 Input Command

Bot menerima tiga pola command:

- Prefix `s!`, `S!`, atau `!`.
- Mention bot, misalnya `@Bot status`.
- Slash command, misalnya `/status`.

```mermaid
flowchart TD
    USER[User Discord] --> INPUT{Jenis input}
    INPUT -->|Prefix/Mention| PREFIX[commands extension]
    INPUT -->|Slash| SLASH[app_commands tree]
    PREFIX --> ROUTE[Command handler]
    SLASH --> ROUTE
    ROUTE --> ACTION{Command}
    ACTION --> STATUS[status: build status embed]
    ACTION --> CHECK[check: live diagnostic]
    ACTION --> ADD[add: tambah target]
    ACTION --> REMOVE[remove: hapus target]
    ACTION --> LIST[list: daftar target]
    ACTION --> PING[ping: latency bot]
    ACTION --> HELP[help: panduan command]
    STATUS --> REPLY[Balas ke Discord]
    CHECK --> REPLY
    ADD --> REPLY
    REMOVE --> REPLY
    LIST --> REPLY
    PING --> REPLY
    HELP --> REPLY
```

### 10.2 `status`

- Membaca status internal seluruh monitor.
- Jika semua target tidak DOWN, embed berstatus `All Systems Operational`.
- Jika minimal satu target DOWN, embed berstatus `Outages Detected`.
- Menampilkan status tiap URL dan jumlah failure jika DOWN.

### 10.3 `check`

- Menjalankan live diagnostic tanpa mengubah state monitor utama.
- Membuat `WebsiteMonitor` sementara untuk tiap target.
- Check beberapa target secara paralel.
- Menampilkan HTTP status, latency, SSL expiry bila tersedia, atau error.

### 10.4 `add`

1. Trim URL.
2. Menambahkan `https://` jika protocol belum ada.
3. Menolak URL duplikat.
4. Menambahkan URL ke `config.target_urls`.
5. Membuat `WebsiteMonitor` baru.
6. Menyimpan daftar ke `targets.json` jika persistence aktif.
7. Mengirim embed hasil operasi.

### 10.5 `remove`

1. Mencocokkan URL secara langsung atau dengan mengabaikan trailing slash.
2. Menghapus URL dari konfigurasi.
3. Menghapus monitor terkait.
4. Menyimpan daftar terbaru ke persistence file.
5. Mengirim embed hasil operasi.

---

## 11. Persistence Data Lokal

Aplikasi tidak menggunakan database. Data runtime utama berada di memory.

```mermaid
flowchart LR
    START[Interactive bot start] --> FILE{targets.json ada?}
    FILE -->|Ya| LOAD[Load list URL]
    FILE -->|Tidak| ENV[Gunakan TARGET_URLS]
    LOAD --> MERGE[Gabungkan tanpa duplikat]
    ENV --> RUNTIME[config.target_urls]
    MERGE --> RUNTIME
    ADD[Command add] --> RUNTIME
    REMOVE[Command remove] --> RUNTIME
    RUNTIME --> SAVE[Simpan targets.json jika TARGETS_FILE aktif]
```

Data yang tidak dipersistenkan:

- Status UP/DOWN.
- Jumlah consecutive failures.
- `down_since`.
- Timestamp cooldown alert.
- Pending alert queue, kecuali `failed_queue_file` dikonfigurasi pada notifier.

Akibatnya, restart aplikasi akan membuat state monitoring kembali dari awal.

---

## 12. Lifecycle dan Graceful Shutdown

### Standalone

```mermaid
flowchart TD
    RUN[Monitoring berjalan] --> SIGNAL{SIGINT/SIGTERM?}
    SIGNAL -->|Tidak| LOOP[Lanjut check dan sleep]
    LOOP --> RUN
    SIGNAL -->|Ya| FLAG[Set running=False]
    FLAG --> EXEC[Shutdown executor tanpa menunggu task baru]
    EXEC --> SESSION[Tutup requests session]
    SESSION --> EXIT[Proses berhenti]
```

### Interactive Bot

`InteractiveMonitoringBot.close()`:

1. Shutdown executor.
2. Menutup HTTP session.
3. Memanggil `super().close()` untuk menutup koneksi Discord.

Jika user menekan `KeyboardInterrupt`, lifecycle wrapper menghentikan loop retry dan proses berakhir dengan log yang sesuai.

---

## 13. Deployment Flow

### 13.1 Docker / Docker Compose

```mermaid
flowchart TD
    SOURCE[Source code] --> BUILD[Docker build]
    REQ[requirements.txt] --> BUILD
    BUILD --> IMAGE[Python 3.12 slim image]
    IMAGE --> USER[Run sebagai non-root appuser]
    ENV[.env] --> CONTAINER[Container environment]
    USER --> CONTAINER
    CONTAINER --> CMD[Default: python bot.py]
    CMD --> RESTART[restart unless-stopped]
```

Default Docker command adalah interactive bot:

```bash
docker compose up -d
```

Untuk mode standalone, command Compose dapat diganti menjadi `python main.py`.

### 13.2 Systemd

```mermaid
flowchart LR
    SYSTEMD[systemd] --> SERVICE[bot-monitoring.service]
    SERVICE --> ENV[EnvironmentFile .env]
    SERVICE --> MAIN[python main.py]
    MAIN --> MONITOR[Monitoring loop]
    MONITOR --> JOURNAL[journald logs]
    FAILURE[Process failure] --> RESTART[RestartSec + Restart=always]
```

Service systemd menjalankan mode standalone dan mengirim stdout/stderr ke journal.

---

## 14. Testing Architecture

Test suite menggunakan `pytest` dan mock agar tidak perlu mengakses website atau Discord sungguhan.

| File test | Cakupan |
|---|---|
| `tests/test_config.py` | Parsing URL, default value, dan konfigurasi environment. |
| `tests/test_monitor.py` | HTTP 200, HTTP 500, timeout, connection error, state transition, multi-monitor state, dan SSL expiry. |
| `tests/test_notifier.py` | Payload alert, Webhook, Bot Token REST API, retry/failover, dan queue. |
| `tests/test_bot.py` | Inisialisasi bot, add/remove target, status embed, live diagnostic, dan persistence target. |

```mermaid
flowchart TD
    TEST[pytest] --> CONFIGT[test_config.py]
    TEST --> MONT[test_monitor.py]
    TEST --> NOTIFT[test_notifier.py]
    TEST --> BOTT[test_bot.py]
    CONFIGT --> MOCK[Mock environment/network/Discord]
    MONT --> MOCK
    NOTIFT --> MOCK
    BOTT --> MOCK
```

---

## 15. Alur Error dan Recovery

### Website Error

```mermaid
flowchart TD
    CHECK[HTTP check] --> ERROR{Request gagal?}
    ERROR -->|Tidak| HEALTHY[Healthy result]
    ERROR -->|Ya| COUNT[Increment consecutive_failures]
    COUNT --> THRESHOLD{Threshold tercapai?}
    THRESHOLD -->|Tidak| WAIT[Tunggu siklus berikutnya]
    THRESHOLD -->|Ya| DOWN{Sudah DOWN?}
    DOWN -->|Ya| NO_SPAM[Tidak kirim alert tambahan]
    DOWN -->|Tidak| MARK[Mark DOWN + set down_since]
    MARK --> ALERT[Send DOWN alert]
```

### Discord Error

```mermaid
flowchart TD
    SEND[Send notification] --> DEST[Webhook/Bot Token]
    DEST --> RETRY{Transient error atau 429?}
    RETRY -->|Ya| RETRYCOUNT[Coba maksimal 3 kali]
    RETRYCOUNT --> SUCCESS{Berhasil?}
    RETRY -->|Tidak: 401/403/404| DISABLE[Disable destination sementara]
    SUCCESS -->|Ya| DONE[Terkirim]
    SUCCESS -->|Tidak| FAILOVER[Coba destination lain]
    DISABLE --> FAILOVER
    FAILOVER --> ANY{Ada yang berhasil?}
    ANY -->|Ya| DONE
    ANY -->|Tidak| PENDING[Simpan pending alert]
```

---

## 16. Ringkasan Alur End-to-End

```mermaid
flowchart TD
    A[Start application] --> B[Load .env]
    B --> C[Build Config]
    C --> D{Interactive mode?}

    D -->|Tidak| E[Create MonitoringRunner]
    D -->|Ya| F[Create InteractiveMonitoringBot]

    E --> G[Create shared HTTP session]
    G --> H[Create DiscordNotifier]
    H --> I[Create one WebsiteMonitor per URL]
    I --> J[Run concurrent monitoring loop]

    F --> K[Load persisted targets]
    K --> L[Create Discord session/executor/notifier/monitors]
    L --> M[Login Discord and sync commands]
    M --> N[Run background monitoring_task]
    M --> O[Receive user commands]

    J --> P[HTTP GET each target]
    N --> P
    O --> Q[Status/check/add/remove/list/ping/help]

    P --> R[Build CheckResult]
    R --> S[Update per-target state]
    S --> T{State/event changed?}
    T -->|DOWN| U[Build DOWN embed]
    T -->|RECOVERED| V[Build RECOVERED embed]
    T -->|Slow| W[Build DEGRADED embed]
    T -->|SSL near expiry| X[Build SSL embed]
    T -->|No alert| Y[Log result]

    U --> Z[DiscordNotifier]
    V --> Z
    W --> Z
    X --> Z
    Z --> AA[Webhook / Bot Token API]
    AA --> AB{Failed?}
    AB -->|Ya| AC[Queue for retry]
    AB -->|Tidak| AD[Alert delivered]
    AC --> AE[Flush on next cycle]

    Q --> AF[Discord response]
```

---

## 17. Catatan Arsitektur Penting

1. **Tidak ada database eksternal.** State dan target aktif disimpan di memory; target dapat disimpan ke JSON.
2. **Monitoring bersifat per target.** Setiap website mempunyai state machine sendiri.
3. **HTTP check bersifat blocking.** Karena itu standalone memakai thread pool dan interactive bot memindahkan pekerjaan blocking ke executor.
4. **Notifikasi bersifat event-driven.** Alert tidak dikirim pada setiap siklus, hanya ketika event memenuhi aturan state/cooldown.
5. **Anti-flapping dikontrol `FAILURE_THRESHOLD`.** Ini mencegah satu glitch singkat langsung menghasilkan alert DOWN.
6. **Latency alert memiliki cooldown 15 menit.** SSL alert memiliki cooldown 24 jam untuk expiry `<= 7 hari`.
7. **Alert recovery membutuhkan state DOWN sebelumnya.** Website yang selalu healthy tidak mengirim RECOVERED.
8. **Mode interactive memerlukan `DISCORD_BOT_TOKEN`.** Webhook saja tidak cukup untuk login ke Discord Gateway dan menerima command.
9. **Mode standalone dapat memakai Webhook atau Bot Token + Channel ID.**
10. **Graceful shutdown tersedia untuk signal proses dan penutupan interactive bot.**

## 18. Referensi File Utama

- `config.py:11` — struktur dan parsing konfigurasi.
- `main.py:23` — `MonitoringRunner` dan lifecycle standalone.
- `main.py:178` — CLI entrypoint.
- `monitor.py:58` — `CheckResult`.
- `monitor.py:69` — `WebsiteMonitor`.
- `monitor.py:94` — HTTP check.
- `monitor.py:170` — state transition dan alert decision.
- `notifier.py:20` — `DiscordNotifier`.
- `notifier.py:172` — routing notifikasi dan failover.
- `notifier.py:220` — pengiriman Webhook.
- `notifier.py:262` — pengiriman Bot Token REST API.
- `bot.py:32` — `InteractiveMonitoringBot`.
- `bot.py:141` — background monitoring task.
- `bot.py:275` — registration prefix dan slash commands.
- `bot.py:446` — interactive bot lifecycle dan reconnect.
