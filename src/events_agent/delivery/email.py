"""Email sending — stdlib smtplib only, no new dependency.

Pattern (STARTTLS + login, multipart HTML-with-plain-fallback) carried over
from a proven working script rather than invented from scratch. Wired to
events-agent's own SMTP secrets (.env) instead of a separate config file.
"""

from __future__ import annotations

import smtplib
import sys
from email.message import EmailMessage


def send_email(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_email: str,
    to_email: str,
    subject: str,
    html_body: str,
    plain_body: str,
) -> bool:
    if not (smtp_host and smtp_user and smtp_password and to_email):
        print("Email skipped: SMTP_HOST/SMTP_USER/SMTP_PASSWORD or a recipient is not configured.", file=sys.stderr)
        return False

    # Gmail app passwords are commonly copy-pasted with spaces in them.
    password = smtp_password.replace(" ", "")

    message = EmailMessage()
    message["From"] = from_email
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, password)
            server.send_message(message)
        return True
    except Exception as exc:
        print(f"Email send failed: {exc}", file=sys.stderr)
        return False
