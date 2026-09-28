from unittest.mock import MagicMock, patch
import pytest
from notifier import (
    COLOR_DOWN,
    COLOR_RECOVERED,
    COLOR_DEGRADED,
    COLOR_INFO,
    DiscordNotifier,
)


def test_send_webhook_empty_url():
    notifier = DiscordNotifier(webhook_url=None)
    result = notifier.send_webhook({"content": "hello"})
    assert result is False


@patch("requests.Session.post")
def test_send_webhook_success(mock_post):
    mock_post.return_value.status_code = 204
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")
    result = notifier.send_webhook({"content": "hello"})
    assert result is True
    mock_post.assert_called_once()


@patch("requests.Session.post")
def test_send_notification_via_bot_token(mock_post):
    mock_post.return_value.status_code = 200
    notifier = DiscordNotifier(
        bot_token="test_bot_token",
        channel_id="987654321",
    )
    result = notifier.send_notification({"content": "bot message"})
    assert result is True
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert "https://discord.com/api/v10/channels/987654321/messages" in args[0]
    assert kwargs["headers"]["Authorization"] == "Bot test_bot_token"


@patch("requests.Session.post")
def test_send_notification_dual_broadcast(mock_post):
    mock_post.return_value.status_code = 200
    notifier = DiscordNotifier(
        webhook_url="https://discord.com/api/webhooks/123/abc",
        bot_token="test_bot_token",
        channel_id="987654321",
    )
    result = notifier.send_notification({"content": "dual message"})
    assert result is True
    assert mock_post.call_count == 2




@patch("requests.Session.post")
def test_notify_down_embed_format(mock_post):
    mock_post.return_value.status_code = 204
    notifier = DiscordNotifier(
        webhook_url="https://discord.com/api/webhooks/123/abc",
        mention="@everyone",
    )

    result = notifier.notify_down(
        target_url="https://deploy.fedora.biz.id/",
        reason="HTTP status code 502 (Bad Gateway)",
        status_code=502,
        latency_ms=120.5,
        consecutive_failures=2,
    )

    assert result is True
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    import json
    payload = json.loads(kwargs["data"])

    assert "@everyone" in payload["content"]
    assert len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    assert embed["color"] == COLOR_DOWN
    assert "DOWN" in embed["title"]
    assert any(f["name"] == "⚠️ Status" and "502" in f["value"] for f in embed["fields"])
    assert any(f["name"] == "🔁 Consecutive Failures" and "2" in f["value"] for f in embed["fields"])


@patch("requests.Session.post")
def test_notify_recovered_embed_format(mock_post):
    mock_post.return_value.status_code = 204
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")

    result = notifier.notify_recovered(
        target_url="https://deploy.fedora.biz.id/",
        downtime_seconds=125.0,
        status_code=200,
        latency_ms=45.2,
    )

    assert result is True
    import json
    _, kwargs = mock_post.call_args
    payload = json.loads(kwargs["data"])

    embed = payload["embeds"][0]
    assert embed["color"] == COLOR_RECOVERED
    assert "RECOVERED" in embed["title"]
    assert any("2 menit 5 detik" in f["value"] for f in embed["fields"] if f["name"] == "⏳ Total Downtime")


@patch("requests.Session.post")
def test_notify_degraded_embed_format(mock_post):
    mock_post.return_value.status_code = 204
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")

    result = notifier.notify_degraded(
        target_url="https://deploy.fedora.biz.id/",
        latency_ms=3500.0,
        threshold_ms=3000.0,
        status_code=200,
    )

    assert result is True
    import json
    _, kwargs = mock_post.call_args
    payload = json.loads(kwargs["data"])

    embed = payload["embeds"][0]
    assert embed["color"] == COLOR_DEGRADED
    assert "DEGRADED" in embed["title"]


@patch("requests.Session.post")
def test_notify_test_connection(mock_post):
    mock_post.return_value.status_code = 204
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")

    result = notifier.notify_test(target_url="https://deploy.fedora.biz.id/")
    assert result is True
    import json
    _, kwargs = mock_post.call_args
    payload = json.loads(kwargs["data"])

    embed = payload["embeds"][0]
    assert embed["color"] == COLOR_INFO
    assert "deploy.fedora.biz.id" in embed["description"]


