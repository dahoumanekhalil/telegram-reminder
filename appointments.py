"""
منطق المواعيد (بدون أي اتصال بتيليغرام):
- فهم التاريخ والوقت الذي يكتبه المالك (15/10 20:00، غدا 20:00، الاثنين 18:30 ...)
- حساب أوقات التذكير لكل موعد حسب الإعدادات في config.py
- تنسيق نصوص الرسائل
"""
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

import config
from database import REPEAT_WEEKLY, Appointment

WEEKDAY_NAMES = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
MONTH_NAMES = [
    "جانفي", "فيفري", "مارس", "أفريل", "ماي", "جوان",
    "جويلية", "أوت", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
]

KIND_DAY_BEFORE = "day_before"
KIND_MORNING = "morning"
KIND_BEFORE_PREFIX = "before_"

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_HARAKAT = re.compile(r"[ً-ْـ]")  # التشكيل والتطويل

# الكلمات بعد التطبيع (بدون همزات ولا تشكيل)
_RELATIVE_DAYS = {"اليوم": 0, "today": 0, "غدا": 1, "tomorrow": 1, "بعدغد": 2}
_WEEKDAYS = {
    "الاثنين": 0, "اثنين": 0, "الثلاثاء": 1, "ثلاثاء": 1, "الاربعاء": 2, "اربعاء": 2,
    "الخميس": 3, "خميس": 3, "الجمعة": 4, "جمعة": 4, "السبت": 5, "سبت": 5, "الاحد": 6, "احد": 6,
}

_TIME_RE = re.compile(r"^(\d{1,2})(?:[:hH](\d{2}))?[hH]?$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY_DATE_RE = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?$")


# ---------------------------------------------------------------------------
# فهم التاريخ والوقت
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    text = _HARAKAT.sub("", text.translate(_DIGITS)).strip().lower()
    text = re.sub("[أإآ]", "ا", text)
    text = text.replace("بعد غد", "بعدغد")
    return re.sub(r"\s+", " ", text)


def _parse_time(token: str) -> Optional[time]:
    m = _TIME_RE.match(token)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _month_first_if_soon(first: int, second: int, today: date) -> Optional[date]:
    """
    هواتف كثيرة تعرض التاريخ بصيغة شهر/يوم (مثل 10/8 = 8 أكتوبر). إن كانت قراءة
    يوم/شهر تقع في الماضي بينما قراءة شهر/يوم تقع خلال الأسبوع القادم، فالمقصود
    على الأرجح هو الثانية - بدل جدولة الموعد بعد سنة كاملة.
    """
    try:
        swapped = date(today.year, first, second)
    except ValueError:
        return None
    return swapped if today <= swapped <= today + timedelta(days=7) else None


def parse_datetime(text: str, now: datetime) -> Optional[datetime]:
    """
    يحوّل نص المالك إلى تاريخ/وقت بتوقيت الجزائر، أو None إن لم يُفهم.
    الصيغ المقبولة (التاريخ ثم الوقت):
      15/10 20:00 | 15/10/2026 20:00 | 2026-10-15 20:00
      اليوم 20:00 | غدا 20:00 | بعد غد 20:00 | الاثنين 20:00
      20:00 (وحده = اليوم)
    """
    tokens = _normalize(text).split(" ")
    if len(tokens) == 1:
        tokens = ["اليوم", tokens[0]]
    if len(tokens) != 2:
        return None

    date_token, time_token = tokens
    at = _parse_time(time_token)
    if at is None:
        return None

    today = now.astimezone(config.TIMEZONE).date()
    try:
        if date_token in _RELATIVE_DAYS:
            day = today + timedelta(days=_RELATIVE_DAYS[date_token])
        elif date_token in _WEEKDAYS:
            # أقرب يوم بهذا الاسم لم يمضِ موعده بعد
            ahead = (_WEEKDAYS[date_token] - today.weekday()) % 7
            day = today + timedelta(days=ahead)
            if datetime.combine(day, at, tzinfo=config.TIMEZONE) <= now:
                day += timedelta(days=7)
        elif m := _ISO_DATE_RE.match(date_token):
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif m := _DMY_DATE_RE.match(date_token):
            d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
            if y is None:
                # بدون سنة: هذه السنة، أو القادمة إن كان اليوم قد مضى
                day = date(today.year, mo, d)
                if day < today:
                    day = _month_first_if_soon(d, mo, today) or date(today.year + 1, mo, d)
            else:
                year = int(y) + 2000 if len(y) == 2 else int(y)
                day = date(year, mo, d)
        else:
            return None
    except ValueError:  # مثل 31/02
        return None

    return datetime.combine(day, at, tzinfo=config.TIMEZONE)


