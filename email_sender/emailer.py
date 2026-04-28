import logging
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)


class EmailSender:

    def __init__(self, smtp_host: str, smtp_port: int, from_addr: str, password: str):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.from_addr = from_addr
        self.password = password

    def send_report(
        self,
        to_addr: str,
        subject: str,
        html_body: str,
        plain_text: str,
    ) -> bool:
        msg = self._build_message(to_addr, subject, html_body, plain_text)
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.from_addr, self.password)
                server.sendmail(self.from_addr, [to_addr], msg.as_string())
            logger.info("Email sent to %s: %s", to_addr, subject)
            return True
        except smtplib.SMTPAuthenticationError:
            logger.error(
                "SMTP authentication failed. "
                "Ensure EMAIL_FROM uses a Gmail App Password, not your account password. "
                "See: https://myaccount.google.com/apppasswords"
            )
        except Exception as exc:
            logger.error("Failed to send email: %s", exc)

        # Fallback: save HTML to disk
        self._save_html_fallback(html_body)
        return False

    def _build_message(
        self,
        to_addr: str,
        subject: str,
        html_body: str,
        plain_text: str,
    ) -> MIMEMultipart:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = to_addr
        msg.attach(MIMEText(plain_text, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        return msg

    @staticmethod
    def _save_html_fallback(html_body: str) -> None:
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)
        path = output_dir / f"report_{date.today().isoformat()}.html"
        path.write_text(html_body, encoding="utf-8")
        logger.info("Email failed — HTML report saved to %s", path)
