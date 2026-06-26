#!/usr/bin/env python3
"""
Radar incidents CX — quotidien (version serveur GitHub Actions).
Chaque matin : lit les conversations Intercom de LA VEILLE, les fait clusteriser
par l'API Claude, applique la baseline 7 jours glissante, et poste UNE alerte
visuelle dans Slack #cx-incidents.

Règles (v3) :
- Seuil : ne garder que les clusters >= 5 conversations sur la veille.
- Niveau 🔴 (indépendant du sentiment) : >= 5 conversations typées "Bug" sur la même
  fonctionnalité, OU bug technique confirmé (même erreur / mention "problème connu" par Fin).
  🟠 sinon. Le sentiment n'est qu'une pastille d'info.
- Baseline : moyenne 7j glissante par sujet (radar_history.csv committé au repo) →
  ⚠️ xN vs 7j / ≈ habituel / 🆕 nouveau (si < 3 dates d'historique).
- Verbatims réels (conversation_parts, pas source.body), users distincts.

Usage:
    python radar_quotidien.py            # run normal (veille)
    python radar_quotidien.py --dry-run  # n'écrit/poste rien, imprime le diagnostic
    python radar_quotidien.py --date 2026-06-25   # rejoue une date précise (backtest)

Secrets : INTERCOM_TOKEN, ANTHROPIC_API_KEY, SLACK_BOT_TOKEN
"""
import os, sys, json, html, re, csv, datetime, zoneinfo
import urllib.request, urllib.error
from collections import defaultdict, Counter

PARIS = zoneinfo.ZoneInfo("Europe/Paris")
MIN_CLUSTER = 5
RED_BUG_THRESHOLD = 5
HISTORY_PATH = "state/radar_history.csv"
INTERCOM_WORKSPACE = "hu6d8oic"
SLACK_CHANNEL = "C0BDGANJV1P"                    # #cx-incidents
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")  # TODO vérifier string exacte
INTERCOM_VERSION = os.environ.get("INTERCOM_VERSION", "2.11")

DRY = "--dry-run" in sys.argv

def arg_date():
    if "--date" in sys.argv:
        return sys.argv[sys.argv.index("--date") + 1]
    return None

# ============================ HTTP ============================
def _req(url, data=None, headers=None, method="GET"):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {url}: {e.read().decode()[:500]}", file=sys.stderr)
        raise

def strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", " ", s or ""))).strip()

# ============================ Fenêtre ============================
def window(date_iso=None):
    if date_iso:
        d = datetime.datetime.strptime(date_iso, "%Y-%m-%d").replace(tzinfo=PARIS)
        start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        now = datetime.datetime.now(PARIS)
        start = (now - datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp()), start

# ============================ Intercom ============================
def intercom_headers():
    return {
        "Authorization": f"Bearer {os.environ['INTERCOM_TOKEN']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Intercom-Version": INTERCOM_VERSION,
    }

def fetch_day(start_ts, end_ts):
    """Conversations de la veille via requête bornée (AND created_at>start ET <end)."""
    out, starting_after, pages = [], None, 0
    while pages < 12:
        pages += 1
        q = {
            "query": {"operator": "AND", "value": [
                {"field": "created_at", "operator": ">", "value": start_ts},
                {"field": "created_at", "operator": "<", "value": end_ts},
            ]},
            "pagination": {"per_page": 150},
        }
        if starting_after:
            q["pagination"]["starting_after"] = starting_after
        d = _req("https://api.intercom.io/conversations/search",
                 data=q, headers=intercom_headers(), method="POST")
        out.extend(d.get("conversations", []))
        nxt = (d.get("pages") or {}).get("next")
        starting_after = nxt.get("starting_after") if isinstance(nxt, dict) else None
        if not starting_after:
            break
    # dédoublonnage par id
    return list({c["id"]: c for c in out}.values())

def conv_brief(c):
    ca = c.get("custom_attributes") or {}
    src = c.get("source") or {}
    return {
        "id": c.get("id"),
        "title": ca.get("AI Title") or c.get("title") or "",
        "theme": ca.get("Thème de la demande"),
        "typology": ca.get("Typologie du contact"),
        "sentiment": ca.get("Sentiment"),
    }

def author_id(c):
    return ((c.get("source") or {}).get("author") or {}).get("id")

