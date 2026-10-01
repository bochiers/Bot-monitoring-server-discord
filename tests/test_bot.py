import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from bot import InteractiveMonitoringBot
from config import Config
from monitor import CheckResult


@pytest.fixture
def mock_config():
    return Config(
        target_urls=["https://site1.com", "https://site2.com"],
        discord_bot_token="test_token_123",
        discord_channel_id="123456789",
        check_interval=60,
        request_timeout=5,
        failure_threshold=1,
        enable_ssl_check=False,
    )


def test_bot_initialization(mock_config):
    bot = InteractiveMonitoringBot(config=mock_config)
    assert len(bot.monitors) == 2
    assert bot.monitors[0].target_url == "https://site1.com"
    assert bot.monitors[1].target_url == "https://site2.com"


def test_bot_add_target_url(mock_config):
    bot = InteractiveMonitoringBot(config=mock_config)

    # Add new URL
    assert bot.add_target_url("https://newsite.com") is True
    assert "https://newsite.com" in bot.config.target_urls
    assert len(bot.monitors) == 3

    # Add duplicate URL (should fail)
    assert bot.add_target_url("https://newsite.com") is False
    assert len(bot.monitors) == 3

    # Add URL without protocol (should prepend https://)
    assert bot.add_target_url("example.org") is True
    assert "https://example.org" in bot.config.target_urls


def test_bot_remove_target_url(mock_config):
    bot = InteractiveMonitoringBot(config=mock_config)

    # Remove existing URL
    assert bot.remove_target_url("https://site1.com") is True
    assert "https://site1.com" not in bot.config.target_urls
    assert len(bot.monitors) == 1

    # Remove non-existing URL
    assert bot.remove_target_url("https://nonexistent.com") is False


def test_bot_get_status_embed_all_healthy(mock_config):
    bot = InteractiveMonitoringBot(config=mock_config)
    embed = bot.get_status_embed()

    assert "All Systems Operational" in (embed.title or "")
    assert len(embed.fields) == 2
    assert all("ONLINE" in f.value for f in embed.fields)


def test_bot_get_status_embed_with_down_target(mock_config):
    bot = InteractiveMonitoringBot(config=mock_config)
    # Mark first monitor as DOWN
    bot.monitors[0].is_currently_down = True
    bot.monitors[0].consecutive_failures = 3

    embed = bot.get_status_embed()
    assert "Outages Detected" in (embed.title or "")
    assert any("DOWN" in f.value for f in embed.fields)


@patch("requests.Session.get")
def test_bot_run_live_diagnostic(mock_get, mock_config):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_get.return_value = mock_resp

    bot = InteractiveMonitoringBot(config=mock_config)
    embed = bot.run_live_diagnostic("https://site1.com")

    assert "Diagnostic Check" in (embed.title or "")
    assert len(embed.fields) == 1
    assert "HTTP 200" in embed.fields[0].value


def test_bot_target_persistence(tmp_path, mock_config):
    persisted_file = str(tmp_path / "targets.json")
    bot1 = InteractiveMonitoringBot(config=mock_config, targets_file=persisted_file)
    bot1.add_target_url("https://persisted-site.com")

    # Second bot loading same file
    bot2 = InteractiveMonitoringBot(config=Config(target_urls=[]), targets_file=persisted_file)
    assert "https://persisted-site.com" in bot2.config.target_urls


def test_bot_prefixes(mock_config):
    bot = InteractiveMonitoringBot(config=mock_config)
    bot._connection.user = MagicMock(id=999)
    prefixes = bot.command_prefix(bot, MagicMock())
    assert "?" in prefixes
    assert "s!" in prefixes
    assert "!" in prefixes


