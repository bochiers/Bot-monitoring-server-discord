import asyncio
import concurrent.futures
import json
import logging
import os
import time
from typing import List, Optional
import discord
from discord import app_commands
from discord.ext import commands, tasks
import requests
from requests.adapters import HTTPAdapter
from config import Config
from monitor import CheckResult, WebsiteMonitor
from notifier import (
    COLOR_DEGRADED,
    COLOR_DOWN,
    COLOR_INFO,
    COLOR_RECOVERED,
    DiscordNotifier,
)

# Setup logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("BotMonitoring.DiscordBot")


class InteractiveMonitoringBot(commands.Bot):
    def __init__(self, config: Config, targets_file: Optional[str] = None):
        intents = discord.Intents.default()
        intents.message_content = True

        # Prefix supports mentioning the bot (@Bot <command>) as well as '?', 's!', 'S!', '!'
        super().__init__(
            command_prefix=commands.when_mentioned_or("?", "s!", "S!", "!"),
            intents=intents,
            help_command=None,
        )
        self.config = config
        self.targets_file = targets_file

        # Persistent requests session with connection pooling
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Reusable ThreadPoolExecutor across cycles
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=20,
            thread_name_prefix="BotMonitorWorker",
        )

        # Load persisted targets if file is configured
        self._load_persisted_targets()

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
        self._setup_events_and_commands()

    def _load_persisted_targets(self):
        """Loads saved target URLs from local JSON file if present."""
        if not self.targets_file:
            return
        try:
            if os.path.exists(self.targets_file):
                with open(self.targets_file, "r", encoding="utf-8") as f:
                    urls = json.load(f)
                if isinstance(urls, list):
                    for u in urls:
                        cleaned = u.strip()
                        if cleaned and cleaned not in self.config.target_urls:
                            self.config.target_urls.append(cleaned)
        except Exception as e:
            logger.warning(f"Could not load persisted targets from {self.targets_file}: {e}")

    def _save_persisted_targets(self):
        """Saves current target URLs to local JSON file for persistence across restarts."""
        if not self.targets_file:
            return
        try:
            with open(self.targets_file, "w", encoding="utf-8") as f:
                json.dump(self.config.target_urls, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save targets to {self.targets_file}: {e}")

    async def close(self):
        """Gracefully shuts down the bot, executor, and HTTP session."""
        self.executor.shutdown(wait=False)
        self.session.close()
        await super().close()

    async def setup_hook(self):
        """Called during bot login setup."""
        if not self.monitoring_task.is_running():
            # Update loop interval from config
            self.monitoring_task.change_interval(seconds=self.config.check_interval)
            self.monitoring_task.start()

    async def on_ready(self):
        logger.info(f"🤖 Interactive Discord Bot logged in as {self.user} (ID: {self.user.id})")
        # Sync slash commands with Discord
        try:
            synced = await self.tree.sync()
            logger.info(f"✅ Synced {len(synced)} application (slash) commands.")
        except Exception as e:
            logger.error(f"Failed to sync slash commands: {e}")

        # Set rich presence
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{len(self.config.target_urls)} website(s) | ?help",
        )
        await self.change_presence(activity=activity)

    def _check_and_process_target(self, monitor: WebsiteMonitor) -> None:
        try:
            result = monitor.check()
            monitor.process_result(result)
        except Exception as e:
            logger.exception(f"Unexpected error checking {monitor.target_url}: {e}")

    @tasks.loop(seconds=60)
    async def monitoring_task(self):
        """Background continuous monitoring loop running checks concurrently and non-blocking."""
        loop = asyncio.get_running_loop()

        # Flush any previously failed alerts if Discord connection is recovered
        if hasattr(self.notifier, "flush_pending_alerts"):
            await loop.run_in_executor(self.executor, self.notifier.flush_pending_alerts)

        monitors_snapshot = list(self.monitors)
        if not monitors_snapshot:
            return

        tasks_list = [
            loop.run_in_executor(self.executor, self._check_and_process_target, monitor)
            for monitor in monitors_snapshot
        ]
        await asyncio.gather(*tasks_list)

    @monitoring_task.before_loop
    async def before_monitoring_task(self):
        await self.wait_until_ready()

    def get_status_embed(self) -> discord.Embed:
        """Builds an embed with the current status of all monitored websites."""
        if not self.monitors:
            return discord.Embed(
                title="📊 Website Monitoring Status",
                description="Belum ada website yang dikonfigurasi untuk dipantau.\nGunakan `@Bot add <url>` atau `/add <url>` untuk menambahkan target.",
                color=COLOR_INFO,
            )

        all_healthy = all(not m.is_currently_down for m in self.monitors)
        embed_color = COLOR_RECOVERED if all_healthy else COLOR_DOWN
        title = "🟢 All Systems Operational" if all_healthy else "🚨 Outages Detected"

        embed = discord.Embed(
            title=f"📊 Website Monitoring Status • {title}",
            color=embed_color,
            timestamp=discord.utils.utcnow(),
        )

        for monitor in self.monitors:
            url = monitor.target_url
            if monitor.is_currently_down:
                since_str = f"sejak <t:{int(monitor.down_since)}:R>" if monitor.down_since else ""
                val = f"🔴 **DOWN** ({monitor.consecutive_failures} gagal) {since_str}"
            else:
                val = "🟢 **ONLINE / HEALTHY**"
            embed.add_field(name=f"🌐 {url}", value=val, inline=False)

        embed.set_footer(text=f"Check interval: {self.config.check_interval}s • Total: {len(self.monitors)} target")
        return embed

    def run_live_diagnostic(self, target_url: Optional[str] = None) -> discord.Embed:
        """Runs an on-demand live HTTP check on specific URL or all targets concurrently."""
        targets = [target_url] if target_url else list(self.config.target_urls)
        if not targets:
            return discord.Embed(
                title="🔍 Diagnostic Check",
                description="Tidak ada target URL untuk dicek.",
                color=COLOR_INFO,
            )

        embed = discord.Embed(
            title=f"🔍 Diagnostic Check ({len(targets)} Website)",
            timestamp=discord.utils.utcnow(),
        )

        def _do_check(url: str):
            temp_mon = WebsiteMonitor(
                config=self.config,
                notifier=self.notifier,
                target_url=url,
                session=self.session,
            )
            return url, temp_mon.check()

        # Run checks concurrently across targets
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(20, max(1, len(targets)))) as executor:
            results = list(executor.map(_do_check, targets))

        all_ok = True
        for url, res in results:
            if res.is_healthy:
                ssl_info = f" • SSL: `{res.ssl_expiry_days}d`" if getattr(res, "ssl_expiry_days", None) is not None else ""
                val = f"✅ `HTTP {res.status_code}` • Latency: `{res.latency_ms:.1f} ms`{ssl_info}"
            else:
                all_ok = False
                val = f"❌ **FAILED** • Error: `{res.error_message}`"
            embed.add_field(name=f"🌐 {url}", value=val, inline=False)

        embed.color = COLOR_RECOVERED if all_ok else COLOR_DOWN
        embed.set_footer(text="Live diagnostic completed (parallel check)")
        return embed

    def add_target_url(self, url: str) -> bool:
        """Adds a URL to the monitoring list if not already present."""
        url = url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        if url in self.config.target_urls:
            return False

        self.config.target_urls.append(url)
        self.monitors.append(
            WebsiteMonitor(
                config=self.config,
                notifier=self.notifier,
                target_url=url,
                session=self.session,
            )
        )
        self._save_persisted_targets()
        return True

    def remove_target_url(self, url: str) -> bool:
        """Removes a URL from monitoring."""
        url = url.strip()
        matched = None
        for u in self.config.target_urls:
            if u == url or u.rstrip("/") == url.rstrip("/"):
                matched = u
                break

        if not matched:
            return False

        self.config.target_urls.remove(matched)
        self.monitors = [m for m in self.monitors if m.target_url != matched]
        self._save_persisted_targets()
        return True

    def _setup_events_and_commands(self):
        """Registers Prefix Commands (@Bot <cmd> / !<cmd>) and Slash Commands."""

        # ------------------ PREFIX COMMANDS (TAG / MENTION BOT) ------------------

        @self.command(name="status", help="Menampilkan status realtime semua website")
        async def cmd_status(ctx: commands.Context):
            embed = self.get_status_embed()
            await ctx.reply(embed=embed, mention_author=False)

        @self.command(name="check", help="Mengecek status live website: @Bot check [url]")
        async def cmd_check(ctx: commands.Context, url: Optional[str] = None):
            async with ctx.typing():
                loop = asyncio.get_running_loop()
                embed = await loop.run_in_executor(None, self.run_live_diagnostic, url)
                await ctx.reply(embed=embed, mention_author=False)

        @self.command(name="add", help="Menambah website ke monitoring: @Bot add <url>")
        async def cmd_add(ctx: commands.Context, url: str):
            success = self.add_target_url(url)
            if success:
                embed = discord.Embed(
                    title="✅ Website Ditambahkan",
                    description=f"Berhasil menambahkan target monitoring:\n🌐 **{url}**\n\nTotal target sekarang: **{len(self.config.target_urls)}**",
                    color=COLOR_RECOVERED,
                )
            else:
                embed = discord.Embed(
                    title="⚠️ Website Sudah Ada",
                    description=f"Target **{url}** sudah ada dalam daftar monitoring.",
                    color=COLOR_DEGRADED,
                )
            await ctx.reply(embed=embed, mention_author=False)

        @self.command(name="remove", aliases=["delete", "rm"], help="Menghapus website: @Bot remove <url>")
        async def cmd_remove(ctx: commands.Context, url: str):
            success = self.remove_target_url(url)
            if success:
                embed = discord.Embed(
                    title="🗑️ Website Dihapus",
                    description=f"Berhasil menghapus target dari monitoring:\n🌐 **{url}**\n\nSisa target: **{len(self.config.target_urls)}**",
                    color=COLOR_INFO,
                )
            else:
                embed = discord.Embed(
                    title="❌ Tidak Ditemukan",
                    description=f"Target **{url}** tidak ditemukan dalam daftar monitoring.",
                    color=COLOR_DOWN,
                )
            await ctx.reply(embed=embed, mention_author=False)

        @self.command(name="list", help="Menampilkan daftar semua website yang dipantau")
        async def cmd_list(ctx: commands.Context):
            if not self.config.target_urls:
                desc = "Belum ada website yang dipantau."
            else:
                desc = "\n".join([f"`{i+1}.` {u}" for i, u in enumerate(self.config.target_urls)])
            embed = discord.Embed(
                title=f"📋 Daftar Target Monitoring ({len(self.config.target_urls)} Website)",
                description=desc,
                color=COLOR_INFO,
            )
            await ctx.reply(embed=embed, mention_author=False)

        @self.command(name="ping", help="Cek latency bot Discord")
        async def cmd_ping(ctx: commands.Context):
            latency_ms = self.latency * 1000.0
            await ctx.reply(f"🏓 Pong! Latency Bot: `{latency_ms:.1f} ms`", mention_author=False)

        @self.command(name="sync", help="Sinkronisasi slash command ke server: ?sync [all|global|clear]")
        async def cmd_sync(ctx: commands.Context, target: Optional[str] = None):
            async with ctx.typing():
                mode = (target or "").strip().lower()

                if mode in ("all", "guilds", "servers"):
                    if not self.guilds:
                        embed = discord.Embed(
                            title="⚠️ Sinkronisasi Command",
                            description="Bot belum tergabung dalam server manapun.",
                            color=COLOR_DEGRADED,
                        )
                        await ctx.reply(embed=embed, mention_author=False)
                        return

                    success_guilds = []
                    failed_guilds = []
                    for guild in self.guilds:
                        try:
                            self.tree.copy_global_to(guild=guild)
                            synced = await self.tree.sync(guild=guild)
                            success_guilds.append(f"• **{guild.name}** (`{len(synced)} command`)")
                        except Exception as e:
                            logger.error(f"Failed to sync commands to guild {guild.name} ({guild.id}): {e}")
                            failed_guilds.append(f"• **{guild.name}**: {e}")

                    desc_lines = []
                    if success_guilds:
                        desc_lines.append(f"✅ Berhasil menyinkronkan ke **{len(success_guilds)}/{len(self.guilds)}** server yang mengundang bot:\n" + "\n".join(success_guilds))
                    if failed_guilds:
                        desc_lines.append(f"\n⚠️ Gagal menyinkronkan ke **{len(failed_guilds)}** server:\n" + "\n".join(failed_guilds))

                    embed = discord.Embed(
                        title="🔄 Sinkronisasi Command (Semua Server)",
                        description="\n\n".join(desc_lines),
                        color=COLOR_RECOVERED if not failed_guilds else COLOR_DEGRADED,
                        timestamp=discord.utils.utcnow(),
                    )
                    await ctx.reply(embed=embed, mention_author=False)

                elif mode in ("global", "g"):
                    try:
                        synced = await self.tree.sync()
                        embed = discord.Embed(
                            title="🌐 Sinkronisasi Global Berhasil",
                            description=(
                                f"Berhasil menyinkronkan **{len(synced)}** application command secara global ke Discord API.\n\n"
                                "⏱️ *Catatan: Propagasi global Discord dapat memakan waktu hingga 1 jam.*"
                            ),
                            color=COLOR_RECOVERED,
                            timestamp=discord.utils.utcnow(),
                        )
                    except Exception as e:
                        logger.error(f"Failed to sync global commands: {e}")
                        embed = discord.Embed(
                            title="❌ Gagal Sinkronisasi Global",
                            description=f"Terjadi kesalahan saat sinkronisasi global:\n```{e}```",
                            color=COLOR_DOWN,
                        )
                    await ctx.reply(embed=embed, mention_author=False)

                elif mode in ("clear", "clean"):
                    if not ctx.guild:
                        embed = discord.Embed(
                            title="⚠️ Gagal Menghapus Command",
                            description="Perintah pembersihan command hanya dapat dijalankan di dalam server.",
                            color=COLOR_DOWN,
                        )
                        await ctx.reply(embed=embed, mention_author=False)
                        return
                    try:
                        self.tree.clear_commands(guild=ctx.guild)
                        await self.tree.sync(guild=ctx.guild)
                        embed = discord.Embed(
                            title="🧹 Pembersihan Command Berhasil",
                            description=f"Semua slash command khusus server **{ctx.guild.name}** telah dihapus.",
                            color=COLOR_INFO,
                        )
                    except Exception as e:
                        logger.error(f"Failed to clear commands for guild {ctx.guild.name}: {e}")
                        embed = discord.Embed(
                            title="❌ Gagal Menghapus Command",
                            description=f"Terjadi kesalahan saat membersihkan command:\n```{e}```",
                            color=COLOR_DOWN,
                        )
                    await ctx.reply(embed=embed, mention_author=False)

                else:
                    # Default: Sync to current guild (or fallback to all if in DM)
                    if ctx.guild:
                        try:
                            self.tree.copy_global_to(guild=ctx.guild)
                            synced = await self.tree.sync(guild=ctx.guild)
                            cmd_list = ", ".join([f"`/{getattr(c, 'name', str(c))}`" for c in synced]) if synced else "Tidak ada"
                            embed = discord.Embed(
                                title="🔄 Sinkronisasi Command Berhasil",
                                description=(
                                    f"Berhasil menyinkronkan **{len(synced)}** slash command ke server **{ctx.guild.name}**!\n\n"
                                    f"**Daftar Command Aktif:**\n{cmd_list}\n\n"
                                    f"💡 *Slash command langsung aktif dan dapat digunakan di server ini.*"
                                ),
                                color=COLOR_RECOVERED,
                                timestamp=discord.utils.utcnow(),
                            )
                            embed.set_footer(
                                text=f"Total server bot: {len(self.guilds)} • Gunakan '?sync all' untuk menyinkronkan ke seluruh server"
                            )
                        except Exception as e:
                            logger.error(f"Failed to sync commands to guild {ctx.guild.name}: {e}")
                            embed = discord.Embed(
                                title="❌ Gagal Sinkronisasi Command",
                                description=f"Terjadi kesalahan saat menyinkronkan command ke server **{ctx.guild.name}**:\n```{e}```",
                                color=COLOR_DOWN,
                            )
                        await ctx.reply(embed=embed, mention_author=False)
                    else:
                        # DM fallback: sync all guilds where bot is invited
                        if not self.guilds:
                            embed = discord.Embed(
                                title="⚠️ Sinkronisasi Command",
                                description="Bot belum tergabung dalam server manapun.",
                                color=COLOR_DEGRADED,
                            )
                            await ctx.reply(embed=embed, mention_author=False)
                            return

                        success_count = 0
                        for guild in self.guilds:
                            try:
                                self.tree.copy_global_to(guild=guild)
                                await self.tree.sync(guild=guild)
                                success_count += 1
                            except Exception as e:
                                logger.error(f"Failed to sync to guild {guild.name}: {e}")

                        embed = discord.Embed(
                            title="🔄 Sinkronisasi Command Berhasil (DM Mode)",
                            description=f"Berhasil menyinkronkan slash command ke **{success_count}/{len(self.guilds)}** server yang mengundang bot.",
                            color=COLOR_RECOVERED,
                            timestamp=discord.utils.utcnow(),
                        )
                        await ctx.reply(embed=embed, mention_author=False)

        @self.command(name="help", help="Bantuan perintah bot")
        async def cmd_help(ctx: commands.Context):
            embed = discord.Embed(
                title="🤖 Panduan Perintah Bot Monitoring",
                description=(
                    "Gunakan prefix **`?`**, **`s!`**, **tag/mention bot** (`@Server Alarm`), atau **Slash Command** (`/`):\n\n"
                    "• `?status` / `s!status` / `/status` — Cek status realtime semua website\n"
                    "• `?check [url]` / `s!check [url]` / `/check` — Jalankan live check diagnostik\n"
                    "• `?add <url>` / `s!add <url>` / `/add` — Tambah website baru ke monitoring\n"
                    "• `?remove <url>` / `s!remove <url>` / `/remove` — Hapus website dari monitoring\n"
                    "• `?list` / `s!list` / `/list` — Daftar website yang sedang dipantau\n"
                    "• `?ping` / `s!ping` / `/ping` — Cek latency koneksi Discord bot\n"
                    "• `?sync [all|global|clear]` — Sinkronisasi slash command ke server Discord"
                ),
                color=COLOR_INFO,
            )
            await ctx.reply(embed=embed, mention_author=False)

        # ------------------ APPLICATION (SLASH) COMMANDS ------------------

        @self.tree.command(name="status", description="Menampilkan status realtime seluruh website yang dipantau")
        async def slash_status(interaction: discord.Interaction):
            embed = self.get_status_embed()
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="check", description="Mengecek diagnostik live website sekarang")
        @app_commands.describe(url="URL website yang ingin dicek (opsional, kosongkan untuk semua)")
        async def slash_check(interaction: discord.Interaction, url: Optional[str] = None):
            await interaction.response.defer()
            loop = asyncio.get_running_loop()
            embed = await loop.run_in_executor(None, self.run_live_diagnostic, url)
            await interaction.followup.send(embed=embed)

        @self.tree.command(name="add", description="Menambahkan website baru ke daftar monitoring")
        @app_commands.describe(url="URL website (misal: https://example.com)")
        async def slash_add(interaction: discord.Interaction, url: str):
            success = self.add_target_url(url)
            if success:
                embed = discord.Embed(
                    title="✅ Website Ditambahkan",
                    description=f"Berhasil menambahkan target monitoring:\n🌐 **{url}**\n\nTotal target sekarang: **{len(self.config.target_urls)}**",
                    color=COLOR_RECOVERED,
                )
            else:
                embed = discord.Embed(
                    title="⚠️ Website Sudah Ada",
                    description=f"Target **{url}** sudah ada dalam daftar monitoring.",
                    color=COLOR_DEGRADED,
                )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="remove", description="Menghapus website dari daftar monitoring")
        @app_commands.describe(url="URL website yang ingin dihapus")
        async def slash_remove(interaction: discord.Interaction, url: str):
            success = self.remove_target_url(url)
            if success:
                embed = discord.Embed(
                    title="🗑️ Website Dihapus",
                    description=f"Berhasil menghapus target dari monitoring:\n🌐 **{url}**\n\nSisa target: **{len(self.config.target_urls)}**",
                    color=COLOR_INFO,
                )
            else:
                embed = discord.Embed(
                    title="❌ Tidak Ditemukan",
                    description=f"Target **{url}** tidak ditemukan dalam daftar monitoring.",
                    color=COLOR_DOWN,
                )
            await interaction.response.send_message(embed=embed)

        # Autocomplete for slash commands with URL arguments
        async def _url_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> List[app_commands.Choice[str]]:
            urls = self.config.target_urls
            return [
                app_commands.Choice(name=u, value=u)
                for u in urls
                if current.lower() in u.lower()
            ][:25]

        slash_check.autocomplete("url")(_url_autocomplete)
        slash_remove.autocomplete("url")(_url_autocomplete)

        @self.tree.command(name="list", description="Menampilkan daftar semua website yang sedang dipantau")
        async def slash_list(interaction: discord.Interaction):
            if not self.config.target_urls:
                desc = "Belum ada website yang dipantau."
            else:
                desc = "\n".join([f"`{i+1}.` {u}" for i, u in enumerate(self.config.target_urls)])
            embed = discord.Embed(
                title=f"📋 Daftar Target Monitoring ({len(self.config.target_urls)} Website)",
                description=desc,
                color=COLOR_INFO,
            )
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="ping", description="Mengecek latency bot Discord")
        async def slash_ping(interaction: discord.Interaction):
            latency_ms = self.latency * 1000.0
            await interaction.response.send_message(f"🏓 Pong! Latency Bot: `{latency_ms:.1f} ms`")


