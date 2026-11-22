"""试跑（dry-run）闸。

设计要点：
- 试跑落点 = 试跑当刻的「规则指纹 + 样本键」，写入 trial_runs（独立凭证），
  同时在同一笔事务里写审计账册；两者要么一起落库，要么一起回滚。
- (规则指纹, 样本键) 在有进行中/已敲定的试跑时唯一，拒覆写。
- 同一策略同一时刻只允许一笔「试跑中」，部分唯一索引充当写锁，
  两侧并发争抢时只会有一笔登记成功，另一笔中文报错回滚。
- 试跑只做预演，不动 capture_items / quarantine_rows 正式数据；
  敲定后才把策略页采数改道到试跑规则，且不与正式规则双轨并行。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from app.db import get_db
from app.services import audit as audit_svc
from app.services.errors import ServiceError

_ACTION_LABEL = {"入舱": "入舱", "直接关闭": "关闭", "观察": "观察"}


# --------------------------------------------------------------------------- #
# 底层小工具
# --------------------------------------------------------------------------- #
def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM lab_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str, label: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO lab_settings(key, value, label) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value, label or key),
    )


def _ts(conn) -> str:
    return conn.execute("SELECT datetime('now') AS t").fetchone()["t"]


def compute_rule_fingerprint(conn) -> str:
    """对当前规则集取稳定指纹；任一规则字段变化都会让指纹漂移。"""
    rows = conn.execute(
        """
        SELECT rule_code, quality_match, action, target_bin_code, enabled
        FROM policy_rules ORDER BY rule_code
        """
    ).fetchall()
    payload = "|".join(
        f"{r['rule_code']}:{r['quality_match']}:{r['action']}:"
        f"{r['target_bin_code']}:{r['enabled']}"
        for r in rows
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _reason(prefix: str, rule_code: str, action: str) -> str:
    """隔离舱分流理由：统一口径，避免规则号/动作拼接时拧花。"""
    return f"{prefix}·规则{rule_code}·{_ACTION_LABEL.get(action, action)}"


def routing_reason(rule_code: str, action: str, prefix: str = "规则") -> str:
    """供正式套规则复用的统一分流理由格式。"""
    return _reason(prefix, rule_code, action)


def build_plan(conn, batch_id: int, rule, prefix: str = "试跑") -> list[dict]:
    """对单条规则在本批待检坏件上做编排预演，不写正式表。"""
    return build_plan_multi(conn, batch_id, [rule], prefix=prefix)


def build_plan_multi(conn, batch_id: int, rules, prefix: str = "试跑") -> list[dict]:
    """对多条规则做编排预演；同质量标记取第一条命中规则。"""
    items = conn.execute(
        """
        SELECT id, item_key, quality, status FROM capture_items
        WHERE batch_id=? AND status='待检' AND quality!='合格'
        ORDER BY id
        """,
        (batch_id,),
    ).fetchall()
    plan: list[dict] = []
    for it in items:
        matched = None
        for rule in rules:
            if rule is not None and rule["quality_match"] == it["quality"]:
                matched = rule
                break
        if matched is None:
            continue
        action = matched["action"]
        bin_code = ""
        if action == "入舱":
            bin_code = matched["target_bin_code"] or "BIN-A"
        plan.append(
            {
                "item_id": it["id"],
                "item_key": it["item_key"],
                "quality": it["quality"],
                "rule_code": matched["rule_code"],
                "action": action,
                "target_bin_code": bin_code,
                "reason": _reason(prefix, matched["rule_code"], action),
            }
        )
    return plan


def _pick_rule(conn, rule_code: str | None):
    if rule_code:
        rule = conn.execute(
            "SELECT * FROM policy_rules WHERE rule_code=?", (rule_code,)
        ).fetchone()
        if not rule:
            raise ServiceError("试跑规则不存在", 404)
        return rule
    # 未指定时退到首条启用规则，便于仓管一键试跑。
    rule = conn.execute(
        "SELECT * FROM policy_rules WHERE enabled=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not rule:
        raise ServiceError("没有可试跑的启用规则")
    return rule


def _spot_check(conn, batch_id: int, rule, plan: list[dict], sample_key: str) -> dict:
    """抽一单编排核对：独立复核样本条目的分流结论是否仍成立。"""
    entry = next((p for p in plan if p["item_key"] == sample_key), None)
    if entry is None:
        return {
            "sample_key": sample_key,
            "rule_code": rule["rule_code"],
            "matched": False,
            "note": "样本键不在本次编排内",
            "checked_at": _ts(conn),
        }
    item = conn.execute(
        "SELECT * FROM capture_items WHERE id=?", (entry["item_id"],)
    ).fetchone()
    ok_item = (
        item is not None
        and item["status"] == "待检"
        and item["quality"] == rule["quality_match"]
    )
    ok_action = entry["action"] == rule["action"] and entry["action"] in _ACTION_LABEL
    ok_bin = True
    if entry["action"] == "入舱":
        ok_bin = (
            conn.execute(
                "SELECT id FROM quarantine_bins WHERE bin_code=?",
                (entry["target_bin_code"],),
            ).fetchone()
            is not None
        )
    return {
        "sample_key": sample_key,
        "rule_code": rule["rule_code"],
        "action": entry["action"],
        "target_bin_code": entry["target_bin_code"],
        "reason": entry["reason"],
        "matched": bool(ok_item and ok_action and ok_bin),
        "checked_at": _ts(conn),
    }


# --------------------------------------------------------------------------- #
# 试跑开关
# --------------------------------------------------------------------------- #
def set_trial_mode(
    enabled: bool, rule_code: str = "", actor: str = "admin", db_path: str | None = None
) -> dict:
    with get_db(db_path, immediate=True) as conn:
        set_setting(conn, "trial_mode", "1" if enabled else "0")
        if enabled and rule_code:
            rule = conn.execute(
                "SELECT rule_code FROM policy_rules WHERE rule_code=?", (rule_code,)
            ).fetchone()
            if not rule:
                raise ServiceError("试跑钉点规则不存在", 404)
            set_setting(conn, "trial_rule_code", rule_code)
        if not enabled:
            set_setting(conn, "trial_rule_code", "")
        detail = ("开启" if enabled else "关闭") + (
            f"·规则{rule_code}" if enabled and rule_code else ""
        )
        audit_svc.write_event(
            "试跑开关", "lab", 0, detail, actor=actor, conn=conn
        )
        return {
            "trial_mode": get_setting(conn, "trial_mode", "0"),
            "trial_rule_code": get_setting(conn, "trial_rule_code", ""),
        }


# --------------------------------------------------------------------------- #
# 试跑凭证
# --------------------------------------------------------------------------- #
def _trial_dict(row) -> dict:
    data = dict(row)
    try:
        data["plan"] = json.loads(data.get("plan_json") or "[]")
    except (TypeError, ValueError):
        data["plan"] = []
    try:
        data["spot_check"] = json.loads(data.get("spot_check_json") or "{}")
    except (TypeError, ValueError):
        data["spot_check"] = {}
    return data


def list_trials(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM trial_runs ORDER BY id DESC"
        ).fetchall()
        return [_trial_dict(r) for r in rows]


def get_trial(trial_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM trial_runs WHERE id=?", (trial_id,)
        ).fetchone()
        if not row:
            raise ServiceError("试跑不存在", 404)
        return _trial_dict(row)


def create_trial(
    batch_id: int,
    rule_code: str | None = None,
    sample_key: str = "",
    actor: str = "admin",
    db_path: str | None = None,
) -> dict:
    """仓管点试跑：整笔落库（凭证 + 审计 + 抽检），失败中文回滚。"""
    with get_db(db_path, immediate=True) as conn:
        batch = conn.execute(
            "SELECT * FROM capture_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已关闭":
            raise ServiceError("已关闭批次不能试跑")

        rule = _pick_rule(conn, rule_code)
        if not rule["enabled"]:
            raise ServiceError("试跑规则未启用，不能发起试跑")
        rule_code = rule["rule_code"]

        fingerprint = compute_rule_fingerprint(conn)
        plan = build_plan(conn, batch_id, rule, prefix="试跑")
        if not plan:
            raise ServiceError("本批没有匹配该规则的待检坏件，无法试跑")

        keys = [p["item_key"] for p in plan]
        sample_key = (sample_key or "").strip()
        if sample_key:
            if sample_key not in keys:
                raise ServiceError("样本键不在本次试跑编排内")
        else:
            sample_key = keys[0]

        # 落点拒覆写：同指纹同一样本键已有生效凭证。
        dup = conn.execute(
            """
            SELECT id FROM trial_runs
            WHERE rule_fingerprint=? AND sample_key=?
              AND status IN ('试跑中','已敲定')
            """,
            (fingerprint, sample_key),
        ).fetchone()
        if dup:
            raise ServiceError("试跑落点已存在（规则指纹+样本键），拒绝覆写", 409)

        # 同策略写锁：已有进行中的试跑则拒绝，避免两侧双落互覆。
        active = conn.execute(
            "SELECT id FROM trial_runs WHERE rule_code=? AND status='试跑中'",
            (rule_code,),
        ).fetchone()
        if active:
            raise ServiceError("该策略已有进行中的试跑，缺少写锁，本次未登记", 409)

        n = conn.execute("SELECT COUNT(*) AS c FROM trial_runs").fetchone()["c"] + 1
        trial_code = f"TRI-{n:04d}"
        try:
            cur = conn.execute(
                """
                INSERT INTO trial_runs(
                  trial_code, batch_id, rule_code, rule_fingerprint,
                  sample_key, status, plan_json, spot_check_json, note
                ) VALUES (?, ?, ?, ?, ?, '试跑中', ?, '{}', ?)
                """,
                (
                    trial_code,
                    batch_id,
                    rule_code,
                    fingerprint,
                    sample_key,
                    json.dumps(plan, ensure_ascii=False),
                    f"试跑口径 {trial_code}",
                ),
            )
        except sqlite3.IntegrityError:
            # 并发兜底：唯一索引触发时同样回滚并给中文。
            raise ServiceError("两侧争抢试跑同一策略，写入锁冲突，已回滚", 409)

        trial_id = cur.lastrowid

        # 试跑后再抽一单编排核对。
        spot = _spot_check(conn, batch_id, rule, plan, sample_key)
        conn.execute(
            "UPDATE trial_runs SET spot_check_json=? WHERE id=?",
            (json.dumps(spot, ensure_ascii=False), trial_id),
        )

        # 开启试跑开关并把钉点指向本规则。
        set_setting(conn, "trial_mode", "1")
        set_setting(conn, "trial_rule_code", rule_code)

        # 审计账册写入试跑当刻的规则指纹连同样本键。
        audit_svc.write_event(
            "试跑落库",
            "trial_run",
            trial_id,
            f"规则指纹{fingerprint}·样本键{sample_key}·规则{rule_code}",
            actor=actor,
            conn=conn,
        )

        row = conn.execute(
            "SELECT * FROM trial_runs WHERE id=?", (trial_id,)
        ).fetchone()
        return _trial_dict(row)


def confirm_trial(
    trial_id: int, actor: str = "admin", db_path: str | None = None
) -> dict:
    """敲定试跑：之后策略页采数改道到本规则，双轨不并行。"""
    with get_db(db_path, immediate=True) as conn:
        row = conn.execute(
            "SELECT * FROM trial_runs WHERE id=?", (trial_id,)
        ).fetchone()
        if not row:
            raise ServiceError("试跑不存在", 404)
        if row["status"] != "试跑中":
            raise ServiceError("仅试跑中的凭证可敲定")
        current = compute_rule_fingerprint(conn)
        if current != row["rule_fingerprint"]:
            raise ServiceError("规则指纹与试跑钉点不一致，已拦下敲定", 409)
        pinned = get_setting(conn, "trial_rule_code", "")
        if pinned and pinned != row["rule_code"]:
            raise ServiceError("规则号与试跑钉点拧了，已拦下敲定", 409)
        conn.execute(
            """
            UPDATE trial_runs SET status='已敲定', confirmed_at=datetime('now')
            WHERE id=?
            """,
            (trial_id,),
        )
        set_setting(conn, "trial_mode", "1")
        set_setting(conn, "trial_rule_code", row["rule_code"])
        audit_svc.write_event(
            "试跑敲定",
            "trial_run",
            trial_id,
            f"规则{row['rule_code']}·指纹{row['rule_fingerprint']}",
            actor=actor,
            conn=conn,
        )
        out = conn.execute(
            "SELECT * FROM trial_runs WHERE id=?", (trial_id,)
        ).fetchone()
        return _trial_dict(out)


def abandon_trial(
    trial_id: int, actor: str = "admin", db_path: str | None = None
) -> dict:
    with get_db(db_path, immediate=True) as conn:
        row = conn.execute(
            "SELECT * FROM trial_runs WHERE id=?", (trial_id,)
        ).fetchone()
        if not row:
            raise ServiceError("试跑不存在", 404)
        if row["status"] == "已废弃":
            raise ServiceError("试跑已废弃")
        conn.execute(
            "UPDATE trial_runs SET status='已废弃' WHERE id=?", (trial_id,)
        )
        if get_setting(conn, "trial_rule_code", "") == row["rule_code"]:
            set_setting(conn, "trial_rule_code", "")
        # 已无进行中/已敲定凭证时，试跑开关一并收起，避免残留悬空钉点。
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM trial_runs WHERE status IN ('试跑中','已敲定')"
        ).fetchone()["c"]
        if remaining == 0:
            set_setting(conn, "trial_mode", "0")
            set_setting(conn, "trial_rule_code", "")
        audit_svc.write_event(
            "试跑废弃", "trial_run", trial_id, row["rule_code"], actor=actor, conn=conn
        )
        out = conn.execute(
            "SELECT * FROM trial_runs WHERE id=?", (trial_id,)
        ).fetchone()
        return _trial_dict(out)


def run_spot_check(
    trial_id: int, actor: str = "admin", db_path: str | None = None
) -> dict:
    with get_db(db_path, immediate=True) as conn:
        row = conn.execute(
            "SELECT * FROM trial_runs WHERE id=?", (trial_id,)
        ).fetchone()
        if not row:
            raise ServiceError("试跑不存在", 404)
        rule = conn.execute(
            "SELECT * FROM policy_rules WHERE rule_code=?", (row["rule_code"],)
        ).fetchone()
        if not rule:
            raise ServiceError("试跑规则不存在", 404)
        plan = json.loads(row["plan_json"] or "[]")
        spot = _spot_check(conn, row["batch_id"], rule, plan, row["sample_key"])
        conn.execute(
            "UPDATE trial_runs SET spot_check_json=? WHERE id=?",
            (json.dumps(spot, ensure_ascii=False), trial_id),
        )
        audit_svc.write_event(
            "抽单核对",
            "trial_run",
            trial_id,
            f"样本{row['sample_key']}·{'对齐' if spot.get('matched') else '不一致'}",
            actor=actor,
            conn=conn,
        )
        out = conn.execute(
            "SELECT * FROM trial_runs WHERE id=?", (trial_id,)
        ).fetchone()
        return _trial_dict(out)


def verify_trials(db_path: str | None = None) -> list[dict]:
    """策略页联验：试跑芯片规则号 / 样本键 与抓取列表逐行对齐。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT t.*, b.batch_code
            FROM trial_runs t
            JOIN capture_batches b ON b.id=t.batch_id
            ORDER BY t.id DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            item = conn.execute(
                "SELECT id FROM capture_items WHERE batch_id=? AND item_key=?",
                (r["batch_id"], r["sample_key"]),
            ).fetchone()
            out.append(
                {
                    "trial_id": r["id"],
                    "trial_code": r["trial_code"],
                    "rule_code": r["rule_code"],
                    "batch_id": r["batch_id"],
                    "batch_code": r["batch_code"],
                    "sample_key": r["sample_key"],
                    "rule_fingerprint": r["rule_fingerprint"],
                    "status": r["status"],
                    "capture_item": item is not None,
                    "aligned": item is not None,
                }
            )
        return out