def test_bot_sync_current_guild(mock_config):
    async def _test():
        bot = InteractiveMonitoringBot(config=mock_config)
        cmd = bot.get_command("sync")
        assert cmd is not None

        ctx = MagicMock()
        ctx.guild = MagicMock(id=101)
        ctx.guild.name = "Production Server"
        ctx.typing.return_value.__aenter__ = AsyncMock()
        ctx.typing.return_value.__aexit__ = AsyncMock()
        ctx.reply = AsyncMock()

        bot.tree.copy_global_to = MagicMock()
        mock_cmd1 = MagicMock()
        mock_cmd1.name = "status"
        mock_cmd2 = MagicMock()
        mock_cmd2.name = "check"
        bot.tree.sync = AsyncMock(return_value=[mock_cmd1, mock_cmd2])

        await cmd.callback(ctx, None)

        bot.tree.copy_global_to.assert_called_once_with(guild=ctx.guild)
        bot.tree.sync.assert_called_once_with(guild=ctx.guild)
        assert ctx.reply.called
        embed = ctx.reply.call_args[1]["embed"]
        assert "Sinkronisasi Command Berhasil" in embed.title
        assert "Production Server" in embed.description
        assert "/status" in embed.description
        assert "/check" in embed.description

    asyncio.run(_test())


def test_bot_sync_all_guilds(mock_config):
    async def _test():
        bot = InteractiveMonitoringBot(config=mock_config)
        cmd = bot.get_command("sync")

        guild1 = MagicMock(id=101)
        guild1.name = "Server A"
        guild2 = MagicMock(id=102)
        guild2.name = "Server B"
        bot._connection._guilds = {101: guild1, 102: guild2}

        ctx = MagicMock()
        ctx.typing.return_value.__aenter__ = AsyncMock()
        ctx.typing.return_value.__aexit__ = AsyncMock()
        ctx.reply = AsyncMock()

        bot.tree.copy_global_to = MagicMock()
        bot.tree.sync = AsyncMock(return_value=[MagicMock(name="status")])

        await cmd.callback(ctx, "all")

        assert bot.tree.copy_global_to.call_count == 2
        assert bot.tree.sync.call_count == 2
        embed = ctx.reply.call_args[1]["embed"]
        assert "Semua Server" in embed.title
        assert "Server A" in embed.description
        assert "Server B" in embed.description

    asyncio.run(_test())


def test_bot_sync_global(mock_config):
    async def _test():
        bot = InteractiveMonitoringBot(config=mock_config)
        cmd = bot.get_command("sync")

        ctx = MagicMock()
        ctx.typing.return_value.__aenter__ = AsyncMock()
        ctx.typing.return_value.__aexit__ = AsyncMock()
        ctx.reply = AsyncMock()

        bot.tree.sync = AsyncMock(return_value=[MagicMock(name="status")])

        await cmd.callback(ctx, "global")

        bot.tree.sync.assert_called_once_with()
        embed = ctx.reply.call_args[1]["embed"]
        assert "Global" in embed.title

    asyncio.run(_test())


def test_bot_sync_clear(mock_config):
    async def _test():
        bot = InteractiveMonitoringBot(config=mock_config)
        cmd = bot.get_command("sync")

        ctx = MagicMock()
        ctx.guild = MagicMock(id=101)
        ctx.guild.name = "Production Server"
        ctx.typing.return_value.__aenter__ = AsyncMock()
        ctx.typing.return_value.__aexit__ = AsyncMock()
        ctx.reply = AsyncMock()

        bot.tree.clear_commands = MagicMock()
        bot.tree.sync = AsyncMock()

        await cmd.callback(ctx, "clear")

        bot.tree.clear_commands.assert_called_once_with(guild=ctx.guild)
        bot.tree.sync.assert_called_once_with(guild=ctx.guild)
        embed = ctx.reply.call_args[1]["embed"]
        assert "Pembersihan Command Berhasil" in embed.title

    asyncio.run(_test())


def test_bot_sync_error_handling(mock_config):
    async def _test():
        bot = InteractiveMonitoringBot(config=mock_config)
        cmd = bot.get_command("sync")

        ctx = MagicMock()
        ctx.guild = MagicMock(id=101)
        ctx.guild.name = "Faulty Server"
        ctx.typing.return_value.__aenter__ = AsyncMock()
        ctx.typing.return_value.__aexit__ = AsyncMock()
        ctx.reply = AsyncMock()

        bot.tree.copy_global_to = MagicMock()
        bot.tree.sync = AsyncMock(side_effect=Exception("Discord API error 500"))

        await cmd.callback(ctx, None)

        assert ctx.reply.called
        embed = ctx.reply.call_args[1]["embed"]
        assert "Gagal" in embed.title
        assert "Discord API error 500" in embed.description

    asyncio.run(_test())

