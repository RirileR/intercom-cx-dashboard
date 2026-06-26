#!/usr/bin/env python3
"""
Cockpit CX Abby — version AUTO (multi-semaines, auto-mise à jour).

Pagine l'historique Intercom, agrège semaine par semaine, et génère un cockpit
HTML autonome : graphique de volume, sélecteur de semaine, et comparaison de
deux semaines côte à côte. Chaque semaine, le robot GitHub relance ce script,
les chiffres se mettent à jour tout seuls et la page est republiée.

Métriques calculées automatiquement et de façon fiable par semaine :
  - volume exact (nb de conversations)
  - répartition jour par jour
  - top thèmes exacts (attribut « Thème de la demande »)
  - % de bugs (attribut « Typologie du contact » contenant "bug")
  - % de sentiment négatif (attribut « Sentiment »)
  - % en chat vs e-mail
  - taux de conversations non qualifiées (taggage manquant)

Usage :
    python cockpit_auto.py                 # ~10 dernières semaines
    python cockpit_auto.py --weeks 12
    python cockpit_auto.py --out index.html
"""

import argparse
import json
import os
import re
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
ATTR_NATURE = "Typologie du contact"
ATTR_SENTIMENT = "Sentiment"
NON_QUAL = "(non qualifié)"
TOP_THEMES = 8
JOURS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
MOIS = ["", "janv", "févr", "mars", "avr", "mai", "juin",
        "juil", "août", "sept", "oct", "nov", "déc"]

# Mots vides FR + bruit, ignorés par le radar à incidents
STOP = set("""
le la les un une des du de da au aux et ou ni mais donc or car que qui quoi dont ou
a as ai ont est sont été être avoir fait faire pour par sur sous avec sans dans en
ce cet cette ces mon ma mes ton ta tes son sa ses notre nos votre vos leur leurs
je tu il elle on nous vous ils elles me te se y lui leur moi toi soi
ne pas plus moins tres très bien mal si comme aussi alors quand comment pourquoi
mon abby bonjour merci salut svp stp cordialement madame monsieur
suite demande question probleme problème souci besoin aide help info impossible
mes mon avoir avez avons puis peux peut faut
""".split())


def load_env():
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
            sys.exit("Token refusé (401). Vérifie INTERCOM_TOKEN.")
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


def monday_utc(dt):
    m = dt - timedelta(days=dt.weekday())
    return datetime(m.year, m.month, m.day, tzinfo=timezone.utc)


def pct(n, d):
    return round(n / d * 100) if d else 0


