#!/usr/bin/env python3
"""Routine quotidienne — DOUBLONS de conversations.
Cherche les clients qui ont PLUSIEURS conversations OUVERTES escaladées à un humain
(regroupées par identité contact ; tickets back-office/tracker exclus ; internes exclus)
et poste un résumé actionnable dans Slack #cx-incidents.

Lancé par GitHub Actions (cron). Secrets : INTERCOM_TOKEN, SLACK_WEBHOOK_URL.
Test local sans rien poster :  python3 doublons_quotidien.py --dry-run
"""
import os, sys, time, json, csv, urllib.request, urllib.error, urllib.parse, collections
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

# --- 3. générer le FICHIER de suivi (CSV) ---
datestr = today.replace("/", "-")
csv_path = os.path.join("/tmp", f"doublons_suivi_{datestr}.csv")
with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig = accents OK dans Excel
    w = csv.writer(f)
    w.writerow(["email_client", "nb_conv_ouvertes", "date_1ere_conv", "date_derniere_conv",
                "lien_a_garder", "liens_a_fermer", "traite (o/n)"])
    for ident, cs in groupes:
        d1 = datetime.fromtimestamp(cs[0]["created_at"]).strftime("%Y-%m-%d %H:%M")
        d2 = datetime.fromtimestamp(cs[-1]["created_at"]).strftime("%Y-%m-%d %H:%M")
        a_garder = lien(cs[-1]["id"])
        a_fermer = " ".join(lien(c["id"]) for c in cs[:-1])
        w.writerow([ident, len(cs), d1, d2, a_garder, a_fermer, ""])

# --- 4. helpers Slack (message via webhook ; fichier via bot token) ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
CHANNEL_NAME = "cx-incidents"

def poster(txt):
    if DRY or not SLACK_URL:
        print("----- MESSAGE SLACK -----"); print(txt); return
    body = json.dumps({"text": txt}).encode()
    req = urllib.request.Request(SLACK_URL, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()

def slack_api_form(method, params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://slack.com/api/{method}", data=data,
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def slack_api_json(method, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"https://slack.com/api/{method}", data=data,
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                 "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def resolve_channel_id(name):
    cursor = ""
    for _ in range(12):
        r = slack_api_form("conversations.list",
                           {"types": "public_channel,private_channel", "limit": "1000", "cursor": cursor})
        if not r.get("ok"):
            return None, r.get("error")
        for ch in r.get("channels", []):
            if ch.get("name") == name:
                return ch["id"], None
        cursor = (r.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor:
            break
    return None, "channel_not_found"

def slack_upload(channel_id, path, filename, comment):
    size = os.path.getsize(path)
    r1 = slack_api_form("files.getUploadURLExternal", {"filename": filename, "length": str(size)})
    if not r1.get("ok"):
        return False, "getUploadURL:" + str(r1.get("error"))
    upload_url = r1["upload_url"]; file_id = r1["file_id"]
    with open(path, "rb") as fh:
        content = fh.read()
    boundary = "----doublonsboundary1234"
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: text/csv\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(upload_url, data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()
    r2 = slack_api_json("files.completeUploadExternal",
                        {"files": [{"id": file_id, "title": filename}],
                         "channel_id": channel_id, "initial_comment": comment})
    if not r2.get("ok"):
        return False, "complete:" + str(r2.get("error"))
    return True, None

# --- 5. poster : message résumé + priorités, puis joindre le fichier de suivi ---
entete = (
    f"🔁 *DOUBLONS DE CONVERSATIONS — {today}*\n"
    f"*{nb_clients} clients* ont plusieurs conversations ouvertes (escaladées à un humain) "
    f"= *~{nb_en_trop} conversations à fusionner*.\n"
    f"_Process : garder la 🟢 (la plus récente), y répondre ; lier les autres + macro « doublons » puis fermer._\n"
    f"📎 *Liste complète + suivi dans le fichier joint ci-dessous.*"
)
prioritaires = [(ident, cs) for ident, cs in groupes if len(cs) >= 3]

if nb_clients == 0:
    poster(f"🔁 *DOUBLONS — {today}* : aucun doublon ouvert détecté aujourd'hui. 🎉")
else:
    poster(entete)
    if prioritaires:
        lignes = [f"• *{ident}* ({len(cs)}) : "
                  + " ".join(f"<{lien(c['id'])}|{'🟢' if c is cs[-1] else '🔗'}>" for c in cs)
                  for ident, cs in prioritaires]
        poster("*⭐ Priorité — clients à 3 conversations ou plus :*\n" + "\n".join(lignes))
    # joindre le fichier de suivi
    if DRY or not SLACK_BOT_TOKEN:
        print("CSV de suivi généré :", csv_path)
    else:
        cid, err = resolve_channel_id(CHANNEL_NAME)
        if not cid:
            poster(f"⚠️ Fichier de suivi non joint (canal `{CHANNEL_NAME}` introuvable : {err}). "
                   f"Vérifie que le bot est bien dans le canal.")
        else:
            ok, err = slack_upload(cid, csv_path, f"doublons_suivi_{datestr}.csv",
                                   f"📎 Suivi des doublons du {today} — {nb_clients} clients, ~{nb_en_trop} conv à fusionner.")
            if not ok:
                poster(f"⚠️ Fichier de suivi non joint ({err}). Le message ci-dessus reste valable.")

print(f"[{'DRY-RUN' if DRY else 'OK'}] {nb_clients} clients / {nb_conv} conv en doublon "
      f"(sur {total_ouvertes} conv ouvertes escaladées). CSV: {csv_path}")
