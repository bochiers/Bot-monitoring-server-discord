import os
from unittest.mock import patch
from config import Config


def test_parse_urls_empty():
    assert Config.parse_urls("") == []
    assert Config.parse_urls("   ") == []


def test_parse_urls_comma_separated():
    raw = "https://a.com, https://b.com,https://c.com"
    assert Config.parse_urls(raw) == [
        "https://a.com",
        "https://b.com",
        "https://c.com",
    ]


def test_parse_urls_newline_separated():
    raw = "https://a.com\nhttps://b.com\r\nhttps://c.com"
    assert Config.parse_urls(raw) == [
        "https://a.com",
        "https://b.com",
        "https://c.com",
    ]


def test_parse_urls_deduplication():
    raw = "https://a.com, https://b.com, https://a.com"
    assert Config.parse_urls(raw) == ["https://a.com", "https://b.com"]


def test_config_backward_compatibility_properties():
    cfg = Config(target_url="https://single.com")
    assert cfg.target_urls == ["https://single.com"]
    assert cfg.target_url == "https://single.com"

    cfg.target_url = "https://updated.com"
    assert cfg.target_urls == ["https://updated.com"]
    assert cfg.target_url == "https://updated.com"


@patch.dict(os.environ, {"TARGET_URLS": "https://site1.com, https://site2.com"}, clear=True)
def test_config_from_env_target_urls():
    cfg = Config.from_env()
    assert cfg.target_urls == ["https://site1.com", "https://site2.com"]
    assert cfg.target_url == "https://site1.com"


@patch.dict(os.environ, {"TARGET_URL": "https://fallback.com"}, clear=True)
def test_config_from_env_fallback_target_url():
    cfg = Config.from_env()
    assert cfg.target_urls == ["https://fallback.com"]
    assert cfg.target_url == "https://fallback.com"


@patch.dict(os.environ, {}, clear=True)
def test_config_from_env_default():
    cfg = Config.from_env()
    assert len(cfg.target_urls) == 1
    assert "https://deploy.fedora.biz.id/" in cfg.target_urls[0]
    assert cfg.is_discord_configured is False


@patch.dict(
    os.environ,
    {
        "DISCORD_BOT_TOKEN": "bot_token_123",
        "DISCORD_CHANNEL_ID": "channel_456",
    },
    clear=True,
)
def test_config_from_env_bot_token():
    cfg = Config.from_env()
    assert cfg.discord_bot_token == "bot_token_123"
    assert cfg.discord_channel_id == "channel_456"
    assert cfg.is_discord_configured is True

