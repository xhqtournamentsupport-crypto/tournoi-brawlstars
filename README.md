# XHQ Tournament

Plateforme de tournois e-sport (Flask + SQLite + HTML/CSS/JS vanille).

## Lancer
```
pip install -r requirements.txt
python app.py
```
Puis ouvrir http://localhost:5000 — `tournois.db` est créé (ou mis à jour) automatiquement.

## Devenir admin
Inscris-toi sur le site, puis dans le terminal (serveur arrêté ou non) :
```
python app.py admin ton@email.fr
```
Le lien **ADMIN** apparaît dans la nav. Le panneau `/admin` permet de :
- enregistrer les résultats de matchs (victoire 3 pts, défaite 0, pas de nul) et les supprimer ;
- déplacer ou supprimer des teams ;
- générer un mot de passe temporaire pour un joueur (vrai « mot de passe oublié ») ;
- lire les messages de contact et donner les droits admin à d'autres joueurs.

Le barème se change en haut de `app.py` (`POINTS_VICTOIRE`, `POINTS_DEFAITE`).

## Structure
- `app.py` : serveur, base de données, API
- `templates/_head.html`, `_nav.html`, `_footer.html` : morceaux communs inclus dans toutes les pages
- `static/style.css` : style de la nav / footer / mise en page
- `static/xhq.js` : `api()` (appels JSON sûrs, gère le 401) et `escapeHtml()`

## Ligues
Une team est dans UNE ligue (`ligue.ligue_num`) et UNE poule (`ligue.poule`).
Nouvelle team -> Ligue 10, première poule avec de la place.
`GET /api/ligue?ligue_num=N[&poule=X]` renvoie uniquement les teams de cette ligue.