def first_user_message(conv_id):
    try:
        d = _req(f"https://api.intercom.io/conversations/{conv_id}",
                 headers=intercom_headers(), method="GET")
    except Exception:
        return None
    parts = (d.get("conversation_parts") or {}).get("conversation_parts", [])
    for p in parts:
        a = p.get("author") or {}
        if p.get("part_type") == "comment" and a.get("type") == "user" and p.get("body"):
            t = strip_html(p["body"])
            if t and "j'ai compris" not in t.lower():
                return t[:150]
    return None

# ============================ Claude clustering ============================
def cluster(convs):
    payload = [conv_brief(c) for c in convs]
    prompt = f"""Tu es un analyste support. Voici {len(payload)} conversations Intercom de la veille (Abby, logiciel micro-entrepreneurs), en JSON :
{json.dumps(payload, ensure_ascii=False)}

Regroupe-les par PROBLÈME DE FOND (par le sens et le champ "theme", pas juste les mots).
Ne renvoie QUE les clusters d'AU MOINS {MIN_CLUSTER} conversations qui valent une alerte incident :
- priorise bug / panne / erreur / blocage / fonctionnalité cassée ;
- une confusion soudaine et massive sur UNE fonctionnalité précise compte aussi ;
- IGNORE les volumes d'usage NORMAUX (questions classiques, leads/avant-vente, compta générique).

Réponds UNIQUEMENT en JSON valide :
{{"clusters": [
  {{"label": "<id_court_stable_kebab, ex: facturation_electronique>",
    "title": "<nom lisible du sujet>",
    "symptom": "<1 phrase: ce qui se passe>",
    "tech_confirmed": <true si un même message d'erreur revient ou si une réponse Fin mentionne un "problème connu"/ticket, sinon false>,
    "conv_ids": ["...tous les ids du cluster..."]}}
]}}
Si rien ne qualifie : {{"clusters": []}}."""
    body = {"model": CLAUDE_MODEL, "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    d = _req("https://api.anthropic.com/v1/messages", data=body, headers=headers, method="POST")
    text = "".join(b.get("text", "") for b in d.get("content", []))
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text).get("clusters", [])
    except Exception:
        print("Réponse Claude non parsable:\n" + text, file=sys.stderr)
        return []