def aggregate(convs, weeks_n):
    # top thèmes globaux (hors non-qualifié)
    theme_total = defaultdict(int)
    for c in convs:
        ca = c.get("custom_attributes", {}) or {}
        theme_total[ca.get(ATTR_THEME) or NON_QUAL] += 1
    ranked = sorted(((t, n) for t, n in theme_total.items() if t != NON_QUAL),
                    key=lambda x: x[1], reverse=True)
    top = [t for t, _ in ranked[:TOP_THEMES]]

    # bornes des N dernières semaines (lundi)
    now = datetime.now(timezone.utc)
    this_mon = monday_utc(now)
    starts = [this_mon - timedelta(days=7 * i) for i in range(weeks_n)][::-1]
    keys = [s.strftime("%Y-%m-%d") for s in starts]

    def blank():
        return {"total": 0, "nonqual": 0, "bug": 0, "neg": 0, "chat": 0,
                "themes": defaultdict(int), "daily": [0] * 7,
                # vue « charge humaine » (conversations ouvertes / traitées par un humain)
                "hum": 0, "hum_bug": 0, "hum_themes": defaultdict(int),
                # radar incidents : compte des bigrammes de titres
                "bigrams": defaultdict(int)}
    W = {k: blank() for k in keys}

    def is_bug(nature):
        return "bug" in nature

    def title_bigrams(text):
        toks = [w for w in re.findall(r"[0-9a-zàâäéèêëîïôöùûüç]+", (text or "").lower())
                if len(w) >= 3 and w not in STOP]
        return [f"{toks[i]} {toks[i+1]}" for i in range(len(toks) - 1)]

    for c in convs:
        ts = c.get("created_at", 0)
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        k = monday_utc(dt).strftime("%Y-%m-%d")
        if k not in W:
            continue
        p = W[k]
        ca = c.get("custom_attributes", {}) or {}
        theme = ca.get(ATTR_THEME) or NON_QUAL
        nature = (ca.get(ATTR_NATURE) or "").lower()
        senti = (ca.get(ATTR_SENTIMENT) or "").lower()
        stype = ((c.get("source") or {}).get("type") or "").lower()
        stats = c.get("statistics") or {}
        # « charge humaine » = un agent humain a réellement répondu.
        # (On n'utilise PAS admin_assignee_id ni open : le bot "operator" est
        #  auto-assigné à presque tout, ce qui fausserait la mesure.)
        human = bool(stats.get("first_admin_reply_at"))
        title = ca.get("AI Title") or c.get("title") or ""

        p["total"] += 1
        p["daily"][dt.weekday()] += 1
        if theme == NON_QUAL:
            p["nonqual"] += 1
            p["themes"]["(non qualifié)"] += 1
        else:
            p["themes"][theme if theme in top else "Autres"] += 1
        if is_bug(nature):
            p["bug"] += 1
        if senti.startswith("nég") or senti.startswith("neg") or "négatif" in senti:
            p["neg"] += 1
        if stype in ("conversation", "chat"):
            p["chat"] += 1
        if human:
            p["hum"] += 1
            if is_bug(nature):
                p["hum_bug"] += 1
            lab = theme if (theme != NON_QUAL and theme in top) else (
                "Autres" if theme != NON_QUAL else "(non qualifié)")
            p["hum_themes"][lab] += 1
        for bg in set(title_bigrams(title)):   # set : 1 conv = 1 vote par bigramme
            p["bigrams"][bg] += 1

    # médiane hebdo de chaque bigramme, pour repérer les pics (« sujet émergent »)
    all_bg = set()
    for p in W.values():
        all_bg.update(p["bigrams"].keys())

    def median(vals):
        s = sorted(vals)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    bg_median = {}
    for bg in all_bg:
        bg_median[bg] = median([W[k]["bigrams"].get(bg, 0) for k in keys])

    weeks = []
    last_idx = len(keys) - 1
    for i, k in enumerate(keys):
        p = W[k]
        t = p["total"]
        d = datetime.strptime(k, "%Y-%m-%d")
        themes = sorted(((name, n) for name, n in p["themes"].items() if name != "(non qualifié)"),
                        key=lambda x: x[1], reverse=True)
        hum_themes = sorted(((name, n) for name, n in p["hum_themes"].items()),
                            key=lambda x: x[1], reverse=True)
        bugp, negp = pct(p["bug"], t), pct(p["neg"], t)
        hum_bug_pct = pct(p["hum_bug"], p["hum"])

        # radar : bigrammes les plus fréquents de la semaine (≥ 4 conv), avec drapeau « pic »
        radar = []
        for bg, n in sorted(p["bigrams"].items(), key=lambda x: x[1], reverse=True):
            if n < 4:
                continue
            med = bg_median.get(bg, 0)
            spike = n >= 4 and n >= 2 * (med if med else 0.5)
            radar.append({"label": bg, "n": n, "spike": bool(spike)})
            if len(radar) >= 8:
                break

        partial = (i == last_idx)
        if partial:
            wtype = "partial"
        elif hum_bug_pct >= 35 or negp >= 15:
            wtype = "peak"
        else:
            wtype = "norm"
        weeks.append({
            "key": k,
            "label": f"{d.day} {MOIS[d.month]}",
            "total": t,
            "type": wtype,
            "partial": partial,
            "daily": {"labels": [f"{j} {(d + timedelta(days=n)).day}" for n, j in enumerate(JOURS)],
                      "values": p["daily"]},
            "bug": bugp, "neg": negp,
            "chat": pct(p["chat"], t), "nonqual": pct(p["nonqual"], t),
            "themes": [[name, n] for name, n in themes[:TOP_THEMES]],
            "humTotal": p["hum"], "humBug": hum_bug_pct,
            "humThemes": [[name, n] for name, n in hum_themes[:6]],
            "radar": radar,
        })
    return weeks


