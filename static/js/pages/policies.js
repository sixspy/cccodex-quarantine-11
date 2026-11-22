
import { api } from '../api.js';

export async function renderPolicies(el) {
  const [rules, batches, trialData, gate] = await Promise.all([
    api('/api/rules'),
    api('/api/batches'),
    api('/api/trials'),
    api('/api/cleaning/gate'),
  ]);
  const openBatches = (batches.items || []).filter((b) => b.status !== '已关闭');
  const trials = trialData.items || [];
  const verify = trialData.verify || [];
  const verifyByTrial = Object.fromEntries(verify.map((v) => [v.trial_id, v]));
  const activeTrial = trials.find((t) => t.status === '试跑中') || trials.find((t) => t.status === '已敲定');
  const trialRuleCode = gate.trial_rule_code || '';
  const pinnedRule = (rules.items || []).find((r) => r.rule_code === trialRuleCode);

  const trialChip = trialRuleCode
    ? `<span class="chip pr" title="试跑钉点规则号">试跑 ${trialRuleCode}${activeTrial ? ` · ${activeTrial.trial_code}` : ''}</span>`
    : '<span class="chip">试跑未开</span>';
  const gateChip = gate.blocked
    ? `<span class="chip bad">编排已拦：${gate.reason}</span>`
    : '<span class="chip ok">编排放行</span>';

  el.innerHTML = `
    <div class="card">
      <h2>入舱规则 ${trialChip} ${gateChip}</h2>
      <p class="muted">按质量标记自动入舱 / 关闭 / 观察。抓取列表亮试跑规则号；策略页联验试跑凭证与抓取条目对齐。</p>
      <div class="row">
        <input id="title" placeholder="规则标题" />
        <select id="qm">
          <option>缺字段</option><option>编码空</option><option>重复键</option><option>合格</option>
        </select>
        <select id="act">
          <option>入舱</option><option>直接关闭</option><option>观察</option>
        </select>
        <input id="bin" placeholder="目标舱号 BIN-A" value="BIN-A" />
        <button id="create">新增规则</button>
      </div>
    </div>

    <div class="card">
      <h2>试跑（Dry-run）</h2>
      <div class="hint-box">
        仓管点试跑：试跑落点走审计账册写入试跑当刻「规则指纹 + 样本键」且拒覆写；
        试跑只预演不动正式表；敲定后策略页采数改道到试跑规则，正式/试跑双轨不并行。
      </div>
      <div class="row">
        <select id="trialBatch">
          ${openBatches.map((b) => `<option value="${b.id}">${b.batch_code} · 坏件${b.bad_count}</option>`).join('')}
        </select>
        <select id="trialRule">
          <option value="">（默认首条启用规则）</option>
          ${(rules.items || []).filter((r) => r.enabled).map((r) => `<option value="${r.rule_code}">${r.rule_code} · ${r.title}</option>`).join('')}
        </select>
        <input id="trialSample" placeholder="样本键（留空取首条）" />
        <button id="runTrial">发起试跑</button>
        <span id="trialMsg" class="muted"></span>
      </div>
      <table>
        <thead><tr><th>凭证</th><th>批次</th><th>规则号</th><th>规则指纹</th><th>样本键</th><th>状态</th><th>联验</th><th>操作</th></tr></thead>
        <tbody>
          ${trials.map((t) => {
            const v = verifyByTrial[t.id] || {};
            const aligned = v.aligned
              ? '<span class="chip ok">对齐</span>'
              : '<span class="chip bad">未对齐</span>';
            const spot = t.spot_check && t.spot_check.matched === true
              ? '<span class="chip ok">抽检对齐</span>'
              : (t.spot_check && t.spot_check.matched === false
                  ? '<span class="chip bad">抽检不一致</span>' : '<span class="chip">未抽检</span>');
            const ops = [];
            if (t.status === '试跑中') {
              ops.push(`<button data-confirm="${t.id}">敲定</button>`);
              ops.push(`<button data-abandon="${t.id}" class="ghost">废弃</button>`);
            }
            ops.push(`<button data-spot="${t.id}" class="ghost">抽单核对</button>`);
            return `
              <tr>
                <td>${t.trial_code}</td>
                <td>${v.batch_code || t.batch_id}</td>
                <td><span class="chip pr">${t.rule_code}</span></td>
                <td class="mono">${t.rule_fingerprint}</td>
                <td>${t.sample_key}</td>
                <td><span class="chip ${t.status === '已敲定' ? 'ok' : (t.status === '已废弃' ? 'bad' : '')}">${t.status}</span></td>
                <td>${aligned} ${spot}</td>
                <td class="row">${ops.join('')}</td>
              </tr>`;
          }).join('') || '<tr><td colspan="8" class="muted">暂无试跑凭证</td></tr>'}
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="row">
        <select id="batch">
          ${openBatches.map((b) => `<option value="${b.id}">${b.batch_code} · 坏件${b.bad_count}</option>`).join('')}
        </select>
        <button id="apply">对批次套正式规则</button>
        <button id="clean" class="ghost">清洗编排预演</button>
        <span id="applyMsg" class="muted"></span>
      </div>
      <table>
        <thead><tr><th>编码</th><th>标题</th><th>匹配</th><th>动作</th><th>舱</th><th>启用</th><th></th></tr></thead>
        <tbody>
          ${(rules.items || []).map((r) => `
            <tr>
              <td>${r.rule_code}${trialRuleCode === r.rule_code ? ' <span class="chip pr">试跑钉点</span>' : ''}</td>
              <td>${r.title}</td>
              <td>${r.quality_match}</td>
              <td>${r.action}</td>
              <td>${r.target_bin_code || '-'}</td>
              <td><span class="chip ${r.enabled ? 'ok' : 'bad'}">${r.enabled ? '开' : '关'}</span></td>
              <td>
                <button data-en="${r.id}" data-v="${r.enabled ? 0 : 1}">
                  ${r.enabled ? '停用' : '启用'}
                </button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;

  const msg = (sel, text) => { el.querySelector(sel).textContent = text; };
  const errText = (e) => String(e.message || e);

  el.querySelector('#create').onclick = async () => {
    try {
      await api('/api/rules', {
        method: 'POST',
        body: JSON.stringify({
          title: el.querySelector('#title').value,
          quality_match: el.querySelector('#qm').value,
          action: el.querySelector('#act').value,
          target_bin_code: el.querySelector('#bin').value,
        }),
      });
      await renderPolicies(el);
    } catch (e) { msg('#trialMsg', errText(e)); }
  };

  const runBtn = el.querySelector('#runTrial');
  if (runBtn) {
    runBtn.onclick = async () => {
      try {
        const t = await api('/api/trials', {
          method: 'POST',
          body: JSON.stringify({
            batch_id: Number(el.querySelector('#trialBatch').value),
            rule_code: el.querySelector('#trialRule').value,
            sample_key: el.querySelector('#trialSample').value,
          }),
        });
        msg('#trialMsg', `试跑落库 ${t.trial_code} · 指纹 ${t.rule_fingerprint} · 样本 ${t.sample_key}`);
        await renderPolicies(el);
      } catch (e) { msg('#trialMsg', errText(e)); }
    };
  }

  el.querySelectorAll('[data-confirm]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api(`/api/trials/${btn.dataset.confirm}/confirm`, { method: 'POST', body: '{}' });
        await renderPolicies(el);
      } catch (e) { msg('#trialMsg', errText(e)); }
    };
  });
  el.querySelectorAll('[data-abandon]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api(`/api/trials/${btn.dataset.abandon}/abandon`, { method: 'POST', body: '{}' });
        await renderPolicies(el);
      } catch (e) { msg('#trialMsg', errText(e)); }
    };
  });
  el.querySelectorAll('[data-spot]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const t = await api(`/api/trials/${btn.dataset.spot}/spot-check`, { method: 'POST', body: '{}' });
        msg('#trialMsg', `抽单核对 ${t.sample_key} · ${t.spot_check && t.spot_check.matched ? '对齐' : '不一致'}`);
        await renderPolicies(el);
      } catch (e) { msg('#trialMsg', errText(e)); }
    };
  });

  el.querySelector('#apply').onclick = async () => {
    const batch_id = Number(el.querySelector('#batch').value);
    try {
      const r = await api('/api/rules/apply', {
        method: 'POST',
        body: JSON.stringify({ batch_id }),
      });
      msg('#applyMsg', `入舱${r.entered} / 关闭${r.closed} / 观察${r.observed}`);
    } catch (e) { msg('#applyMsg', errText(e)); }
  };

  el.querySelector('#clean').onclick = async () => {
    const batch_id = Number(el.querySelector('#batch').value);
    try {
      const r = await api('/api/cleaning/orchestrate', {
        method: 'POST',
        body: JSON.stringify({ batch_id }),
      });
      msg('#applyMsg', `清洗编排预演（${r.mode}口径）：条目${r.count}`);
    } catch (e) { msg('#applyMsg', errText(e)); }
  };

  el.querySelectorAll('[data-en]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api(`/api/rules/${btn.dataset.en}/enable`, {
          method: 'POST',
          body: JSON.stringify({ enabled: btn.dataset.v === '1' }),
        });
        await renderPolicies(el);
      } catch (e) { msg('#trialMsg', errText(e)); }
    };
  });
}
