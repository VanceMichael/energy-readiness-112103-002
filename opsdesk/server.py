"""只读本地工作台：把依据链渲染成值班员可直接浏览的页面。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>迎峰期采集工作台</title>
<style>
  :root { color-scheme: light; }
  body { font-family: -apple-system, "Noto Sans SC", sans-serif; margin: 0; background: #f6f7f9; color: #1b1f24; }
  header { background: #16324f; color: #fff; padding: 16px 24px; }
  header h1 { font-size: 18px; margin: 0 0 6px; }
  header p { margin: 0; opacity: .8; font-size: 13px; }
  main { padding: 20px 24px 48px; }
  .bar { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }
  .bar input, .bar select, .bar button { padding: 6px 10px; border-radius: 6px; border: 1px solid #c7ccd4; font-size: 13px; }
  .bar button { background: #16324f; color: #fff; border: none; cursor: pointer; }
  .summary { display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 18px; font-size: 13px; color: #46505c; }
  .summary b { font-size: 18px; display: block; color: #16324f; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; }
  .card { background: #fff; border: 1px solid #e1e5ea; border-radius: 10px; padding: 16px; }
  .card h2 { font-size: 15px; margin: 0 0 4px; }
  .meta { font-size: 12px; color: #6a7480; margin-bottom: 10px; }
  .tag { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 10px; margin-right: 6px; }
  .alert { background: #fde8e8; color: #a11f1f; }
  .watch { background: #fff3d6; color: #8a6100; }
  .normal { background: #e6f5ec; color: #1f6b3b; }
  .unknown { background: #eceef2; color: #545e6b; }
  .row { font-size: 13px; margin: 4px 0; }
  .row .k { color: #6a7480; display: inline-block; min-width: 62px; }
  .gap { font-size: 12.5px; margin: 3px 0 0 0; padding-left: 10px; border-left: 3px solid #e0a030; color: #6a4b00; }
  .gap.blocking { border-color: #c0392b; color: #8f2318; }
  .disp { margin-top: 10px; padding: 10px; background: #f8fafc; border-radius: 8px; font-size: 13px; }
  .disp.needs { background: #fff4f2; border: 1px solid #f0c3bb; }
  .chain { margin: 8px 0 0 0; padding-left: 16px; font-size: 12.5px; color: #46505c; }
  .chain li { margin: 3px 0; }
  table { width: 100%; border-collapse: collapse; background: #fff; font-size: 13px; margin-top: 8px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef1f4; vertical-align: top; }
  h3 { font-size: 14px; margin: 26px 0 6px; }
  .empty { color: #6a7480; font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>迎峰期巡检与监测采集工作台</h1>
  <p>合成演练数据。依据链、未核实项与处置复核均按所选时点计算。</p>
</header>
<main>
  <div class="bar">
    <label>调度时点 <input id="asof" type="datetime-local" step="60"></label>
    <label>时段起 <input id="wstart" type="datetime-local" step="60"></label>
    <label>时段止 <input id="wend" type="datetime-local" step="60"></label>
    <label>设施 <select id="asset"><option value="">全部</option></select></label>
    <button id="go">刷新</button>
  </div>
  <div class="summary" id="summary"></div>
  <div class="grid" id="assets"></div>
  <h3>未核实项</h3>
  <table id="unverified"><thead><tr><th>设施</th><th>缺口类别</th><th>说明</th></tr></thead><tbody></tbody></table>
  <h3>复测 / 补证队列</h3>
  <table id="recheck"><thead><tr><th>优先级</th><th>设施</th><th>动作</th><th>原因</th></tr></thead><tbody></tbody></table>
  <h3>未通过校验的来源</h3>
  <table id="rejected"><thead><tr><th>来源</th><th>类型</th><th>原因</th></tr></thead><tbody></tbody></table>
</main>
<script>
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const local = iso => {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('zh-CN', {hour12:false, month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
};
const levelTag = lvl => `<span class="tag ${esc(lvl)}">${esc({alert:'告警',watch:'关注',normal:'正常',unknown:'待补证'}[lvl] || lvl)}</span>`;

async function load() {
  const params = new URLSearchParams();
  const asof = document.getElementById('asof').value;
  if (asof) params.set('as_of', new Date(asof).toISOString());
  const ws = document.getElementById('wstart').value, we = document.getElementById('wend').value;
  if (ws && we) { params.set('window', new Date(ws).toISOString() + '/' + new Date(we).toISOString()); }
  const asset = document.getElementById('asset').value;
  if (asset) params.set('asset', asset);
  const resp = await fetch('/api/report?' + params.toString());
  if (!resp.ok) { alert('读取失败：' + await resp.text()); return; }
  render(await resp.json());
}

function render(r) {
  const s = r.summary;
  document.getElementById('summary').innerHTML = [
    ['设施', s.asset_count], ['告警', s.alert_assets], ['关注', s.watch_assets],
    ['待补证', s.indeterminate_assets], ['处置', s.dispatch_total],
    ['需复核', s.dispatch_needs_review], ['复测项', s.recheck_items], ['拒收来源', s.rejected_sources]
  ].map(([k,v]) => `<div><b>${v}</b>${k}</div>`).join('');

  const select = document.getElementById('asset');
  const current = select.value;
  select.innerHTML = '<option value="">全部</option>' + r.assets.map(a => `<option value="${esc(a.asset_id)}">${esc(a.asset_id)} ${esc(a.name)}</option>`).join('');
  select.value = current;

  document.getElementById('assets').innerHTML = r.assets.map(a => {
    const mon = a.monitoring, eff = mon.effective, insp = a.inspection, wx = a.weather;
    const gaps = a.gaps.map(g => `<div class="gap ${g.blocking?'blocking':''}">${esc(g.category_label)}：${esc(g.message)}</div>`).join('');
    const hist = mon.history.slice(-4).map(h => `<div class="row"><span class="k">${h.superseded_by?'已修正':'历史'}</span>${esc(h.source_id)} 修订 ${h.revision}：${esc(h.value)} ${esc(h.unit)}（${local(h.observed_at)}）</div>`).join('');
    const disps = a.dispatch.map(d => `
      <div class="disp ${d.review_status==='needs_review'?'needs':''}">
        <div><b>${esc(d.dispatch_id)}</b> 班组 ${esc(d.crew)}｜${esc(d.review_label)}｜有效状态 <b>${esc(d.effective_state)}</b></div>
        ${d.reasons.map(x => `<div class="row">↳ ${esc(x)}</div>`).join('')}
        <ol class="chain">${d.basis_chain.map(c => `<li><b>${esc(c.step)}</b> ${esc(c.detail)} <span class="k">${esc(c.source_id||'')}</span></li>`).join('')}</ol>
      </div>`).join('');
    return `<div class="card">
      <h2>${levelTag(a.assessment.level)} ${esc(a.asset_id)} ${esc(a.name)}</h2>
      <div class="meta">${esc(a.area_name)}｜${esc(a.kind_label)}${a.maintenance?`｜检修 ${esc(a.maintenance.event_id)} ${esc(a.maintenance.label)}`:''}</div>
      <div class="row">${esc(a.assessment.summary)}</div>
      <div class="row"><span class="k">监测</span>${eff?`${esc(eff.metric_label)} ${esc(eff.value)} ${esc(eff.unit)}（${esc(eff.profile_label)}，${local(eff.observed_at)}，${esc(mon.status_label)}）→ ${esc(eff.risk_label)}：${esc(eff.risk_reason)}`:`${esc(mon.status_label)}`}</div>
      ${hist}
      <div class="row"><span class="k">现场</span>${insp.source_id?`${esc(insp.check_id||insp.source_id)} ${esc(insp.condition_label)}（${local(insp.observed_at)}，${esc(insp.status_label)}）`:esc(insp.status_label)}</div>
      <div class="row"><span class="k">气象</span>${esc(wx.status_label)}${wx.alerts.length?'｜'+wx.alerts.map(x=>esc(x.alert_label)+' '+local(x.starts_at)+'→'+local(x.ends_at)).join('；'):''}</div>
      ${gaps}${disps}
    </div>`;
  }).join('');

  document.querySelector('#unverified tbody').innerHTML = r.unverified.length ? r.unverified.map(u =>
    `<tr><td>${esc(u.asset_id)} ${esc(u.asset_name)}</td><td>${esc(u.category_label)}</td><td>${esc(u.message)}</td></tr>`).join('') : '<tr><td colspan="3" class="empty">无</td></tr>';

  document.querySelector('#recheck tbody').innerHTML = r.recheck_queue.length ? r.recheck_queue.map(i =>
    `<tr><td>${esc(i.priority)}</td><td>${esc(i.asset_id)}</td><td>${esc(i.kind_label)}</td><td>${esc(i.reason)}</td></tr>`).join('') : '<tr><td colspan="4" class="empty">无</td></tr>';

  document.querySelector('#rejected tbody').innerHTML = r.rejected_sources.length ? r.rejected_sources.map(x =>
    `<tr><td>${esc(x.source_id)}</td><td>${esc(x.record_kind)}</td><td>${x.issues.map(i=>esc(i.message)).join('；')}</td></tr>`).join('') : '<tr><td colspan="3" class="empty">无</td></tr>';
}

document.getElementById('go').addEventListener('click', load);
load();
</script>
</body>
</html>
"""


def make_handler(registry_path: str, sources_path: str):
    from .evidence import build_report

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 静默，避免演练输出被访问日志淹没
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/report":
                query = parse_qs(parsed.query)
                as_of = query.get("as_of", [None])[0]
                asset = query.get("asset", [None])[0]
                window_raw = query.get("window", [None])[0]
                window = None
                if window_raw is not None:
                    if "/" not in window_raw:
                        self._send(
                            400,
                            "时间窗口格式应为 开始/结束".encode("utf-8"),
                            "text/plain; charset=utf-8",
                        )
                        return
                    window = tuple(window_raw.split("/", 1))
                try:
                    report = build_report(
                        registry_path,
                        sources_path,
                        as_of=as_of,
                        window=window,
                        asset_ids=[asset] if asset else None,
                    )
                except ValueError as exc:
                    self._send(400, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
                    return
                body = json.dumps(report, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
                return
            if parsed.path == "/healthz":
                self._send(200, b"ok", "text/plain")
                return
            self._send(404, b"not found", "text/plain")

    return Handler


def serve(host: str, port: int, registry_path: str, sources_path: str) -> int:
    handler = make_handler(registry_path, sources_path)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"采集工作台已启动：http://{host}:{port}/（Ctrl-C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
