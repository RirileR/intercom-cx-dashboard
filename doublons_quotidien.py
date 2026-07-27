#!/usr/bin/env python3
"""Routine quotidienne — DOUBLONS de conversations.
Cherche les clients qui ont PLUSIEURS conversations OUVERTES escaladées à un humain
(regroupées par identité contact ; tickets back-office/tracker exclus ; internes exclus)
et poste un résumé actionnable dans Slack #cx-incidents.

Lancé par GitHub Actions (cron). Secrets : INTERCOM_TOKEN, SLACK_WEBHOOK_URL.
Test local sans rien poster :  python3 doublons_quotidien.py --dry-run
"""
import os, sys, time, json, urllib.request, urllib.error, collections
from datetime import datetime, timezone, timedelta

DRY = "--dry-run" in sys.argv
APP_ID = "hu6d8oic"
BOT_ADMIN_ID = 5643664
ESC_STATES = {"Routed to team", "Procedure Handoff", "Abandoned"}
EXCLURE_CAT = {"Back-office", "Tracker"}

# --- token : env (GitHub secret) sinon .env local ---
def get_token():
    if os.environ.get("INTERCOM_TOKEN"):
        return os.environ["INTERCOM_TOKEN"]
    ici = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(ici, ".env")) as f:
            for l in f:
                if l.strip().startswith("INTERCOM_TOKEN"):
                    return l.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    sys.exit("INTERCOM_TOKEN introuvable")

TOKEN = get_token()
SLACK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
API = "https://api.intercom.io/conversations/search"
HEAD = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json",
        "Content-Type": "application/json", "Intercom-Version": "2.11"}

def appel(url, payload=None, method="POST", essais=6):
    data = json.dumps(payload).encode() if payload is not None else None
    for i in range(essais):
        try:
            req = urllib.request.Request(url, data=data, headers=HEAD, method=method)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            code = getattr(e, "code", None)
            if code in (429, 500, 502, 503) or code is None:
                time.sleep(2 ** i); continue
            raise
    raise RuntimeError("échec API Intercom")

def ticket_categories():
    h = {k: v for k, v in HEAD.items() if k != "Content-Type"}
    req = urllib.request.Request("https://api.intercom.io/ticket_types", headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    return {t.get("name"): t.get("category") for t in d.get("data", [])}

def est_interne(e):
    e = (e or "").lower()
    return (not e) or e.startswith("operator+") or e.endswith("@abby.fr") or e.endswith("@intercom.io")

def lien(cid):
    return f"https://app.intercom.com/a/inbox/{APP_ID}/inbox/conversation/{cid}"

# --- 1. récupérer toutes les conv ouvertes ---
TCAT = ticket_categories()
payload = {"query": {"field": "open", "operator": "=", "value": True},
           "pagination": {"per_page": 150},
           "sort": {"field": "created_at", "order": "descending"}}
par_contact = collections.defaultdict(list)
total_ouvertes = 0
while True:
    d = appel(API, payload)
    for c in d.get("conversations", []):
        if c.get("ticket") and TCAT.get((c.get("ticket") or {}).get("ticket_type")) in EXCLURE_CAT:
            continue
        ca = c.get("custom_attributes") or {}
        fin = ca.get("Fin AI Agent resolution state") or ""
        team = c.get("team_assignee_id"); admin = c.get("admin_assignee_id")
        if not (bool(team) or (bool(admin) and admin != BOT_ADMIN_ID) or (fin in ESC_STATES)):
            continue  # pas escaladé à un humain
        conts = ((c.get("contacts") or {}).get("contacts") or [])
        cref = (conts[0].get("external_id") or conts[0].get("id")) if conts else ""
        if not cref:
            continue
        au = (c.get("source") or {}).get("author") or {}
        total_ouvertes += 1
        par_contact[cref].append({"id": c.get("id"), "created_at": c.get("created_at", 0),
                                  "email": (au.get("email") or ""), "name": au.get("name") or "",
                                  "theme": ca.get("Thème de la demande") or "(non catégorisé)"})
    nxt = (d.get("pages") or {}).get("next")
    if not nxt:
        break
    sa = nxt.get("starting_after") if isinstance(nxt, dict) else None
    if not sa:
        break
    payload["pagination"]["starting_after"] = sa
    time.sleep(0.2)

# --- 2. garder les clients avec ≥2 conv, hors internes ---
groupes = []
for cref, cs in par_contact.items():
    if len(cs) < 2:
        continue
    ident = next((x["email"] for x in cs if x["email"] and not est_interne(x["email"])), "")
    if not ident:  # groupe uniquement interne (bot/agent) → ignorer
        continue
    if any(x["name"] == "Stan" for x in cs) and not ident:
        continue
    groupes.append((ident, sorted(cs, key=lambda x: x["created_at"])))
groupes.sort(key=lambda g: -len(g[1]))

nb_clients = len(groupes)
nb_conv = sum(len(cs) for _, cs in groupes)
nb_en_trop = nb_conv - nb_clients
today = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%d/%m/%Y")

# --- 3. construire le message Slack ---
entete = (
    f"🔁 *DOUBLONS DE CONVERSATIONS — {today}*\n"
    f"*{nb_clients} clients* ont plusieurs conversations ouvertes (escaladées à un humain) "
    f"= *~{nb_en_trop} conversations à fusionner*.\n"
    f"_Process : garder la 🟢 (la plus récente), y répondre ; lier les autres + macro « doublons » puis fermer._\n"
    f"_Priorité aux clients avec 3 conversations ou plus._"
)
lignes = []
for ident, cs in groupes:
    liens = " ".join(f"<{lien(c['id'])}|{'🟢' if c is cs[-1] else '🔗'}>" for c in cs)
    lignes.append(f"• *{ident}* ({len(cs)}) : {liens}")

# --- 4. poster (ou afficher en dry-run), en découpant si trop long ---
def poster(txt):
    if DRY or not SLACK_URL:
        print("----- MESSAGE SLACK -----")
        print(txt)
        return
    body = json.dumps({"text": txt}).encode()
    req = urllib.request.Request(SLACK_URL, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()

if nb_clients == 0:
    poster(f"🔁 *DOUBLONS — {today}* : aucun doublon ouvert détecté aujourd'hui. 🎉")
else:
    poster(entete)
    # chunks de ~25 clients pour rester lisible
    for i in range(0, len(lignes), 25):
        poster("\n".join(lignes[i:i+25]))
        time.sleep(1)

print(f"[{'DRY-RUN' if DRY else 'OK'}] {nb_clients} clients / {nb_conv} conv ouvertes escaladées en doublon "
      f"(sur {total_ouvertes} conv ouvertes escaladées).")
