"""SMTP email delivery. Sends the brief as HTML. ASCII-only source.
Robust to GitHub Actions injecting unset secrets as EMPTY strings (not unset):
use `os.getenv(x) or default` so '' falls back to the default."""
from __future__ import annotations
import os, smtplib, ssl, logging
from email.mime.text import MIMEText
from email.utils import formataddr
log = logging.getLogger("cockpit.notify")

def send(subject: str, body_md: str) -> bool:
    sender = os.getenv("EMAIL_SENDER"); pw = os.getenv("EMAIL_PASSWORD")
    if not (sender and pw):
        log.warning("email not configured; printing instead\n%s", body_md)
        return False
    receivers = [x.strip() for x in (os.getenv("EMAIL_RECEIVERS") or sender).split(",") if x.strip()]
    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    try:
        port = int(os.getenv("SMTP_PORT") or "587")
    except ValueError:
        port = 587
    html = ("<pre style='font-family:ui-monospace,Menlo,monospace;white-space:pre-wrap'>"
            + body_md.replace("&", "&amp;").replace("<", "&lt;") + "</pre>")
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Cockpit", sender))
    msg["To"] = ", ".join(receivers)
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(sender, pw)
            s.sendmail(sender, receivers, msg.as_string())
        log.info("email sent to %s", receivers)
        return True
    except Exception as e:
        log.error("email failed: %s", e)
        return False
