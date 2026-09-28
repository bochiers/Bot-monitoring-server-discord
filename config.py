import os
import re
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()


@dataclass
class Config:
    target_urls: List[str] = field(default_factory=list)
    discord_webhook_url: Optional[str] = None
    discord_bot_token: Optional[str] = None
    discord_channel_id: Optional[str] = None
    check_interval: int = 60
    request_timeout: int = 10
    failure_threshold: int = 1
    alert_mention: Optional[str] = None
    degraded_latency_threshold_ms: int = 3000
    enable_ssl_check: bool = True

    def __init__(
        self,
        target_urls: Optional[List[str]] = None,
        discord_webhook_url: Optional[str] = None,
        discord_bot_token: Optional[str] = None,
        discord_channel_id: Optional[str] = None,
        check_interval: int = 60,
        request_timeout: int = 10,
        failure_threshold: int = 1,
        alert_mention: Optional[str] = None,
        degraded_latency_threshold_ms: int = 3000,
        target_url: Optional[str] = None,
        enable_ssl_check: bool = True,
    ):
        if target_urls is not None:
            if isinstance(target_urls, str):
                self.target_urls = self.parse_urls(target_urls)
            else:
                self.target_urls = list(target_urls)
        elif target_url is not None:
            self.target_urls = self.parse_urls(target_url)
        else:
            self.target_urls = ["https://deploy.fedora.biz.id/"]

        self.discord_webhook_url = discord_webhook_url
        self.discord_bot_token = discord_bot_token
        self.discord_channel_id = discord_channel_id
        self.check_interval = check_interval
        self.request_timeout = request_timeout
        self.failure_threshold = failure_threshold
        self.alert_mention = alert_mention
        self.degraded_latency_threshold_ms = degraded_latency_threshold_ms
        self.enable_ssl_check = enable_ssl_check

    @property
    def target_url(self) -> str:
        """Returns the primary target URL (first in the list) for backward compatibility."""
        return self.target_urls[0] if self.target_urls else ""

    @target_url.setter
    def target_url(self, value: str):
        self.target_urls = self.parse_urls(value) if value else []

    @property
    def is_discord_configured(self) -> bool:
        """Returns True if either webhook or bot token + channel is configured."""
        return bool(self.discord_webhook_url or (self.discord_bot_token and self.discord_channel_id))

    @classmethod
    def parse_urls(cls, raw: str) -> List[str]:
        """Parses comma-separated or newline-separated URL strings."""
        if not raw:
            return []
        tokens = re.split(r"[,\n\r]+", raw)
        cleaned = [t.strip() for t in tokens if t.strip()]
        seen = set()
        result = []
        for url in cleaned:
            if url not in seen:
                seen.add(url)
                result.append(url)
        return result

    @classmethod
    def from_env(cls) -> "Config":
        raw_urls = os.getenv("TARGET_URLS", "").strip() or os.getenv("TARGET_URL", "").strip()
        target_urls = cls.parse_urls(raw_urls)
        if not target_urls:
            target_urls = ["https://deploy.fedora.biz.id/"]

        discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None
        discord_bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip() or None
        discord_channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip() or None
        
        try:
            check_interval = max(5, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
        except ValueError:
            check_interval = 60

        try:
            request_timeout = max(1, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10")))
        except ValueError:
            request_timeout = 10

        try:
            failure_threshold = max(1, int(os.getenv("FAILURE_THRESHOLD", "1")))
        except ValueError:
            failure_threshold = 1

        alert_mention = os.getenv("ALERT_MENTION", "").strip() or None

        try:
            degraded_latency = max(0, int(os.getenv("DEGRADED_LATENCY_THRESHOLD_MS", "3000")))
        except ValueError:
            degraded_latency = 3000

        raw_ssl = os.getenv("ENABLE_SSL_CHECK", "true").strip().lower()
        enable_ssl_check = raw_ssl not in ("0", "false", "no", "off")

        return cls(
            target_urls=target_urls,
            discord_webhook_url=discord_webhook_url,
            discord_bot_token=discord_bot_token,
            discord_channel_id=discord_channel_id,
            check_interval=check_interval,
            request_timeout=request_timeout,
            failure_threshold=failure_threshold,
            alert_mention=alert_mention,
            degraded_latency_threshold_ms=degraded_latency,
            enable_ssl_check=enable_ssl_check,
        )
