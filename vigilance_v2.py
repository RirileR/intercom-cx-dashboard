#!/usr/bin/env python3
"""
Vigilance V2 annuaire — détection serveur (GitHub Actions).
Lit les conversations Intercom récentes, les fait clusteriser par l'API Claude,
et poste une alerte Slack (#cx-incidents) en pingant Inès et Dim dès qu'un
symptôme lié à la V2 annuaire touche >= 3 conversations.

Usage:
    python vigilance_v2.py            # run normal (respecte le garde-fou date)
    python vigilance_v2.py --dry-run  # n'écrit rien, ignore le garde-fou date, imprime le diagnostic

Secrets attendus en variables d'env :
    INTERCOM_TOKEN, ANTHROPIC_API_KEY, SLACK_WEBHOOK_URL
"""
import os, sys, json, time, html, re, datetime, zoneinfo
import urllib.request, urllib.error

# ----- Constantes métier -----
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
LAUNCH_DATES = {"2026-06-29", "2026-06-30"}     # garde-fou : la vigilance n'agit que ces jours-là
LOOKBACK_SECONDS = 2 * 3600                      # fenêtre glissante ~2h
MIN_CLUSTER = 3                                  # seuil bas de détection
INTERCOM_WORKSPACE = "hu6d8oic"
SLACK_PING = "<@U05ANE64S5P> <@U09SVB7343E>"     # Inès + Dim
STATE_PATH = "state/vigilance_v2_state.json"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")  # TODO: vérifier la string exacte
INTERCOM_VERSION = os.environ.get("INTERCOM_VERSION", "2.11")

DRY = "--dry-run" in sys.argv

# Contexte V2 injecté dans le prompt de clustering
V2_CONTEXT = """\
Contexte : Abby lance la V2 de l'annuaire de facturation électronique. Nouveautés et points de vigilance :
- Vérification d'identité à la signature (NOUVEAU, parcours 3 étapes : nom/prénom via SIREN, puis CNI recto+verso / passeport / titre de séjour). C'EST LA BRIQUE LA PLUS À RISQUE (toute neuve, peut buguer). PRIORITÉ.
- Après signature : statut "inscription en cours" jusqu'à 24h = NORMAL, ne PAS considérer comme un incident.
- 4 statuts d'injection : Active / Suspended (conflit autre plateforme) / Erreur (retour Docoon) / Inactive. Suspended et Erreur => contact support.
- Anciens mandats : pop-up de confirmation (confirmer Abby = injection, ou "j'ai changé d'avis" = annulation).
Signaux à repérer : "vérification d'identité", "CNI/passeport/titre de séjour", bouton "procéder à la signature" inactif, "Oups une erreur s'est produite", "injection/Suspended/Erreur/Docoon", "mandat"/pop-up, "annuaire".
"""

# ============================ HTTP helpers ============================
def _req(url, data=None, headers=None, method="GET"):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} sur {url}: {e.read().decode()[:500]}", file=sys.stderr)
        raise

# ============================ Intercom ============================
def intercom_headers():
    return {
        "Authorization": f"Bearer {os.environ['INTERCOM_TOKEN']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Intercom-Version": INTERCOM_VERSION,
    }

def fetch_recent_conversations(lookback_ts):
    """Conversations créées dans les ~2 dernières heures (paginé)."""
    out, starting_after, pages = [], None, 0
    while pages < 6:
        pages += 1
        query = {
            "query": {"field": "created_at", "operator": ">", "value": lookback_ts},
            "pagination": {"per_page": 150},
        }
        if starting_after:
            query["pagination"]["starting_after"] = starting_after
        d = _req("https://api.intercom.io/conversations/search",
                 data=query, headers=intercom_headers(), method="POST")
        out.extend(d.get("conversations", []))
        nxt = (d.get("pages") or {}).get("next")
        starting_after = nxt.get("starting_after") if isinstance(nxt, dict) else None
        if not starting_after:
            break
    # ne garder que la vraie fenêtre
    return [c for c in out if c.get("created_at", 0) >= lookback_ts]

def conv_summary(c):
    ca = c.get("custom_attributes") or {}
    src = c.get("source") or {}
    return {
        "id": c.get("id"),
        "title": ca.get("AI Title") or c.get("title") or "",
        "theme": ca.get("Thème de la demande"),
        "typology": ca.get("Typologie du contact"),
        "sentiment": ca.get("Sentiment"),
        "author": (src.get("author") or {}).get("id"),
        "snippet": strip_html(src.get("body") or "")[:200],
    }

