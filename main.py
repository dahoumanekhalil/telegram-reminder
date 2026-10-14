"""
نقطة تشغيل البوت: يبني التطبيق، يسجّل كل المعالجات، يفعّل فحص التذكيرات، ويبدأ الاستماع.
شغّل هذا الملف بأمر: python main.py
"""
import logging
import warnings

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, Update
from telegram.error import TelegramError
from telegram.warnings import PTBUserWarning
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import database as db
import handlers
from scheduler import schedule_reminder_checks

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
)
logger = logging.getLogger(__name__)

# تحذير غير مهم هنا: المحادثة تُتابَع لكل مستخدم (المالك فقط) وليس لكل رسالة، وهذا ما نريده
warnings.filterwarnings("ignore", message=r".*CallbackQueryHandler.*", category=PTBUserWarning)

USER_COMMANDS = [
    BotCommand("upcoming", "المواعيد القادمة"),
    BotCommand("start", "تفعيل التذكيرات"),
    BotCommand("stop", "إيقاف التذكيرات"),
]

OWNER_COMMANDS = [
    BotCommand("add", "إضافة موعد جديد"),
    BotCommand("appointments", "المواعيد القادمة وتذكيراتها"),
    BotCommand("announce", "إعلان موعد الآن: /announce رقم"),
    BotCommand("delete", "حذف موعد: /delete رقم"),
    BotCommand("subscribers", "عدد المشتركين"),
    BotCommand("add_email", "إضافة عنوان بريد (أو عدة عناوين)"),
    BotCommand("emails", "قائمة عناوين البريد"),
    BotCommand("remove_email", "حذف عنوان بريد"),
    BotCommand("test_email", "إرسال بريد تجربة"),
    BotCommand("list_groups", "المجموعات المسجّلة"),
    BotCommand("help", "كل الأوامر"),
]


async def on_startup(application: Application) -> None:
    """يُنفَّذ مرة عند إقلاع البوت: تهيئة قاعدة البيانات، الجدولة، وقائمة الأوامر."""
    db.init_db()
    schedule_reminder_checks(application.job_queue)

    await application.bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    try:
        await application.bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(config.OWNER_ID))
    except TelegramError as e:
        # يحدث إن لم يبدأ المالك محادثة مع البوت بعد - أرسل /start ثم أعد التشغيل
        logger.warning("تعذّر ضبط قائمة أوامر المالك: %s", e)

    if config.EMAIL_ENABLED:
        logger.info("البريد مُفعّل: %s عبر %s:%d", config.EMAIL_FROM, config.SMTP_HOST, config.SMTP_PORT)
    else:
        logger.info("البريد غير مُفعّل (لا توجد إعدادات SMTP في .env).")
    logger.info("البوت جاهز ويعمل.")


def build_application() -> Application:
    application = Application.builder().token(config.BOT_TOKEN).post_init(on_startup).build()

    # تسجيل/تعطيل المجموعات تلقائيًا + اكتشاف من حظر البوت في الخاص
    application.add_handler(ChatMemberHandler(handlers.on_bot_membership_changed, ChatMemberHandler.MY_CHAT_MEMBER))

    # إضافة موعد خطوة بخطوة - يجب أن تُسجَّل قبل معالج البث حتى لا تُبث إجابات المالك
    text_answer = filters.TEXT & ~filters.COMMAND
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("add", handlers.add_start, filters=filters.ChatType.PRIVATE)],
            states={
                handlers.ASK_DATETIME: [MessageHandler(text_answer, handlers.add_datetime)],
                handlers.ASK_TITLE: [MessageHandler(text_answer, handlers.add_title)],
                handlers.ASK_DETAILS: [
                    CommandHandler("skip", handlers.add_skip_details),
                    MessageHandler(text_answer, handlers.add_details),
                ],
                handlers.ASK_REPEAT: [
                    CallbackQueryHandler(handlers.add_repeat_button, pattern=r"^repeat:(none|weekly)$"),
                    MessageHandler(text_answer, handlers.add_repeat),
                ],
                ConversationHandler.TIMEOUT: [TypeHandler(Update, handlers.add_timeout)],
            },
            fallbacks=[CommandHandler("cancel", handlers.add_cancel)],
            allow_reentry=True,
            conversation_timeout=15 * 60,
        )
    )

    # أوامر عامة
    application.add_handler(CommandHandler("start", handlers.start_command))
    application.add_handler(CommandHandler("stop", handlers.stop_command))
    application.add_handler(CommandHandler("upcoming", handlers.upcoming_command))
    application.add_handler(CommandHandler("help", handlers.help_command))

    # إدارة المواعيد (owner-only داخليًا عبر decorator)
    application.add_handler(CommandHandler("appointments", handlers.appointments_command))
    application.add_handler(CommandHandler("delete", handlers.delete_command))
    application.add_handler(CommandHandler("announce", handlers.announce_command))
    application.add_handler(CallbackQueryHandler(handlers.appointment_button, pattern=r"^(announce|delete):\d+$"))

    # إدارة المشتركين والمجموعات
    application.add_handler(CommandHandler("subscribers", handlers.subscribers_command))
    application.add_handler(CommandHandler("list_groups", handlers.list_groups_command))
    application.add_handler(CommandHandler("activate", handlers.activate_command))
    application.add_handler(CommandHandler("deactivate", handlers.deactivate_command))

    # إدارة عناوين البريد
    application.add_handler(CommandHandler("add_email", handlers.add_email_command))
    application.add_handler(CommandHandler("remove_email", handlers.remove_email_command))
    application.add_handler(CommandHandler("emails", handlers.emails_command))
    application.add_handler(CommandHandler("test_email", handlers.test_email_command))

    # البث اليدوي: أي رسالة (نص/صورة/فيديو/مستند) من المالك في الخاص، بشرط ألا تكون أمرًا
    broadcast_filter = (
        filters.ChatType.PRIVATE
        & filters.User(config.OWNER_ID)
        & (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
        & ~filters.COMMAND
    )
    application.add_handler(MessageHandler(broadcast_filter, handlers.broadcast_message))

    return application


def main() -> None:
    application = build_application()
    application.run_polling(allowed_updates=["message", "my_chat_member", "callback_query"])


if __name__ == "__main__":
    main()
