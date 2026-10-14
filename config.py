"""
إعدادات البوت - كل ما قد تحتاج لتعديله موجود هنا فقط.
الأسرار (التوكن، معرّف المالك) تُقرأ من ملف .env ولا توضع مباشرة في الكود.
"""
import os
from datetime import time
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# أسرار (من .env) - لا تُعدَّل هنا، عدّلها في ملف .env
# ---------------------------------------------------------------------------
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير موجود. أنشئ ملف .env بناءً على .env.example")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID غير موجود. أنشئ ملف .env بناءً على .env.example")

# ---------------------------------------------------------------------------
# التوقيت
# ---------------------------------------------------------------------------
TIMEZONE = ZoneInfo("Africa/Algiers")

# ---------------------------------------------------------------------------
# مواعيد التذكير - عدّلها هنا فقط، تنطبق تلقائيًا على كل المواعيد
# ---------------------------------------------------------------------------
# تذكير مساء اليوم السابق للموعد (ضع None لإلغائه)
DAY_BEFORE_REMINDER_TIME: Optional[time] = time(20, 0)

# تذكير صباح يوم الموعد (ضع None لإلغائه)
MORNING_REMINDER_TIME: Optional[time] = time(8, 0)

# تذكيرات قبل بداية الموعد بعدد من الدقائق
# مثال: [60] = قبل ساعة فقط، [60, 10] = قبل ساعة وقبل 10 دقائق، [] = بدون
MINUTES_BEFORE_REMINDERS: list[int] = [60]

# كل كم ثانية يفحص البوت إن كان هناك تذكير حان وقته
CHECK_INTERVAL_SECONDS: int = 30

# ---------------------------------------------------------------------------
# نصوص الرسائل
# المتغيرات المتاحة: {title} {weekday} {date} {time} {details}
# وفي رسالة "قبل الموعد" أيضًا: {remaining} (مثل: ساعة، 10 دقائق)
# {details} تكون فارغة إن لم تكتب تفاصيل للموعد، أو تبدأ بسطر فارغ إن وُجدت.
# ---------------------------------------------------------------------------
ANNOUNCE_TEMPLATE: str = (
    "📢 موعد جديد\n\n"
    "📌 {title}\n"
    "📅 {weekday} {date}\n"
    "⏰ الساعة {time} (بتوقيت الجزائر)"
    "{details}"
)

DAY_BEFORE_TEMPLATE: str = (
    "🔔 تذكير: غدًا لدينا موعد\n\n"
    "📌 {title}\n"
    "📅 {weekday} {date}\n"
    "⏰ الساعة {time} (بتوقيت الجزائر)"
    "{details}"
)

MORNING_TEMPLATE: str = (
    "☀️ صباح الخير!\n"
    "تذكير بموعد اليوم:\n\n"
    "📌 {title}\n"
    "⏰ الساعة {time} (بتوقيت الجزائر)"
    "{details}"
)

BEFORE_TEMPLATE: str = (
    "⏳ يبدأ بعد {remaining}!\n\n"
    "📌 {title}\n"
    "⏰ الساعة {time} (بتوقيت الجزائر)"
    "{details}"
)

# ---------------------------------------------------------------------------
# البريد الإلكتروني (SMTP) - اختياري، القيم تُقرأ من ملف .env
# إن تُرك SMTP_HOST فارغًا يُعطَّل البريد ويعمل البوت على تيليغرام فقط.
# ---------------------------------------------------------------------------
SMTP_HOST: str = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT: int = int(os.getenv("SMTP_PORT") or "587")
SMTP_USER: str = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
# starttls (المنفذ 587) | ssl (المنفذ 465) | none
SMTP_SECURITY: str = (os.getenv("SMTP_SECURITY") or "starttls").strip().lower()
EMAIL_FROM: str = (os.getenv("EMAIL_FROM") or SMTP_USER).strip()
EMAIL_FROM_NAME: str = os.getenv("EMAIL_FROM_NAME") or "تذكير المواعيد"

EMAIL_ENABLED: bool = bool(SMTP_HOST and EMAIL_FROM)

# عناوين الرسائل - نفس المتغيرات المتاحة في نصوص الرسائل أعلاه
# (نص البريد نفسه هو نفس نص رسالة تيليغرام)
EMAIL_SUBJECT_ANNOUNCE: str = "📢 موعد جديد: {title} — {weekday} {date}"
EMAIL_SUBJECT_DAY_BEFORE: str = "🔔 تذكير: غدًا {title} الساعة {time}"
EMAIL_SUBJECT_MORNING: str = "☀️ اليوم: {title} الساعة {time}"
EMAIL_SUBJECT_BEFORE: str = "⏳ يبدأ بعد {remaining}: {title}"

# سطر يُضاف في آخر كل بريد (اتركه فارغًا "" لإلغائه)
EMAIL_FOOTER: str = "—\nوصلتك هذه الرسالة لأنك مسجّل في تذكيرات المواعيد. لإيقافها، رُدّ على هذا البريد."

# ---------------------------------------------------------------------------
# التخزين
# ---------------------------------------------------------------------------
# يمكن تغيير المسار عبر متغير البيئة DB_PATH (يُستخدم في Docker)
DB_PATH: str = os.getenv("DB_PATH") or os.path.join(os.path.dirname(__file__), "groups.db")

# ---------------------------------------------------------------------------
# السجلات (Logging)
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