def fetch_first_user_message(conv_id):
    """Premier vrai message client (conversation_parts), pas le source.body."""
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
                return t[:160]
    return None

def strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", " ", s or ""))).strip()

# ============================ Claude (clustering sémantique) ============================
def cluster_with_claude(convs):
    """Renvoie une liste d'incidents V2 : [{label, symptom, conv_ids:[...]}]."""
    payload = [conv_summary(c) for c in convs]
    prompt = f"""{V2_CONTEXT}

Voici {len(payload)} conversations Intercom des ~2 dernières heures (JSON) :
{json.dumps(payload, ensure_ascii=False)}

Regroupe-les par PROBLÈME DE FOND. Ne retiens QUE les clusters qui :
- sont liés à la V2 de l'annuaire / facturation électronique (surtout vérif d'identité, signature, injection, mandat) ;
- comptent AU MOINS {MIN_CLUSTER} conversations sur le même symptôme ;
- ne sont PAS un comportement normal ("inscription en cours" < 24h).
Réponds UNIQUEMENT en JSON valide, sans texte autour :
{{"incidents": [{{"label": "<id_court_stable_kebab>", "symptom": "<symptôme en 1 phrase>", "conv_ids": ["..."]}}]}}
Si aucun cluster ne qualifie, renvoie {{"incidents": []}}."""
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    d = _req("https://api.anthropic.com/v1/messages", data=body, headers=headers, method="POST")
    text = "".join(blk.get("text", "") for blk in d.get("content", []))
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text).get("incidents", [])
    except Exception:
        print("Réponse Claude non parsable:\n" + text, file=sys.stderr)
        return []

# ============================ État (anti-répétition) ============================
def load_state(today):
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
        if st.get("date") == today:
            return st
    except Exception:
        pass
    return {"date": today, "alerted": {}}

def save_state(st):
    if DRY:
        return
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

# ============================ Slack ============================
def post_slack(text):
    if DRY:
        print("---- [DRY-RUN] message Slack qui aurait été posté ----\n" + text + "\n----")
        return
    _req(os.environ["SLACK_WEBHOOK_URL"], data={"text": text},
         headers={"Content-Type": "application/json"}, method="POST")

def build_message(inc, convs_by_id, now):
    ids = inc.get("conv_ids", [])
    users = len({(convs_by_id.get(i, {}).get("source") or {}).get("author", {}).get("id")
                 for i in ids if i in convs_by_id})
    verbatims = []
    for i in ids[:3]:
        v = fetch_first_user_message(i)
        if v:
            verbatims.append(f'💬 "{v}"')
        if len(verbatims) >= 2:
            break
    links = " · ".join(
        f"<https://app.intercom.com/a/inbox/{INTERCOM_WORKSPACE}/inbox/conversation/{i}|conv {n+1}>"
        for n, i in enumerate(ids[:3]))
    lines = [
        f"🚨 {SLACK_PING} *signal V2 — {inc.get('symptom','?')}*",
        f"*{len(ids)} conv.* en ~2h · *{users} users* · {now.strftime('%H:%M')}",
    ]
    lines += verbatims
    lines.append(f"🔎 exemples : {links}")
    lines.append("→ à vérifier / débloquer manuellement (vérif d'identité = priorité)")
    return "\n".join(lines)

# ============================ Main ============================
def main():
    now = datetime.datetime.now(PARIS)
    today = now.strftime("%Y-%m-%d")
    if not DRY and today not in LAUNCH_DATES:
        print(f"Hors fenêtre de lancement ({today}) — rien à faire.")
        return

    lookback = int(time.time()) - LOOKBACK_SECONDS
    convs = fetch_recent_conversations(lookback)
    print(f"{len(convs)} conversations sur la fenêtre ~2h.")
    if not convs:
        return

    convs_by_id = {c["id"]: c for c in convs}
    incidents = cluster_with_claude(convs)
    print(f"{len(incidents)} cluster(s) V2 >= {MIN_CLUSTER} détecté(s).")

    st = load_state(today)
    for inc in incidents:
        label = inc.get("label") or inc.get("symptom", "?")
        n = len(inc.get("conv_ids", []))
        if n < MIN_CLUSTER:
            continue
        prev = st["alerted"].get(label)
        # 1ère alerte du jour pour ce cluster, ou volume qui a doublé => on (re)ping
        if prev is None or n >= prev * 2:
            post_slack(build_message(inc, convs_by_id, now))
            st["alerted"][label] = n
        else:
            print(f"Cluster '{label}' déjà signalé (prev={prev}, now={n}) — silencieux.")
    save_state(st)

if __name__ == "__main__":
    main()
