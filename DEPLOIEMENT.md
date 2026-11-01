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
   pip3.10 install --user -r requirements.txt
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
