import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union
import requests
from requests.adapters import HTTPAdapter

import os

logger = logging.getLogger(__name__)

COLOR_DOWN = 0xE74C3C     # Red
COLOR_RECOVERED = 0x2ECC71 # Green
COLOR_DEGRADED = 0xF1C40F  # Yellow
COLOR_INFO = 0x3498DB      # Blue
COLOR_SSL = 0xE67E22       # Orange


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: Optional[str] = None,
        mention: Optional[str] = None,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
        session: Optional[requests.Session] = None,
        failed_queue_file: Optional[str] = None,
    ):
        self.webhook_url = webhook_url
        self.mention = mention
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.failed_queue_file = failed_queue_file

        # Circuit breaker states for permanent HTTP errors (401/403/404)
        self.webhook_disabled = False
        self.bot_token_disabled = False
        self.pending_alerts: List[Dict[str, Any]] = []

        if session is not None:
            self.session = session
        else:
            self.session = requests.Session()
            adapter = HTTPAdapter(pool_connections=5, pool_maxsize=10)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)

        self._load_pending_alerts()

    def _load_pending_alerts(self) -> None:
        """Loads any queued failed alerts from file on disk."""
        if not self.failed_queue_file:
            return
        try:
            if os.path.exists(self.failed_queue_file):
                with open(self.failed_queue_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.pending_alerts = data
                    if self.pending_alerts:
                        logger.info(
                            f"📂 [Notifier Handler] Dimuat {len(self.pending_alerts)} notifikasi tertunda dari {self.failed_queue_file}"
                        )
        except Exception as e:
            logger.warning(f"Could not load queued alerts from {self.failed_queue_file}: {e}")

    def _save_pending_alerts(self) -> None:
        """Persists queued alerts to file on disk."""
        if not self.failed_queue_file:
            return
        try:
            with open(self.failed_queue_file, "w", encoding="utf-8") as f:
                json.dump(self.pending_alerts, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save queued alerts to {self.failed_queue_file}: {e}")

    def _queue_failed_alert(self, payload: Dict[str, Any]) -> None:
        """Stores a failed alert into queue for future retry when connection recovers."""
        self.pending_alerts.append({
            "timestamp": time.time(),
            "payload": payload,
        })
        if len(self.pending_alerts) > 50:
            self.pending_alerts = self.pending_alerts[-50:]
        self._save_pending_alerts()

    def flush_pending_alerts(self) -> int:
        """Attempts to resend queued alerts when Discord connection is back online."""
        if not self.pending_alerts:
            return 0

        logger.info(f"🔄 [Notifier Handler] Mencoba mengirim ulang {len(self.pending_alerts)} notifikasi tertunda...")
        success_count = 0
        remaining = []

        for item in self.pending_alerts:
            payload = item.get("payload", {})
            sent = False
            if self.webhook_url and not self.webhook_disabled:
                sent = self._send_via_webhook(self.webhook_url, payload)
            if not sent and self.bot_token and self.channel_id and not self.bot_token_disabled:
                sent = self._send_via_bot_token(self.bot_token, self.channel_id, payload)

            if sent:
                success_count += 1
            else:
                remaining.append(item)

        self.pending_alerts = remaining
        self._save_pending_alerts()
        if success_count > 0:
            logger.info(f"✅ [Notifier Handler] Berhasil mengirim {success_count} notifikasi tertunda!")
        return success_count

    def validate_connection(self) -> Dict[str, Any]:
        """Pre-flight check to verify Webhook URL and Bot Token connectivity."""
        results: Dict[str, Any] = {
            "webhook_ok": False,
            "webhook_message": "Not configured",
            "bot_token_ok": False,
            "bot_token_message": "Not configured",
        }

        # Check Webhook
        if self.webhook_url:
            try:
                resp = self.session.get(self.webhook_url, timeout=5)
                if resp.status_code == 200:
                    results["webhook_ok"] = True
                    name = resp.json().get("name", "Webhook")
                    results["webhook_message"] = f"Connected (Name: '{name}')"
                    self.webhook_disabled = False
                elif resp.status_code == 404:
                    results["webhook_ok"] = False
                    results["webhook_message"] = "Invalid Webhook URL (HTTP 404 Unknown Webhook)"
                    self.webhook_disabled = True
                else:
                    results["webhook_ok"] = False
                    results["webhook_message"] = f"HTTP {resp.status_code} - {resp.text}"
            except Exception as e:
                results["webhook_ok"] = False
                results["webhook_message"] = f"Connection failed: {e}"

        # Check Bot Token
        if self.bot_token:
            try:
                headers = {"Authorization": f"Bot {self.bot_token}"}
                resp = self.session.get("https://discord.com/api/v10/users/@me", headers=headers, timeout=5)
                if resp.status_code == 200:
                    username = resp.json().get("username", "Bot")
                    results["bot_token_ok"] = True
                    results["bot_token_message"] = f"Authenticated as '{username}'"
                    self.bot_token_disabled = False
                elif resp.status_code == 401:
                    results["bot_token_ok"] = False
                    results["bot_token_message"] = "Unauthorized (Invalid DISCORD_BOT_TOKEN)"
                    self.bot_token_disabled = True
                else:
                    results["bot_token_ok"] = False
                    results["bot_token_message"] = f"HTTP {resp.status_code} - {resp.text}"
            except Exception as e:
                results["bot_token_ok"] = False
                results["bot_token_message"] = f"Connection failed: {e}"

        return results

    def send_webhook(self, payload: Dict[str, Any]) -> bool:
        """Alias for send_notification for backward compatibility."""
        return self.send_notification(payload)

    def send_notification(self, payload: Dict[str, Any]) -> bool:
        """Sends payload to Discord with automatic failover, circuit breaker, and offline queueing."""
        sent_any = False
        has_destination = False
        webhook_failed = False

        # 1. Send via Webhook if configured and not permanently disabled
        if self.webhook_url:
            has_destination = True
            if not self.webhook_disabled:
                ok = self._send_via_webhook(self.webhook_url, payload)
                if ok:
                    sent_any = True
                else:
                    webhook_failed = True
            else:
                webhook_failed = True

        # 2. Send via Bot Token REST API
        # If both are configured, attempt bot token as well (or as automatic failover if webhook failed)
        if self.bot_token and self.channel_id:
            has_destination = True
            if not self.bot_token_disabled:
                # If webhook succeeded, send via bot token if dual broadcast is enabled
                # If webhook failed, bot token acts as automatic failover!
                if webhook_failed:
                    logger.info("🔀 [Notifier Handler] Webhook gagal, melakukan failover otomatis ke Discord Bot Token API...")
                ok = self._send_via_bot_token(self.bot_token, self.channel_id, payload)
                if ok:
                    sent_any = True

        if not has_destination:
            logger.warning(
                "[Notifier] Neither DISCORD_WEBHOOK_URL nor (DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID) is set. "
                "Skipping Discord notification."
            )
            return False

        # If all active destinations failed, store in pending queue
        if not sent_any:
            logger.error(
                "🚨 [Notifier Handler] Gagal mengirim alert ke Discord (semua saluran gagal/tidak tersedia). "
                "Alert disimpan ke antrean lokal dan akan dicoba kembali saat koneksi pulih."
            )
            self._queue_failed_alert(payload)

        return sent_any

    def _send_via_webhook(self, webhook_url: str, payload: Dict[str, Any]) -> bool:
        if self.webhook_disabled:
            logger.debug("[Notifier] Webhook dilewati karena dalam status nonaktif (circuit breaker).")
            return False

        headers = {"Content-Type": "application/json"}
        for attempt in range(3):
            try:
                response = self.session.post(
                    webhook_url,
                    data=json.dumps(payload),
                    headers=headers,
                    timeout=10,
                )

                if response.status_code in (200, 204):
                    logger.info("[Notifier] Discord notification sent successfully via Webhook.")
                    return True
                elif response.status_code == 429:
                    retry_after = response.json().get("retry_after", 2)
                    logger.warning(f"[Notifier] Rate limited by Discord. Retrying in {retry_after}s...")
                    time.sleep(float(retry_after))
                elif response.status_code in (401, 403, 404):
                    # Permanent failure: webhook deleted or token invalidated
                    self.webhook_disabled = True
                    logger.critical(
                        f"🚨 [Notifier Handler] Discord Webhook tidak valid atau dihapus (HTTP {response.status_code}): {response.text}. "
                        "Webhook dinonaktifkan sementara untuk mencegah spam request berulang. "
                        "Periksa nilai DISCORD_WEBHOOK_URL di .env!"
                    )
                    return False
                else:
                    logger.error(f"[Notifier] Failed to send Discord webhook: HTTP {response.status_code} - {response.text}")
                    return False
            except requests.RequestException as e:
                logger.error(f"[Notifier] Connection error sending to Discord webhook (percobaan {attempt+1}/3): {e}")
                if attempt < 2:
                    time.sleep(2)
                else:
                    return False
        return False

    def _send_via_bot_token(self, bot_token: str, channel_id: str, payload: Dict[str, Any]) -> bool:
        if self.bot_token_disabled:
            logger.debug("[Notifier] Bot Token API dilewati karena dalam status nonaktif (circuit breaker).")
            return False

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
        }
        for attempt in range(3):
            try:
                response = self.session.post(
                    url,
                    data=json.dumps(payload),
                    headers=headers,
                    timeout=10,
                )

                if response.status_code in (200, 201):
                    logger.info("[Notifier] Discord notification sent successfully via Bot Token REST API.")
                    return True
                elif response.status_code == 429:
                    retry_after = response.json().get("retry_after", 2)
                    logger.warning(f"[Notifier] Rate limited by Discord. Retrying in {retry_after}s...")
                    time.sleep(float(retry_after))
                elif response.status_code in (401, 403, 404):
                    self.bot_token_disabled = True
                    logger.critical(
                        f"🚨 [Notifier Handler] DISCORD_BOT_TOKEN atau CHANNEL_ID tidak valid/tidak ada izin (HTTP {response.status_code}): {response.text}. "
                        "Pengiriman via Bot Token dinonaktifkan sementara. "
                        "Periksa DISCORD_BOT_TOKEN dan DISCORD_CHANNEL_ID di .env!"
                    )
                    return False
                else:
                    logger.error(f"[Notifier] Failed to send Discord message via Bot Token: HTTP {response.status_code} - {response.text}")
                    return False
            except requests.RequestException as e:
                logger.error(f"[Notifier] Connection error sending to Discord Bot API (percobaan {attempt+1}/3): {e}")
                if attempt < 2:
                    time.sleep(2)
                else:
                    return False
        return False

    def notify_down(
        self,
        target_url: str,
        reason: str,
        status_code: Optional[int] = None,
        latency_ms: Optional[float] = None,
        consecutive_failures: int = 1,
    ) -> bool:
        """Send DOWN alert to Discord."""
        now_iso = datetime.now(timezone.utc).isoformat()
        status_text = f"HTTP {status_code}" if status_code else "Unreachable / Error"
        latency_text = f"{latency_ms:.1f} ms" if latency_ms is not None else "N/A"

        fields = [
            {"name": "🌐 Target URL", "value": f"[{target_url}]({target_url})", "inline": False},
            {"name": "⚠️ Status", "value": f"`{status_text}`", "inline": True},
            {"name": "⏱️ Response Time", "value": f"`{latency_text}`", "inline": True},
            {"name": "🔁 Consecutive Failures", "value": f"`{consecutive_failures}`", "inline": True},
            {"name": "📝 Error Details", "value": f"```{reason}```", "inline": False},
        ]

        embed = {
            "title": "🚨 WEBSITE DOWN ALERT",
            "description": f"Website **{target_url}** mengalami masalah atau tidak dapat diakses.",
            "color": COLOR_DOWN,
            "fields": fields,
            "timestamp": now_iso,
            "footer": {"text": "Bot Monitoring • Alert System"},
        }

        payload: Dict[str, Any] = {"embeds": [embed]}
        if self.mention:
            payload["content"] = f"{self.mention} 🚨 **Website is DOWN!**"

        return self.send_webhook(payload)

    def notify_recovered(
        self,
        target_url: str,
        downtime_seconds: float,
        status_code: int = 200,
        latency_ms: Optional[float] = None,
    ) -> bool:
        """Send RECOVERED alert to Discord."""
        now_iso = datetime.now(timezone.utc).isoformat()
        latency_text = f"{latency_ms:.1f} ms" if latency_ms is not None else "N/A"

        # Format downtime
        hours, rem = divmod(int(downtime_seconds), 3600)
        minutes, seconds = divmod(rem, 60)
        if hours > 0:
            downtime_str = f"{hours} jam {minutes} menit {seconds} detik"
        elif minutes > 0:
            downtime_str = f"{minutes} menit {seconds} detik"
        else:
            downtime_str = f"{seconds} detik"

        fields = [
            {"name": "🌐 Target URL", "value": f"[{target_url}]({target_url})", "inline": False},
            {"name": "✅ Status", "value": f"`HTTP {status_code} OK`", "inline": True},
            {"name": "⏱️ Response Time", "value": f"`{latency_text}`", "inline": True},
            {"name": "⏳ Total Downtime", "value": f"`{downtime_str}`", "inline": True},
        ]

        embed = {
            "title": "✅ WEBSITE RECOVERED / ONLINE",
            "description": f"Website **{target_url}** telah kembali normal dan dapat diakses.",
            "color": COLOR_RECOVERED,
            "fields": fields,
            "timestamp": now_iso,
            "footer": {"text": "Bot Monitoring • Recovery System"},
        }

        payload = {"embeds": [embed]}
        return self.send_webhook(payload)

    def notify_degraded(
        self,
        target_url: str,
        latency_ms: float,
        threshold_ms: float,
        status_code: int = 200,
    ) -> bool:
        """Send DEGRADED (Slow response) alert to Discord."""
        now_iso = datetime.now(timezone.utc).isoformat()

        fields = [
            {"name": "🌐 Target URL", "value": f"[{target_url}]({target_url})", "inline": False},
            {"name": "⚠️ Status", "value": f"`HTTP {status_code} OK (Slow)`", "inline": True},
            {"name": "⏱️ Latency", "value": f"`{latency_ms:.1f} ms` (Batas: `{threshold_ms:.0f} ms`)", "inline": True},
        ]

        embed = {
            "title": "🟡 WEBSITE PERFORMANCE DEGRADED",
            "description": f"Website **{target_url}** merespons dengan waktu sangat lambat.",
            "color": COLOR_DEGRADED,
            "fields": fields,
            "timestamp": now_iso,
            "footer": {"text": "Bot Monitoring • Performance Alert"},
        }

        payload = {"embeds": [embed]}
        return self.send_webhook(payload)

    def notify_test(self, target_url: Union[str, List[str]]) -> bool:
        """Send a test message to confirm webhook configuration."""
        now_iso = datetime.now(timezone.utc).isoformat()

        if isinstance(target_url, str):
            urls = [target_url]
        else:
            urls = list(target_url)

        if len(urls) == 1:
            desc = f"Bot monitoring berhasil terhubung ke channel Discord ini.\nMemantau target: **{urls[0]}**"
        else:
            target_list_str = "\n".join([f"• `{u}`" for u in urls])
            desc = f"Bot monitoring berhasil terhubung ke channel Discord ini.\n**Target Website ({len(urls)} website):**\n{target_list_str}"

        embed = {
            "title": "🤖 Discord Webhook Monitoring Connected",
            "description": desc,
            "color": COLOR_INFO,
            "timestamp": now_iso,
            "footer": {"text": "Bot Monitoring • Test Connection"},
        }

        payload = {"embeds": [embed]}
        return self.send_webhook(payload)

    def notify_ssl_expiring(self, target_url: str, days_remaining: int) -> bool:
        """Send SSL expiring warning alert to Discord."""
        now_iso = datetime.now(timezone.utc).isoformat()

        fields = [
            {"name": "🌐 Target URL", "value": f"[{target_url}]({target_url})", "inline": False},
            {"name": "⏳ Sisa Waktu", "value": f"`{days_remaining} hari lagi`", "inline": True},
            {"name": "⚠️ Rekomendasi", "value": "Segera lakukan renewal / perpanjangan sertifikat SSL sebelum kedaluwarsa.", "inline": False},
        ]

        embed = {
            "title": "🔒 PERINGATAN: SERTIFIKAT SSL AKAN KEDALUWARSA",
            "description": f"Sertifikat SSL untuk **{target_url}** akan kedaluwarsa dalam **{days_remaining} hari**.",
            "color": COLOR_SSL,
            "fields": fields,
            "timestamp": now_iso,
            "footer": {"text": "Bot Monitoring • SSL Certificate Alert"},
        }

        payload: Dict[str, Any] = {"embeds": [embed]}
        if self.mention:
            payload["content"] = f"{self.mention} ⚠️ **Peringatan: Sertifikat SSL akan kedaluwarsa!**"

        return self.send_webhook(payload)

