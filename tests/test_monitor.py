import time
from unittest.mock import MagicMock, patch
import pytest
import requests
from config import Config
from monitor import CheckResult, WebsiteMonitor


@pytest.fixture
def mock_config():
    return Config(
        target_url="https://deploy.fedora.biz.id/",
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
        check_interval=60,
        request_timeout=5,
        failure_threshold=2,
        alert_mention=None,
        degraded_latency_threshold_ms=3000,
        enable_ssl_check=False,
    )


@pytest.fixture
def mock_notifier():
    notifier = MagicMock()
    notifier.notify_down.return_value = True
    notifier.notify_recovered.return_value = True
    notifier.notify_degraded.return_value = True
    return notifier


@patch("requests.Session.get")
def test_check_healthy_200(mock_get, mock_config, mock_notifier):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_get.return_value = mock_response

    monitor = WebsiteMonitor(config=mock_config, notifier=mock_notifier)
    result = monitor.check()

    assert result.is_healthy is True
    assert result.status_code == 200
    assert result.error_message is None
    assert result.latency_ms is not None


@patch("requests.Session.get")
def test_check_unhealthy_500(mock_get, mock_config, mock_notifier):
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.reason = "Internal Server Error"
    mock_get.return_value = mock_response

    monitor = WebsiteMonitor(config=mock_config, notifier=mock_notifier)
    result = monitor.check()

    assert result.is_healthy is False
    assert result.status_code == 500
    assert "500" in (result.error_message or "")


@patch("requests.Session.get")
def test_check_timeout(mock_get, mock_config, mock_notifier):
    mock_get.side_effect = requests.exceptions.Timeout("Connection timed out")

    monitor = WebsiteMonitor(config=mock_config, notifier=mock_notifier)
    result = monitor.check()

    assert result.is_healthy is False
    assert "timed out" in (result.error_message or "").lower()


@patch("requests.Session.get")
def test_check_connection_error(mock_get, mock_config, mock_notifier):
    mock_get.side_effect = requests.exceptions.ConnectionError("DNS lookup failed")

    monitor = WebsiteMonitor(config=mock_config, notifier=mock_notifier)
    result = monitor.check()

    assert result.is_healthy is False
    assert "Connection failed" in (result.error_message or "")


def test_state_transition_down_and_recovered(mock_config, mock_notifier):
    # failure_threshold is 2
    monitor = WebsiteMonitor(config=mock_config, notifier=mock_notifier)

    # 1st Failure: Should increment failure count, but NOT trigger notify_down yet (threshold=2)
    fail_result = CheckResult(
        is_healthy=False,
        status_code=503,
        error_message="HTTP status code 503 (Service Unavailable)",
        timestamp=time.time(),
    )
    monitor.process_result(fail_result)
    assert monitor.consecutive_failures == 1
    assert monitor.is_currently_down is False
    mock_notifier.notify_down.assert_not_called()

    # 2nd Failure: Should reach threshold and trigger notify_down
    monitor.process_result(fail_result)
    assert monitor.consecutive_failures == 2
    assert monitor.is_currently_down is True
    mock_notifier.notify_down.assert_called_once()

    # 3rd Failure: Still down, but should NOT spam notify_down again
    monitor.process_result(fail_result)
    assert monitor.consecutive_failures == 3
    assert mock_notifier.notify_down.call_count == 1

    # Recovery: Send healthy result
    ok_result = CheckResult(
        is_healthy=True,
        status_code=200,
        latency_ms=35.0,
        timestamp=time.time(),
    )
    monitor.process_result(ok_result)
    assert monitor.is_currently_down is False
    assert monitor.consecutive_failures == 0
    mock_notifier.notify_recovered.assert_called_once()


def test_multiple_monitors_independent_states(mock_config, mock_notifier):
    # Monitor 1 for site A, Monitor 2 for site B
    monitor_a = WebsiteMonitor(config=mock_config, notifier=mock_notifier, target_url="https://site-a.com")
    monitor_b = WebsiteMonitor(config=mock_config, notifier=mock_notifier, target_url="https://site-b.com")

    # Site A fails twice (threshold=2) -> triggers down alert for Site A
    fail_result_a = CheckResult(
        is_healthy=False,
        target_url="https://site-a.com",
        status_code=500,
        error_message="Internal Server Error",
    )
    monitor_a.process_result(fail_result_a)
    monitor_a.process_result(fail_result_a)

    assert monitor_a.is_currently_down is True
    assert monitor_b.is_currently_down is False
    assert monitor_b.consecutive_failures == 0

    mock_notifier.notify_down.assert_called_once_with(
        target_url="https://site-a.com",
        reason="Internal Server Error",
        status_code=500,
        latency_ms=None,
        consecutive_failures=2,
    )

    # Site B succeeds
    ok_result_b = CheckResult(
        is_healthy=True,
        target_url="https://site-b.com",
        status_code=200,
        latency_ms=20.0,
    )
    monitor_b.process_result(ok_result_b)
    assert monitor_b.is_currently_down is False
    assert monitor_a.is_currently_down is True


def test_check_ssl_expiry_unit():
    from monitor import check_ssl_expiry
    with patch("socket.create_connection") as mock_conn:
        with patch("ssl.create_default_context") as mock_ctx:
            mock_sock = MagicMock()
            mock_ssock = MagicMock()
            mock_conn.return_value.__enter__.return_value = mock_sock
            mock_ctx.return_value.wrap_socket.return_value.__enter__.return_value = mock_ssock

            # Set certificate notAfter in future
            mock_ssock.getpeercert.return_value = {
                "notAfter": "Jan 01 00:00:00 2030 GMT"
            }

            days = check_ssl_expiry("https://example.com")
            assert days is not None
            assert days > 100