# ============================ Baseline ============================
def load_history():
    rows = []
    try:
        with open(HISTORY_PATH, newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        pass
    return rows

def baseline_tag(history, label, n_today, today_iso):
    past = [r for r in history if r.get("sujet") == label and r.get("date") != today_iso]
    dates = sorted({r["date"] for r in past}, reverse=True)[:7]
    vals = [int(r["n_conv"]) for r in past if r["date"] in dates]
    if len(dates) < 3 or not vals:
        return "🆕 nouveau"
    avg = sum(vals) / len(vals)
    ratio = n_today / avg if avg else 0
    if ratio >= 1.5:
        return f"⚠️ x{ratio:.1f} vs 7j"
    return f"≈ habituel ({avg:.0f}/j)"

def append_history(today_iso, clusters_out):
    if DRY:
        return
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    exists = os.path.exists(HISTORY_PATH)
    with open(HISTORY_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["date", "sujet", "n_conv", "n_users", "n_bug", "sentiment"])
        for c in clusters_out:
            w.writerow([today_iso, c["label"], c["n"], c["users"], c["n_bug"], c["sentiment"]])

# ============================ Slack ============================
def slack_headers():
    return {
        "Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}",
        "Content-Type": "application/json; charset=utf-8",
    }

def post_slack(text):
    if DRY:
        print("---- [DRY-RUN] message Slack ----\n" + text + "\n----")
        return
    resp = _req("https://slack.com/api/chat.postMessage",
                data={"channel": SLACK_CHANNEL, "text": text},
                headers=slack_headers(), method="POST")
    if not resp.get("ok"):
        print(f"Erreur Slack: {resp.get('error')}", file=sys.stderr)

def bar(n, biggest):
    filled = round(n / biggest * 10) if biggest else 0
    return "▰" * filled + "▱" * (10 - filled)

def sentiment_emoji(sents):
    c = Counter(s for s in sents if s)
    if not c:
        return "😐"
    top = c.most_common(1)[0][0]
    return {"Negatif": "😠", "Positif": "🙂"}.get(top, "😐")

def search_link(title):
    import urllib.parse
    q = urllib.parse.quote(title)
    return f"<https://app.intercom.com/a/inbox/{INTERCOM_WORKSPACE}/inbox/search?query={q}|« {title} »>"

# ============================ Main ============================
def main():
    date_iso = arg_date()
    start_ts, end_ts, start_dt = window(date_iso)
    today_iso = start_dt.strftime("%Y-%m-%d")
    label_jour = start_dt.strftime("%A %d/%m")

    convs = fetch_day(start_ts, end_ts)
    total = len(convs)
    print(f"{total} conversations le {today_iso}.")
    by_id = {c["id"]: c for c in convs}

    clusters = cluster(convs) if convs else []

    # Enrichissement Python : N, users, n_bug, sentiment, niveau, baseline
    history = load_history()
    enriched = []
    for cl in clusters:
        ids = [i for i in cl.get("conv_ids", []) if i in by_id]
        n = len(ids)
        if n < MIN_CLUSTER:
            continue
        n_bug = sum(1 for i in ids if (by_id[i].get("custom_attributes") or {}).get("Typologie du contact") == "Bug")
        users = len({author_id(by_id[i]) for i in ids})
        sents = [(by_id[i].get("custom_attributes") or {}).get("Sentiment") for i in ids]
        red = n_bug >= RED_BUG_THRESHOLD or (cl.get("tech_confirmed") and n >= MIN_CLUSTER)
        enriched.append({
            "label": cl.get("label", "sujet"), "title": cl.get("title", cl.get("label", "Sujet")),
            "symptom": cl.get("symptom", ""), "ids": ids, "n": n, "n_bug": n_bug,
            "users": users, "sentiment": sentiment_emoji(sents),
            "sentiment_majo": Counter(s for s in sents if s).most_common(1)[0][0] if any(sents) else "Neutre",
            "level": "🔴" if red else "🟠", "tech": bool(cl.get("tech_confirmed")),
        })

    if not enriched:
        post_slack(f"🛰️ *RADAR INCIDENTS CX — {label_jour}*\n_{total} conversations hier_\n✅ RAS — aucun sujet n'atteint {MIN_CLUSTER} conversations. Rien d'anormal détecté.")
        append_history(today_iso, [])
        return

    # tri : 🔴 d'abord puis par taille
    enriched.sort(key=lambda c: (c["level"] != "🔴", -c["n"]))
    biggest = max(c["n"] for c in enriched)
    n_red = sum(1 for c in enriched if c["level"] == "🔴")
    n_orange = len(enriched) - n_red

    lines = [f"🛰️ *RADAR INCIDENTS CX — {label_jour}*",
             f"_{total} conversations hier · {n_red} incident(s) probable(s), {n_orange} sujet(s) à surveiller_"]
    for c in enriched:
        tag = baseline_tag(history, c["label"], c["n"], today_iso)
        sent = c["sentiment"] + (" _(trompeur)_" if c["level"] == "🔴" and c["sentiment"] == "😐" else "")
        verbs = []
        for i in c["ids"]:
            v = first_user_message(i)
            if v:
                verbs.append(f'💬 "{v}"')
            if len(verbs) >= 2:
                break
        links = " · ".join(
            f"<https://app.intercom.com/a/inbox/{INTERCOM_WORKSPACE}/inbox/conversation/{i}|conv {k+1}>"
            for k, i in enumerate(c["ids"][:5]))
        bug_note = f" · {c['n_bug']} typées Bug" if c["n_bug"] else ""
        lines.append("────────────────────")
        lines.append(f"{c['level']} *{c['title']}* · {tag}")
        lines.append(f"`{bar(c['n'], biggest)}` *{c['n']} conv.* · *{c['users']} users*{bug_note} · sentiment {sent}")
        if c["symptom"]:
            lines.append(c["symptom"])
        lines += verbs
        lines.append(f"🔎 Voir tout : {search_link(c['title'])} · exemples : {links}")
        lines.append("→ bug confirmé, à prioriser" if c["level"] == "🔴" else "→ à surveiller / vérifier côté produit")
    lines.append("────────────────────")
    lines.append(f"_Seuil ≥ {MIN_CLUSTER} conv. · 🔴 = ≥{RED_BUG_THRESHOLD} conv. typées Bug (indépendant du sentiment) · baseline 7j glissante._")

    post_slack("\n".join(lines))
    append_history(today_iso, enriched)

if __name__ == "__main__":
    main()
