#!/usr/bin/env python3
"""
Tendances CX Abby — agrège l'historique Intercom et génère un dashboard HTML
des tendances semaine par semaine et mois par mois (volume, thèmes exacts,
taux de conversations non qualifiées).

Contrairement à build.py (qui montre le détail des dernières conversations),
ce script pagine TOUTE la période demandée et n'embarque dans le HTML que des
chiffres agrégés — léger, même sur des dizaines de milliers de conversations.

Usage :
    python trends.py                       # depuis le 1er janvier 2026 (défaut)
    python trends.py --since 2026-03-01    # depuis une autre date
    python trends.py --out tendances.html

Le token est lu depuis INTERCOM_TOKEN ou depuis le fichier .env (voir .env.example).
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    sys.exit("Module 'requests' manquant. Lance : pip install -r requirements.txt")

API = "https://api.intercom.io"
ATTR_THEME = "Thème de la demande"
NON_QUAL = "(non qualifié)"
TOP_THEMES = 12  # nombre de thèmes affichés dans la heatmap (le reste = « Autres thèmes »)


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


def week_key(dt):
    """Lundi de la semaine (YYYY-MM-DD), en UTC."""
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def month_key(dt):
    return dt.strftime("%Y-%m")


def aggregate(convs):
    """Construit les structures agrégées par semaine et par mois."""
    # comptage global des thèmes pour déterminer le top N
    theme_total = defaultdict(int)
    for c in convs:
        ca = c.get("custom_attributes", {}) or {}
        theme_total[ca.get(ATTR_THEME) or NON_QUAL] += 1

    # top thèmes hors non-qualifié
    ranked = sorted(
        ((t, n) for t, n in theme_total.items() if t != NON_QUAL),
        key=lambda x: x[1], reverse=True,
    )
    top = [t for t, _ in ranked[:TOP_THEMES]]
    # ordre d'affichage : top thèmes, puis "Autres thèmes", puis non-qualifié
    theme_order = top + ["Autres thèmes", NON_QUAL]

    def blank_period():
        return {"total": 0, "nonqual": 0, "themes": defaultdict(int)}

    weeks = defaultdict(blank_period)
    months = defaultdict(blank_period)

    for c in convs:
        ts = c.get("created_at", 0)
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        ca = c.get("custom_attributes", {}) or {}
        raw = ca.get(ATTR_THEME) or NON_QUAL
        if raw == NON_QUAL:
            bucket = NON_QUAL
        elif raw in top:
            bucket = raw
        else:
            bucket = "Autres thèmes"

        for store, key in ((weeks, week_key(dt)), (months, month_key(dt))):
            p = store[key]
            p["total"] += 1
            if raw == NON_QUAL:
                p["nonqual"] += 1
            p["themes"][bucket] += 1

    def serialize(store, label_fn):
        out = []
        for key in sorted(store.keys()):
            p = store[key]
            out.append({
                "key": key,
                "label": label_fn(key),
                "total": p["total"],
                "nonqual": p["nonqual"],
                "themes": {t: p["themes"].get(t, 0) for t in theme_order},
            })
        return out

    def week_label(k):
        d = datetime.strptime(k, "%Y-%m-%d")
        return f"{d.day:02d}/{d.month:02d}"

    MOIS = ["", "janv", "févr", "mars", "avr", "mai", "juin",
            "juil", "août", "sept", "oct", "nov", "déc"]

    def month_label(k):
        y, m = k.split("-")
        return f"{MOIS[int(m)]} {y[2:]}"

    return {
        "themeOrder": theme_order,
        "weeks": serialize(weeks, week_label),
        "months": serialize(months, month_label),
    }


def build_html(agg, meta):
    return (TEMPLATE
            .replace("/*DATA*/", json.dumps(agg, ensure_ascii=False))
            .replace("/*META*/", json.dumps(meta, ensure_ascii=False)))


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-01-01", help="date de départ AAAA-MM-JJ (défaut 2026-01-01)")
    ap.add_argument("--out", default="tendances.html", help="fichier de sortie")
    args = ap.parse_args()

    token = os.environ.get("INTERCOM_TOKEN")
    if not token or "colle_ton_token" in token:
        sys.exit("INTERCOM_TOKEN absent. Copie .env.example en .env et colle ton token.")

    try:
        since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        sys.exit("Format de --since invalide. Attendu : AAAA-MM-JJ (ex. 2026-01-01).")
    since_ts = int(since_dt.timestamp())

    print(f"Récupération des conversations depuis le {args.since} (ça peut prendre quelques minutes)…", file=sys.stderr)
    raw = fetch(token, since_ts)
    print(f"  → {len(raw)} conversations récupérées. Agrégation…", file=sys.stderr)
    agg = aggregate(raw)

    meta = {
        "generated": datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M"),
        "since": args.since,
        "total": len(raw),
    }
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_html(agg, meta))
    print(f"\n✓ Dashboard tendances généré : {out_path}\n  Ouvre-le dans ton navigateur (double-clic).", file=sys.stderr)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tendances CX — Abby</title>
<style>
:root{color-scheme:light}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:#f7f6f3;color:#1e1a2e;padding:24px;line-height:1.5}
h1{font-size:22px;font-weight:700}
.sub{font-size:13px;color:#7c6e8a;margin-top:2px}
.toggle{display:inline-flex;background:#ece8f0;border-radius:10px;padding:3px;margin:18px 0}
.toggle button{border:none;background:transparent;padding:7px 18px;font-size:13px;font-weight:700;border-radius:8px;cursor:pointer;color:#6b6478}
.toggle button.on{background:#fff;color:#5a3ec8;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:8px 0 20px}
.card{background:#fff;border:1px solid #ece8f0;border-radius:12px;padding:16px}
.card .lab{font-size:12px;color:#7c6e8a}
.card .val{font-size:26px;font-weight:700;margin-top:4px}
.delta{font-size:12.5px;font-weight:700;margin-top:2px}
.delta.up{color:#c0392b}.delta.down{color:#1d9e75}
.panel{background:#fff;border:1px solid #ece8f0;border-radius:12px;padding:18px;margin-bottom:16px}
.panel h2{font-size:13px;font-weight:700;margin-bottom:14px;text-transform:uppercase;letter-spacing:.04em;color:#7c6e8a}
.vol{display:flex;align-items:flex-end;gap:4px;height:180px;overflow-x:auto;padding-bottom:4px}
.vcol{display:flex;flex-direction:column;align-items:center;justify-content:flex-end;min-width:34px;flex:1}
.vcol .vbar{width:70%;background:#cfc6f5;border-radius:3px 3px 0 0;min-height:2px}
.vcol.cur .vbar{background:#7c6ce0}
.vcol .vn{font-size:10.5px;color:#7c6e8a;margin-bottom:3px}
.vcol .vl{font-size:10.5px;color:#9a90ab;margin-top:4px;white-space:nowrap}
.hint{font-size:12px;color:#9a90ab;margin-top:8px}
.hmwrap{overflow-x:auto}
table.hm{border-collapse:collapse;font-size:12px;min-width:100%}
table.hm th,table.hm td{padding:5px 7px;text-align:center;white-space:nowrap}
table.hm th.row,table.hm td.row{text-align:left;position:sticky;left:0;background:#fff;color:#4a4458;max-width:230px;overflow:hidden;text-overflow:ellipsis}
table.hm thead th{color:#9a90ab;font-weight:600;border-bottom:1px solid #ece8f0}
table.hm td.cell{color:#2c2c2a;border-radius:3px}
table.hm tr.nq td.row{color:#b06a00;font-weight:600}
.legend{font-size:12px;color:#7c6e8a;margin-top:10px;display:flex;align-items:center;gap:8px}
.legend .sw{display:inline-flex;gap:2px}
.legend .sw span{width:16px;height:12px;border-radius:2px}
</style></head><body>
<h1>Tendances CX · Abby</h1>
<div class="sub" id="sub"></div>

<div class="toggle">
  <button id="b-week" class="on">Par semaine</button>
  <button id="b-month">Par mois</button>
</div>

<div class="cards" id="cards"></div>

<div class="panel">
  <h2 id="vol-title">Volume par semaine</h2>
  <div class="vol" id="vol"></div>
  <div class="hint">La dernière colonne est la période en cours (souvent incomplète).</div>
</div>

<div class="panel">
  <h2>Thèmes par période — nombre exact de conversations</h2>
  <div class="hmwrap"><table class="hm" id="hm"></table></div>
  <div class="legend">Intensité = volume relatif. <span class="sw" id="lg"></span> faible → fort. Ligne orange = conversations non qualifiées (taggage manquant).</div>
</div>

<script>
const DATA=/*DATA*/;
const META=/*META*/;
let mode="week";

function periods(){return mode==="week"?DATA.weeks:DATA.months;}
function heat(v,mx){ // v/mx -> couleur violette claire->foncée
  if(!v)return "#f7f6f3";
  const t=Math.min(1,v/mx);
  const r=Math.round(247-(247-90)*t),g=Math.round(246-(246-76)*t),b=Math.round(243-(243-224)*t);
  return `rgb(${r},${g},${b})`;
}
function txtColor(v,mx){return (v/mx)>0.55?"#fff":"#2c2c2a";}

function renderCards(){
  const p=periods();
  const cur=p[p.length-1]||{total:0,nonqual:0};
  const prev=p[p.length-2]||{total:0,nonqual:0};
  const full=p.slice(0,-1);
  const avg=full.length?Math.round(full.reduce((a,x)=>a+x.total,0)/full.length):0;
  const nqTot=p.reduce((a,x)=>a+x.nonqual,0),tot=p.reduce((a,x)=>a+x.total,0)||1;
  let d="";
  if(prev.total){const pc=Math.round((cur.total-prev.total)/prev.total*100);
    d=`<div class="delta ${pc>=0?'up':'down'}">${pc>=0?'▲ +':'▼ '}${pc}% vs ${mode==="week"?'sem.':'mois'} préc.</div>`;}
  document.getElementById("cards").innerHTML=
    `<div class="card"><div class="lab">Total depuis ${META.since}</div><div class="val">${META.total}</div></div>`
    +`<div class="card"><div class="lab">${mode==="week"?'Semaine':'Mois'} en cours</div><div class="val">${cur.total}</div>${d}</div>`
    +`<div class="card"><div class="lab">Moyenne / ${mode==="week"?'semaine':'mois'}</div><div class="val">${avg}</div></div>`
    +`<div class="card"><div class="lab">Non qualifié (global)</div><div class="val">${Math.round(nqTot/tot*100)}%</div></div>`;
}

function renderVol(){
  const p=periods(),mx=Math.max(...p.map(x=>x.total),1),el=document.getElementById("vol");
  el.innerHTML=p.map((x,i)=>
    `<div class="vcol ${i===p.length-1?'cur':''}" title="${x.label} : ${x.total} conv.">`
    +`<span class="vn">${x.total}</span>`
    +`<div class="vbar" style="height:${Math.round(x.total/mx*150)}px"></div>`
    +`<span class="vl">${x.label}</span></div>`).join("");
  document.getElementById("vol-title").textContent=mode==="week"?"Volume par semaine":"Volume par mois";
}

function renderHeat(){
  const p=periods(),order=DATA.themeOrder;
  // max par ligne (thème) pour l'intensité, calculé sur l'ensemble des périodes
  const rowMax={};order.forEach(t=>{rowMax[t]=Math.max(...p.map(x=>x.themes[t]||0),1);});
  let head="<thead><tr><th class='row'>Thème \\ "+(mode==="week"?"semaine":"mois")+"</th>"
    +p.map(x=>`<th>${x.label}</th>`).join("")+"</tr></thead>";
  let body="<tbody>"+order.map(t=>{
    const isNq=t==="(non qualifié)";
    const cells=p.map(x=>{const v=x.themes[t]||0;
      return `<td class="cell" style="background:${heat(v,rowMax[t])};color:${txtColor(v,rowMax[t])}">${v||""}</td>`;}).join("");
    return `<tr class="${isNq?'nq':''}"><td class="row" title="${t}">${t}</td>${cells}</tr>`;
  }).join("")+"</tbody>";
  document.getElementById("hm").innerHTML=head+body;
}

function render(){renderCards();renderVol();renderHeat();}

function setMode(m){mode=m;
  document.getElementById("b-week").classList.toggle("on",m==="week");
  document.getElementById("b-month").classList.toggle("on",m==="month");
  render();}

document.getElementById("sub").textContent=`${META.total} conversations · depuis le ${META.since} · généré le ${META.generated}`;
document.getElementById("lg").innerHTML=[0,.25,.5,.75,1].map(t=>`<span style="background:${heat(t*10,10)}"></span>`).join("");
document.getElementById("b-week").onclick=()=>setMode("week");
document.getElementById("b-month").onclick=()=>setMode("month");
render();
</script></body></html>
"""

if __name__ == "__main__":
    main()
