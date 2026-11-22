"""清洗编排闸。

要求：
- 试跑规则本身不应误拦清洗编排（试跑开关打开不等于清洗停摆）。
- 只有「规则号与试跑钉点拧了」（钉点与凭证规则号不一致、或钉点悬空）才拦。
- 拦下时吐中文，编排不落任何正式数据。
"""

from __future__ import annotations

from app.db import get_db
from app.services import audit as audit_svc
from app.services import trial as trial_svc
from app.services.errors import ServiceError


def _active_trial(conn):
    return conn.execute(
        """
        SELECT * FROM trial_runs
        WHERE status IN ('试跑中', '已敲定')
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()


def evaluate_gate(conn) -> dict:
    """返回 {blocked, reason, trial_mode, trial_rule_code, trial_code}。"""
    mode = trial_svc.get_setting(conn, "trial_mode", "0")
    pin = trial_svc.get_setting(conn, "trial_rule_code", "")
    active = _active_trial(conn)

    if active is None:
        if pin:
            return {
                "blocked": True,
                "reason": "试跑钉点无对应凭证，已拦清洗编排",
                "trial_mode": mode,
                "trial_rule_code": pin,
                "trial_code": "",
            }
        return {
            "blocked": False,
            "reason": "",
            "trial_mode": mode,
            "trial_rule_code": pin,
            "trial_code": "",
        }

    # 有生效凭证：钉点必须与凭证规则号一致，否则视为“拧了”。
    if pin != active["rule_code"]:
        return {
            "blocked": True,
            "reason": "规则号与试跑钉点拧了，已拦清洗编排",
            "trial_mode": mode,
            "trial_rule_code": pin,
            "trial_code": active["trial_code"],
        }

    return {
        "blocked": False,
        "reason": "",
        "trial_mode": mode,
        "trial_rule_code": pin,
        "trial_code": active["trial_code"],
    }


def evaluate_gate_public(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        return evaluate_gate(conn)


def orchestrate(
    batch_id: int, actor: str = "admin", db_path: str | None = None
) -> dict:
    # 先在只读连接里判闸：拦下不写正式库，但拦截审计要独立落账。
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM capture_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        gate = evaluate_gate(conn)

    if gate["blocked"]:
        audit_svc.write_event(
            "清洗编排拦截",
            "capture_batch",
            batch_id,
            gate["reason"],
            actor=actor,
            db_path=db_path,
        )
        raise ServiceError(gate["reason"], 409)

    with get_db(db_path, immediate=True) as conn:
        # 试跑一致时不误拦：编排按当前有效规则预演，不写正式表。
        if gate["trial_mode"] == "1" and gate["trial_rule_code"]:
            rule = conn.execute(
                "SELECT * FROM policy_rules WHERE rule_code=?",
                (gate["trial_rule_code"],),
            ).fetchone()
            rules = [rule] if rule else []
            mode = "试跑"
        else:
            rules = conn.execute(
                "SELECT * FROM policy_rules WHERE enabled=1 ORDER BY id"
            ).fetchall()
            mode = "正式"

        plan = trial_svc.build_plan_multi(conn, batch_id, rules, prefix="清洗编排")
        audit_svc.write_event(
            "清洗编排预演",
            "capture_batch",
            batch_id,
            f"{mode}口径·条目{len(plan)}",
            actor=actor,
            conn=conn,
        )
        return {
            "allowed": True,
            "mode": mode,
            "trial_code": gate["trial_code"],
            "rule_codes": [r["rule_code"] for r in rules if r is not None],
            "plan": plan,
            "count": len(plan),
        }
