from __future__ import annotations

import threading

import pytest

from app.db import get_db
from app.services import captures, cleaning, policies, quarantine, settings, trial
from app.services.errors import ServiceError


def _open_batch(db_path):
    return next(b for b in captures.list_batches(db_path) if b["status"] == "已收齐")


def test_trial_lands_whole_with_fingerprint_and_sample(db_path):
    batch = _open_batch(db_path)
    t = trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    assert t["status"] == "试跑中"
    assert t["rule_fingerprint"]
    assert t["sample_key"] == "sku-1002"
    assert t["plan"], "试跑须产出编排计划"
    # 整笔落库：审计账册同时写入指纹与样本键。
    with get_db(db_path) as conn:
        ev = conn.execute(
            "SELECT * FROM audit_events WHERE action='试跑落库' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert ev is not None
    assert t["rule_fingerprint"] in ev["detail"]
    assert "sku-1002" in ev["detail"]


def test_trial_does_not_touch_formal_tables(db_path):
    batch = _open_batch(db_path)
    before_rows = len(quarantine.list_rows(db_path=db_path))
    detail_before = captures.get_batch(batch["id"], db_path)
    statuses_before = {i["id"]: i["status"] for i in detail_before["items"]}
    trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    assert len(quarantine.list_rows(db_path=db_path)) == before_rows
    detail_after = captures.get_batch(batch["id"], db_path)
    assert {i["id"]: i["status"] for i in detail_after["items"]} == statuses_before


def test_trial_pin_rejects_overwrite(db_path):
    batch = _open_batch(db_path)
    trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    with pytest.raises(ServiceError) as exc:
        trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    assert "拒" in exc.value.message or "已存在" in exc.value.message


def test_formal_rule_apply_blocked_while_trial_unconfirmed(db_path):
    batch = _open_batch(db_path)
    trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    with pytest.raises(ServiceError) as exc:
        policies.apply_to_batch(batch["id"], db_path=db_path)
    assert "试跑" in exc.value.message


def test_formal_rule_apply_reroutes_after_confirm(db_path):
    batch = _open_batch(db_path)
    t = trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    trial.confirm_trial(t["id"], db_path=db_path)
    out = policies.apply_to_batch(batch["id"], db_path=db_path)
    assert out["routed"] is True
    assert out["trial_rule_code"] == "POL-001"
    # 改道只套钉点规则：本批命中的入舱理由带试跑改道前缀。
    rows = [r for r in quarantine.list_rows(db_path=db_path) if r["item_key"] == "sku-1002"]
    assert rows and "试跑改道" in rows[0]["reason"]


def test_cleaning_gate_blocks_on_twisted_pin(db_path):
    batch = _open_batch(db_path)
    trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    # 把钉点拧到另一条规则。
    with get_db(db_path, immediate=True) as conn:
        trial.set_setting(conn, "trial_rule_code", "POL-002")
    gate = cleaning.evaluate_gate_public(db_path)
    assert gate["blocked"] is True
    with pytest.raises(ServiceError) as exc:
        cleaning.orchestrate(batch["id"], db_path=db_path)
    assert "拧" in exc.value.message or "拦" in exc.value.message


def test_cleaning_block_writes_audit_not_formal_data(db_path):
    batch = _open_batch(db_path)
    trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    with get_db(db_path, immediate=True) as conn:
        trial.set_setting(conn, "trial_rule_code", "POL-002")
    before = len(quarantine.list_rows(db_path=db_path))
    with pytest.raises(ServiceError):
        cleaning.orchestrate(batch["id"], db_path=db_path)
    # 拦下：正式库不动，但拦截审计独立落账。
    assert len(quarantine.list_rows(db_path=db_path)) == before
    from app.services import audit as audit_svc
    events = audit_svc.list_events(50, db_path)
    assert any(e["action"] == "清洗编排拦截" for e in events)


def test_abandon_clears_dangling_pin(db_path):
    batch = _open_batch(db_path)
    t = trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    trial.abandon_trial(t["id"], db_path=db_path)
    gate = cleaning.evaluate_gate_public(db_path)
    assert gate["blocked"] is False
    assert gate["trial_rule_code"] == ""


def test_cleaning_not_blocked_when_trial_consistent(db_path):
    batch = _open_batch(db_path)
    trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    gate = cleaning.evaluate_gate_public(db_path)
    assert gate["blocked"] is False
    out = cleaning.orchestrate(batch["id"], db_path=db_path)
    assert out["allowed"] is True
    assert out["mode"] == "试跑"


def test_confirm_then_trial_drives_cleaning(db_path):
    batch = _open_batch(db_path)
    t = trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    trial.confirm_trial(t["id"], db_path=db_path)
    out = cleaning.orchestrate(batch["id"], db_path=db_path)
    assert out["allowed"] is True
    assert out["mode"] == "试跑"
    assert out["trial_code"] == t["trial_code"]


def test_spot_check_aligns(db_path):
    batch = _open_batch(db_path)
    t = trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    spot = trial.run_spot_check(t["id"], db_path=db_path)["spot_check"]
    assert spot["matched"] is True
    assert spot["sample_key"] == "sku-1002"


def test_verify_trials_aligns_with_capture_rows(db_path):
    batch = _open_batch(db_path)
    t = trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    verify = trial.verify_trials(db_path)
    row = next(v for v in verify if v["trial_id"] == t["id"])
    assert row["rule_code"] == t["rule_code"]
    assert row["sample_key"] == t["sample_key"]
    assert row["aligned"] is True


def test_same_strategy_concurrent_only_one_lands(db_path):
    batch = _open_batch(db_path)
    # 追加第二条缺字段条目，保证两个样本都命中 POL-001。
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO capture_items(batch_id, item_key, payload, quality, status)"
            " VALUES (?,?,?,?,?)",
            (batch["id"], "sku-9001", "{}", "缺字段", "待检"),
        )
    results = []

    def worker(sample):
        try:
            r = trial.create_trial(batch["id"], "POL-001", sample, db_path=db_path)
            results.append(("ok", r["trial_code"]))
        except ServiceError as exc:
            results.append(("err", exc.message))

    threads = [
        threading.Thread(target=worker, args=("sku-1002",)),
        threading.Thread(target=worker, args=("sku-9001",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]
    assert len(oks) == 1, results
    assert len(errs) == 1, results
    with get_db(db_path) as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM trial_runs WHERE rule_code='POL-001' AND status='试跑中'"
        ).fetchone()["c"]
    assert active == 1


def test_trial_settings_are_readonly(db_path):
    items = {s["key"]: s for s in settings.list_settings(db_path)}
    for key in ("trial_mode", "trial_scope", "trial_rule_code"):
        assert items[key]["readonly"] is True
        with pytest.raises(ServiceError):
            settings.update_setting(key, "1", db_path)


def test_batches_expose_trial_rule_code(db_path):
    batch = _open_batch(db_path)
    trial.create_trial(batch["id"], "POL-001", "sku-1002", db_path=db_path)
    listed = next(b for b in captures.list_batches(db_path) if b["id"] == batch["id"])
    assert listed["trial_mode"] is True
    assert listed["trial_rule_code"] == "POL-001"


def test_bins_carry_audit_strip(db_path):
    batch = _open_batch(db_path)
    detail = captures.get_batch(batch["id"], db_path)
    bad = next(i for i in detail["items"] if i["quality"] != "合格" and i["status"] == "待检")
    open_bin = next(b for b in quarantine.list_bins(db_path) if b["status"] == "开放")
    quarantine.enter_item(bad["id"], open_bin["id"], "审计条测试", db_path)
    bins = {b["bin_code"]: b for b in quarantine.list_bins(db_path)}
    assert bins[open_bin["bin_code"]]["audit_strip"]
