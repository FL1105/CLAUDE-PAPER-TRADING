# ruff: noqa: E501
"""Self-contained fleet comparison page (equity lines, 30-day income range, gate status)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="300"><title>polybot fleet</title>
<style>
:root{--bg:#fafafa;--fg:#1a1a1a;--mut:#666;--card:#fff;--bd:#ddd;--ok:#1a7f4b;--bad:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#999;--card:#1b1b1b;--bd:#333;--ok:#4cc38a;--bad:#ff6b63}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif}
h1{font-size:18px;margin:0 0 4px}.mut{color:var(--mut)}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:12px;margin:12px 0}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--bd)}
td.n{text-align:right;font-variant-numeric:tabular-nums}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.bar{position:relative;height:14px;background:var(--bd);border-radius:7px;min-width:140px}
.bar i{position:absolute;top:2px;height:10px;border-radius:5px;opacity:.85}.bar b{position:absolute;top:0;width:2px;height:14px;background:var(--fg)}
svg{width:100%;height:auto;display:block}.wrap{overflow-x:auto}
</style></head><body>
<h1>Candidate fleet</h1><div class="mut" id="meta"></div>
<div class="card"><div class="mut">Settled paper equity (USDC)</div><svg id="chart" viewBox="0 0 800 280"></svg><div id="legend"></div></div>
<div class="card wrap"><table id="tbl"></table>
<p class="mut">Range = 5th to 95th percentile of a bootstrap over settled paper trades, scaled to 30 days
(black tick = median). It describes past evidence, not a forecast. Wide or empty means too little data.</p></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const COL=['#2a6fdb','#d9822b','#8a4fd0','#1a9e8f','#c2415d','#6b7d1f'];
const bank=D.bankroll||1000,C=D.candidates;
document.getElementById('meta').textContent=C.length+' candidates · bankroll $'+bank+' · generated '+new Date(D.generated_ts*1000).toISOString().slice(0,16)+'Z';
const pts=C.flatMap(c=>c.equity.map(p=>p));
const svg=document.getElementById('chart');
if(pts.length){
 const t0=Math.min(...pts.map(p=>p.ts)),t1=Math.max(...pts.map(p=>p.ts))||t0+1;
 let lo=Math.min(bank,...pts.map(p=>p.equity)),hi=Math.max(bank,...pts.map(p=>p.equity));if(hi-lo<1){hi+=1;lo-=1}
 const X=t=>40+(t-t0)/Math.max(1,t1-t0)*750,Y=v=>260-(v-lo)/(hi-lo)*240;
 let h=`<line x1="40" x2="790" y1="${Y(bank)}" y2="${Y(bank)}" stroke="currentColor" stroke-dasharray="3 3" opacity=".4"/>`;
 h+=`<text x="2" y="${Y(hi)+4}" font-size="10" fill="currentColor">${hi.toFixed(0)}</text><text x="2" y="${Y(lo)}" font-size="10" fill="currentColor">${lo.toFixed(0)}</text>`;
 C.forEach((c,i)=>{if(!c.equity.length)return;h+=`<polyline fill="none" stroke="${COL[i%6]}" stroke-width="2" points="${c.equity.map(p=>X(p.ts)+','+Y(p.equity)).join(' ')}"/>`});
 svg.innerHTML=h;
}else svg.innerHTML='<text x="20" y="40" fill="currentColor">No settled trades yet. Run `polybot fleet run`.</text>';
document.getElementById('legend').innerHTML=C.map((c,i)=>`<span style="margin-right:12px"><span class="sw" style="background:${COL[i%6]}"></span>${c.name}</span>`).join('');
let ext=1;C.forEach(c=>{const r=c.income_30d;if(r.lo!=null)ext=Math.max(ext,Math.abs(r.lo),Math.abs(r.hi))});
const bar=r=>{if(r.lo==null)return '<span class="mut">not enough trades</span>';
 const P=v=>50+v/ext*50,l=P(r.lo),h=P(r.hi),col=r.lo>0?'var(--ok)':(r.hi<0?'var(--bad)':'var(--mut)');
 return `<div class="bar"><i style="left:${l}%;width:${Math.max(1,h-l)}%;background:${col}"></i><b style="left:${P(r.mid)}%"></b></div><div class="mut">$${r.lo.toFixed(0)} to $${r.hi.toFixed(0)}</div>`};
const prog=c=>{const p=c.progress||{};const stale=p.heartbeat_age_s!=null&&p.heartbeat_age_s>900?'<div style="color:var(--bad)">STALE: no heartbeat for '+Math.round(p.heartbeat_age_s/60)+' min</div>':'';
 return `<div>${p.trades}/${p.trades_needed} trades</div><div>${c.days}/${p.days_needed} days</div><div class="mut">${p.eta_days==null?'ETA unknown':'ETA ~'+p.eta_days+' d'}</div>${stale}`};
const adj=c=>{const a=c.fleet_adjusted||{};if(a.roi==null)return '<span class="mut">no data</span>';
 return `<div style="color:${a.passes?'var(--ok)':'var(--mut)'}">${a.passes?'passes':'not yet'}</div><div class="mut">ROI ${(a.roi*100).toFixed(1)}%, lower ${(a.ci_lower*100).toFixed(1)}% @ ${(a.level*100).toFixed(2)}% (${a.n_candidates} candidates)</div>`};
document.getElementById('tbl').innerHTML='<tr><th>Candidate</th><th>Tier</th><th class="n">Days</th><th class="n">Settled</th><th class="n">PnL</th><th class="n">Max DD</th><th>30-day range</th><th>Progress</th><th>Fleet-adjusted edge</th><th>Blocking gates</th></tr>'+
 C.map((c,i)=>`<tr><td><span class="sw" style="background:${COL[i%6]}"></span>${c.name}${c.halted?' <b style="color:var(--bad)">HALTED</b>':''}</td><td>${c.tier||'none yet'}</td><td class="n">${c.days}</td><td class="n">${c.summary.settled_trades}</td><td class="n">${c.summary.pnl.toFixed(2)}</td><td class="n">${c.summary.max_drawdown==null?'-':(c.summary.max_drawdown*100).toFixed(1)+'%'}</td><td>${bar(c.income_30d)}</td><td>${prog(c)}</td><td>${adj(c)}</td><td class="mut">${c.blocking.join(', ')||'-'}</td></tr>`).join('');
</script></body></html>"""


def write_html(data: dict[str, Any], path: Path) -> None:
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PAGE.replace("__DATA__", payload), encoding="utf-8")