def build_html(weeks, meta):
    return (TEMPLATE
            .replace("/*DATA*/", json.dumps(weeks, ensure_ascii=False))
            .replace("/*META*/", json.dumps(meta, ensure_ascii=False)))


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=10, help="nombre de semaines (défaut 10)")
    ap.add_argument("--out", default="index.html", help="fichier de sortie")
    args = ap.parse_args()

    token = os.environ.get("INTERCOM_TOKEN")
    if not token or "colle_ton_token" in token:
        sys.exit("INTERCOM_TOKEN absent.")

    now = datetime.now(timezone.utc)
    since = monday_utc(now) - timedelta(days=7 * (args.weeks - 1))
    print(f"Récupération depuis le {since.date()} ({args.weeks} semaines)…", file=sys.stderr)
    raw = fetch(token, int(since.timestamp()))
    print(f"  → {len(raw)} conversations. Agrégation…", file=sys.stderr)
    weeks = aggregate(raw, args.weeks)
    meta = {"generated": now.astimezone().strftime("%d/%m/%Y %H:%M"), "weeks": args.weeks}

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_html(weeks, meta))
    print(f"\n✓ Cockpit généré : {out_path}", file=sys.stderr)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cockpit CX Abby — toutes les semaines</title>
<style>
:root{color-scheme:light}*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:#f7f6f3;color:#1e1a2e;line-height:1.5}
.wrap{max-width:1000px;margin:0 auto;padding:24px 20px 60px}
header h1{font-size:22px;font-weight:600}header .sub{font-size:13px;color:#7c6e8a;margin-top:3px}
.tabs{display:inline-flex;background:#ece8f0;border-radius:10px;padding:3px;margin:20px 0 4px}
.tabs button{border:none;background:transparent;padding:8px 18px;font-size:13px;font-weight:600;border-radius:8px;cursor:pointer;color:#6b6478}
.tabs button.on{background:#fff;color:#5a3ec8;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.panel{background:#fff;border:1px solid #ece8f0;border-radius:12px;padding:20px;margin-top:16px}
.panel h2{font-size:15px;font-weight:600;margin-bottom:4px}.panel .ph{font-size:12px;color:#9a90ab;margin-bottom:16px}
.chart{display:flex;align-items:flex-end;gap:14px;height:240px;padding:10px 4px 0}
.col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;cursor:pointer}
.col .bar{width:100%;max-width:60px;border-radius:6px 6px 0 0;background:#b9aef0;transition:background .15s}
.col:hover .bar{background:#7c6ce0}.col.peak .bar{background:#c0392b}.col.sel .bar{background:#5a3ec8}
.col.partial .bar{background:repeating-linear-gradient(45deg,#cfc6ef,#cfc6ef 6px,#e2dcf5 6px,#e2dcf5 12px)}
.col .v{font-size:12px;font-weight:600;margin-bottom:5px}.col .l{font-size:11px;color:#7c6e8a;margin-top:7px;text-align:center}
.legend{font-size:11px;color:#9a90ab;margin-top:14px;display:flex;gap:16px;flex-wrap:wrap}
.legend span::before{content:'';display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:middle}
.legend .l-peak::before{background:#c0392b}.legend .l-norm::before{background:#b9aef0}.legend .l-part::before{background:#cfc6ef}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:4px 0 8px}
.kc{background:#faf9fc;border:1px solid #ece8f0;border-radius:10px;padding:14px}
.kc .lab{font-size:11px;color:#7c6e8a}.kc .val{font-size:24px;font-weight:600;margin-top:3px}.kc .val.alert{color:#c0392b}.kc .val.ok{color:#1d9e75}
.kc .hint{font-size:10.5px;color:#9a90ab;margin-top:3px}
.row{display:flex;align-items:center;gap:10px;margin-bottom:7px;font-size:13px}
.row .name{width:200px;flex-shrink:0;color:#4a4458}.row .bar2{height:15px;border-radius:4px;background:#7c6ce0}.row .n{font-weight:600;min-width:26px}
.note{font-size:12.5px;color:#6b5e78;background:#faf9fc;border:1px solid #ece8f0;border-radius:8px;padding:12px 14px;margin-top:10px}
.sect-t{font-size:12px;font-weight:600;color:#5a3ec8;text-transform:uppercase;letter-spacing:.4px;margin:18px 0 8px}
.selectors{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.selectors select{font-size:13px;padding:8px 12px;border-radius:8px;border:1px solid #d8d2e2;background:#fff;color:#1e1a2e}
.cmp{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.cmp .cc{border-radius:10px;padding:16px;border:1px solid #ece8f0}.cmp .cc h3{font-size:14px;font-weight:600;margin-bottom:10px}
.cmp .crow{display:flex;justify-content:space-between;font-size:12.5px;padding:5px 0;border-bottom:1px solid #f0edf4}
.cmp .crow b{color:#1e1a2e}.cmp .crow span{color:#6b5e78}
.badge{display:inline-block;font-size:10.5px;font-weight:600;padding:2px 8px;border-radius:20px}
.b-tech{background:#fdf3f2;color:#c0392b}.b-norm{background:#f0edf4;color:#5a3ec8}.b-part{background:#fef6e7;color:#b06a00}
@media (max-width:680px){.cmp{grid-template-columns:1fr}.row .name{width:120px}.chart{gap:6px}}
.hide{display:none}
</style></head><body><div class="wrap">
<header><h1>Cockpit CX · Abby</h1>
<div class="sub" id="sub"></div></header>
<div class="tabs"><button id="t-vue" class="on" onclick="tab('vue')">Vue d'ensemble</button>
<button id="t-cmp" onclick="tab('cmp')">Comparer 2 semaines</button></div>
<div id="vue">
  <div class="panel"><h2>Volume par semaine</h2>
    <div class="ph">Clique sur une barre, ou choisis la semaine dans le menu ci-dessous.</div>
    <div class="chart" id="chart"></div>
    <div class="legend"><span class="l-norm">Semaine normale</span><span class="l-peak">Tension technique (backlog bugs ≥ 35 % ou négatif ≥ 15 %)</span><span class="l-part">Semaine en cours (incomplète)</span></div>
  </div>
  <div class="panel" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <label for="weekSel" style="font-size:13px;font-weight:600;color:#4a4458">Semaine à détailler :</label>
    <select id="weekSel" onchange="onSel()"></select></div>
  <div id="detail"></div>
</div>
<div id="cmp" class="hide"><div class="panel"><h2>Comparer deux semaines</h2>
  <div class="ph">Pour montrer que deux semaines chargées peuvent avoir des causes opposées.</div>
  <div class="selectors"><select id="selA" onchange="renderCmp()"></select><select id="selB" onchange="renderCmp()"></select></div>
  <div class="cmp" id="cmpBody"></div></div></div>
</div>
<script>
const WEEKS=/*DATA*/;const META=/*META*/;
const $=s=>document.querySelector(s);
const both=WEEKS.filter(w=>w.type==='peak'||w.partial).slice(-2).map(w=>w.key);
const KEYS_BOTH=both.length?both:WEEKS.slice(-2).map(w=>w.key);
let selected='both';
function tab(n){$('#vue').classList.toggle('hide',n!=='vue');$('#cmp').classList.toggle('hide',n!=='cmp');
  $('#t-vue').classList.toggle('on',n==='vue');$('#t-cmp').classList.toggle('on',n==='cmp');}
function isSel(k){return selected==='both'?KEYS_BOTH.includes(k):selected===k;}
function renderChart(){const mx=Math.max.apply(null,WEEKS.map(w=>w.total));
  $('#chart').innerHTML=WEEKS.map(w=>{const h=Math.round(w.total/mx*180);
    const cls=['col',w.type==='peak'?'peak':'',w.type==='partial'?'partial':'',isSel(w.key)?'sel':''].join(' ');
    return `<div class="${cls}" onclick="selectWeek('${w.key}')"><div class="v">${w.total.toLocaleString('fr-FR')}</div><div class="bar" style="height:${h}px"></div><div class="l">${w.label}</div></div>`;}).join('');}
function selectWeek(k){selected=k;$('#weekSel').value=k;renderChart();renderDetail();}
function onSel(){selected=$('#weekSel').value;renderChart();renderDetail();}
function fillWeekSel(){let o=`<option value="both">⭐ Les 2 semaines marquantes</option>`;
  o+=WEEKS.slice().reverse().map(w=>`<option value="${w.key}">Semaine du ${w.label}</option>`).join('');
  $('#weekSel').innerHTML=o;$('#weekSel').value=selected;}
function bars(arr){const mx=Math.max.apply(null,arr.map(r=>r[1]))||1;
  return arr.map(r=>`<div class="row"><span class="name">${r[0]}</span><span class="bar2" style="width:${Math.round(r[1]/mx*200)}px"></span><span class="n">${r[1]}</span></div>`).join('');}
function autoNote(w){
  if(w.humBug>=35||w.neg>=15)return `<b>Tension technique</b> : ${w.bug}% de bugs sur tout le volume, mais <b>${w.humBug}% de la charge humaine</b> (conversations escaladées) sont des bugs, et ${w.neg}% de sentiment négatif. Action : corriger le produit et communiquer sur les incidents.`;
  if(w.bug<15&&w.humBug<25)return `Peu de bugs (${w.bug}% global, ${w.humBug}% du backlog) : volume tiré par des <b>questions d'usage</b>. Action : FAQ, articles d'aide, pédagogie.`;
  return `Semaine mixte : ${w.bug}% de bugs (global), ${w.humBug}% du backlog, ${w.neg}% de sentiment négatif.`;}
function badge(w){if(w.partial)return '<span class="badge b-part">en cours</span>';
  if(w.type==='peak')return '<span class="badge b-tech">tension technique</span>';return '<span class="badge b-norm">normale</span>';}
function weekPanel(w){
  let h=`<div class="panel"><h2>Semaine du ${w.label} — ${w.total.toLocaleString('fr-FR')} conversations ${badge(w)}</h2>`;
  h+=`<div class="ph">${w.partial?'Semaine en cours (chiffres partiels).':'Données Intercom.'}</div>`;
  h+=`<div class="cards">`+
    `<div class="kc"><div class="lab">Part de bugs</div><div class="val ${w.bug>=25?'alert':(w.bug<15?'ok':'')}">${w.bug} %</div></div>`+
    `<div class="kc"><div class="lab">Sentiment négatif</div><div class="val ${w.neg>=15?'alert':(w.neg<6?'ok':'')}">${w.neg} %</div></div>`+
    `<div class="kc"><div class="lab">Part en chat</div><div class="val">${w.chat} %</div></div>`+
    `<div class="kc"><div class="lab">Non qualifié</div><div class="val">${w.nonqual} %</div><div class="hint">taggage manquant</div></div>`+
    `</div>`;
  const mx=Math.max.apply(null,w.daily.values)||1;
  h+=`<div class="sect-t">Volume jour par jour</div><div class="chart" style="height:160px">`+
    w.daily.labels.map((l,i)=>{const v=w.daily.values[i];const pk=v===mx&&v>0;
      return `<div class="col ${pk?'peak':''}"><div class="v">${v}</div><div class="bar" style="height:${Math.round(v/mx*120)}px"></div><div class="l">${l}</div></div>`;}).join('')+`</div>`;
  if(w.themes.length){h+=`<div class="sect-t">Top thèmes (tout le volume)</div>`+bars(w.themes);}
  // --- Vue charge humaine / backlog ---
  h+=`<div class="sect-t">Charge humaine — conversations où un agent a répondu</div>`;
  h+=`<div class="ph" style="margin:-4px 0 10px">${w.humTotal.toLocaleString('fr-FR')} conversations ont mobilisé un agent humain (Fin a géré le reste seul). Voilà de quoi elles sont faites — c'est ici que les bugs ressortent.</div>`;
  h+=`<div class="cards"><div class="kc"><div class="lab">Bugs dans le backlog</div><div class="val ${w.humBug>=35?'alert':(w.humBug<20?'ok':'')}">${w.humBug} %</div><div class="hint">vs ${w.bug}% sur tout le volume</div></div></div>`;
  if(w.humThemes.length){h+=bars(w.humThemes);}
  // --- Radar à incidents ---
  h+=`<div class="sect-t">Radar à incidents — sujets récurrents de la semaine</div>`;
  if(w.radar&&w.radar.length){
    h+=`<div style="display:flex;flex-wrap:wrap;gap:8px">`+w.radar.map(r=>{
      const c=r.spike?'background:#fdf3f2;border:1px solid #f3d9d6;color:#c0392b':'background:#faf9fc;border:1px solid #ece8f0;color:#4a4458';
      return `<span style="font-size:12.5px;padding:5px 11px;border-radius:20px;${c}">${r.spike?'🔺 ':''}${r.label} <b>· ${r.n}</b></span>`;}).join('')+`</div>`;
    h+=`<div class="ph" style="margin-top:8px">🔺 = sujet en forte hausse vs les autres semaines (incident probable). Détecté sur les titres des conversations.</div>`;
  } else { h+=`<div class="ph">Aucun sujet ne ressort particulièrement cette semaine.</div>`; }
  h+=`<div class="note">${autoNote(w)}</div></div>`;return h;}
function renderDetail(){
  if(selected==='both'){$('#detail').innerHTML=KEYS_BOTH.map(k=>weekPanel(WEEKS.find(w=>w.key===k))).join('');}
  else{$('#detail').innerHTML=weekPanel(WEEKS.find(w=>w.key===selected));}}
function fillSelectors(){const o=WEEKS.map(w=>`<option value="${w.key}">Semaine du ${w.label}</option>`).join('');
  $('#selA').innerHTML=o;$('#selB').innerHTML=o;
  $('#selA').value=KEYS_BOTH[0];$('#selB').value=KEYS_BOTH[KEYS_BOTH.length-1];}
function cmpCard(w){const inc=(w.radar||[]).filter(r=>r.spike).slice(0,2).map(r=>r.label).join(', ')||'—';
  let rows=`<div class="crow"><span>Volume</span><b>${w.total.toLocaleString('fr-FR')}</b></div>`+
  `<div class="crow"><span>Bugs (tout le volume)</span><b>${w.bug} %</b></div>`+
  `<div class="crow"><span>Bugs (charge humaine)</span><b>${w.humBug} %</b></div>`+
  `<div class="crow"><span>Sentiment négatif</span><b>${w.neg} %</b></div>`+
  `<div class="crow"><span>Part en chat</span><b>${w.chat} %</b></div>`+
  `<div class="crow"><span>Thème n°1</span><b>${w.themes[0]?w.themes[0][0]:'—'}</b></div>`+
  `<div class="crow"><span>Incident détecté</span><b>${inc}</b></div>`;
  return `<div class="cc"><h3>Semaine du ${w.label} ${badge(w)}</h3>${rows}<div class="note" style="margin-top:12px">${autoNote(w)}</div></div>`;}
function renderCmp(){const a=WEEKS.find(w=>w.key===$('#selA').value),b=WEEKS.find(w=>w.key===$('#selB').value);
  $('#cmpBody').innerHTML=cmpCard(a)+cmpCard(b);}
$('#sub').textContent=`Pilotage du support Intercom · ${META.weeks} dernières semaines · mis à jour le ${META.generated}`;
fillWeekSel();renderChart();renderDetail();fillSelectors();renderCmp();
</script></body></html>
"""

if __name__ == "__main__":
    main()
