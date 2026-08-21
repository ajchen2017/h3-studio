"""Gmail SMTP mailer - same STARTTLS pattern as the doc/stock projects'
email_service.py, ported for consistency across this user's projects."""
import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send_email(to_addr, subject, html_body):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", 587))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    if not user or not password:
        logger.error("[Email] SMTP_USER / SMTP_PASSWORD 未設定，無法寄信")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to_addr
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=30) as srv:
            srv.ehlo()
            srv.starttls(context=ctx)
            srv.ehlo()
            srv.login(user, password)
            srv.sendmail(user, to_addr, msg.as_string())
        logger.info(f"[Email] 已寄出至 {to_addr}: {subject}")
        return True
    except Exception as e:
        logger.error(f"[Email] 寄送失敗: {e}")
        return False


def build_reset_email_html(reset_url):
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="background:#14151A;color:#EDEAE3;font-family:Arial,sans-serif;padding:24px;max-width:520px;margin:0 auto">
  <h2 style="color:#F2A96F">H3 Studio 管理密碼重設</h2>
  <p>收到一筆重設管理密碼的請求。點下面的連結設定新密碼，連結 30 分鐘內有效：</p>
  <p><a href="{reset_url}" style="background:#E8935A;color:#1A120A;padding:10px 18px;border-radius:6px;text-decoration:none;display:inline-block;font-weight:bold">重設密碼</a></p>
  <p style="color:#93949E;font-size:13px">如果不是你本人操作，請忽略這封信。</p>
</body></html>"""
