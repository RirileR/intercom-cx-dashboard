#!/usr/bin/env python3
"""
Cockpit CX Abby — tire les conversations Intercom et génère un dashboard HTML interactif.

Usage :
    python build.py                # 7 derniers jours (défaut)
    python build.py --days 14      # 14 derniers jours
    python build.py --out rapport.html

Le token est lu depuis la variable d'environnement INTERCOM_TOKEN
ou depuis un fichier .env (voir .env.example).
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("Module 'requests' manquant. Lance : pip install -r requirements.txt")

API = "https://api.intercom.io"
ATTR_THEME = "Thème de la demande"
ATTR_NATURE = "Typologie du contact"
ATTR_URGENCY = "Urgency"
ATTR_SENTIMENT = "Sentiment"
ATTR_RESO = "Fin AI Agent resolution state"
ATTR_TITLE = "AI Title"
BUTTON_NOISE = {"j'ai compris", "j'ai compris 🫡", "bonjour", "merci", ""}


def load_env():
    """Charge le .env s'il existe, sans dépendance externe."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def strip_html(raw):
    if not raw:
        return ""
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = html.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def fetch(token, since_ts):
    """Pagine l'API search d'Intercom et renvoie la liste brute des conversations."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Intercom-Version": "2.11",
    }
    out, starting_after, page = [], None, 0
    while True:
        page += 1
        body = {
            "query": {"field": "created_at", "operator": ">", "value": since_ts},
            "pagination": {"per_page": 150},
        }
        if starting_after:
            body["pagination"]["starting_after"] = starting_after
        r = requests.post(f"{API}/conversations/search", headers=headers, json=body, timeout=60)
        if r.status_code == 401:
            sys.exit("Token refusé (401). Vérifie INTERCOM_TOKEN dans ton .env.")
        if r.status_code == 429:
            time.sleep(2)
            page -= 1
            continue
        r.raise_for_status()
        data = r.json()
        convs = data.get("conversations", [])
        out.extend(convs)
        total = data.get("total_count", len(out))
        print(f"  page {page} — {len(out)}/{total} conversations", file=sys.stderr)
        nxt = (data.get("pages") or {}).get("next")
        starting_after = nxt.get("starting_after") if isinstance(nxt, dict) else nxt
        if not starting_after:
            break
        time.sleep(0.25)
    return out


def normalize(convs, app_id):
    rows = []
    for c in convs:
        ca = c.get("custom_attributes", {}) or {}
        src = c.get("source", {}) or {}
        company = c.get("company") or {}
        plan = (company.get("plan") or {}).get("name") if isinstance(company.get("plan"), dict) else None
        verbatim = strip_html(src.get("body", ""))
        if verbatim.lower() in BUTTON_NOISE:
            verbatim = ca.get(ATTR_TITLE) or ""
        reso = (ca.get(ATTR_RESO) or "").lower()
        rows.append({
            "id": str(c.get("id", "")),
            "ts": c.get("created_at", 0),
            "theme": ca.get(ATTR_THEME) or "(non qualifié)",
            "nature": ca.get(ATTR_NATURE) or "(non qualifié)",
            "urgency": ca.get(ATTR_URGENCY) or "—",
            "sentiment": ca.get(ATTR_SENTIMENT) or "—",
            "reso": ca.get(ATTR_RESO) or "—",
            "escalated": "escalat" in reso or "handoff" in reso,
            "title": ca.get(ATTR_TITLE) or "",
            "channel": src.get("type", ""),
            "page": src.get("url") or "",
            "verbatim": verbatim[:280],
            "plan": plan or "—",
            "state": c.get("state", ""),
            "link": f"https://app.intercom.com/a/inbox/{app_id}/inbox/conversation/{c.get('id')}",
        })
    return rows


def build_html(rows, meta):
    payload = json.dumps(rows, ensure_ascii=False)
    metaj = json.dumps(meta, ensure_ascii=False)
    return TEMPLATE.replace("/*DATA*/", payload).replace("/*META*/", metaj)


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="profondeur en jours (défaut 7)")
    ap.add_argument("--out", default="rapport.html", help="fichier de sortie")
    args = ap.parse_args()

    token = os.environ.get("INTERCOM_TOKEN")
    if not token or "colle_ton_token" in token:
        sys.exit("INTERCOM_TOKEN absent. Copie .env.example en .env et colle ton token.")
    app_id = os.environ.get("INTERCOM_APP_ID", "hu6d8oic")

    since = int(time.time()) - args.days * 86400
    print(f"Récupération des conversations des {args.days} derniers jours…", file=sys.stderr)
    raw = fetch(token, since)
    rows = normalize(raw, app_id)
    rows.sort(key=lambda r: r["ts"], reverse=True)

    meta = {
        "generated": datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M"),
        "days": args.days,
        "total": len(rows),
    }
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_html(rows, meta))
    print(f"\n✓ Dashboard généré : {out_path}\n  Ouvre-le dans ton navigateur (double-clic).", file=sys.stderr)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cockpit CX — Abby</title>
<style>
:root{color-scheme:light}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:#f7f6f3;color:#1e1a2e;padding:24px;line-height:1.5}
h1{font-size:22px;font-weight:700}
.sub{font-size:13px;color:#7c6e8a;margin-top:2px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:20px 0}
.card{background:#fff;border:1px solid #ece8f0;border-radius:12px;padding:16px}
.card .lab{font-size:12px;color:#7c6e8a}
.card .val{font-size:26px;font-weight:700;margin-top:4px}
.alert{color:#c0392b}.ok{color:#1d9e75}
.layout{display:grid;grid-template-columns:300px 1fr;gap:18px;align-items:start}
@media(max-width:820px){.layout{grid-template-columns:1fr}}
.panel{background:#fff;border:1px solid #ece8f0;border-radius:12px;padding:16px;margin-bottom:16px}
.panel h2{font-size:13px;font-weight:700;margin-bottom:12px;text-transform:uppercase;letter-spacing:.04em;color:#7c6e8a}
.tbar{display:flex;align-items:center;gap:8px;margin-bottom:7px;font-size:12.5px;cursor:pointer}
.tbar:hover .nm{color:#7c6ce0}
.tbar .nm{flex:1;color:#4a4458;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tbar .bar{height:14px;border-radius:3px;background:#cfc6f5;min-width:2px}
.tbar.active .bar{background:#7c6ce0}
.tbar .n{font-weight:700;width:26px;text-align:right}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
select,input[type=text]{font:inherit;font-size:13px;padding:7px 10px;border:1px solid #ddd6e3;border-radius:8px;background:#fff;color:#1e1a2e}
input[type=text]{flex:1;min-width:160px}
button.reset{font:inherit;font-size:13px;padding:7px 12px;border:1px solid #ddd6e3;border-radius:8px;background:#fff;cursor:pointer}
button.reset:hover{background:#f3f0f8}
.conv{border:1px solid #ece8f0;border-radius:10px;padding:13px 15px;margin-bottom:10px}
.conv .top{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:7px}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600}
.b-theme{background:#ede9fb;color:#5a4bbf}
.b-nature{background:#e7f0fb;color:#2c6cb0}
.b-bug{background:#fdecea;color:#c0392b}
.b-esc{background:#fff3e0;color:#b3691a}
.b-neg{background:#fdecea;color:#c0392b}
.b-urg{background:#fde8e8;color:#c0392b}
.conv .vb{font-size:13.5px;color:#3a3450;margin:6px 0}
.conv .meta{font-size:11.5px;color:#9a90ab;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.conv a{color:#7c6ce0;text-decoration:none;font-weight:600}
.conv a:hover{text-decoration:underline}
.count{font-size:13px;color:#7c6e8a;margin-bottom:10px}
.empty{color:#9a90ab;font-size:13px;padding:20px;text-align:center}
</style></head><body>
<h1>Cockpit CX · Abby</h1>
<div class="sub" id="sub"></div>
<div class="cards" id="cards"></div>
<div class="layout">
  <div>
    <div class="panel"><h2>Thèmes</h2><div id="themes"></div></div>
    <div class="panel"><h2>Nature du contact</h2><div id="natures"></div></div>
  </div>
  <div class="panel">
    <div class="controls">
      <input type="text" id="q" placeholder="Rechercher (verbatim, titre)…">
      <select id="fUrg"><option value="">Urgence : toutes</option></select>
      <select id="fEsc"><option value="">Tout</option><option value="1">Escaladé humain</option><option value="0">Résolu par Fin</option></select>
      <select id="fSent"><option value="">Sentiment : tous</option></select>
      <button class="reset" id="reset">Réinitialiser</button>
    </div>
    <div class="count" id="count"></div>
    <div id="list"></div>
  </div>
</div>
<script>
const DATA = /*DATA*/;
const META = /*META*/;
let fTheme="", fNature="";

function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function dt(ts){const d=new Date(ts*1000);return d.toLocaleDateString("fr-FR")+" "+d.toLocaleTimeString("fr-FR",{hour:"2-digit",minute:"2-digit"});}

function filtered(){
  const q=document.getElementById("q").value.toLowerCase();
  const u=document.getElementById("fUrg").value;
  const e=document.getElementById("fEsc").value;
  const s=document.getElementById("fSent").value;
  return DATA.filter(r=>{
    if(fTheme&&r.theme!==fTheme)return false;
    if(fNature&&r.nature!==fNature)return false;
    if(u&&r.urgency!==u)return false;
    if(s&&r.sentiment!==s)return false;
    if(e==="1"&&!r.escalated)return false;
    if(e==="0"&&r.escalated)return false;
    if(q&&!((r.verbatim||"").toLowerCase().includes(q)||(r.title||"").toLowerCase().includes(q)))return false;
    return true;
  });
}

function tally(rows,key){
  const m={};rows.forEach(r=>{m[r[key]]=(m[r[key]]||0)+1});
  return Object.entries(m).sort((a,b)=>b[1]-a[1]);
}

function renderBars(elId,rows,key,sel){
  const t=tally(rows,key),mx=t.length?t[0][1]:1,el=document.getElementById(elId);
  el.innerHTML=t.slice(0,12).map(([k,n])=>
    `<div class="tbar ${sel===k?'active':''}" data-k="${esc(k)}"><span class="nm">${esc(k)}</span>`
    +`<span class="bar" style="width:${Math.round(n/mx*120)}px"></span><span class="n">${n}</span></div>`).join("");
  el.querySelectorAll(".tbar").forEach(b=>b.onclick=()=>{
    const v=b.getAttribute("data-k");
    if(key==="theme")fTheme=fTheme===v?"":v;else fNature=fNature===v?"":v;
    render();
  });
}

function kpis(rows){
  const n=rows.length||1;
  const esc=rows.filter(r=>r.escalated).length;
  const bug=rows.filter(r=>r.nature.toLowerCase().includes("bug")).length;
  const neg=rows.filter(r=>r.sentiment.toLowerCase().startsWith("neg")||r.sentiment.toLowerCase().startsWith("nég")).length;
  document.getElementById("cards").innerHTML=
    card("Conversations",rows.length,"")+
    card("Résolu par Fin",Math.round((1-esc/n)*100)+"%","ok")+
    card("Escaladé humain",esc,"alert")+
    card("Qualifié bug",Math.round(bug/n*100)+"%","")+
    card("Sentiment négatif",Math.round(neg/n*100)+"%","");
}
function card(l,v,c){return `<div class="card"><div class="lab">${l}</div><div class="val ${c}">${v}</div></div>`;}

function convCard(r){
  const b=[];
  b.push(`<span class="badge b-theme">${esc(r.theme)}</span>`);
  if(r.nature&&r.nature!=="(non qualifié)")b.push(`<span class="badge ${r.nature.toLowerCase().includes("bug")?'b-bug':'b-nature'}">${esc(r.nature)}</span>`);
  if(r.escalated)b.push(`<span class="badge b-esc">escaladé</span>`);
  if(r.urgency==="High")b.push(`<span class="badge b-urg">urgence haute</span>`);
  if(r.sentiment.toLowerCase().startsWith("neg")||r.sentiment.toLowerCase().startsWith("nég"))b.push(`<span class="badge b-neg">négatif</span>`);
  const vb=r.verbatim?`<div class="vb">"${esc(r.verbatim)}"</div>`:"";
  const pg=r.page?` · <span>${esc(r.page.replace("https://app.abby.fr",""))}</span>`:"";
  return `<div class="conv"><div class="top">${b.join("")}</div>${vb}`
    +`<div class="meta"><a href="${r.link}" target="_blank">Ouvrir dans Intercom ↗</a> · ${dt(r.ts)} · ${esc(r.plan)} · ${esc(r.channel)}${pg}</div></div>`;
}

function render(){
  const rows=filtered();
  kpis(rows);
  renderBars("themes",DATA,"theme",fTheme);
  renderBars("natures",DATA,"nature",fNature);
  document.getElementById("count").textContent=rows.length+" conversation(s)"+
    (fTheme?` · thème : ${fTheme}`:"")+(fNature?` · nature : ${fNature}`:"");
  const list=document.getElementById("list");
  if(!rows.length){list.innerHTML='<div class="empty">Aucune conversation pour ces filtres.</div>';return;}
  list.innerHTML=rows.slice(0,400).map(convCard).join("")+
    (rows.length>400?'<div class="empty">… 400 premières affichées. Affine les filtres.</div>':"");
}

function fillSelect(id,key){
  const vals=[...new Set(DATA.map(r=>r[key]).filter(v=>v&&v!=="—"))].sort();
  const sel=document.getElementById(id);
  vals.forEach(v=>{const o=document.createElement("option");o.value=v;o.textContent=v;sel.appendChild(o);});
}

document.getElementById("sub").textContent=`${META.total} conversations · ${META.days} derniers jours · généré le ${META.generated}`;
fillSelect("fUrg","urgency");fillSelect("fSent","sentiment");
["q","fUrg","fEsc","fSent"].forEach(id=>document.getElementById(id).addEventListener("input",render));
document.getElementById("reset").onclick=()=>{fTheme="";fNature="";document.getElementById("q").value="";
  ["fUrg","fEsc","fSent"].forEach(id=>document.getElementById(id).value="");render();};
render();
</script></body></html>
"""

if __name__ == "__main__":
    main()