# ---------------------------------------------------------------------------
# حساب أوقات التذكير
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Reminder:
    kind: str
    send_at: datetime


def build_reminders(appt: Appointment) -> list[Reminder]:
    """
    كل تذكيرات الموعد مرتبة زمنيًا. يُحذف تذكير الصباح/المساء السابق إن جاء
    بعد (أو مع) أول تذكير "قبل الموعد" - مثلاً موعد الساعة 09:00 لا يحتاج
    تذكير صباح الساعة 08:00 وتذكير "بعد ساعة" في نفس اللحظة.
    """
    start = appt.starts_at
    reminders = [
        Reminder(f"{KIND_BEFORE_PREFIX}{m}", start - timedelta(minutes=m))
        for m in sorted(set(config.MINUTES_BEFORE_REMINDERS), reverse=True)
        if m > 0
    ]
    earliest = reminders[0].send_at if reminders else start

    if config.MORNING_REMINDER_TIME is not None:
        morning = datetime.combine(start.date(), config.MORNING_REMINDER_TIME, tzinfo=config.TIMEZONE)
        if morning < earliest:
            reminders.append(Reminder(KIND_MORNING, morning))
            earliest = min(earliest, morning)

    if config.DAY_BEFORE_REMINDER_TIME is not None:
        evening = datetime.combine(
            start.date() - timedelta(days=1), config.DAY_BEFORE_REMINDER_TIME, tzinfo=config.TIMEZONE
        )
        if evening < earliest:
            reminders.append(Reminder(KIND_DAY_BEFORE, evening))

    return sorted(reminders, key=lambda r: r.send_at)


def is_still_relevant(reminder: Reminder, now: datetime) -> bool:
    """
    إن كان البوت متوقفًا وفاته وقت تذكير: تذكير "غدًا" لا معنى له إن صار
    اليوم هو يوم الموعد. باقي التذكيرات تبقى مفيدة ما دام الموعد لم يبدأ.
    """
    if reminder.kind == KIND_DAY_BEFORE:
        return now.date() == reminder.send_at.date()
    return True


def next_weekly_occurrence(appt: Appointment, now: datetime) -> datetime:
    start = appt.starts_at
    while start <= now:
        start += timedelta(days=7)
    return start


# ---------------------------------------------------------------------------
# تنسيق النصوص
# ---------------------------------------------------------------------------
def date_text(dt: datetime) -> str:
    """التاريخ باسم الشهر (10 أكتوبر 2026) حتى لا يُخلط بين اليوم والشهر."""
    return f"{dt.day} {MONTH_NAMES[dt.month - 1]} {dt.year}"


def format_date(dt: datetime) -> str:
    return f"{WEEKDAY_NAMES[dt.weekday()]} {date_text(dt)}"


def minutes_to_text(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append({1: "ساعة", 2: "ساعتين"}.get(hours, f"{hours} ساعات" if hours <= 10 else f"{hours} ساعة"))
    if mins:
        parts.append({1: "دقيقة", 2: "دقيقتين"}.get(mins, f"{mins} دقائق" if mins <= 10 else f"{mins} دقيقة"))
    return " و".join(parts) or "لحظات"


def _fill(template: str, appt: Appointment, **extra) -> str:
    start = appt.starts_at
    return template.format(
        title=appt.title,
        weekday=WEEKDAY_NAMES[start.weekday()],
        date=date_text(start),
        time=f"{start:%H:%M}",
        details=f"\n\n{appt.details}" if appt.details else "",
        **extra,
    )


def announce_text(appt: Appointment) -> str:
    return _fill(config.ANNOUNCE_TEMPLATE, appt)


def reminder_text(appt: Appointment, kind: str, now: datetime) -> str:
    if kind == KIND_DAY_BEFORE:
        return _fill(config.DAY_BEFORE_TEMPLATE, appt)
    if kind == KIND_MORNING:
        return _fill(config.MORNING_TEMPLATE, appt)
    # الوقت المتبقي يُحسب لحظة الإرسال، حتى يبقى صحيحًا لو تأخر الإرسال
    remaining = math.ceil((appt.starts_at - now).total_seconds() / 60)
    return _fill(config.BEFORE_TEMPLATE, appt, remaining=minutes_to_text(max(remaining, 1)))


def kind_label(kind: str) -> str:
    if kind == KIND_DAY_BEFORE:
        return "مساء اليوم السابق"
    if kind == KIND_MORNING:
        return "صباح يوم الموعد"
    return f"قبل {minutes_to_text(int(kind.removeprefix(KIND_BEFORE_PREFIX)))}"


def repeat_label(appt: Appointment) -> str:
    return "🔁 كل أسبوع" if appt.repeat == REPEAT_WEEKLY else "مرة واحدة"
