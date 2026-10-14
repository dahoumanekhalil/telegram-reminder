"""
الفحص الدوري للتذكيرات: كل CHECK_INTERVAL_SECONDS ثانية يمر البوت على المواعيد
القادمة ويرسل أي تذكير حان وقته. لا تُحفظ المهام في الذاكرة بل يُحسب كل شيء من
قاعدة البيانات، لذلك لا يضيع أي تذكير عند إعادة تشغيل البوت ولا يتكرر.
تعتمد على JobQueue المدمجة في python-telegram-bot.
"""
import logging
import math
from datetime import datetime

from telegram.error import TelegramError
from telegram.ext import ContextTypes

import appointments
import broadcast
import config
import database as db
from database import Appointment

logger = logging.getLogger(__name__)


def skip_past_reminders(appt: Appointment, now: datetime) -> None:
    """
    يعلّم التذكيرات التي فات وقتها كمُرسلة، حتى لا تُرسل متأخرة دفعة واحدة.
    يُستدعى عند إنشاء موعد (مثلاً موعد اليوم يُضاف بعد الساعة 8 صباحًا)
    وعند انتقال موعد أسبوعي لأسبوعه التالي.
    إن كان الموعد قريبًا (بعد دقائق) يبقى آخر تذكير "قبل الموعد" ليُرسل فورًا.
    """
    past = [r for r in appointments.build_reminders(appt) if r.send_at <= now]
    # موعد قريب (بعد دقائق): نُبقي أحدث تذكير "قبل الموعد" ليُرسل فورًا
    # بنص "يبدأ بعد X دقيقة". أما تذكير الصباح/المساء الفائت فلا معنى لإرساله متأخرًا.
    if past and past[-1].kind.startswith(appointments.KIND_BEFORE_PREFIX):
        past.pop()
    if past:
        db.mark_reminders_sent(appt.id, [r.kind for r in past])


def _finish(appt: Appointment, now: datetime) -> None:
    """موعد بدأ: ينتهي، أو ينتقل للأسبوع القادم إن كان أسبوعيًا."""
    if appt.repeat == db.REPEAT_WEEKLY:
        next_start = appointments.next_weekly_occurrence(appt, now)
        db.reschedule_appointment(appt.id, next_start)
        skip_past_reminders(db.get_appointment(appt.id), now)
        logger.info("الموعد الأسبوعي #%d انتقل إلى %s", appt.id, next_start)
    else:
        db.set_appointment_status(appt.id, db.APPT_DONE)
        logger.info("الموعد #%d انتهى", appt.id)


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(config.TIMEZONE)

    for appt in db.get_scheduled_appointments():
        if appt.starts_at <= now:
            _finish(appt, now)
            continue

        sent = db.get_sent_kinds(appt.id)
        due = [r for r in appointments.build_reminders(appt) if r.send_at <= now and r.kind not in sent]
        if not due:
            continue

        # إن تراكمت عدة تذكيرات (البوت كان متوقفًا) نرسل الأحدث فقط
        db.mark_reminders_sent(appt.id, [r.kind for r in due])
        latest = due[-1]
        if not appointments.is_still_relevant(latest, now):
            continue

        subject, text = appointments.reminder_content(appt, latest.kind, now)
        report = await broadcast.notify_all(context.bot, subject, text)
        logger.info("تذكير #%d (%s):\n%s", appt.id, latest.kind, report.summary())

        label = appointments.kind_label(latest.kind)
        if latest.kind.startswith(appointments.KIND_BEFORE_PREFIX):
            minutes_left = max(1, math.ceil((appt.starts_at - now).total_seconds() / 60))
            label = f"قبل {appointments.minutes_to_text(minutes_left)}"
        try:
            await context.bot.send_message(
                chat_id=config.OWNER_ID,
                text=f"🔔 أُرسل تذكير «{appt.title}» ({label}):\n{report.summary()}",
            )
        except TelegramError:
            pass


def schedule_reminder_checks(job_queue) -> None:
    """يُسجّل الفحص الدوري في JobQueue. تُستدعى مرة واحدة عند الإقلاع."""
    job_queue.run_repeating(
        check_reminders,
        interval=config.CHECK_INTERVAL_SECONDS,
        first=5,
        name="reminder_checks",
    )
    logger.info("تم تفعيل فحص التذكيرات كل %d ثانية.", config.CHECK_INTERVAL_SECONDS)
