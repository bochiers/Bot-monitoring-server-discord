from unittest.mock import MagicMock, patch
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

