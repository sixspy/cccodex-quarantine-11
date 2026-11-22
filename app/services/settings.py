from __future__ import annotations

from app.db import get_db
from app.services.errors import ServiceError

# 试跑口径只读：只能由试跑流程（发起/敲定/废弃）改写，设置页不得直改。
READONLY_KEYS = {"trial_mode", "trial_scope", "trial_rule_code"}


def list_settings(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value, label FROM lab_settings ORDER BY key"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["readonly"] = item["key"] in READONLY_KEYS
            out.append(item)
        return out


def update_setting(key: str, value: str, db_path: str | None = None) -> dict:
    if not key or value is None:
        raise ServiceError("参数不完整")
    if key in READONLY_KEYS:
        raise ServiceError("该项为试跑只读口径，请到入舱规则页操作试跑")
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT key, value, label FROM lab_settings WHERE key=?", (key,)
        ).fetchone()
        if not row:
            raise ServiceError("设置项不存在", 404)
        conn.execute("UPDATE lab_settings SET value=? WHERE key=?", (str(value), key))
        out = conn.execute(
            "SELECT key, value, label FROM lab_settings WHERE key=?", (key,)
        ).fetchone()
        return dict(out)


def get_value(key: str, default: str = "", db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM lab_settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default
