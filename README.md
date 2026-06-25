# Cockpit CX — Abby

Petit outil pour piloter le support Intercom : tire les conversations via l'API et génère un **dashboard HTML interactif** (filtres, liens directs vers chaque conversation, verbatims, répartition par thème et par nature).

Aucune donnée ni token n'est envoyé ailleurs : tout tourne en local sur ta machine, le rapport est un simple fichier HTML.

## Installation (une seule fois)

```bash
pip install -r requirements.txt
cp .env.example .env
```

Puis ouvre `.env` et colle ton token Intercom (Developer Hub → ton app → Authentication → Access Token).
Le `.env` est ignoré par git : **ton token ne partira jamais sur GitHub.**

## Utilisation

```bash
python build.py              # 7 derniers jours
python build.py --days 14    # 14 derniers jours
python build.py --out lundi.html
```

Ça génère `rapport.html`. Double-clique dessus pour l'ouvrir dans ton navigateur.

### Tendances semaine / mois (`trends.py`)

Là où `build.py` montre le détail des dernières conversations, `trends.py` agrège **tout l'historique** pour voir l'évolution dans le temps :

```bash
python trends.py                     # depuis le 1er janvier 2026 (défaut)
python trends.py --since 2026-03-01  # depuis une autre date
python trends.py --out tendances.html
```

Ça génère `tendances.html` : un bouton bascule entre **volume par semaine** et **volume par mois**, avec le delta vs période précédente, le taux global de non-qualifiés, et surtout une **heatmap « thème × période »** qui donne le nombre exact de conversations par thème et par semaine/mois (la ligne orange = conversations sans thème, pour suivre le taggage). Le HTML n'embarque que des chiffres agrégés : il reste léger même sur des dizaines de milliers de conversations. La récupération depuis janvier peut prendre quelques minutes (pagination de toute la période).

## Ce que tu peux faire dans le dashboard

- **KPI en haut** : volume, % résolu par Fin, escalades, % bug, % sentiment négatif
- **Cliquer sur un thème ou une nature** (colonne de gauche) pour filtrer
- **Filtres** : urgence, sentiment, escaladé / résolu par Fin, recherche plein texte (verbatim + titre)
- **Chaque conversation** affiche son verbatim, ses badges, et un lien « Ouvrir dans Intercom »

## Le mettre sur GitHub

```bash
git init && git add . && git commit -m "Cockpit CX"
# crée un repo PRIVÉ sur github.com puis :
git remote add origin git@github.com:ton-compte/intercom-cx-dashboard.git
git push -u origin main
```

Le `.gitignore` exclut déjà `.env` et les `.html` générés. Garde le repo **privé** (il contient ta logique métier, et les rapports peuvent contenir des verbatims clients).

## Personnaliser

Les attributs lus sont en haut de `build.py` (`ATTR_THEME`, `ATTR_NATURE`, etc.) — ce sont les noms exacts de tes custom attributes Intercom. La taxonomie de référence est dans `../Taxonomie/`.

## Idées d'évolution

- Onglet « tendances » sur plusieurs semaines
- Export CSV des conversations filtrées
- Détection automatique des clusters/incidents (titres récurrents)
- Publication quotidienne automatique (GitHub Actions)
