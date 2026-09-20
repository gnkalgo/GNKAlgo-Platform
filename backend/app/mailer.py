import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote
from .config import get_settings

settings = get_settings()

class MailDeliveryError(RuntimeError):
    """Raised when the configured SMTP service cannot accept a message."""

def _send(to: str, subject: str, text: str) -> None:
    if not settings.smtp_host: return
    message = EmailMessage(); message["From"] = settings.smtp_from; message["To"] = to; message["Subject"] = subject; message.set_content(text)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_username and settings.smtp_password: smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailDeliveryError("SMTP delivery failed") from exc

def send_verification(to: str, token: str) -> None:
    url = f"{settings.frontend_url}/verify-email?token={quote(token)}"
    _send(to, "Verify your GnKAlgo account", f"Verify your account using this link (expires soon):\n\n{url}\n\nIf you did not register, ignore this message.")

def send_password_reset(to: str, token: str) -> None:
    url = f"{settings.frontend_url}/reset-password?token={quote(token)}"
    _send(to, "Reset your GnKAlgo password", f"Reset your password using this link (expires soon):\n\n{url}\n\nIf you did not request this, ignore this message.")
