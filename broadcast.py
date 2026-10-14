"""
الإرسال الجماعي لكل المستقبِلين (المشتركين في الخاص + المجموعات النشطة).
مُعاد استخدامه من التذكيرات التلقائية، إعلان المواعيد، والبث اليدوي.
notify_all ترسل أيضًا نفس النص بالبريد الإلكتروني لكل العناوين المسجّلة.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from telegram import Bot
from telegram.error import ChatMigrated, Forbidden, RetryAfter, TelegramError

import config
import database as db
import mailer

logger = logging.getLogger(__name__)

# مهلة صغيرة بين كل رسالة وأخرى لتجنّب تجاوز حدود تيليغرام (~30 رسالة/ثانية)
_DELAY_BETWEEN_SENDS = 0.05


async def _deliver(send: Callable[[int], Awaitable], exclude_chat_id: int | None = None) -> tuple[int, int]:
    """يرسل لكل مستقبِل على حدة: فشل واحد لا يوقف الباقي. يعيد (نجح، فشل)."""
    success, failed = 0, 0
    for chat_id, name, kind in db.get_recipients():
        if chat_id == exclude_chat_id:
            continue
        try:
            try:
                await send(chat_id)
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await send(chat_id)
            success += 1
        except ChatMigrated as e:
            # المجموعة تحوّلت إلى supergroup: نحدّث معرّفها ونرسل للمعرّف الجديد
            logger.info("المجموعة '%s' تغيّر معرّفها %s -> %s", name, chat_id, e.new_chat_id)
            if db.migrate_group(chat_id, e.new_chat_id):
                try:
                    await send(e.new_chat_id)
                    success += 1
                except TelegramError as e2:
                    logger.error("فشل الإرسال لـ '%s' (%s): %s", name, e.new_chat_id, e2)
                    failed += 1
            # وإلا فالمعرّف الجديد مسجّل أصلاً وسيصله الإرسال ضمن القائمة
        except Forbidden:
            # المستخدم حظر البوت أو البوت لم يعد في المجموعة - نوقف الإرسال له
            logger.warning("تعذّر الإرسال لـ '%s' (%s): محظور/خارج. سيتم تعطيله.", name, chat_id)
            db.deactivate_recipient(chat_id, kind)
            failed += 1
        except TelegramError as e:
            logger.error("فشل الإرسال لـ '%s' (%s): %s", name, chat_id, e)
            failed += 1
        await asyncio.sleep(_DELAY_BETWEEN_SENDS)
    return success, failed


async def send_text_to_all(bot: Bot, text: str) -> tuple[int, int]:
    return await _deliver(lambda chat_id: bot.send_message(chat_id=chat_id, text=text))


async def copy_to_all(bot: Bot, from_chat_id: int, message_id: int) -> tuple[int, int]:
    """ينسخ رسالة (بدون forward يكشف المصدر) لكل المستقبِلين، ما عدا المحادثة الأصلية."""
    return await _deliver(
        lambda chat_id: bot.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id),
        exclude_chat_id=from_chat_id,
    )


@dataclass(frozen=True)
class Report:
    telegram_sent: int
    telegram_failed: int
    email: Optional[mailer.EmailResult]  # None = البريد غير مُفعّل

    def summary(self) -> str:
        lines = [f"💬 تيليغرام: نجح لـ {self.telegram_sent}، فشل لـ {self.telegram_failed}"]
        if self.email is not None:
            lines.append(f"📧 البريد: نجح لـ {self.email.sent}، فشل لـ {self.email.failed}")
            if self.email.error:
                lines.append(f"⚠️ مشكلة في البريد: {self.email.error}")
        return "\n".join(lines)


async def notify_all(bot: Bot, subject: str, text: str) -> Report:
    """
    يرسل النص لكل المستقبِلين على تيليغرام ولكل العناوين بالبريد، في نفس الوقت
    (البريد يعمل في thread منفصل أثناء إرسال رسائل تيليغرام).
    """
    email_task = None
    if config.EMAIL_ENABLED:
        email_task = asyncio.create_task(mailer.send_to_list(db.get_emails(), subject, text))

    telegram_sent, telegram_failed = await send_text_to_all(bot, text)
    email_result = await email_task if email_task else None
    return Report(telegram_sent, telegram_failed, email_result)
