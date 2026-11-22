import { api } from '../api.js';

export async function renderSettings(el) {
  const data = await api('/api/settings');
  el.innerHTML = `
    <div class="card">
      <h2>台参数</h2>
      <table>
        <thead><tr><th>项</th><th>说明</th><th>值</th><th></th></tr></thead>
        <tbody>
          ${(data.items || []).map((s) => `
            <tr>
              <td>${s.key}${s.readonly ? ' <span class="chip pr">只读</span>' : ''}</td>
              <td>${s.label}</td>
              <td>${s.readonly
                ? `<input value="${s.value}" disabled title="试跑只读口径，请在入舱规则页操作试跑" />`
                : `<input data-key="${s.key}" value="${s.value}" />`}</td>
              <td>${s.readonly
                ? '<span class="muted">试跑只读</span>'
                : `<button data-save="${s.key}">保存</button>`}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  el.querySelectorAll('[data-save]').forEach((btn) => {
    btn.onclick = async () => {
      const input = el.querySelector(`input[data-key="${btn.dataset.save}"]`);
      await api('/api/settings/update', {
        method: 'POST',
        body: JSON.stringify({ key: btn.dataset.save, value: input.value }),
      });
      await renderSettings(el);
    };
  });
}
