"""
طبقة تخزين بسيطة فوق SQLite:
- groups: المجموعات التي أُضيف إليها البوت
- users: الأشخاص الذين بدؤوا محادثة مع البوت (/start) - هم مستقبلو التذكيرات
- appointments: المواعيد التي يحددها المالك
- sent_reminders: التذكيرات التي أُرسلت فعلاً لكل موعد (حتى لا تتكرر بعد إعادة التشغيل)
كل دالة هنا مستقلة وتفتح/تغلق الاتصال بنفسها (كافٍ لحجم استخدام هذا البوت).
"""
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import config

STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"

APPT_SCHEDULED = "scheduled"
APPT_DONE = "done"

REPEAT_NONE = "none"
REPEAT_WEEKLY = "weekly"

RECIPIENT_USER = "user"
RECIPIENT_GROUP = "group"


@dataclass(frozen=True)
class Appointment:
    id: int
    title: str
    details: str
    starts_at: datetime  # مع المنطقة الزمنية (config.TIMEZONE)
    repeat: str
    status: str


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(config.DB_PATH)


def _to_db_time(dt: datetime) -> str:
    """تُخزَّن الأوقات بالتوقيت المحلي (الجزائر) بدقة الدقيقة."""
    return dt.astimezone(config.TIMEZONE).strftime("%Y-%m-%dT%H:%M")


def _from_db_time(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=config.TIMEZONE)


def _row_to_appointment(row) -> Appointment:
    appt_id, title, details, starts_at, repeat, status = row
    return Appointment(appt_id, title, details or "", _from_db_time(starts_at), repeat, status)


def init_db() -> None:
    """ينشئ الجداول إن لم تكن موجودة. تُستدعى مرة عند إقلاع البوت."""
    with closing(_connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                chat_id     INTEGER PRIMARY KEY,
                title       TEXT NOT NULL,
                added_date  TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                first_name  TEXT NOT NULL,
                username    TEXT,
                joined_date TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS appointments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                details     TEXT NOT NULL DEFAULT '',
                starts_at   TEXT NOT NULL,
                repeat      TEXT NOT NULL DEFAULT 'none',
                status      TEXT NOT NULL DEFAULT 'scheduled',
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sent_reminders (
                appointment_id INTEGER NOT NULL,
                kind           TEXT NOT NULL,
                PRIMARY KEY (appointment_id, kind)
            );
            """
        )
        conn.commit()


# ---------------------------------------------------------------------------
# المجموعات
# ---------------------------------------------------------------------------
def upsert_group(chat_id: int, title: str) -> None:
    """
    يسجّل مجموعة جديدة أو يحدّث اسمها إن كانت مسجّلة مسبقًا.
    عند إعادة إضافة البوت لمجموعة كانت 'inactive'، تُعاد لحالة 'active' تلقائيًا
    لأن إعادة الإضافة تعني نيّة استخدامها من جديد.
    """
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO groups (chat_id, title, added_date, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                status = ?
            """,
            (chat_id, title, datetime.now().isoformat(timespec="seconds"), STATUS_ACTIVE, STATUS_ACTIVE),
        )
        conn.commit()


def set_group_status(chat_id: int, status: str) -> bool:
    """يغيّر حالة مجموعة مسجّلة. يعيد False إن لم توجد المجموعة أصلاً."""
    with closing(_connect()) as conn:
        cur = conn.execute("UPDATE groups SET status = ? WHERE chat_id = ?", (status, chat_id))
        conn.commit()
        return cur.rowcount > 0