def run_interactive_bot(config: Config):
    """Entry point for running interactive Discord bot with rate limit handling."""
    if not config.discord_bot_token:
        logger.critical("DISCORD_BOT_TOKEN is not configured! Cannot start interactive bot.")
        return

    retry_delay = 15
    max_delay = 300

    while True:
        try:
            bot = InteractiveMonitoringBot(
                config=config,
                targets_file=os.getenv("TARGETS_FILE", "targets.json"),
            )
            bot.run(config.discord_bot_token, log_handler=None)
            break
        except discord.errors.HTTPException as e:
            if e.status == 429:
                logger.warning(
                    f"⚠️ [Rate Limit] Discord API rate limit (429) encountered: {e}. "
                    f"Menunggu {retry_delay} detik sebelum mencoba login kembali agar tidak terkena ban global..."
                )
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)
            else:
                logger.error(f"Discord HTTP Exception: {e}")
                time.sleep(10)
        except discord.errors.LoginFailure as e:
            logger.critical(f"❌ [Auth Error] DISCORD_BOT_TOKEN tidak valid atau revoked: {e}")
            break
        except (
            discord.errors.GatewayNotFound,
            discord.errors.ConnectionClosed,
            discord.errors.DiscordServerError,
            ConnectionError,
            TimeoutError,
            OSError,
        ) as e:
            logger.warning(
                f"⚠️ [Connection Error] Gagal terhubung ke Discord Gateway/API: {e}. "
                f"Menghubungkan ulang dalam {retry_delay} detik..."
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_delay)
        except KeyboardInterrupt:
            logger.info("Bot dihentikan oleh user.")
            break
        except Exception as e:
            logger.exception(f"Unexpected error in bot lifecycle: {e}")
            time.sleep(10)


if __name__ == "__main__":
    cfg = Config.from_env()
    run_interactive_bot(cfg)