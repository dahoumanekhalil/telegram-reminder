"""
معالجات الأوامر والرسائل:
- الاشتراك: أي شخص يرسل /start للبوت يصبح مشتركًا وتصله التذكيرات (/stop لإيقافها)
- المواعيد (للمالك): /add (محادثة خطوة بخطوة) /appointments /delete /announce
- البريد (للمالك): /add_email /remove_email /emails /test_email
- تسجيل/إلغاء تسجيل المجموعات تلقائيًا عبر my_chat_member
- البث اليدوي: أي رسالة (نص/صورة/فيديو/مستند) من المالك في الخاص تُنسخ لكل المستقبِلين
"""
import logging
from datetime import datetime
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler

import appointments
import broadcast
import config
import database as db
import mailer
from scheduler import check_reminders, skip_past_reminders

logger = logging.getLogger(__name__)

# حالات العضوية التي تعني أن البوت "داخل" المجموعة فعليًا
MEMBER_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
# حالات العضوية التي تعني أن البوت خرج من المجموعة (طرد أو مغادرة) أو حُظر في الخاص
LEFT_STATUSES = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}

# كم موعدًا يُعرض في القوائم كحد أقصى (لتجنّب تجاوز طول رسالة تيليغرام)
MAX_LISTED = 25


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == config.OWNER_ID


