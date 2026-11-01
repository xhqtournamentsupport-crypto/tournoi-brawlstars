# XHQ League

Plateforme de tournois e-sport (Flask + SQLite + HTML/CSS/JS vanille).

## Lancer
```
pip install -r requirements.txt
python app.py
```
Puis ouvrir http://localhost:5000 — `tournois.db` est créé (ou mis à jour) automatiquement.

## Sécurité
La clé qui signe les sessions n'est pas dans le code : elle est générée au premier lancement dans
`secret_key.txt` (ignoré par Git, ne jamais le partager). En ligne, définir plutôt la variable
d'environnement `XHQ_SECRET_KEY`.

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

## Fin de saison
Onglet **Saison** du panneau admin : aperçu des mouvements (champion, montées, descentes), puis
clôture en tapant `CLOTURER`. Le classement est archivé (palmarès), les teams changent de ligue,
les points et stats repartent à 0. Une team doit avoir joué au moins `MATCHS_MIN_POUR_MONTER`
match(s) pour monter ou être championne.
Une team **inactive** (aucun match de la saison, quelle que soit sa ligue) est **retirée du classement** ;
son capitaine peut la réinscrire en Ligue 10 depuis la page Teams. Les places libres d'une ligue sont
données aux meilleures teams actives de la ligue juste en dessous (**repêchage**, une ligue max par saison).

**Pénalités** (appliquées à la clôture, avant le classement final) : −1 pt par match non joué
(matchs attendus = teams de la poule − 1), doublé si la team était déjà pénalisée ou retirée la
saison d'avant, plafond −15. Réglages en haut de `app.py` (`PENALITE_PAR_MATCH`,
`MULTIPLICATEUR_RECIDIVE`, `PENALITE_MAX`). Les teams voient leur participation (« 3 / 9 ») dans Ma Team.

## Shop
Tout est **gratuit** et **purement cosmétique** (aucun avantage en jeu). Tout le monde démarre à 0 €.
- **Badge Fondateur** : réservé aux 100 premiers inscrits.
- **Badge Saison 1** : récupérable uniquement pendant la saison 1.
- **Cadre Or / Cadre Carbone** : contour autour de l'avatar (profil, menu, liste des membres).
- **Avatars rares** : 4 avatars en plus dans le sélecteur du profil.

Un badge et un cadre équipés à la fois, à choisir dans *Profil → Mon inventaire*.

## Teams
- 5 joueurs maximum par team (`MAX_MEMBRES` en haut de `app.py`).
- Tout membre peut **quitter** sa team ; si le capitaine part, le rôle passe au membre le plus ancien
  (team supprimée s'il était seul).
- Le **capitaine** (👑) peut exclure un membre, nommer un autre capitaine, modifier le nom et la bio.

## Structure
- `app.py` : serveur, base de données, API
- `templates/_head.html`, `_nav.html`, `_footer.html` : morceaux communs inclus dans toutes les pages
- `static/style.css` : style de la nav / footer / mise en page
- `static/xhq.js` : `api()` (appels JSON sûrs, gère le 401) et `escapeHtml()`

## Ligues
Une team est dans UNE ligue (`ligue.ligue_num`) et UNE poule (`ligue.poule`).
Nouvelle team -> Ligue 10, première poule avec de la place.
`GET /api/ligue?ligue_num=N[&poule=X]` renvoie uniquement les teams de cette ligue.