@patch("requests.Session.post")
def test_notify_test_multiple_targets(mock_post):
    mock_post.return_value.status_code = 204
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")

    result = notifier.notify_test(target_url=["https://site1.com", "https://site2.com"])
    assert result is True
    import json
    _, kwargs = mock_post.call_args
    payload = json.loads(kwargs["data"])

    embed = payload["embeds"][0]
    assert embed["color"] == COLOR_INFO
    assert "site1.com" in embed["description"]
    assert "site2.com" in embed["description"]
    assert "(2 website)" in embed["description"]


@patch("requests.Session.post")
def test_notify_ssl_expiring(mock_post):
    from notifier import COLOR_SSL
    mock_post.return_value.status_code = 204
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/abc")

    result = notifier.notify_ssl_expiring(
        target_url="https://deploy.fedora.biz.id/",
        days_remaining=5,
    )
    assert result is True
    import json
    _, kwargs = mock_post.call_args
    payload = json.loads(kwargs["data"])

    embed = payload["embeds"][0]
    assert embed["color"] == COLOR_SSL
    assert "SSL" in embed["title"]
    assert any("5 hari lagi" in f["value"] for f in embed["fields"] if f["name"] == "⏳ Sisa Waktu")


@patch("requests.Session.post")
def test_webhook_circuit_breaker_on_404(mock_post):
    mock_post.return_value.status_code = 404
    mock_post.return_value.text = "Unknown Webhook"

    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/invalid")
    result = notifier.send_notification({"content": "test"})

    assert result is False
    assert notifier.webhook_disabled is True
    # Verify it was attempted once and disabled, not looping forever
    assert mock_post.call_count == 1

    # Second call should be skipped immediately by circuit breaker
    result2 = notifier.send_notification({"content": "test 2"})
    assert result2 is False
    assert mock_post.call_count == 1  # No additional post made!


@patch("requests.Session.post")
def test_failover_to_bot_token_when_webhook_fails(mock_post):
    # Webhook fails (404), Bot Token succeeds (200)
    def side_effect(url, **kwargs):
        resp = MagicMock()
        if "webhooks" in url:
            resp.status_code = 404
            resp.text = "Unknown Webhook"
        else:
            resp.status_code = 200
        return resp

    mock_post.side_effect = side_effect

    notifier = DiscordNotifier(
        webhook_url="https://discord.com/api/webhooks/broken",
        bot_token="test_token",
        channel_id="channel_123",
    )
    result = notifier.send_notification({"content": "urgent alert"})

    assert result is True
    assert notifier.webhook_disabled is True
    assert notifier.bot_token_disabled is False


@patch("requests.Session.post")
def test_offline_alert_queue_and_flush(mock_post, tmp_path):
    # First, mock network error
    import requests
    mock_post.side_effect = requests.RequestException("Internet down")

    queue_file = str(tmp_path / "pending_alerts.json")
    notifier = DiscordNotifier(
        webhook_url="https://discord.com/api/webhooks/myhook",
        failed_queue_file=queue_file,
    )
    result = notifier.send_notification({"content": "queued message"})
    assert result is False
    assert len(notifier.pending_alerts) == 1

    # Now internet is back: mock post success (200)
    mock_post.side_effect = None
    mock_post.return_value = MagicMock(status_code=204)

    flushed = notifier.flush_pending_alerts()
    assert flushed == 1
    assert len(notifier.pending_alerts) == 0


@patch("requests.Session.get")
def test_validate_connection(mock_get):
    # Webhook 200, Bot Token 200
    def side_effect(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "webhooks" in url:
            resp.json.return_value = {"name": "Server Alarm"}
        else:
            resp.json.return_value = {"username": "MonitoringBot"}
        return resp

    mock_get.side_effect = side_effect

    notifier = DiscordNotifier(
        webhook_url="https://discord.com/api/webhooks/valid",
        bot_token="valid_token",
    )
    results = notifier.validate_connection()
    assert results["webhook_ok"] is True
    assert "Server Alarm" in results["webhook_message"]
    assert results["bot_token_ok"] is True
    assert "MonitoringBot" in results["bot_token_message"]


