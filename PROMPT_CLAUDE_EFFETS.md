# Mission : rendre le site XHQ League spectaculaire 🔥 (sans rien casser)

Tu reprends **XHQ League**, un site de ligue et de tournois e-sport amateur, gratuit, déjà en ligne.
Le fond est terminé et testé. Ton travail : **ajouter des effets visuels et des animations « wow »** qui donnent envie de rester sur le site, sans casser une seule fonctionnalité.

Réponds-moi en **français**, en tutoyant, avec des explications simples (je ne suis pas développeur).

---

## 1. Le projet

- **Dépôt** : `xhqtournamentsupport-crypto/tournoi-brawlstars`, branche `main`. Le dépôt est **public**.
- **En ligne** : https://xhqtounament.pythonanywhere.com
  - Compte PythonAnywhere **XHQTounament** (sans « r »).
  - Interface en français : Fichiers, Consoles, Web, Tâches, Recharger.
- **Technique** :
  - Flask (Python 3.10) + SQLite, dans un seul fichier `app.py` d'environ 3300 lignes.
  - Gabarits Jinja dans `templates/`.
  - JavaScript « vanilla », sans framework, dans `static/xhq.js`.
  - Un seul fichier CSS : `static/style.css`, recopié aussi dans `docs/assets/style.css`.
  - Pillow pour les images.
- **Pas d'étape de build**, pas de npm côté site. Le site doit continuer à marcher tel quel sur PythonAnywhere (compte gratuit).

### Les pages

| Page | Fichier | Contenu |
|---|---|---|
| Accueil | `index.html` | Hero « scène » avec composition 3D, bento d'infos, matchs à venir, fil d'actu, compteurs animés |
| Classements | `ligue.html` | Onglets de ligues 1 à 5, poules, colonnes rang/team/forme/J/+/-/Pts, logos des teams, légende |
| Calendrier | `calendrier.html` | Matchs prévus, par journée |
| Tournois | `tournois.html`, `tournoi.html` | Inscriptions, poules, bracket, champion |
| Teams | `teams.html`, `team_publique.html` | Grille de cartes, ma team (bannière, logo, emblème, chiffres, prochains matchs), éditeur |
| Profil | `profil.html`, `profil_public.html` | Photo, bannière déplaçable et zoomable, bio, @handle, avatars, badge et cadre équipés |
| Shop | `shop.html` | Objets gratuits : badges Fondateur / Saison 1 / Champion, cadres Violet / Carbone, avatars rares. Aperçu « Ton look », collection X/6, filtres, Équiper / Retirer |
| Admin | `admin.html` | Bloc « À faire », onglets à icônes, matchs, teams, saison (montées et descentes, pénalités, repêchage), calendrier, joueurs, messages, sauvegardes, modération |
| Autres | `login.html`, `signup.html`, `mot_de_passe.html`, `contact.html`, `cgv.html`, `confidentialite.html` | Connexion, inscription, mot de passe oublié, contact, pages légales |

Les éléments communs sont dans `_nav.html`, `_head.html`, `_footer.html` et `_jeux.html` :

- **Barre du haut** : logo, capsule centrale avec icônes, cloche de notifications, menu compte avec changement de compte, burger mobile. Elle prend la classe `colle` au défilement.
- **Footer** et **sélecteur de jeu**.

### Ce qui existe déjà côté fonctionnalités (ne pas refaire)

- **Comptes** : plusieurs comptes par navigateur, notifications (cloche), mot de passe oublié par email (Gmail SMTP qui fonctionne).
- **Organisation** : calendrier round-robin, saisons avec montées, descentes et repêchage, tournois.
- **Gestion du site** : modération, suspension de compte, sauvegardes zip quotidiennes, mode multi-jeux.

## 2. Le design actuel (à respecter et à sublimer)

- **3 couleurs seulement** :
  - noir `#0a0a0f` ;
  - violet `#7c6cff` (violet clair `#ab9fff` pour le texte et les liens) ;
  - blanc.
  - Les gris sont des blancs transparents. Tout est déjà en variables CSS dans `:root` de `style.css`, **réutilise-les**.
- **Polices** : Sora pour les titres (`var(--display)`), Manrope pour le texte (`var(--body)`).
- **Formes** : coins arrondis à 16–22 px, boutons en pilule.
- **Courbe d'animation** maison : `var(--courbe)`.
- **Déjà en place** :
  - halo animé en fond (`body::before`), grille de points, SVG décoratifs `.decor-bracket` ;
  - apparition au défilement : `xhq.js` ajoute `.anim` sur `<html>`, puis `.reveal` et `.vu` sur les éléments listés dans `CIBLES`, avec un MutationObserver ;
  - compteurs animés `[data-compte-a]` ;
  - un bloc `@media (prefers-reduced-motion: reduce)`.
- **Le style voulu** : moderne, animé, e-sport premium, « pas fait par une IA ». Pas de bleu corporate, pas d'effets cheap ni de couleurs en plus.

## 3. Ce que je veux : des effets de fou 🤯

Propose-moi puis réalise un pack d'effets cohérent. Pistes, à toi de choisir les meilleures et d'en proposer d'autres :

- **Accueil** :
  - hero cinématique : lumière qui suit la souris, parallaxe 3D de la composition, titre qui se révèle lettre par lettre ou en masque ;
  - grain ou bruit subtil ;
  - aurora violette qui respire.
