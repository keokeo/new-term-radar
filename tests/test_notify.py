"""Mail transport checks. All network entry points are replaced with offline fakes."""
from contextlib import nullcontext
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import json
import smtplib
import ssl
from types import SimpleNamespace
import urllib.request

import pytest

from radar import notify


USER = "sender@example.test"
PASSWORD = "abcdefghijklmnop"


@pytest.fixture(autouse=True)
def isolated_mail_environment(monkeypatch):
    for name in ("RADAR_MAIL_PROVIDER", "RADAR_EMAIL_TO", "EMAIL_FROM", "RESEND_API_KEY",
                 "GMAIL_USER", "GMAIL_APP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    def unexpected_network(*args, **kwargs):
        pytest.fail("A mail test attempted to use an unmocked network transport")

    monkeypatch.setattr(smtplib, "SMTP_SSL", unexpected_network)
    monkeypatch.setattr(urllib.request, "urlopen", unexpected_network)


@pytest.fixture
def gmail_environment(monkeypatch):
    monkeypatch.setenv("RADAR_MAIL_PROVIDER", "gmail_smtp")
    monkeypatch.setenv("RADAR_EMAIL_TO", "recipient@example.test")
    monkeypatch.setenv("GMAIL_USER", USER)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", PASSWORD)


@pytest.fixture
def smtp_transport(monkeypatch):
    captured = SimpleNamespace(
        connections=[], logins=[], messages=[], refused={},
        connect_error=None, login_error=None, send_error=None,
    )

    class SMTP:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, password):
            captured.logins.append((user, password))
            if captured.login_error:
                raise captured.login_error

        def send_message(self, message, *, from_addr, to_addrs):
            captured.messages.append((message, from_addr, to_addrs))
            if captured.send_error:
                raise captured.send_error
            return captured.refused

    def connect(host, port, **kwargs):
        captured.connections.append((host, port, kwargs))
        if captured.connect_error:
            raise captured.connect_error
        return SMTP()

    monkeypatch.setattr(smtplib, "SMTP_SSL", connect)
    return captured


def test_gmail_sends_utf8_plain_and_html_over_verified_tls(
        monkeypatch, gmail_environment, smtp_transport):
    monkeypatch.setenv("RADAR_EMAIL_TO", "first@example.test，second@example.test")
    # A stale Resend sender must not become the Gmail envelope or visible sender.
    monkeypatch.setenv("EMAIL_FROM", "Unrelated <other@example.test>")
    subject = "新词雷达 · 每日报告"
    markdown = "## 今天的新词\n\n| 词 | 状态 |\n|---|---|\n| 测试词 | 已发现 |"

    assert notify.send(subject, markdown).startswith("mail sent (")

    assert len(smtp_transport.connections) == 1
    host, port, options = smtp_transport.connections[0]
    assert (host, port, options["timeout"]) == ("smtp.gmail.com", 465, 30)
    assert isinstance(options["context"], ssl.SSLContext)
    assert options["context"].check_hostname is True
    assert options["context"].verify_mode == ssl.CERT_REQUIRED
    assert smtp_transport.logins == [(USER, PASSWORD)]
    assert len(smtp_transport.messages) == 1
    message, envelope_from, envelope_to = smtp_transport.messages[0]
    assert envelope_from == USER
    assert envelope_to == ["first@example.test", "second@example.test"]

    # Check the MIME after serialisation, including encoded headers and body parts.
    decoded = BytesParser(policy=policy.default).parsebytes(message.as_bytes(policy=policy.SMTP))
    assert decoded["Subject"] == subject
    assert getaddresses([decoded["From"]]) == [("", USER)]
    assert [address for _, address in getaddresses([decoded["To"]])] == envelope_to
    assert decoded.get_content_type() == "multipart/alternative"
    assert decoded.get_body(preferencelist=("plain",)).get_content().strip().splitlines() == markdown.splitlines()
    html = decoded.get_body(preferencelist=("html",)).get_content()
    assert "<table style=" in html and "测试词" in html and "已发现" in html
    assert decoded["Date"] and decoded["Message-ID"]


def test_gmail_accepts_grouped_app_password(monkeypatch, gmail_environment, smtp_transport):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", " abcd efgh\nijkl\tmnop ")
    notify.send("test", "body")
    assert smtp_transport.logins == [(USER, PASSWORD)]


def test_mail_without_recipient_never_connects(monkeypatch):
    monkeypatch.setenv("RADAR_MAIL_PROVIDER", "gmail_smtp")
    assert notify.send("test", "body").startswith("mail skipped")


