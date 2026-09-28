import logging
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse
import requests
from requests.adapters import HTTPAdapter
from config import Config
from notifier import DiscordNotifier

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; FedoraBotMonitor/1.0; +https://deploy.fedora.biz.id/)"

# TTL cache for SSL expiry: {hostname: (expiry_days, cached_timestamp)}
_SSL_CACHE: Dict[str, Tuple[int, float]] = {}
_SSL_CACHE_TTL = 21600  # 6 hours in seconds


def check_ssl_expiry(url: str, timeout: float = 3.0) -> Optional[int]:
    """Returns the number of days until the SSL certificate expires, cached for 6 hours."""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https":
            return None
        hostname = parsed.hostname
        if not hostname:
            return None
        port = parsed.port or 443

        now = time.time()
        if hostname in _SSL_CACHE:
            days, cached_time = _SSL_CACHE[hostname]
            if now - cached_time < _SSL_CACHE_TTL:
                return days

        context = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                if not cert or "notAfter" not in cert:
                    return None
                # Format example: 'May 20 12:00:00 2026 GMT'
                expire_date = datetime.strptime(
                    cert["notAfter"], "%b %d %H:%M:%S %Y %Z"
                ).replace(tzinfo=timezone.utc)
                remaining_days = max(0, (expire_date - datetime.now(timezone.utc)).days)
                _SSL_CACHE[hostname] = (remaining_days, now)
                return remaining_days
    except Exception as e:
        logger.debug(f"SSL certificate check skipped/failed for {url}: {e}")
        return None


@dataclass
class CheckResult:
    is_healthy: bool
    target_url: str = ""
    status_code: Optional[int] = None
    latency_ms: Optional[float] = None
    error_message: Optional[str] = None
    timestamp: float = 0.0
    ssl_expiry_days: Optional[int] = None