- **Cartes** (teams, shop, bento) :
  - inclinaison 3D au survol (tilt) avec reflet brillant qui suit le curseur ;
  - bordure lumineuse animée en dégradé conique ;
  - effet « holo » sur les objets rares du shop.
- **Classements** :
  - lignes qui entrent en cascade ;
  - podium mis en avant (1er avec halo) ;
  - flèches de montée et de descente animées ;
  - points qui défilent comme un compteur ;
  - transition fluide entre les onglets de ligues.
- **Tournois** :
  - bracket qui se dessine, les traits SVG s'animent ;
  - le chemin du champion s'illumine ;
  - confettis ou explosion de particules violettes sur le champion.
- **Shop** :
  - « Récupérer » déclenche une animation d'ouverture (flash, particules, l'objet vole vers l'aperçu « Ton look ») ;
  - la barre de collection se remplit avec un éclat.
- **Profil et team** :
  - bannière en parallaxe ;
  - avatar avec anneau lumineux animé pour le cadre équipé ;
  - badge qui brille.
- **Navigation** :
  - transitions entre pages (View Transitions API si dispo, sinon fondu) ;
  - indicateur actif de la capsule qui glisse d'un onglet à l'autre ;
  - micro-interactions sur tous les boutons (pression, ripple violet).
- **Global** :
  - curseur lumineux discret sur ordinateur ;
  - scroll progress ;
  - skeletons animés pendant le chargement à la place de « Chargement… » ;
  - toasts animés pour les messages.

## 4. Règles strictes (très important)

1. **Ne casse rien.**
   - Ne renomme et ne supprime aucun `id` ni aucune classe existante utilisée par le JavaScript ou les tests. Ajoute des classes, n'en retire pas.
   - Exemples à garder : `.item-card`, `.item-name`, `.item-btn`, `.item-limite`, `#solde-display` (doit contenir « 0 € »), `#shop-message`, `.tab[data-tab=…]`, `#m-team1`, `#liste-*`, `#pastille`, `.dropdown-item[data-ligue]`, `.logo-mini`, `a.lien-team`, `#cloche-btn`, `#cloche-menu`, `#cloche-compte`, `.cloche-item`, `#compte-btn`, `#nav-avatar`, `.tache`, `.stat[data-aller]`.
   - Ne change pas les routes ni les réponses de l'API.
2. **Pas de nouvelle dépendance lourde.**
   - CSS et JS « vanilla » en priorité.
   - Si une petite librairie est vraiment utile (par exemple des confettis), charge-la depuis un CDN avec une version exacte, ou mieux, écris l'effet toi-même en quelques lignes (canvas).
3. **Performance** :
   - anime uniquement `transform`, `opacity` et `filter` ;
   - `requestAnimationFrame` pour le JS ;
   - pas d'animation lourde en boucle sur mobile ;
   - le site doit rester fluide sur un téléphone moyen.
4. **Accessibilité** :
   - tout effet de mouvement est désactivé ou adouci avec `prefers-reduced-motion: reduce` ;
   - les effets de survol ne s'appliquent pas aux écrans tactiles (`@media (hover: hover)`) ;
   - les contrastes restent lisibles.
5. **Mobile** : rien ne déborde à 390 px de large (pas de scroll horizontal). Teste aussi en 1280 px.
6. **Les 3 couleurs** : noir, violet, blanc. Pas d'autres teintes.
7. **Sécurité** :
   - ne commite jamais `secret_key.txt`, `tournois.db`, `config_mail.json`, `uploads/` ni `sauvegardes/` (ils sont dans `.gitignore`) ;
   - garde `escapeHtml` partout où tu injectes du texte venu des utilisateurs.
8. **Après chaque modification de `style.css`**, recopie-le dans `docs/assets/style.css`.

## 5. Comment tester

1. Lance le site : `python3 app.py`, sur http://localhost:5000.
2. Crée un admin avec `python3 app.py admin email@x.fr`, après l'avoir inscrit.
3. Remplis le site avec quelques teams, matchs et un tournoi pour voir les effets avec du contenu.
4. Prends des captures Playwright en 1280 px et 390 px de chaque page.
   - Fais défiler la page avant la capture pleine page, sinon les éléments `.reveal` restent invisibles.
5. Vérifie qu'il n'y a **aucune erreur JavaScript** dans la console.
6. Vérifie les parcours clés :
   - inscription et connexion ;
   - créer une team ;
   - récupérer et équiper un objet du shop ;
   - saisir un résultat en admin ;
   - notifications ;
   - changer d'onglet de ligue.
7. Après les tests, supprime `tournois.db secret_key.txt uploads sauvegardes`.

## 6. Livraison

- Travaille page par page.
- Montre-moi des captures avant et après.
- Fais des commits clairs en français, puis pousse sur `main`.
- À la fin, rappelle-moi la mise à jour sur PythonAnywhere :
  1. onglet « Consoles », ouvre une console Bash et tape `cd ~/tournoi-brawlstars && git pull` ;
  2. onglet « Web », clique sur « Recharger ».
- Termine par un résumé simple de chaque effet ajouté, et où le voir sur le site.

Vas-y, surprends-moi 🔥🔥🤯🤯🤯
