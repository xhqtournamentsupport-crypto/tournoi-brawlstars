# Mettre XHQ League en ligne (PythonAnywhere, gratuit)

Résultat : le site sera accessible à tout le monde sur `https://TON_PSEUDO.pythonanywhere.com`.
Durée : environ 15 minutes. Tes données (comptes, teams, matchs) sont conservées entre les redémarrages.

## 1. Créer le compte
1. Va sur https://www.pythonanywhere.com et clique sur **Pricing & signup**, puis **Create a Beginner account** (gratuit).
2. Choisis bien ton **nom d'utilisateur** : il devient l'adresse du site (ex. `xhqleague` donne `xhqleague.pythonanywhere.com`).

## 2. Récupérer le code
1. Dans le menu du haut : **Consoles**, puis **Bash**.
2. Colle ces lignes (une par une) puis Entrée :
   ```
   git clone https://github.com/xhqtournamentsupport-crypto/tournoi-brawlstars.git
   cd tournoi-brawlstars
   pip3.10 install --user -U -r requirements.txt
   ```

## 3. Créer l'application web
1. Menu du haut : **Web**, puis **Add a new web app**, puis **Next**.
2. Choisis **Manual configuration** (surtout pas « Flask »), puis **Python 3.10**, puis **Next**.
3. Sur la page Web, section **Code** :
   - **Source code** : `/home/TON_PSEUDO/tournoi-brawlstars`
   - Clique sur le lien **WSGI configuration file**, efface tout son contenu, colle le contenu du fichier
     `deploiement/pythonanywhere_wsgi.py` en remplaçant `TON_PSEUDO`, puis **Save**.
4. Toujours sur la page Web, section **Security** : active **Force HTTPS**.
5. En haut de la page Web, clique sur le gros bouton vert **Reload**.

Le site est en ligne : ouvre `https://TON_PSEUDO.pythonanywhere.com`.

## 4. Devenir admin
1. Inscris-toi sur le site en ligne avec ton email.
2. Dans la console Bash :
   ```
   cd ~/tournoi-brawlstars
   python3.10 app.py admin ton@email.fr
   ```

## Garder ses anciennes données (facultatif)
Menu **Files**, va dans `tournoi-brawlstars/`, clique sur **Upload a file** et envoie ton `tournois.db`,
puis **Reload** dans l'onglet Web. Il est converti automatiquement.

## Mettre à jour le site plus tard
Dans la console Bash :
```
cd ~/tournoi-brawlstars
git pull
```
puis **Reload** dans l'onglet Web.

## À savoir
- Compte gratuit : une fois tous les 3 mois, PythonAnywhere demande de cliquer sur
  **Run until 3 months from today** (onglet Web), sinon le site se met en pause. Un email te prévient.
- Ne partage jamais le fichier `secret_key.txt` (créé automatiquement) : il protège les connexions.


## Sauvegarde automatique (rien à faire)
Le site crée lui-même une sauvegarde par jour (base + images) dans `sauvegardes/`, dès la première visite
de la journée, et garde les 14 dernières. Les tâches planifiées de PythonAnywhere sont payantes : on n'en a pas besoin.
Pense quand même à en télécharger une de temps en temps depuis Admin → Sauvegardes.


## Activer les emails « mot de passe oublié » (facultatif)
Tant que ce n'est pas fait, « Mot de passe oublié » renvoie vers le formulaire de contact.

1. Sur le Gmail de XHQ : *Compte Google → Sécurité → Validation en 2 étapes* (à activer), puis
   *Mots de passe des applications* → crée-en un nommé « XHQ » → copie les 16 lettres.
2. Sur PythonAnywhere, onglet **Files** → dossier `tournoi-brawlstars` → *New file* `config_mail.json` :

        {
          "hote": "smtp.gmail.com",
          "port": 587,
          "utilisateur": "xhqtournamentsupport@gmail.com",
          "mot_de_passe": "les 16 lettres du mot de passe d'application",
          "expediteur": "XHQ League <xhqtournamentsupport@gmail.com>",
          "site": "https://xhqtounament.pythonanywhere.com"
        }

3. **Reload** dans l'onglet Web. Ce fichier n'est jamais envoyé sur GitHub.

## Compte bloqué (même un admin) : commande de secours
Après 10 essais de mot de passe ratés, un compte est bloqué 7 jours (« Mot de passe oublié » le débloque) ;
10 essais de plus = verrouillage définitif, levé par un admin (Admin → Joueurs → « Déverrouiller »).
Si ce sont les admins eux-mêmes qui sont verrouillés, onglet **Consoles** → console Bash :

    cd ~/tournoi-brawlstars && python3.10 app.py debloquer ton-email@gmail.com

