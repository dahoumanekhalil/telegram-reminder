"""
إرسال البريد الإلكتروني عبر SMTP (إعداداته في .env).
- كل عنوان يصله بريد مستقل (لا أحد يرى عناوين الآخرين).
- اتصال واحد بالخادم لكل دفعة، ويُعاد الاتصال تلقائيًا إن انقطع.
- smtplib تعمل بشكل متزامن (blocking)، لذلك تُشغَّل في thread منفصل
  حتى لا يتوقف البوت عن الرد أثناء إرسال الرسائل.
"""
import asyncio
import html
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional

import config

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_URL_RE = re.compile(r"(https?://[^\s<]+)")


@dataclass(frozen=True)
class EmailResult:
    sent: int
    failed: int
    error: Optional[str] = None  # خطأ عام (اتصال/تسجيل دخول) أوقف الإرسال


def normalize_email(raw: str) -> Optional[str]:
    """ينظّف العنوان (مسافات، <>، فواصل، أحرف كبيرة). يعيد None إن لم يكن عنوانًا صالحًا."""
    email = raw.strip().strip("<>,;\"'()[]").lower()
    return email if EMAIL_RE.match(email) else None


def describe_error(exc: Exception) -> str:
    """رسالة خطأ مفهومة للمالك بدل رسائل SMTP التقنية."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (
            "رفض الخادم اسم المستخدم أو كلمة المرور. في Gmail يجب استعمال "
            "\"كلمة مرور التطبيقات\" (App Password) وليس كلمة مرور حسابك العادية."
        )
    if isinstance(exc, (TimeoutError, ConnectionRefusedError, OSError)) and not isinstance(exc, smtplib.SMTPException):
        return f"تعذّر الاتصال بالخادم {config.SMTP_HOST}:{config.SMTP_PORT} — تأكد من SMTP_HOST و SMTP_PORT و SMTP_SECURITY. ({exc})"
    return str(exc) or exc.__class__.__name__


# ---------------------------------------------------------------------------
# بناء الرسالة
# ---------------------------------------------------------------------------
def _html_body(text: str) -> str:
    """نسخة HTML من النص: من اليمين لليسار، والروابط قابلة للضغط."""
    escaped = html.escape(text)
    linked = _URL_RE.sub(r'<a href="\1">\1</a>', escaped).replace("\n", "<br>\n")
    return (
        '<!doctype html><html lang="ar" dir="rtl"><body style="margin:0;padding:16px;">'
        '<div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;font-size:16px;'
        'line-height:1.7;text-align:right;color:#222;">'
        f"{linked}</div></body></html>"
    )


def _build_message(to: str, subject: str, text: str) -> EmailMessage:
    body = f"{text}\n\n{config.EMAIL_FOOTER}" if config.EMAIL_FOOTER else text
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((config.EMAIL_FROM_NAME, config.EMAIL_FROM))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=config.EMAIL_FROM.split("@")[-1])
    msg.set_content(body)
    msg.add_alternative(_html_body(body), subtype="html")
    return msg


# ---------------------------------------------------------------------------
# الاتصال والإرسال (متزامن - يُستدعى داخل thread)
# ---------------------------------------------------------------------------
def _connect() -> smtplib.SMTP:
    context = ssl.create_default_context()
    if config.SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30, context=context)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
        if config.SMTP_SECURITY == "starttls":
            server.starttls(context=context)
    if config.SMTP_USER:
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
    return server


def _close(server: Optional[smtplib.SMTP]) -> None:
    if server is None:
        return
    try:
        server.quit()
    except OSError:  # يشمل أخطاء SMTP - الاتصال منتهٍ على أي حال
        pass


def _send_batch(recipients: list[str], subject: str, text: str) -> EmailResult:
    try:
        server = _connect()
    except (smtplib.SMTPException, OSError) as e:
        logger.error("البريد: تعذّر الاتصال بخادم SMTP: %s", e)
        return EmailResult(0, len(recipients), describe_error(e))

    sent, failed = 0, 0
    try:
        for index, to in enumerate(recipients):
            msg = _build_message(to, subject, text)
            try:
                try:
                    server.send_message(msg)
                except smtplib.SMTPServerDisconnected:
                    # الخادم أغلق الاتصال (مهلة/حد) - نعيد الاتصال مرة واحدة
                    _close(server)
                    server = _connect()
                    server.send_message(msg)
                sent += 1
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError, smtplib.SMTPSenderRefused) as e:
                logger.error("البريد: رُفض الإرسال إلى %s: %s", to, e)
                failed += 1
            except (smtplib.SMTPException, OSError) as e:
                # تعذّر إعادة الاتصال: لا فائدة من المتابعة
                logger.error("البريد: توقف الإرسال عند %s: %s", to, e)
                remaining = len(recipients) - index
                return EmailResult(sent, failed + remaining, describe_error(e))
    finally:
        _close(server)

    return EmailResult(sent, failed)


# ---------------------------------------------------------------------------
# الواجهة غير المتزامنة (تُستدعى من البوت)
# ---------------------------------------------------------------------------
async def send_to_list(recipients: list[str], subject: str, text: str) -> EmailResult:
    if not recipients:
        return EmailResult(0, 0)
    result = await asyncio.to_thread(_send_batch, recipients, subject, text)
    logger.info("البريد: «%s» نجح %d، فشل %d", subject, result.sent, result.failed)
    return result


async def send_test(to: str) -> Optional[str]:
    """يرسل بريد تجربة. يعيد None عند النجاح، أو وصف الخطأ."""
    result = await send_to_list(
        [to],
        "✅ تجربة بريد بوت التذكير",
        "هذه رسالة تجربة من بوت التذكير بالمواعيد.\nإن وصلتك، فإعدادات البريد تعمل بشكل صحيح. 🎉",
    )
    if result.sent:
        return None
    return result.error or "رفض الخادم الإرسال لهذا العنوان (راجع bot.log للتفاصيل)."