class WebsiteMonitor:
    def __init__(
        self,
        config: Config,
        notifier: DiscordNotifier,
        target_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.config = config
        self.notifier = notifier
        self.target_url = target_url or config.target_url
        if session is not None:
            self.session = session
        else:
            self.session = requests.Session()
            adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)

        self.is_currently_down = False
        self.consecutive_failures = 0
        self.down_since: Optional[float] = None
        self.last_degraded_alert: float = 0.0
        self.last_ssl_alert: float = 0.0

    def check(self) -> CheckResult:
        """Performs lightweight HTTP GET check on target URL with keep-alive and returns CheckResult."""
        target_url = self.target_url
        timeout = self.config.request_timeout
        headers = {"User-Agent": USER_AGENT}

        start_time = time.perf_counter()
        try:
            # stream=True ensures we only fetch headers without consuming full body payload
            response = self.session.get(
                target_url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
                stream=True,
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            status_code = response.status_code
            reason = getattr(response, "reason", "")
            response.close()

            # Consider 2xx and 3xx as healthy
            if 200 <= status_code < 400:
                ssl_days = None
                if getattr(self.config, "enable_ssl_check", True):
                    ssl_days = check_ssl_expiry(target_url, timeout=min(3.0, float(timeout)))

                return CheckResult(
                    is_healthy=True,
                    target_url=target_url,
                    status_code=status_code,
                    latency_ms=elapsed_ms,
                    timestamp=time.time(),
                    ssl_expiry_days=ssl_days,
                )
            else:
                return CheckResult(
                    is_healthy=False,
                    target_url=target_url,
                    status_code=status_code,
                    latency_ms=elapsed_ms,
                    error_message=f"HTTP status code {status_code} ({reason})",
                    timestamp=time.time(),
                )

        except requests.exceptions.Timeout:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return CheckResult(
                is_healthy=False,
                target_url=target_url,
                latency_ms=elapsed_ms,
                error_message=f"Request timed out (> {timeout}s)",
                timestamp=time.time(),
            )
        except requests.exceptions.SSLError as e:
            return CheckResult(
                is_healthy=False,
                target_url=target_url,
                error_message=f"SSL/TLS Certificate error: {e}",
                timestamp=time.time(),
            )
        except requests.exceptions.ConnectionError as e:
            return CheckResult(
                is_healthy=False,
                target_url=target_url,
                error_message=f"Connection failed / DNS error: {e}",
                timestamp=time.time(),
            )
        except requests.exceptions.RequestException as e:
            return CheckResult(
                is_healthy=False,
                target_url=target_url,
                error_message=f"Request exception: {e}",
                timestamp=time.time(),
            )

    def process_result(self, result: CheckResult) -> None:
        """Updates internal state and sends notifications on state changes."""
        target_url = result.target_url or self.target_url

        if result.is_healthy:
            # Check if recovering from DOWN state
            if self.is_currently_down:
                downtime_seconds = (time.time() - self.down_since) if self.down_since else 0.0
                logger.warning(
                    f"🟢 [RECOVERED] {target_url} is back ONLINE! "
                    f"Status: {result.status_code}, Latency: {result.latency_ms:.1f}ms, Downtime: {downtime_seconds:.1f}s"
                )
                self.notifier.notify_recovered(
                    target_url=target_url,
                    downtime_seconds=downtime_seconds,
                    status_code=result.status_code or 200,
                    latency_ms=result.latency_ms,
                )
                self.is_currently_down = False
                self.down_since = None

            self.consecutive_failures = 0
            ssl_log = f" (SSL: {result.ssl_expiry_days}d)" if result.ssl_expiry_days is not None else ""
            logger.info(
                f"✅ [UP] {target_url} - HTTP {result.status_code} ({result.latency_ms:.1f} ms){ssl_log}"
            )

            # Warning if SSL expires in <= 14 days
            if result.ssl_expiry_days is not None and result.ssl_expiry_days <= 14:
                logger.warning(
                    f"⚠️ [SSL EXPIRING] Certificate for {target_url} expires in {result.ssl_expiry_days} days!"
                )
                now = time.time()
                if (
                    result.ssl_expiry_days <= 7
                    and now - self.last_ssl_alert > 86400
                    and hasattr(self.notifier, "notify_ssl_expiring")
                ):
                    self.notifier.notify_ssl_expiring(
                        target_url=target_url,
                        days_remaining=result.ssl_expiry_days,
                    )
                    self.last_ssl_alert = now

            # Optional slow response alert (cooldown: 15 minutes)
            if (
                self.config.degraded_latency_threshold_ms > 0
                and result.latency_ms
                and result.latency_ms > self.config.degraded_latency_threshold_ms
            ):
                now = time.time()
                if now - self.last_degraded_alert > 900:  # 15 mins cooldown
                    logger.warning(
                        f"🟡 [DEGRADED] High latency detected: {result.latency_ms:.1f} ms "
                        f"(Threshold: {self.config.degraded_latency_threshold_ms} ms)"
                    )
                    self.notifier.notify_degraded(
                        target_url=target_url,
                        latency_ms=result.latency_ms,
                        threshold_ms=float(self.config.degraded_latency_threshold_ms),
                        status_code=result.status_code or 200,
                    )
                    self.last_degraded_alert = now

        else:
            self.consecutive_failures += 1
            logger.error(
                f"❌ [FAIL #{self.consecutive_failures}] {target_url} - {result.error_message}"
            )

            # Trigger DOWN alert once threshold is reached and we haven't already marked DOWN
            if self.consecutive_failures >= self.config.failure_threshold:
                if not self.is_currently_down:
                    self.is_currently_down = True
                    self.down_since = time.time()
                    logger.critical(
                        f"🚨 [DOWN ALERT] Triggering alert for {target_url}. Error: {result.error_message}"
                    )
                    self.notifier.notify_down(
                        target_url=target_url,
                        reason=result.error_message or "Unknown error",
                        status_code=result.status_code,
                        latency_ms=result.latency_ms,
                        consecutive_failures=self.consecutive_failures,
                    )
                else:
                    logger.debug(
                        f"Target {target_url} remains DOWN ({self.consecutive_failures} failures so far)."
                    )
