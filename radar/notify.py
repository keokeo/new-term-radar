"""Mail a Markdown report through Resend or Gmail SMTP. Optional.

RADAR_MAIL_PROVIDER selects resend (the default) or gmail_smtp. Resend requires
RESEND_API_KEY and EMAIL_FROM. Gmail requires GMAIL_USER and GMAIL_APP_PASSWORD;
the authenticated Gmail account is always the sender. RADAR_EMAIL_TO accepts
comma-separated recipients. Without it, sending is skipped.
Needs the `markdown` package (pip install markdown).
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import UA

# Inline styles: Gmail drops a <style> block that sits outside <head>, which leaves
# tables unbordered with centred headers. Every tag carries its own.
FONT = "-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif"
INLINE = {
    "h2": "font-size:18px;margin:0 0 12px;color:#1f2328",
    "h3": "font-size:15px;margin:24px 0 8px;color:#1f2328",
    "p": "margin:8px 0",
    "table": "border-collapse:collapse;margin:8px 0;width:100%",
    "th": "border:1px solid #d0d7de;padding:6px 10px;font-size:13px;background:#f6f8fa;text-align:left;white-space:nowrap",
    "td": "border:1px solid #d0d7de;padding:6px 10px;font-size:13px;vertical-align:top",
    "ul": "padding-left:0;margin:8px 0;list-style:none",
    "li": "margin:0 0 10px;padding:8px 12px;border-left:3px solid #d0d7de;background:#f6f8fa;font-size:13px",
    "a": "color:#0969da",
}
TAG = re.compile(r"<(" + "|".join(INLINE) + r")(\s[^>]*)?>")
_ADDRESS = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[a-z]{2,}$", re.I)


def _inline(m: re.Match) -> str:
    attrs = m.group(2) or ""
    if m.group(1) == "td" and 'style="text-align' in attrs:   # markdown's own column alignment
        return f"<td{attrs}>"
    return f'<{m.group(1)} style="{INLINE[m.group(1)]}"{attrs}>'


def html(md_text: str) -> str:
    import markdown
    body = TAG.sub(_inline, markdown.markdown(md_text, extensions=["tables"]))
    return f'<div style="font:14px/1.6 {FONT};color:#1f2328;max-width:900px">{body}</div>'


def recipients(raw: str) -> list[str]:
    """RADAR_EMAIL_TO as people type it: commas (also full-width), semicolons or spaces
    between addresses; quotes and angle brackets around them are dropped."""
    parts = re.split(r"[,，;；\s]+", raw.strip().strip("\"'"))
    return [p.strip("\"'<>") for p in parts if p.strip("\"'<>")]


def send(subject: str, md_text: str) -> str:
    to = recipients(os.environ.get("RADAR_EMAIL_TO", ""))
    if not to:
        return "mail skipped: RADAR_EMAIL_TO not set"
    bad = [i + 1 for i, a in enumerate(to) if not _ADDRESS.match(a)]
    if bad:   # the address itself stays out of the log
        raise SystemExit(f"RADAR_EMAIL_TO: {len(to)} address(es), number {bad} is not an email address")
    provider = os.environ.get("RADAR_MAIL_PROVIDER", "resend").strip().lower()
    if provider == "gmail_smtp":
        return _gmail(subject, md_text, to)
    if provider == "resend":
        return _resend(subject, md_text, to)
    raise SystemExit("RADAR_MAIL_PROVIDER: choose resend or gmail_smtp")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"mail: set {name}")
    return value


def _gmail(subject: str, md_text: str, to: list[str]) -> str:
    user = _required("GMAIL_USER")
    if not _ADDRESS.fullmatch(user):
        raise SystemExit("GMAIL_USER: set a single email address")
    password = "".join(_required("GMAIL_APP_PASSWORD").split())
    if len(password) != 16:
        raise SystemExit("GMAIL_APP_PASSWORD: use a 16-character Google app password")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = ", ".join(to)
    message["Date"] = formatdate()
    message["Message-ID"] = make_msgid(domain="gmail.com")
    message.set_content(md_text)
    message.add_alternative(html(md_text), subtype="html")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(user, password)
            refused = smtp.send_message(message, from_addr=user, to_addrs=to)
            if refused:
                raise SystemExit(f"Gmail refused {len(refused)} recipient(s)")
    except smtplib.SMTPAuthenticationError:
        raise SystemExit("Gmail authentication failed: check GMAIL_USER and GMAIL_APP_PASSWORD") from None
    except smtplib.SMTPRecipientsRefused:
        raise SystemExit("Gmail refused the recipient(s)") from None
    except smtplib.SMTPException as exc:
        code = getattr(exc, "smtp_code", None)
        detail = f" (SMTP {code})" if isinstance(code, int) else ""
        raise SystemExit(f"Gmail SMTP send failed{detail}") from None
    except OSError as exc:
        raise SystemExit(f"Gmail connection failed ({type(exc).__name__})") from None
    return "mail sent (Gmail SMTP)"


def _resend(subject: str, md_text: str, to: list[str]) -> str:
    sender = _required("EMAIL_FROM")
    key = _required("RESEND_API_KEY")
    body = json.dumps({"from": sender, "to": to, "subject": subject, "html": html(md_text)}).encode()
    # Resend sits behind Cloudflare, which answers Python's default User-Agent with 403 "error code: 1010".
    req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return f"mail sent ({res.status})"
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Resend {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