@pytest.mark.parametrize("name", ["GMAIL_USER", "GMAIL_APP_PASSWORD"])
def test_gmail_missing_settings_fail_before_connecting(monkeypatch, gmail_environment, name):
    monkeypatch.delenv(name)
    with pytest.raises(SystemExit, match=name):
        notify.send("test", "body")


@pytest.mark.parametrize("password", ["   ", "short", "a" * 17])
def test_gmail_rejects_invalid_app_password(monkeypatch, gmail_environment, password):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", password)
    with pytest.raises(SystemExit, match="GMAIL_APP_PASSWORD"):
        notify.send("test", "body")


@pytest.mark.parametrize("user", ["not-an-email", "Sender <sender@example.test>",
                                  "sender@example.test\nBcc: other@example.test"])
def test_gmail_requires_one_plain_sender_address(monkeypatch, gmail_environment, user):
    monkeypatch.setenv("GMAIL_USER", user)
    with pytest.raises(SystemExit, match="GMAIL_USER"):
        notify.send("test", "body")


def test_invalid_recipient_is_rejected_without_exposing_it(monkeypatch, gmail_environment):
    bad_address = "private-invalid-address"
    monkeypatch.setenv("RADAR_EMAIL_TO", bad_address)
    with pytest.raises(SystemExit) as error:
        notify.send("test", "body")
    assert bad_address not in str(error.value)


def test_unknown_provider_never_falls_back_to_sending(monkeypatch):
    monkeypatch.setenv("RADAR_EMAIL_TO", "recipient@example.test")
    monkeypatch.setenv("RADAR_MAIL_PROVIDER", "misspelled-provider")
    with pytest.raises(SystemExit, match="RADAR_MAIL_PROVIDER"):
        notify.send("test", "body")


@pytest.mark.parametrize("stage,error", [
    ("connect_error", OSError(f"network error containing {PASSWORD}")),
    ("login_error", smtplib.SMTPAuthenticationError(535, f"bad password {PASSWORD}".encode())),
    ("send_error", smtplib.SMTPRecipientsRefused({"recipient@example.test": (550, b"refused")})),
    ("send_error", smtplib.SMTPDataError(554, f"server error containing {PASSWORD}".encode())),
    ("send_error", smtplib.SMTPServerDisconnected(f"server error containing {PASSWORD}")),
])
def test_gmail_transport_failures_are_redacted_and_not_retried(
        gmail_environment, smtp_transport, stage, error):
    setattr(smtp_transport, stage, error)
    with pytest.raises(SystemExit) as failure:
        notify.send("test", "body")
    assert PASSWORD not in str(failure.value)
    assert "recipient@example.test" not in str(failure.value)
    assert failure.value.__suppress_context__ is True
    assert len(smtp_transport.connections) == 1
    assert len(smtp_transport.logins) <= 1
    assert len(smtp_transport.messages) <= 1


def test_gmail_does_not_report_success_if_some_recipients_are_refused(
        gmail_environment, smtp_transport):
    smtp_transport.refused = {"recipient@example.test": (550, b"refused")}
    with pytest.raises(SystemExit, match="refused") as failure:
        notify.send("test", "body")
    assert "recipient@example.test" not in str(failure.value)
    assert len(smtp_transport.messages) == 1


def test_gmail_prevents_subject_header_injection(gmail_environment):
    with pytest.raises(ValueError):
        notify.send("report\nBcc: other@example.test", "body")


def test_default_provider_preserves_resend_request(monkeypatch):
    monkeypatch.setenv("RADAR_EMAIL_TO", "first@example.test; second@example.test")
    monkeypatch.setenv("EMAIL_FROM", "Radar <reports@example.test>")
    monkeypatch.setenv("RESEND_API_KEY", "resend-test-key")
    requests = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    assert notify.send("新词雷达", "## 正文") == "mail sent (200)"
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == "https://api.resend.com/emails"
    assert request.get_method() == "POST" and timeout == 30
    assert request.get_header("Authorization") == "Bearer resend-test-key"
    payload = json.loads(request.data)
    assert payload["from"] == "Radar <reports@example.test>"
    assert payload["to"] == ["first@example.test", "second@example.test"]
    assert payload["subject"] == "新词雷达" and "正文" in payload["html"]


@pytest.mark.parametrize("name", ["EMAIL_FROM", "RESEND_API_KEY"])
def test_resend_missing_setting_fails_before_connecting(monkeypatch, name):
    monkeypatch.setenv("RADAR_EMAIL_TO", "recipient@example.test")
    monkeypatch.setenv("EMAIL_FROM", "reports@example.test")
    monkeypatch.setenv("RESEND_API_KEY", "resend-test-key")
    monkeypatch.delenv(name)
    with pytest.raises(SystemExit, match=name):
        notify.send("test", "body")