def migrate_group(old_chat_id: int, new_chat_id: int) -> bool:
    """
    عند تحويل مجموعة عادية إلى supergroup يتغيّر معرّفها. ينقل السجل للمعرّف الجديد،
    أو يعطّل القديم إن كان الجديد مسجّلًا أصلاً. يعيد True إن نُقل السجل فعلاً.
    """
    with closing(_connect()) as conn:
        if conn.execute("SELECT 1 FROM groups WHERE chat_id = ?", (new_chat_id,)).fetchone():
            conn.execute("UPDATE groups SET status = ? WHERE chat_id = ?", (STATUS_INACTIVE, old_chat_id))
            moved = False
        else:
            conn.execute("UPDATE groups SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id))
            moved = True
        conn.commit()
        return moved


def get_all_groups() -> list[tuple[int, str, str, str]]:
    """يعيد كل المجموعات المسجّلة تاريخيًا (chat_id, title, added_date, status)."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT chat_id, title, added_date, status FROM groups ORDER BY added_date DESC"
        )
        return cur.fetchall()


def group_exists(chat_id: int) -> Optional[str]:
    """يعيد حالة المجموعة إن كانت مسجّلة، أو None إن لم تكن."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT status FROM groups WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# المشتركون (المستخدمون في الخاص)
# ---------------------------------------------------------------------------
def upsert_user(user_id: int, first_name: str, username: Optional[str]) -> bool:
    """يسجّل/يعيد تفعيل مشترك. يعيد True إن كان جديدًا تمامًا."""
    with closing(_connect()) as conn:
        is_new = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is None
        conn.execute(
            """
            INSERT INTO users (user_id, first_name, username, joined_date, status)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username,
                status = excluded.status
            """,
            (user_id, first_name, username, datetime.now().isoformat(timespec="seconds"), STATUS_ACTIVE),
        )
        conn.commit()
        return is_new


def set_user_status(user_id: int, status: str) -> bool:
    with closing(_connect()) as conn:
        cur = conn.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
        conn.commit()
        return cur.rowcount > 0


def is_user_active(user_id: int) -> bool:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None and row[0] == STATUS_ACTIVE


def count_users() -> tuple[int, int]:
    """يعيد (عدد المشتركين النشطين، عدد من أوقفوا التذكيرات أو حظروا البوت)."""
    with closing(_connect()) as conn:
        rows = dict(conn.execute("SELECT status, COUNT(*) FROM users GROUP BY status").fetchall())
        return rows.get(STATUS_ACTIVE, 0), rows.get(STATUS_INACTIVE, 0)


# ---------------------------------------------------------------------------
# المستقبِلون (مشتركون + مجموعات نشطة)
# ---------------------------------------------------------------------------
def get_recipients() -> list[tuple[int, str, str]]:
    """
    يعيد (chat_id, name, kind) لكل من يجب أن تصله التذكيرات:
    المشتركون النشطون في الخاص (بمن فيهم المالك إن أرسل /start) + المجموعات النشطة.
    """
    with closing(_connect()) as conn:
        users = conn.execute(
            "SELECT user_id, first_name FROM users WHERE status = ?", (STATUS_ACTIVE,)
        ).fetchall()
        groups = conn.execute(
            "SELECT chat_id, title FROM groups WHERE status = ?", (STATUS_ACTIVE,)
        ).fetchall()
    return [(cid, name, RECIPIENT_USER) for cid, name in users] + [
        (cid, name, RECIPIENT_GROUP) for cid, name in groups
    ]


def deactivate_recipient(chat_id: int, kind: str) -> None:
    if kind == RECIPIENT_USER:
        set_user_status(chat_id, STATUS_INACTIVE)
    else:
        set_group_status(chat_id, STATUS_INACTIVE)


# ---------------------------------------------------------------------------
# المواعيد
# ---------------------------------------------------------------------------
def add_appointment(title: str, details: str, starts_at: datetime, repeat: str) -> Appointment:
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            INSERT INTO appointments (title, details, starts_at, repeat, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title, details, _to_db_time(starts_at), repeat, APPT_SCHEDULED,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        appt_id = cur.lastrowid
    return get_appointment(appt_id)


def get_appointment(appt_id: int) -> Optional[Appointment]:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id, title, details, starts_at, repeat, status FROM appointments WHERE id = ?",
            (appt_id,),
        ).fetchone()
    return _row_to_appointment(row) if row else None


def get_scheduled_appointments() -> list[Appointment]:
    """كل المواعيد القادمة (غير المنتهية)، مرتبة من الأقرب للأبعد."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT id, title, details, starts_at, repeat, status FROM appointments
            WHERE status = ? ORDER BY starts_at
            """,
            (APPT_SCHEDULED,),
        ).fetchall()
    return [_row_to_appointment(r) for r in rows]


def set_appointment_status(appt_id: int, status: str) -> None:
    with closing(_connect()) as conn:
        conn.execute("UPDATE appointments SET status = ? WHERE id = ?", (status, appt_id))
        conn.commit()


def reschedule_appointment(appt_id: int, starts_at: datetime) -> None:
    """ينقل الموعد لوقت جديد (للمواعيد الأسبوعية) ويصفّر سجل التذكيرات المرسلة."""
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE appointments SET starts_at = ? WHERE id = ?", (_to_db_time(starts_at), appt_id)
        )
        conn.execute("DELETE FROM sent_reminders WHERE appointment_id = ?", (appt_id,))
        conn.commit()


def delete_appointment(appt_id: int) -> bool:
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM appointments WHERE id = ?", (appt_id,))
        conn.execute("DELETE FROM sent_reminders WHERE appointment_id = ?", (appt_id,))
        conn.commit()
        return cur.rowcount > 0


def get_sent_kinds(appt_id: int) -> set[str]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT kind FROM sent_reminders WHERE appointment_id = ?", (appt_id,)
        ).fetchall()
    return {r[0] for r in rows}


def mark_reminders_sent(appt_id: int, kinds: list[str]) -> None:
    with closing(_connect()) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO sent_reminders (appointment_id, kind) VALUES (?, ?)",
            [(appt_id, k) for k in kinds],
        )
        conn.commit()