Conseil : pour ton compte admin, utilise un email **différent** de l'email de contact public du site.

## Annonces et boutique (sans toucher au code)
- **Admin → Annonces** : publier tout de suite ou programmer (date et heure de France). L'annonce part toute seule
  à l'heure prévue (à la première visite du site), en bandeau sur l'accueil, dans la page `/annonces`, en notification
  à tous les joueurs et, si coché, sur le Discord.
- **Relier le Discord** (fondateurs) : salon d'annonces → Paramètres → Intégrations → Webhooks → Nouveau webhook →
  « Copier l'URL », puis la coller dans Admin → Annonces → « Relier le Discord ». Un message de test part dans le salon.
  Si rien n'arrive sur un compte gratuit PythonAnywhere, c'est que discord.com n'est pas joignable depuis le serveur.
- **Admin → Boutique** : créer des badges (libres, limités aux X premiers, réservés à donner soi-même, ou disponibles
  entre deux dates), les donner à un joueur par son @, les masquer ou les supprimer. Les badges de base sont protégés.

## Automatique (Admin → Automatique)
- **Discord automatique** : cocher « Résultats des matchs », « Nouveaux tournois et champions », « Fin de saison ».
  Les messages partent tout seuls dans le salon relié (onglet Annonces), dans la minute.
- **Tournois récurrents** : « XHQ Cup chaque samedi à 18h, 8 places, inscriptions 5 jours avant » → le tournoi
  se crée tout seul chaque semaine (#1, #2…), les capitaines sont prévenus. Pause / arrêt possibles.
- **Jeux** : ajouter un jeu (nom, format 1v1/3v3/5v5, joueurs max par team) : il a ses propres teams, ligues,
  saisons et tournois. Un jeu sans team ni tournoi peut être supprimé.
Tout ce qui est automatique se déclenche pendant les visites du site (pas besoin de tâche planifiée).

## Textes, réglages et statistiques (sans toucher au code)
- **Admin → Contenu** : le grand titre de l'accueil (3 lignes, celle du milieu brille) et le texte dessous,
  le **règlement** (page `/reglement`) et la **FAQ** (page `/faq`). Dans le règlement, `# ` = titre, `- ` = puce,
  et `{points_victoire}`, `{delai_h}`, `{penalite_max}`… sont remplacés par les vrais réglages. Vider un champ remet le texte d'origine.
- **Admin → Réglages** (fondateurs) : points par victoire / défaite, délai avant validation automatique d'un score,
  pénalités de fin de saison. Ça compte à partir du prochain match (rien n'est recalculé).
  **Mots interdits** : un mot par ligne, refusés dans les pseudos, noms de team et bios (même en l33t ou avec des points).
- **Admin → Saison → « Programmer »** (fondateurs) : la saison se clôture toute seule à la date choisie, joueurs prévenus. Annulable.
- **Admin → Stats** : joueurs, teams actives, matchs, tournois, part des scores réglés par un admin,
  et deux graphiques sur 12 semaines (inscriptions, matchs), avec un tableau pour les chiffres exacts.

## Lancement automatique des ligues (rien à faire)
- Une nouvelle team prend la **plus haute ligue libre** : la Ligue 1 se remplit en premier, puis la 2, la 3…
- Une ligue **pleine depuis au moins 24 h** reçoit son calendrier le **dimanche à 15h** et commence le **lundi à 9h** :
  7 journées, une tous les 3 jours (lun, jeu, dim, mer, sam, mar, ven), soit 3 semaines.
- Quand **toutes** les ligues ont leur calendrier, la fin de saison commune est programmée toute seule
  (le dimanche 15h après la dernière journée). Ensuite, toutes les ligues repartent ensemble le lundi à 9h.
- Tout est plein : les nouvelles teams vont en **liste d'attente**. À chaque fin de saison, le dernier de chaque
  poule de Ligue 5 leur laisse sa place. Suivi dans **Admin → Saison → Lancement des ligues**.
- Si le site ne se remplit jamais complètement, tu peux toujours clôturer ou programmer la fin de saison à la main.

## Confirmation de l'email
- Quand l'envoi d'emails est activé, chaque nouveau compte reçoit un lien de confirmation. Tant qu'il n'a pas cliqué,
  il ne peut ni créer / rejoindre une team, ni récupérer de badge (ça empêche les faux comptes en série pour le badge OG).
- Un bandeau en haut du site lui rappelle, avec un bouton « Renvoyer l'email » (3 fois par heure maximum).
- Les comptes créés avant cette mise à jour sont déjà considérés comme confirmés.