def owner_only(handler):
    """Decorator يحصر تنفيذ الأمر بصاحب الحساب المصرح له (OWNER_ID) فقط."""

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update):
            user = update.effective_user
            if update.callback_query:
                await update.callback_query.answer("⛔ مخصص لمالك البوت فقط.")
            elif update.effective_message:
                await update.effective_message.reply_text("⛔ هذا الأمر مخصص لمالك البوت فقط.")
            logger.warning("محاولة استخدام أمر محمي من مستخدم غير مصرح: %s", user.id if user else "unknown")
            return ConversationHandler.END
        return await handler(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# تسجيل المجموعات تلقائيًا + اكتشاف حظر البوت في الخاص
# ---------------------------------------------------------------------------
async def on_bot_membership_changed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    يُستدعى عند أي تغيّر في عضوية البوت:
    - في مجموعة: الإضافة تسجّلها كـ 'active'، والخروج يعطّلها دون حذفها من السجل.
    - في الخاص: إن حظر المستخدم البوت يتوقف إرسال التذكيرات له.
    """
    chat_member_update = update.my_chat_member
    if chat_member_update is None:
        return

    chat = chat_member_update.chat
    new_status = chat_member_update.new_chat_member.status

    if chat.type == ChatType.PRIVATE:
        if new_status in LEFT_STATUSES:
            db.set_user_status(chat.id, db.STATUS_INACTIVE)
            logger.info("المستخدم %s حظر البوت، تم إيقاف التذكيرات له.", chat.id)
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return  # نتجاهل القنوات

    if new_status in MEMBER_STATUSES:
        db.upsert_group(chat.id, chat.title or f"مجموعة {chat.id}")
        logger.info("تم تسجيل/تحديث المجموعة: %s (%s)", chat.title, chat.id)
        try:
            await context.bot.send_message(
                chat_id=config.OWNER_ID,
                text=f"✅ تم تسجيل مجموعة جديدة تلقائيًا:\n{chat.title}\nID: {chat.id}",
            )
        except TelegramError:
            pass  # لا نوقف التسجيل إن تعذّر إشعار المالك

    elif new_status in LEFT_STATUSES:
        if db.set_group_status(chat.id, db.STATUS_INACTIVE):
            logger.info("تم تعطيل المجموعة تلقائيًا بعد خروج البوت منها: %s (%s)", chat.title, chat.id)


# ---------------------------------------------------------------------------
# أوامر عامة (لكل المستخدمين)
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    # المالك أيضًا يُسجَّل كمشترك حتى تصله التذكيرات مثل الجميع (/stop لإيقافها)
    user = update.effective_user
    db.upsert_user(user.id, user.first_name or str(user.id), user.username)

    if is_owner(update):
        await update.message.reply_text(
            "أهلاً! أنا بوت التذكير بالمواعيد.\n"
            "أضف موعدًا بـ /add وسأذكّر الجميع به تلقائيًا.\n\n"
            "✅ أنت مسجّل أيضًا كمشترك: ستصلك التذكيرات مثل الجميع (/stop لإيقافها).\n"
            "أرسل /help لرؤية كل الأوامر."
        )
        return

    await update.message.reply_text(
        f"مرحبًا {user.first_name or ''} 👋\n\n"
        "✅ تم تفعيل التذكيرات لك. ستصلك رسالة قبل كل موعد "
        "(في الصباح وقبل البداية بقليل).\n\n"
        "📅 /upcoming — المواعيد القادمة\n"
        "🔕 /stop — إيقاف التذكيرات"
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    db.set_user_status(update.effective_user.id, db.STATUS_INACTIVE)
    await update.message.reply_text("🔕 تم إيقاف التذكيرات. أرسل /start في أي وقت لإعادة تفعيلها.")


async def upcoming_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    appts = db.get_scheduled_appointments()
    if not appts:
        await update.message.reply_text("لا توجد مواعيد قادمة حاليًا.")
        return

    blocks = []
    for appt in appts[:MAX_LISTED]:
        block = f"📌 {appt.title}\n📅 {appointments.format_date(appt.starts_at)} — ⏰ {appt.starts_at:%H:%M}"
        if appt.details:
            block += f"\n{appt.details}"
        blocks.append(block)
    await update.message.reply_text("📅 المواعيد القادمة:\n\n" + "\n\n".join(blocks))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text(
            "📅 /upcoming — المواعيد القادمة\n"
            "🔔 /start — تفعيل التذكيرات\n"
            "🔕 /stop — إيقاف التذكيرات"
        )
        return

    await update.message.reply_text(
        "📋 الأوامر المتاحة (للمالك):\n\n"
        "🗓 المواعيد:\n"
        "/add — إضافة موعد جديد (خطوة بخطوة)\n"
        "/appointments — المواعيد القادمة مع أرقامها وتذكيراتها\n"
        "/announce <رقم> — إعلان موعد للجميع الآن\n"
        "/delete <رقم> — حذف موعد\n"
        "/cancel — إلغاء إضافة موعد جارية\n\n"
        "👥 المستقبِلون:\n"
        "/subscribers — عدد المشتركين\n"
        "/list_groups — المجموعات المسجّلة وحالتها\n"
        "/activate <chat_id> — تفعيل مجموعة\n"
        "/deactivate <chat_id> — إيقاف مجموعة\n\n"
        "📧 البريد الإلكتروني (التذكيرات والإعلانات تُرسل بالبريد أيضًا):\n"
        "/add_email <عنوان أو عدة عناوين> — إضافة عناوين\n"
        "/remove_email <عنوان> — حذف عنوان\n"
        "/emails — قائمة العناوين\n"
        "/test_email [عنوان] — إرسال بريد تجربة للتأكد من إعدادات SMTP\n\n"
        "📤 للبث اليدوي: أرسل لي نصًا أو صورة أو فيديو أو مستندًا هنا في الخاص، "
        "وسأرسله لكل المشتركين والمجموعات النشطة (على تيليغرام فقط)."
    )


# ---------------------------------------------------------------------------
# إضافة موعد (محادثة خطوة بخطوة)
# ---------------------------------------------------------------------------
ASK_DATETIME, ASK_TITLE, ASK_DETAILS, ASK_REPEAT = range(4)

# الإجابات النصية المقبولة لسؤال التكرار (إلى جانب الأزرار)
REPEAT_ONCE_ANSWERS = {"1", "١", "مرة", "مرة واحدة", "لا"}
REPEAT_WEEKLY_ANSWERS = {"2", "٢", "كل أسبوع", "كل اسبوع", "أسبوعي", "اسبوعي", "نعم"}

DATETIME_HELP = (
    "اكتب التاريخ ثم الوقت (بتوقيت الجزائر)، مثلاً:\n"
    "• 15/10 20:00\n"
    "• غدا 18:30\n"
    "• الاثنين 20:00\n"
    "• اليوم 21:00"
)


@owner_only
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_appt"] = {}
    await update.message.reply_text(
        f"🗓 متى الموعد؟\n\n{DATETIME_HELP}\n\n(للإلغاء: /cancel)",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_DATETIME


async def add_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    now = datetime.now(config.TIMEZONE)
    starts_at = appointments.parse_datetime(update.message.text, now)
    if starts_at is None:
        await update.message.reply_text(f"❓ لم أفهم التاريخ.\n\n{DATETIME_HELP}")
        return ASK_DATETIME
    if starts_at <= now:
        await update.message.reply_text("⏪ هذا الوقت قد مضى. أرسل تاريخًا ووقتًا في المستقبل.")
        return ASK_DATETIME

    context.user_data["new_appt"]["starts_at"] = starts_at
    days_ahead = (starts_at.date() - now.date()).days
    warning = (
        f"\n\n⚠️ انتبه: هذا الموعد بعد {days_ahead} يومًا. إن كان التاريخ خطأ أرسل /add من جديد "
        "(اكتب اليوم ثم الشهر، أو استعمل: اليوم / غدا / اسم اليوم)."
        if days_ahead > 30
        else ""
    )
    await update.message.reply_text(
        f"✔️ {appointments.format_date(starts_at)} الساعة {starts_at:%H:%M}{warning}\n\n"
        "📌 ما عنوان الموعد؟ (مثلاً: جلسة اللايف الأسبوعية)"
    )
    return ASK_TITLE


async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_appt"]["title"] = update.message.text.strip()
    await update.message.reply_text(
        "📝 أرسل التفاصيل التي ستظهر في التذكير (رابط Google Meet، ملاحظات...)\n"
        "أو أرسل /skip إن لم تكن هناك تفاصيل."
    )
    return ASK_DETAILS


async def add_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_appt"]["details"] = update.message.text.strip()
    return await _ask_repeat(update)


async def add_skip_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_appt"]["details"] = ""
    return await _ask_repeat(update)


async def _ask_repeat(update: Update) -> int:
    await update.message.reply_text(
        "🔁 هل يتكرر هذا الموعد كل أسبوع في نفس اليوم والوقت؟\n"
        "(اضغط زرًا، أو اكتب 1 لمرة واحدة / 2 لكل أسبوع)",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("1️⃣ مرة واحدة", callback_data="repeat:none"),
                InlineKeyboardButton("2️⃣ كل أسبوع 🔁", callback_data="repeat:weekly"),
            ]]
        ),
    )
    return ASK_REPEAT


async def add_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    if choice in REPEAT_ONCE_ANSWERS:
        repeat = db.REPEAT_NONE
    elif choice in REPEAT_WEEKLY_ANSWERS:
        repeat = db.REPEAT_WEEKLY
    else:
        await update.message.reply_text("اكتب 1 لمرة واحدة، أو 2 إن كان يتكرر كل أسبوع.")
        return ASK_REPEAT
    return await _save_new_appointment(update.message, context, repeat)


async def add_repeat_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    repeat = query.data.removeprefix("repeat:")
    label = "كل أسبوع 🔁" if repeat == db.REPEAT_WEEKLY else "مرة واحدة"
    await query.edit_message_text(f"🔁 التكرار: {label}")
    return await _save_new_appointment(query.message, context, repeat)


async def _save_new_appointment(message, context: ContextTypes.DEFAULT_TYPE, repeat: str) -> int:
    data = context.user_data.pop("new_appt")
    now = datetime.now(config.TIMEZONE)
    if data["starts_at"] <= now:
        await message.reply_text("⏪ وقت الموعد مضى أثناء الإضافة. أعد المحاولة بـ /add")
        return ConversationHandler.END

    appt = db.add_appointment(data["title"], data["details"], data["starts_at"], repeat)
    skip_past_reminders(appt, now)
    logger.info("موعد جديد #%d: %s في %s", appt.id, appt.title, appt.starts_at)
    # فحص فوري بدل انتظار الدورة التالية، حتى يصل تذكير الموعد القريب خلال ثوانٍ
    context.job_queue.run_once(check_reminders, when=2)

    not_subscribed = (
        "" if db.is_user_active(config.OWNER_ID)
        else "\n\nℹ️ أنت غير مشترك، فلن تصلك التذكيرات بنفسك (فقط الملخص). أرسل /start لتصلك."
    )
    await message.reply_text(
        f"✅ تم حفظ الموعد رقم {appt.id}.\n\n"
        + _appointment_summary(appt, now)
        + not_subscribed
        + "\n\nهل تريد إعلانه للجميع الآن؟",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("📢 أعلنه الآن", callback_data=f"announce:{appt.id}"),
                InlineKeyboardButton("🗑 حذف", callback_data=f"delete:{appt.id}"),
            ]]
        ),
    )
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_appt", None)
    await update.message.reply_text("❌ تم إلغاء إضافة الموعد.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def add_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("new_appt", None)
    try:
        await context.bot.send_message(
            chat_id=config.OWNER_ID,
            text="⌛ انتهت مهلة إضافة الموعد ولم يُحفظ. ابدأ من جديد بـ /add",
            reply_markup=ReplyKeyboardRemove(),
        )
    except TelegramError:
        pass


def _appointment_summary(appt: db.Appointment, now: datetime) -> str:
    """وصف الموعد للمالك: البيانات + أوقات التذكيرات القادمة."""
    sent = db.get_sent_kinds(appt.id)
    upcoming = [r for r in appointments.build_reminders(appt) if r.kind not in sent]

    lines = [
        f"#{appt.id} — 📌 {appt.title}",
        f"📅 {appointments.format_date(appt.starts_at)} — ⏰ {appt.starts_at:%H:%M} — {appointments.repeat_label(appt)}",
    ]
    if appt.details:
        lines.append(f"📝 {appt.details}")
    if upcoming:
        lines.append("🔔 التذكيرات:")
        for r in upcoming:
            if r.send_at <= now:  # موعد قريب: التذكير يُرسل فورًا
                lines.append("   • الآن (الموعد قريب)")
            else:
                lines.append(
                    f"   • {appointments.kind_label(r.kind)}: {appointments.format_date(r.send_at)} {r.send_at:%H:%M}"
                )
    else:
        lines.append("🔔 لا توجد تذكيرات متبقية لهذا الموعد.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# إدارة المواعيد
# ---------------------------------------------------------------------------
@owner_only
async def appointments_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    appts = db.get_scheduled_appointments()
    if not appts:
        await update.message.reply_text("لا توجد مواعيد قادمة. أضف موعدًا بـ /add")
        return

    now = datetime.now(config.TIMEZONE)
    blocks = [_appointment_summary(a, now) for a in appts[:MAX_LISTED]]
    await update.message.reply_text(
        "🗓 المواعيد القادمة:\n\n" + "\n\n".join(blocks)
        + "\n\nللحذف: /delete <رقم> — للإعلان الآن: /announce <رقم>"
    )


async def _appointment_from_args(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.message.reply_text("حدّد رقم الموعد، مثلاً: /delete 3 (الأرقام في /appointments)")
        return None
    appt = db.get_appointment(int(context.args[0].lstrip("#")))
    if appt is None or appt.status != db.APPT_SCHEDULED:
        await update.message.reply_text("لا يوجد موعد قادم بهذا الرقم. راجع /appointments")
        return None
    return appt


@owner_only
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    appt = await _appointment_from_args(update, context)
    if appt:
        db.delete_appointment(appt.id)
        await update.message.reply_text(f"🗑 تم حذف الموعد #{appt.id}: {appt.title}")


@owner_only
async def announce_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    appt = await _appointment_from_args(update, context)
    if appt:
        report = await broadcast.notify_all(context.bot, *appointments.announce_content(appt))
        await update.message.reply_text(f"📢 تم إعلان «{appt.title}»:\n{report.summary()}")


@owner_only
async def appointment_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أزرار رسالة تأكيد الموعد: إعلان الآن / حذف."""
    query = update.callback_query
    action, _, raw_id = query.data.partition(":")
    appt = db.get_appointment(int(raw_id))
    if appt is None or appt.status != db.APPT_SCHEDULED:
        await query.answer("هذا الموعد لم يعد موجودًا.")
        await query.edit_message_reply_markup(None)
        return

    if action == "announce":
        await query.answer("جارٍ الإعلان...")
        await query.edit_message_reply_markup(None)
        report = await broadcast.notify_all(context.bot, *appointments.announce_content(appt))
        await query.message.reply_text(f"📢 تم إعلان «{appt.title}»:\n{report.summary()}")
    elif action == "delete":
        db.delete_appointment(appt.id)
        await query.answer("تم الحذف")
        await query.edit_message_text(f"🗑 تم حذف الموعد #{appt.id}: {appt.title}")


# ---------------------------------------------------------------------------
# المشتركون والمجموعات
# ---------------------------------------------------------------------------
@owner_only
async def subscribers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active, inactive = db.count_users()
    groups = sum(1 for g in db.get_all_groups() if g[3] == db.STATUS_ACTIVE)
    await update.message.reply_text(
        f"👥 المشتركون النشطون: {active}\n"
        f"🔕 أوقفوا التذكيرات / حظروا البوت: {inactive}\n"
        f"💬 المجموعات النشطة: {groups}\n"
        f"📧 عناوين البريد: {len(db.get_emails())}"
        + ("" if config.EMAIL_ENABLED else " (البريد غير مُفعّل — أضف إعدادات SMTP في .env)")
    )


# ---------------------------------------------------------------------------
# عناوين البريد الإلكتروني
# ---------------------------------------------------------------------------
def _split_email_args(context: ContextTypes.DEFAULT_TYPE) -> tuple[list[str], list[str]]:
    """
    يقسم ما بعد الأمر إلى (عناوين صالحة، نصوص غير صالحة). يقبل عنوانًا واحدًا
    أو قائمة كاملة ملصوقة (مفصولة بمسافات أو أسطر أو فواصل).
    """
    valid, invalid = [], []
    for raw in " ".join(context.args).replace(",", " ").replace(";", " ").split():
        email = mailer.normalize_email(raw)
        if email:
            if email not in valid:
                valid.append(email)
        else:
            invalid.append(raw)
    return valid, invalid


@owner_only
async def add_email_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    valid, invalid = _split_email_args(context)
    if not valid and not invalid:
        await update.message.reply_text(
            "أرسل العنوان بعد الأمر، مثلاً:\n"
            "/add_email ali@gmail.com\n\n"
            "ويمكنك إضافة عدة عناوين دفعة واحدة (كل عنوان في سطر أو مفصولة بمسافات):\n"
            "/add_email ali@gmail.com sara@yahoo.com\nkarim@outlook.com"
        )
        return

    added = db.add_emails(valid)
    lines = [f"✅ أُضيف {len(added)} عنوان."]
    if len(added) < len(valid):
        lines.append(f"ℹ️ {len(valid) - len(added)} عنوان مسجّل مسبقًا.")
    if invalid:
        lines.append("⚠️ عناوين غير صالحة (لم تُضف):\n" + "\n".join(f"   • {x}" for x in invalid[:20]))
    lines.append(f"📧 المجموع الآن: {len(db.get_emails())}")
    if not config.EMAIL_ENABLED:
        lines.append("\n⚠️ البريد غير مُفعّل بعد: أضف إعدادات SMTP في ملف .env ثم أعد تشغيل البوت.")
    await update.message.reply_text("\n".join(lines))


@owner_only
async def remove_email_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    valid, invalid = _split_email_args(context)
    if not valid:
        await update.message.reply_text("أرسل العنوان بعد الأمر، مثلاً: /remove_email ali@gmail.com")
        return

    removed = [e for e in valid if db.remove_email(e)]
    not_found = [e for e in valid if e not in removed]
    lines = [f"🗑 حُذف {len(removed)} عنوان."]
    if not_found:
        lines.append("غير موجودة في القائمة:\n" + "\n".join(f"   • {x}" for x in not_found))
    await update.message.reply_text("\n".join(lines))


@owner_only
async def emails_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    emails = db.get_emails()
    status = (
        f"✅ البريد مُفعّل (يُرسل من {config.EMAIL_FROM})"
        if config.EMAIL_ENABLED
        else "⚠️ البريد غير مُفعّل: أضف إعدادات SMTP في .env"
    )
    if not emails:
        await update.message.reply_text(f"{status}\n\nلا توجد عناوين بعد. أضف بـ /add_email")
        return

    text = f"{status}\n\n📧 العناوين ({len(emails)}):\n" + "\n".join(emails)
    if len(text) <= 4000:
        await update.message.reply_text(text)
    else:
        # قائمة طويلة: تُرسل كملف نصي بدل رسالة
        await update.message.reply_document(
            document="\n".join(emails).encode("utf-8"),
            filename="emails.txt",
            caption=f"{status}\n📧 {len(emails)} عنوان",
        )


@owner_only
async def test_email_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.EMAIL_ENABLED:
        await update.message.reply_text(
            "⚠️ البريد غير مُفعّل. أضف في ملف .env:\n"
            "SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD\n"
            "ثم أعد تشغيل البوت."
        )
        return

    valid, _ = _split_email_args(context)
    to = valid[0] if valid else config.EMAIL_FROM
    await update.message.reply_text(f"⏳ جارٍ إرسال بريد تجربة إلى {to} ...")
    error = await mailer.send_test(to)
    if error:
        await update.message.reply_text(f"❌ فشل الإرسال:\n{error}")
    else:
        await update.message.reply_text(f"✅ تم الإرسال إلى {to}. تحقق من البريد الوارد (وأيضًا مجلد Spam).")


@owner_only
async def list_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    groups = db.get_all_groups()
    if not groups:
        await update.message.reply_text("لا توجد أي مجموعات مسجّلة بعد.")
        return

    lines = []
    for chat_id, title, added_date, status in groups:
        icon = "🟢" if status == db.STATUS_ACTIVE else "🔴"
        lines.append(f"{icon} {title}\n   ID: {chat_id} | أُضيفت: {added_date[:10]} | الحالة: {status}")

    await update.message.reply_text("📋 المجموعات المسجّلة:\n\n" + "\n\n".join(lines))


@owner_only
async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_status_from_args(update, context, db.STATUS_ACTIVE, "✅ تم تفعيل المجموعة")


@owner_only
async def deactivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_status_from_args(update, context, db.STATUS_INACTIVE, "⏸️ تم إيقاف المجموعة")


async def _set_status_from_args(update, context, status: str, success_text: str) -> None:
    if not context.args:
        await update.message.reply_text(
            "يجب تحديد معرّف المجموعة (Chat ID). استخدم /list_groups لمعرفة المعرّفات."
        )
        return

    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("معرّف المجموعة يجب أن يكون رقمًا.")
        return

    if db.group_exists(chat_id) is None:
        await update.message.reply_text("لا توجد مجموعة مسجّلة بهذا المعرّف.")
        return

    db.set_group_status(chat_id, status)
    await update.message.reply_text(f"{success_text} ({chat_id})")


# ---------------------------------------------------------------------------
# البث اليدوي
# ---------------------------------------------------------------------------
@owner_only
async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    يُستدعى عند إرسال المالك أي محتوى (نص/صورة/فيديو/مستند) في الخاص مع البوت.
    ينسخ الرسالة (copy_message) لكل مشترك ومجموعة نشطة - بدون forward يكشف المصدر.
    """
    if not db.get_recipients():
        await update.message.reply_text("لا يوجد أي مشترك أو مجموعة نشطة حاليًا لإرسال البث إليها.")
        return

    success, failed = await broadcast.copy_to_all(
        context.bot, update.effective_chat.id, update.message.message_id
    )
    await update.message.reply_text(f"📤 تم البث: نجح لـ {success}، فشل لـ {failed}.")
