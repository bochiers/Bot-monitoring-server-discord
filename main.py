import argparse
import concurrent.futures
import logging
import signal
import sys
import time
from typing import List
import requests
from requests.adapters import HTTPAdapter
from config import Config
from monitor import CheckResult, WebsiteMonitor
from notifier import DiscordNotifier

# Setup logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("BotMonitoring")


class MonitoringRunner:
    def __init__(self, config: Config):
        self.config = config

        # Persistent requests session with connection pooling
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.notifier = DiscordNotifier(
            webhook_url=config.discord_webhook_url,
            mention=config.alert_mention,
            bot_token=config.discord_bot_token,
            channel_id=config.discord_channel_id,
            session=self.session,
        )
        self.monitors: List[WebsiteMonitor] = [
            WebsiteMonitor(
                config=self.config,
                notifier=self.notifier,
                target_url=url,
                session=self.session,
            )
            for url in self.config.target_urls
        ]
        self.running = True

        # Reusable ThreadPoolExecutor
        max_workers = min(20, max(1, len(self.monitors)))
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="MonitorRunner",
        )

        # Signal handlers
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        logger.info("Shutdown signal received. Stopping monitoring bot gracefully...")
        self.running = False
        try:
            self.executor.shutdown(wait=False)
            self.session.close()
        except Exception:
            pass

    def run_check_once(self) -> bool:
        """Run single check on all configured targets concurrently and output results."""
        logger.info(f"Running diagnostic check on {len(self.monitors)} target website(s)...")
        all_healthy = True

        def _do_check(monitor: WebsiteMonitor) -> CheckResult:
            return monitor.check()

        future_to_monitor = {
            self.executor.submit(_do_check, monitor): monitor for monitor in self.monitors
        }
        for future in concurrent.futures.as_completed(future_to_monitor):
            monitor = future_to_monitor[future]
            try:
                result = future.result()
                if result.is_healthy:
                    ssl_info = f" | SSL: {result.ssl_expiry_days}d" if result.ssl_expiry_days is not None else ""
                    logger.info(
                        f"✅ [{monitor.target_url}] Status: HEALTHY | HTTP {result.status_code} | Latency: {result.latency_ms:.1f} ms{ssl_info}"
                    )
                else:
                    all_healthy = False
                    logger.error(
                        f"❌ [{monitor.target_url}] Status: UNHEALTHY | Error: {result.error_message}"
                    )
            except Exception as e:
                all_healthy = False
                logger.exception(f"❌ [{monitor.target_url}] Unexpected exception during check: {e}")

        if all_healthy:
            logger.info("🎉 All target websites are HEALTHY!")
        else:
            logger.error("⚠️ One or more target websites are UNHEALTHY.")
        return all_healthy

    def test_discord_webhook(self) -> bool:
        """Test Discord webhook or bot token notification integration."""
        if not self.config.is_discord_configured:
            logger.error(
                "❌ Neither DISCORD_WEBHOOK_URL nor (DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID) is configured in .env or environment!"
            )
            return False

        logger.info(f"Sending test notification to Discord for {len(self.config.target_urls)} target(s)...")
        success = self.notifier.notify_test(target_url=self.config.target_urls)
        if success:
            logger.info("✅ Test notification successfully received by Discord!")
        else:
            logger.error("❌ Failed to send test notification to Discord.")
        return success

    def _check_and_process_target(self, monitor: WebsiteMonitor) -> None:
        try:
            result = monitor.check()
            monitor.process_result(result)
        except Exception as e:
            logger.exception(f"Unexpected error in monitoring cycle for {monitor.target_url}: {e}")

    def run_loop(self):
        """Continuous monitoring loop for all targets concurrently reusing thread pool."""
        logger.info("==========================================")
        logger.info("🚀 Website Monitoring Bot Started")
        logger.info(f"🌐 Target URLs ({len(self.config.target_urls)} website(s)):")
        for url in self.config.target_urls:
            logger.info(f"   • {url}")
        logger.info(f"⏱️ Check Interval:    {self.config.check_interval} seconds")
        logger.info(f"⌛ Request Timeout:   {self.config.request_timeout} seconds")
        logger.info(f"🔁 Failure Threshold: {self.config.failure_threshold} consecutive fails")
        if self.config.discord_webhook_url:
            masked_url = self.config.discord_webhook_url[:35] + "..."
            logger.info(f"🔔 Discord Mode:      Webhook ({masked_url})")
        elif self.config.discord_bot_token and self.config.discord_channel_id:
            masked_token = self.config.discord_bot_token[:10] + "..."
            logger.info(f"🔔 Discord Mode:      Bot Token (Channel: {self.config.discord_channel_id}, Token: {masked_token})")
        if self.config.is_discord_configured:
            diag = self.notifier.validate_connection()
            if self.config.discord_webhook_url:
                if diag.get("webhook_ok"):
                    logger.info(f"✅ Discord Webhook OK: {diag.get('webhook_message')}")
                else:
                    logger.warning(f"⚠️ Discord Webhook Check Warning: {diag.get('webhook_message')}")
            if self.config.discord_bot_token:
                if diag.get("bot_token_ok"):
                    logger.info(f"✅ Discord Bot Token OK: {diag.get('bot_token_message')}")
                else:
                    logger.warning(f"⚠️ Discord Bot Token Check Warning: {diag.get('bot_token_message')}")
        logger.info("==========================================")

        while self.running:
            # Flush any pending failed alerts if Discord is back online
            self.notifier.flush_pending_alerts()

            futures = [
                self.executor.submit(self._check_and_process_target, monitor)
                for monitor in self.monitors
            ]
            concurrent.futures.wait(futures)

            # Sleep in 1-second chunks so signal handler responds promptly
            for _ in range(self.config.check_interval):
                if not self.running:
                    break
                time.sleep(1)

        logger.info("Monitoring bot stopped.")


def main():
    parser = argparse.ArgumentParser(description="Website Monitoring Bot with Discord Alerts")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run a single check on all target URLs and exit",
    )
    parser.add_argument(
        "--test-discord",
        action="store_true",
        help="Send a test notification to the configured Discord Webhook or Bot Token channel and exit",
    )
    parser.add_argument(
        "--interval",
        type=int,
        help="Override check interval in seconds",
    )
    parser.add_argument(
        "--target",
        "--targets",
        dest="target",
        type=str,
        help="Override target URL(s) to monitor (comma or whitespace separated)",
    )
    parser.add_argument(
        "--bot",
        action="store_true",
        help="Run as an interactive Discord Bot (@Bot mention & Slash Commands)",
    )

    args = parser.parse_args()
    config = Config.from_env()

    if args.interval:
        config.check_interval = max(1, args.interval)
    if args.target:
        config.target_urls = Config.parse_urls(args.target)

    if args.bot:
        if not config.discord_bot_token:
            logger.critical("❌ DISCORD_BOT_TOKEN is required to run in --bot interactive mode!")
            sys.exit(1)
        from bot import run_interactive_bot
        run_interactive_bot(config)
        return

    runner = MonitoringRunner(config)

    if args.check_only:
        is_ok = runner.run_check_once()
        sys.exit(0 if is_ok else 1)

    if args.test_discord:
        is_ok = runner.test_discord_webhook()
        sys.exit(0 if is_ok else 1)

    runner.run_loop()


if __name__ == "__main__":
    main()
