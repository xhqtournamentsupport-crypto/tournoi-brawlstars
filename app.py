import io
import os
import re
from datetime import datetime, timedelta
import secrets
import sys
import sqlite3
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, g, abort, has_request_context, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'tournois.db')
SECRET_KEY_PATH = os.path.join(BASE_DIR, 'secret_key.txt')


# Clés déjà publiées (anciennes versions du code sur GitHub) : refusées, une nouvelle est créée à la place
CLES_COMPROMISES = {'xhq-tournament-secret-2026'}


def charger_secret_key():
    """Clé qui signe les sessions. Elle ne doit JAMAIS être dans le code (le dépôt est public).
    1) variable d'environnement XHQ_SECRET_KEY si elle existe (pour la mise en ligne)
    2) sinon le fichier secret_key.txt (créé automatiquement, ignoré par Git)"""
    cle = os.environ.get('XHQ_SECRET_KEY', '').strip()
    if cle and cle not in CLES_COMPROMISES and len(cle) >= 32:
        return cle
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, encoding='utf-8') as f:
            cle = f.read().strip()
        if cle and cle not in CLES_COMPROMISES and len(cle) >= 32:
            return cle
    cle = secrets.token_hex(32)
    with open(SECRET_KEY_PATH, 'w', encoding='utf-8') as f:
        f.write(cle)
    return cle


app = Flask(__name__)
app.secret_key = charger_secret_key()
app.config['MAX_CONTENT_LENGTH'] = 9 * 1024 * 1024   # taille max d'une requête (images comprises)


@app.url_defaults
def version_fichiers_statiques(endpoint, valeurs):
    """style.css?v=… : après une mise à jour, les navigateurs (téléphones surtout) prennent tout de suite
    le nouveau fichier au lieu de garder l'ancien en mémoire."""
    if endpoint == 'static' and 'filename' in valeurs and 'v' not in valeurs:
        try:
            valeurs['v'] = int(os.path.getmtime(os.path.join(app.static_folder, valeurs['filename'])))
        except (OSError, TypeError, ValueError):
            pass

# Cookie de session : illisible par JavaScript, jamais envoyé depuis un autre site, HTTPS seulement en ligne
# (le serveur de test local « python app.py » repasse SECURE à False, voir tout en bas).
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = True
MOT_DE_PASSE_MIN = 8
ESSAIS_MAX = 10          # essais de mot de passe ratés avant blocage
BLOCAGE_JOURS = 7        # 1er blocage : 7 jours ; 10 essais ratés de plus = verrouillage définitif (admin)
from werkzeug.security import generate_password_hash as _gph
HASH_FACTICE = _gph('compte-inexistant')
SITE_URL_DEFAUT = 'https://xhqtounament.pythonanywhere.com'
LIMITES_ACTIVES = True   # limites anti-spam / anti-force brute (coupées sur le serveur de test local)


@app.before_request
def _securite_requete():
    # Site en ligne : on force HTTPS (le proxy de PythonAnywhere indique le protocole d'origine)
    if request.headers.get('X-Forwarded-Proto', '').lower() == 'http':
        return redirect(request.url.replace('http://', 'https://', 1), code=301)
    # Anti-CSRF : une action (POST) doit venir d'une page de ce site, jamais d'un autre site
    if request.method == 'POST':
        from urllib.parse import urlparse
        origine = request.headers.get('Origin') or request.headers.get('Referer')
        if origine and urlparse(origine).netloc != request.host:
            return jsonify({'success': False, 'error': 'Requête refusée (origine inconnue)'}), 403


@app.errorhandler(OverflowError)
def _nombre_geant(_e):
    """Un identifiant énorme dans l'adresse (/team/99999999999999999999) = « introuvable », pas une panne."""
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Introuvable'}), 404
    return 'Page introuvable', 404


@app.errorhandler(500)
def _erreur_interne(_e):
    # Jamais de détail technique montré au visiteur (le détail reste dans le journal d'erreurs de PythonAnywhere)
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Erreur du serveur, réessaie dans un instant'}), 500
    return 'Erreur du serveur, réessaie dans un instant.', 500


@app.after_request
def _entetes_securite(reponse):
    reponse.headers.setdefault('X-Frame-Options', 'DENY')                 # pas d'affichage du site dans un cadre (clickjacking)
    reponse.headers.setdefault('X-Content-Type-Options', 'nosniff')
    reponse.headers.setdefault('Referrer-Policy', 'same-origin')          # les liens secrets (mot de passe) ne fuitent pas
    reponse.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    reponse.headers.setdefault('Content-Security-Policy', CSP)
    if request.headers.get('X-Forwarded-Proto', '').lower() == 'https' or request.is_secure:
        reponse.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')   # le navigateur n'essaie plus jamais http://
    if session.get('user_id') and reponse.mimetype == 'text/html':
        reponse.headers.setdefault('Cache-Control', 'private, no-store')
    if request.path.startswith(('/api/', '/admin', '/reinitialiser', '/profil')):
        reponse.headers.setdefault('Cache-Control', 'no-store')            # données perso jamais gardées en cache
    return reponse


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'self'; object-src 'none'")


def ip_client():
    """Adresse IP du visiteur. PythonAnywhere la fournit dans X-Real-IP (posé par son proxy).
    Sinon, la DERNIÈRE adresse de X-Forwarded-For (les précédentes peuvent être inventées par le visiteur)."""
    if request.headers.get('X-Real-IP'):
        return request.headers['X-Real-IP'].strip()[:64]
    transmis = [x.strip() for x in request.headers.get('X-Forwarded-For', '').split(',') if x.strip()]
    return (transmis[-1] if transmis else (request.remote_addr or '?'))[:64]


def limite_atteinte(cle, maximum, minutes):
    """Vrai si cette action a déjà été faite `maximum` fois dans les `minutes` dernières minutes."""
    if not LIMITES_ACTIVES:
        return False
    return get_db().execute("SELECT COUNT(*) FROM limites WHERE cle = ? AND created_at > datetime('now', ?)",
                            (cle, f'-{int(minutes)} minutes')).fetchone()[0] >= maximum


def noter_action(cle):
    if not LIMITES_ACTIVES:
        return
    db = get_db()
    db.execute("INSERT INTO limites (cle, created_at) VALUES (?, datetime('now'))", (cle,))
    db.execute("DELETE FROM limites WHERE created_at < datetime('now', '-1 day')")
    db.commit()


TROP = 'Trop d\'essais : réessaie dans quelques minutes.'

# Photos de profil et bannières : stockées dans uploads/ (ignoré par Git), servies par /media/...
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
IMAGE_MAX_OCTETS = 8 * 1024 * 1024
AVATAR_TAILLE = 400            # photo de profil carrée 400 x 400
BANNIERE_LARGEUR = 1500        # bannière redimensionnée à 1500 px de large max

# Identifiant @ : 3 à 20 caractères, modifiable 3 fois, avec 30 jours d'attente entre deux changements
HANDLE_REGEX = re.compile(r'^[a-z0-9_.]{3,20}$')
HANDLE_DELAI_JOURS = 30
HANDLE_MAX_CHANGEMENTS = 3
HANDLES_RESERVES = {'admin', 'xhq', 'xhqleague', 'support', 'staff', 'moderateur', 'modo', 'officiel', 'root', 'system'}

AVATARS = ['👤', '🎮', '🏆', '⚡', '🔥', '💎', '👑', '🚀', '⭐', '🎯', '💪', '🌟']
# Avatars exclusifs débloqués par le Skin Premium
AVATARS_PREMIUM = ['🐉', '🦄', '🤖', '👽']
AVATAR_DEFAUT = '👤'
# Emblèmes des teams (affichés quand la team n'a pas de logo)
EMBLEMES = ['🛡️', '⚔️', '🐺', '🦅', '🐉', '🔥', '⚡', '👑', '🎯', '💀', '🦁', '🌪️', '🚀', '💎', '🌙', '🐍']

# ---------------------------------------------------------------------------
# Ligues : nombre de poules, rangs qui montent / descendent (dans chaque poule)
# ---------------------------------------------------------------------------
TEAMS_PAR_POULE = 8
LETTRES_POULES = 'AB'
LIGUE_DEPART = 5          # toutes les nouvelles teams démarrent ici

# Pour l'instant 5 ligues ; on en ajoutera quand il y aura plus de teams.
LIGUES = {
    1: {'poules': 1, 'monte': [], 'descend': [8], 'champion': True},
    2: {'poules': 1, 'monte': [1], 'descend': [8], 'champion': False},
    3: {'poules': 1, 'monte': [1], 'descend': [7, 8], 'champion': False},
    4: {'poules': 2, 'monte': [1], 'descend': [8], 'champion': False},
    5: {'poules': 2, 'monte': [1], 'descend': [], 'champion': False},
}
LIGUE_MAX = max(LIGUES)

# Lancement automatique des ligues (heure de France)
# Une ligue pleine depuis au moins 24 h reçoit son calendrier le dimanche à 15h et commence le lundi à 9h.
# Une journée de matchs tous les 3 jours : lun, jeu, dim, mer, sam, mar, ven -> une saison dure 3 semaines.
DELAI_LIGUE_PLEINE_H = 24
JOUR_CALENDRIER, HEURE_CALENDRIER = 6, 15     # dimanche 15h
JOUR_DEBUT, HEURE_DEBUT = 0, 9                # lundi 9h
JOURS_ENTRE_JOURNEES = 3

# ---------------------------------------------------------------------------
# Jeux : chaque jeu a ses teams, ses ligues, ses saisons et son palmarès.
# Pour ajouter un jeu plus tard : une ligne ici (code, nom, format), puis redémarrer.
# ---------------------------------------------------------------------------
JEUX_DEFAUT = [
    ('brawlstars', 'Brawl Stars', '3v3'),
]
JEU_DEFAUT = 'brawlstars'

# Contact affiché sur le site (page Contact, pied de page, accueil)
CONTACT_EMAIL = 'xhqtournamentsupport@gmail.com'
DISCORD_URL = 'https://discord.gg/cwMZ7VQW74'      # lien d'origine (remplaçable dans Admin → Réglages)
DISCORD_INVITE_REGEX = re.compile(r'^https://(discord\.gg|discord\.com/invite)/[A-Za-z0-9-]{2,32}$')


def lien_discord(db):
    """Lien d'invitation Discord affiché sur le site : celui choisi par le fondateur, sinon celui d'origine."""
    r = db.execute("SELECT valeur FROM parametres WHERE cle = 'discord_invite'").fetchone()
    return r['valeur'] if r and DISCORD_INVITE_REGEX.match(r['valeur'] or '') else DISCORD_URL

# Tout le monde démarre à 0 €
SOLDE_DEPART = 0

# Objets exclusifs, gratuits et purement cosmétiques : (code, nom, description, prix, type)
SHOP_DEFAUT = [
    ('pionnier', 'OG', "Réservé aux 100 premiers inscrits. Une fois les 100 places prises, il ne revient pas.", 0, 'badge'),
    ('saison1', 'Saison 1', "Disponible pendant la saison 1 uniquement. À la fin de la saison, il disparaît du shop.", 0, 'badge'),
    ('champion_tournoi', 'Champion', "Donné à chaque joueur de la team qui gagne un tournoi XHQ. Impossible à récupérer autrement.", 0, 'badge'),
    ('fondateur', 'Fondateur', "Porté uniquement par les 3 fondateurs de la XHQ League. Donné par l'orga, impossible à récupérer.", 0, 'badge'),
    ('admin', 'Admin', "Porté par l'équipe d'organisation (10 admins maximum). Donné avec les droits admin.", 0, 'badge'),
]
# Plus de cadres ni d'avatars au choix : tout le monde a le contour violet, et l'avatar par défaut
OBJETS_RETIRES = ('cadre_or', 'cadre_carbone', 'pack_rare')
CADRE_POUR_TOUS = 'cadre_or'
PIONNIERS_MAX = 100   # badge Pionnier : les 100 premiers inscrits
FONDATEURS_MAX = 3    # badge Fondateur : donné à la main par un admin
ADMINS_MAX = 10       # nombre maximum de comptes admin (ils portent le badge Admin)

# Nombre maximum de joueurs par team (3 titulaires + 2 remplaçants en 3v3)
MAX_MEMBRES = 5

# Barème des matchs (pas de match nul : un BO3 a toujours un gagnant)
POINTS_VICTOIRE = 3
POINTS_DEFAITE = 0
# Valeurs par défaut des réglages modifiables dans Admin → Réglages : (défaut, minimum, maximum, libellé)
REGLAGES = {
    'points_victoire': (3, 1, 10, 'Points pour une victoire'),
    'points_defaite': (0, 0, 5, 'Points pour une défaite'),
    'delai_confirmation_h': (48, 6, 168, 'Heures avant validation automatique d\'un score déclaré'),
    'penalite_par_match': (1, 0, 5, 'Pénalité par match non joué (fin de saison)'),
    'penalite_max': (15, 0, 50, 'Pénalité maximum'),
    'joueurs_min': (3, 1, 5, 'Joueurs minimum pour qu\'une team entre en ligue'),
}
SCORE_MAX = 99

# Fin de saison : nombre de matchs minimum pour monter (ou être champion)
MATCHS_MIN_POUR_MONTER = 1

# Pénalités de fin de saison : -1 pt par match non joué (attendus = teams de la poule - 1),
# doublé en cas de récidive (déjà pénalisée ou retirée la saison d'avant), plafond -15.
PENALITE_PAR_MATCH = 1
MULTIPLICATEUR_RECIDIVE = 2
PENALITE_MAX = 15


# ---------------------------------------------------------------------------
# Base de données
# ---------------------------------------------------------------------------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def colonnes(c, table):
    return {row[1] for row in c.execute(f'PRAGMA table_info({table})')}


def ajouter_colonne(c, table, nom, definition):
    if nom not in colonnes(c, table):
        c.execute(f'ALTER TABLE {table} ADD COLUMN {nom} {definition}')


def lettres_poules(ligue_num):
    return LETTRES_POULES[:LIGUES[ligue_num]['poules']]


def choisir_poule(c, jeu_id, ligue_num):
    """Première poule qui a encore de la place (A, puis B...). Si tout est plein, la moins remplie."""
    lettres = lettres_poules(ligue_num)
    compte = {l: 0 for l in lettres}
    for row in c.execute("""SELECT l.poule, COUNT(*) AS n FROM ligue l JOIN teams t ON t.id = l.team_id
                            WHERE t.jeu_id = ? AND l.ligue_num = ? GROUP BY l.poule""", (jeu_id, ligue_num)):
        if row['poule'] in compte:
            compte[row['poule']] = row['n']
    for l in lettres:
        if compte[l] < TEAMS_PAR_POULE:
            return l
    return min(lettres, key=lambda l: (compte[l], l))


def supprimer_team(c, team_id):
    try:
        f = c.execute('SELECT logo, banniere FROM teams WHERE id = ?', (team_id,)).fetchone()
        if f:
            supprimer_media('logos', f['logo'])
            supprimer_media('bannieres', f['banniere'])
    except sqlite3.OperationalError:
        pass
    c.execute('DELETE FROM ligue WHERE team_id = ?', (team_id,))
    try:   # une team supprimée ne reste pas inscrite à un tournoi qui n'a pas commencé (sinon : équipe fantôme)
        c.execute("""DELETE FROM tournoi_inscriptions WHERE team_id = ? AND tournoi_id IN
                     (SELECT id FROM tournois WHERE statut = 'inscriptions')""", (team_id,))
        c.execute('DELETE FROM rencontres WHERE match_id IS NULL AND (team1_id = ? OR team2_id = ?)', (team_id, team_id))
    except sqlite3.OperationalError:
        pass
    c.execute('DELETE FROM team_members WHERE team_id = ?', (team_id,))
    c.execute('UPDATE users SET team_id = NULL WHERE team_id = ?', (team_id,))
    c.execute('DELETE FROM teams WHERE id = ?', (team_id,))


def retirer_membre(c, user_id, jeu_id):
    """Retire un joueur de sa team dans ce jeu. Si c'était le capitaine : un autre membre prend la main,
    ou la team est supprimée s'il était seul."""
    row = c.execute('SELECT team_id FROM team_members WHERE user_id = ? AND jeu_id = ?', (user_id, jeu_id)).fetchone()
    if not row:
        return
    team_id = row['team_id']
    c.execute('DELETE FROM team_members WHERE user_id = ? AND jeu_id = ?', (user_id, jeu_id))
    team = c.execute('SELECT createur_id FROM teams WHERE id = ?', (team_id,)).fetchone()
    if team and team['createur_id'] == user_id:
        successeur = c.execute(
            'SELECT user_id FROM team_members WHERE team_id = ? ORDER BY joined_at, id LIMIT 1', (team_id,)
        ).fetchone()
        if successeur:
            c.execute('UPDATE teams SET createur_id = ? WHERE id = ?', (successeur['user_id'], team_id))
        else:
            supprimer_team(c, team_id)
            return
    if c.execute('SELECT 1 FROM teams WHERE id = ?', (team_id,)).fetchone():
        verifier_effectif(c, team_id)


def reparer_coherence(c):
    """Remet la base d'aplomb (utile pour les anciennes versions de tournois.db)."""
    # Membres orphelins
    c.execute('DELETE FROM team_members WHERE team_id NOT IN (SELECT id FROM teams) OR user_id NOT IN (SELECT id FROM users)')
    # Un membre est toujours rattaché au jeu de sa team
    c.execute('UPDATE team_members SET jeu_id = (SELECT jeu_id FROM teams WHERE teams.id = team_members.team_id)')
    # Teams dont le créateur n'est plus membre : transfert ou suppression
    for team in c.execute('SELECT id, createur_id FROM teams').fetchall():
        createur_ok = c.execute('SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?',
                                (team['id'], team['createur_id'])).fetchone()
        if createur_ok:
            continue
        successeur = c.execute('SELECT user_id FROM team_members WHERE team_id = ? ORDER BY joined_at, id LIMIT 1',
                               (team['id'],)).fetchone()
        if successeur:
            c.execute('UPDATE teams SET createur_id = ? WHERE id = ?', (successeur['user_id'], team['id']))
        else:
            supprimer_team(c, team['id'])
    # Ligue : une ligne par team, ligue valide, poule valide
    c.execute('DELETE FROM ligue WHERE team_id NOT IN (SELECT id FROM teams)')
    c.execute(f'UPDATE ligue SET ligue_num = {LIGUE_DEPART}, poule = NULL WHERE ligue_num IS NULL OR ligue_num NOT BETWEEN 1 AND {LIGUE_MAX}')
    c.execute(f"""INSERT INTO ligue (team_id, ligue_num, points)
                  SELECT id, {LIGUE_DEPART}, 0 FROM teams
                  WHERE COALESCE(hors_classement, 0) = 0 AND attente_depuis IS NULL AND COALESCE(en_formation, 0) = 0
                  AND id NOT IN (SELECT team_id FROM ligue)""")
    for row in c.execute('SELECT l.id, l.ligue_num, l.poule, t.jeu_id FROM ligue l JOIN teams t ON t.id = l.team_id '
                         'ORDER BY l.id').fetchall():
        if not row['poule'] or row['poule'] not in lettres_poules(row['ligue_num']):
            c.execute('UPDATE ligue SET poule = ? WHERE id = ?', (choisir_poule(c, row['jeu_id'], row['ligue_num']), row['id']))


def migrer_multi_jeux(c):
    """Ajoute la notion de jeu. Les données existantes sont rattachées au premier jeu (Brawl Stars)."""
    c.execute("""CREATE TABLE IF NOT EXISTS jeux (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     code TEXT UNIQUE NOT NULL,
                     nom TEXT NOT NULL,
                     format TEXT DEFAULT '',
                     ordre INTEGER DEFAULT 0
                 )""")
    for i, (code, nom, format_) in enumerate(JEUX_DEFAUT):
        if c.execute('SELECT 1 FROM jeux WHERE code = ?', (code,)).fetchone():
            c.execute('UPDATE jeux SET nom = ?, format = ?, ordre = ? WHERE code = ?', (nom, format_, i, code))
        else:
            c.execute('INSERT INTO jeux (code, nom, format, ordre) VALUES (?, ?, ?, ?)', (code, nom, format_, i))
    defaut = c.execute('SELECT id FROM jeux WHERE code = ?', (JEU_DEFAUT,)).fetchone()[0]
    ajouter_colonne(c, 'jeux', 'max_membres', f'INTEGER DEFAULT {MAX_MEMBRES}')

    ajouter_colonne(c, 'teams', 'jeu_id', 'INTEGER')
    c.execute('UPDATE teams SET jeu_id = ? WHERE jeu_id IS NULL', (defaut,))
    ajouter_colonne(c, 'matchs', 'jeu_id', 'INTEGER')
    c.execute('UPDATE matchs SET jeu_id = ? WHERE jeu_id IS NULL', (defaut,))
    ajouter_colonne(c, 'saison_classements', 'jeu_id', 'INTEGER')
    c.execute('UPDATE saison_classements SET jeu_id = ? WHERE jeu_id IS NULL', (defaut,))

    # team_members : « un joueur = une team » devient « un joueur = une team PAR JEU »
    if 'jeu_id' not in colonnes(c, 'team_members'):
        c.execute("""CREATE TABLE team_members_v2 (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         team_id INTEGER NOT NULL,
                         user_id INTEGER NOT NULL,
                         jeu_id INTEGER NOT NULL,
                         joined_at TEXT,
                         UNIQUE (user_id, jeu_id)
                     )""")
        # anciennes bases : le lien était parfois seulement dans users.team_id
        c.execute("""INSERT OR IGNORE INTO team_members (team_id, user_id, joined_at)
                     SELECT team_id, id, datetime('now') FROM users
                     WHERE team_id IS NOT NULL AND team_id IN (SELECT id FROM teams)""")
        c.execute("""INSERT OR IGNORE INTO team_members_v2 (id, team_id, user_id, jeu_id, joined_at)
                     SELECT tm.id, tm.team_id, tm.user_id, COALESCE(t.jeu_id, ?), tm.joined_at
                     FROM team_members tm LEFT JOIN teams t ON t.id = tm.team_id""", (defaut,))
        c.execute('DROP TABLE team_members')
        c.execute('ALTER TABLE team_members_v2 RENAME TO team_members')

    # saisons : numérotées séparément pour chaque jeu
    if 'jeu_id' not in colonnes(c, 'saisons'):
        c.execute("""CREATE TABLE saisons_v2 (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         jeu_id INTEGER NOT NULL,
                         numero INTEGER NOT NULL,
                         cloturee_at TEXT,
                         cloturee_par INTEGER,
                         UNIQUE (jeu_id, numero)
                     )""")
        c.execute("""INSERT INTO saisons_v2 (id, jeu_id, numero, cloturee_at, cloturee_par)
                     SELECT id, ?, numero, cloturee_at, cloturee_par FROM saisons""", (defaut,))
        c.execute('DROP TABLE saisons')
        c.execute('ALTER TABLE saisons_v2 RENAME TO saisons')


def creer_tables_tournois(c):
    c.executescript("""
        CREATE TABLE IF NOT EXISTS tournois (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            jeu_id INTEGER NOT NULL,
            nom TEXT NOT NULL,
            description TEXT DEFAULT '',
            quand TEXT DEFAULT '',
            places INTEGER NOT NULL,
            statut TEXT NOT NULL DEFAULT 'inscriptions',
            champion_team_id INTEGER,
            champion_nom TEXT,
            created_at TEXT,
            termine_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tournoi_inscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournoi_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            team_nom TEXT,
            poule TEXT,
            created_at TEXT,
            UNIQUE (tournoi_id, team_id)
        );
        CREATE TABLE IF NOT EXISTS tournoi_matchs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournoi_id INTEGER NOT NULL,
            phase TEXT NOT NULL,
            poule TEXT,
            tour INTEGER NOT NULL DEFAULT 1,
            position INTEGER NOT NULL DEFAULT 0,
            team1_id INTEGER,
            team2_id INTEGER,
            score1 INTEGER,
            score2 INTEGER,
            vainqueur_id INTEGER,
            bye INTEGER DEFAULT 0
        );
    """)


def notifier(c, user_ids, texte, lien=None):
    """Ajoute une notification (cloche de la barre du haut) à chacun de ces joueurs."""
    for uid in {u for u in user_ids if u}:
        c.execute("INSERT INTO notifications (user_id, texte, lien, created_at) VALUES (?, ?, ?, datetime('now'))",
                  (uid, texte[:200], lien))


def membres_de(c, team_id):
    return [r[0] for r in c.execute('SELECT user_id FROM team_members WHERE team_id = ?', (team_id,))]


def handle_depuis(texte):
    """Pseudo -> identifiant valide : minuscules, sans accents ni espaces."""
    import unicodedata
    t = unicodedata.normalize('NFKD', texte or '').encode('ascii', 'ignore').decode().lower()
    t = re.sub(r'[^a-z0-9_.]', '', t.replace(' ', '_')).strip('.')[:20]
    return t if len(t) >= 3 else (t + 'joueur')[:20]


def attribuer_handle(c, user_id, pseudo):
    """Donne un identifiant libre (pseudo, sinon pseudo2, pseudo3...) à un compte qui n'en a pas."""
    base = handle_depuis(pseudo)
    essai, n = base, 1
    while (c.execute('SELECT 1 FROM users WHERE handle = ? AND id != ?', (essai, user_id)).fetchone() or essai in HANDLES_RESERVES
           or c.execute("""SELECT 1 FROM anciens_handles WHERE handle = ? AND user_id != ?
                           AND libere_at > datetime('now', '-90 days')""", (essai, user_id)).fetchone()):
        n += 1
        essai = base[:20 - len(str(n))] + str(n)
    c.execute('UPDATE users SET handle = ? WHERE id = ?', (essai, user_id))
    return essai


def sceau_journal(precedent, ligne):
    """Empreinte d'une ligne du journal, liée à la ligne précédente et à la clé secrète du site :
    modifier, insérer ou effacer une ligne casse la chaîne, et on ne peut pas la recalculer sans la clé."""
    import hmac
    import hashlib
    contenu = '|'.join(str(ligne[k] if ligne[k] is not None else '') for k in ('id', 'admin_id', 'admin_nom', 'action', 'cible', 'ip', 'created_at'))
    return hmac.new(app.secret_key.encode(), f'{precedent}|{contenu}'.encode(), hashlib.sha256).hexdigest()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            username TEXT UNIQUE,
            password TEXT,
            bio TEXT DEFAULT '',
            profile_pic TEXT DEFAULT '👤',
            team_id INTEGER,
            solde INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT UNIQUE,
            createur_id INTEGER,
            bio TEXT DEFAULT '',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS team_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL UNIQUE,
            joined_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ligue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER UNIQUE,
            ligue_num INTEGER DEFAULT 5,
            points INTEGER DEFAULT 0,
            poule TEXT
        );
        CREATE TABLE IF NOT EXISTS shop_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT,
            description TEXT DEFAULT '',
            prix INTEGER,
            type TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            bought_at TEXT
        );
        CREATE TABLE IF NOT EXISTS matchs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ligue_num INTEGER NOT NULL,
            poule TEXT NOT NULL,
            team1_id INTEGER,
            team2_id INTEGER,
            team1_nom TEXT,
            team2_nom TEXT,
            score1 INTEGER NOT NULL,
            score2 INTEGER NOT NULL,
            points1 INTEGER NOT NULL,
            points2 INTEGER NOT NULL,
            saisi_par INTEGER,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS saisons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero INTEGER UNIQUE NOT NULL,
            cloturee_at TEXT,
            cloturee_par INTEGER
        );
        CREATE TABLE IF NOT EXISTS saison_classements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            saison INTEGER NOT NULL,
            team_id INTEGER,
            team_nom TEXT,
            ligue_num INTEGER,
            poule TEXT,
            rang INTEGER,
            points INTEGER,
            joues INTEGER,
            victoires INTEGER,
            defaites INTEGER,
            diff INTEGER,
            mouvement TEXT,
            destination INTEGER
        );
        CREATE TABLE IF NOT EXISTS contact_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            nom TEXT,
            email TEXT,
            sujet TEXT,
            message TEXT,
            created_at TEXT
        );
    """)

    # Migration des anciennes bases (colonnes ajoutées depuis)
    ajouter_colonne(c, 'users', 'created_at', 'TEXT')
    ajouter_colonne(c, 'teams', 'created_at', 'TEXT')
    ajouter_colonne(c, 'ligue', 'poule', 'TEXT')
    ajouter_colonne(c, 'shop_items', 'description', "TEXT DEFAULT ''")
    ajouter_colonne(c, 'shop_items', 'type', "TEXT DEFAULT ''")
    ajouter_colonne(c, 'users', 'is_admin', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'contact_messages', 'traite', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'matchs', 'saison', 'INTEGER DEFAULT 1')
    ajouter_colonne(c, 'teams', 'hors_classement', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'teams', 'attente_depuis', 'TEXT')          # liste d'attente (toutes les places prises)
    ajouter_colonne(c, 'teams', 'en_formation', 'INTEGER DEFAULT 0')  # pas encore assez de joueurs pour entrer en ligue
    ajouter_colonne(c, 'saison_classements', 'penalite', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'saison_classements', 'matchs_manques', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'saison_classements', 'points_avant', 'INTEGER')
    ajouter_colonne(c, 'saison_classements', 'recidive', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'shop_items', 'code', 'TEXT')
    ajouter_colonne(c, 'users', 'badge_equipe', 'TEXT')
    ajouter_colonne(c, 'users', 'cadre_equipe', 'TEXT')
    c.execute('CREATE TABLE IF NOT EXISTS parametres (cle TEXT PRIMARY KEY, valeur TEXT)')
    migrer_multi_jeux(c)
    creer_tables_tournois(c)
    c.execute('CREATE TABLE IF NOT EXISTS limites (cle TEXT NOT NULL, created_at TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS team_exclusions (team_id INTEGER, user_id INTEGER, created_at TEXT)')
    c.execute("""CREATE TABLE IF NOT EXISTS journal_admin (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER,
                 admin_nom TEXT, action TEXT, cible TEXT, ip TEXT, created_at TEXT)""")
    c.execute('CREATE TABLE IF NOT EXISTS anciens_handles (handle TEXT, user_id INTEGER, libere_at TEXT)')
    # Annonces de l'orga (publiées tout de suite ou programmées), et badges créés depuis l'admin
    c.execute("""CREATE TABLE IF NOT EXISTS annonces (id INTEGER PRIMARY KEY AUTOINCREMENT, titre TEXT NOT NULL, texte TEXT,
                 lien TEXT, publier_at TEXT NOT NULL, fin_at TEXT, notifier INTEGER DEFAULT 1, discord INTEGER DEFAULT 0,
                 publiee INTEGER DEFAULT 0, discord_ok INTEGER, auteur_id INTEGER, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS discord_file (id INTEGER PRIMARY KEY AUTOINCREMENT, contenu TEXT,
                 created_at TEXT, envoye_at TEXT, essais INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS tournois_recurrents (id INTEGER PRIMARY KEY AUTOINCREMENT, jeu_id INTEGER, nom TEXT,
                 places INTEGER, description TEXT, jour INTEGER, heure TEXT, ouvrir_avant_jours INTEGER DEFAULT 5,
                 actif INTEGER DEFAULT 1, dernier_pour TEXT, compteur INTEGER DEFAULT 0, created_at TEXT)""")
    for col, definition in (('actif', 'INTEGER DEFAULT 1'), ('mode', "TEXT DEFAULT 'libre'"), ('quantite_max', 'INTEGER'),
                            ('debut_at', 'TEXT'), ('fin_at', 'TEXT'), ('style', "TEXT DEFAULT 'violet'"), ('perso', 'INTEGER DEFAULT 0')):
        ajouter_colonne(c, 'shop_items', col, definition)
    ajouter_colonne(c, 'journal_admin', 'hachage', 'TEXT')
    # Journal ineffaçable : la base elle-même refuse toute suppression ou modification
    # (seule exception : poser l'empreinte d'une ligne qui vient d'être ajoutée)
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'journal_pas_de_suppression'").fetchone():
        precedent = 'debut'
        for ligne in c.execute('SELECT * FROM journal_admin ORDER BY id').fetchall():
            empreinte = ligne['hachage'] or sceau_journal(precedent, ligne)
            if not ligne['hachage']:
                c.execute('UPDATE journal_admin SET hachage = ? WHERE id = ?', (empreinte, ligne['id']))
            precedent = empreinte
        c.execute("""CREATE TRIGGER journal_pas_de_suppression BEFORE DELETE ON journal_admin
                     BEGIN SELECT RAISE(ABORT, 'Le journal des admins ne peut pas être effacé'); END""")
        c.execute("""CREATE TRIGGER journal_pas_de_modification BEFORE UPDATE ON journal_admin WHEN OLD.hachage IS NOT NULL
                     BEGIN SELECT RAISE(ABORT, 'Le journal des admins ne peut pas être modifié'); END""")
    # Un objet ne peut être possédé qu'une fois (deux clics simultanés ne le donnent pas deux fois)
    c.execute('DELETE FROM inventory WHERE id NOT IN (SELECT MIN(id) FROM inventory GROUP BY user_id, item_id)')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS inventory_unique ON inventory (user_id, item_id)')
    c.execute('CREATE INDEX IF NOT EXISTS limites_cle ON limites (cle, created_at)')
    ajouter_colonne(c, 'users', 'session_v', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'users', 'email_verifie', 'INTEGER DEFAULT 1')     # comptes d'avant : déjà valables
    ajouter_colonne(c, 'users', 'verif_jeton', 'TEXT')                    # empreinte du lien de confirmation
    ajouter_colonne(c, 'users', 'verif_envoye_at', 'TEXT')
    ajouter_colonne(c, 'users', 'echecs_connexion', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'users', 'verrou_niveau', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'users', 'verrou_definitif', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'users', 'bloque_jusqua', 'TEXT')
    c.execute("""CREATE TABLE IF NOT EXISTS declarations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        jeu_id INTEGER NOT NULL,
        saison INTEGER NOT NULL,
        team1_id INTEGER NOT NULL,      -- la team qui déclare
        team2_id INTEGER NOT NULL,      -- l'adversaire, qui confirme ou conteste
        score1 INTEGER NOT NULL,
        score2 INTEGER NOT NULL,
        par_user_id INTEGER,
        statut TEXT NOT NULL DEFAULT 'attente',   -- attente, valide, conteste, refuse, annule
        motif TEXT,
        reponse TEXT,                   -- confirme, auto (48 h sans réponse), admin
        match_id INTEGER,
        created_at TEXT,
        traite_at TEXT)""")
    # Garde-fous dans la base elle-même (même en cas de clics simultanés) :
    # une seule déclaration en cours par paire d'équipes, pas deux pseudos / noms de team identiques à la casse près
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS declarations_en_cours ON declarations
                 (min(team1_id, team2_id), max(team1_id, team2_id)) WHERE statut IN ('attente', 'conteste', 'traitement')""")
    for table, colonne, index in (('users', 'username', 'users_pseudo_unique'), ('teams', 'nom', 'teams_nom_unique')):
        doublons = c.execute(f'SELECT 1 FROM {table} GROUP BY lower({colonne}) HAVING COUNT(*) > 1').fetchone()
        if not doublons:
            c.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS {index} ON {table} (lower({colonne}))')
    ajouter_colonne(c, 'tournois', 'debut', 'TEXT')
    ajouter_colonne(c, 'tournois', 'recompense', "TEXT DEFAULT ''")   # ce que gagne le champion (texte libre, remis à la main)
    ajouter_colonne(c, 'tournois', 'recompense_envoyee_at', 'TEXT')
    ajouter_colonne(c, 'tournois_recurrents', 'recompense', "TEXT DEFAULT ''")
    ajouter_colonne(c, 'tournois', 'lance_at', 'TEXT')   # début réel (lancement des poules)   # 'AAAA-MM-JJTHH:MM' (heure de France)
    c.execute("UPDATE users SET created_at = datetime('now') WHERE created_at IS NULL")
    c.execute("UPDATE teams SET created_at = datetime('now') WHERE created_at IS NULL")
    tous_avatars = AVATARS + AVATARS_PREMIUM
    c.execute("UPDATE users SET profile_pic = ? WHERE profile_pic IS NULL OR profile_pic NOT IN (%s)"
              % ','.join('?' * len(tous_avatars)), (AVATAR_DEFAUT, *tous_avatars))

    # Migration unique : l'ancien badge « Fondateur » des 100 premiers devient « Pionnier »
    # (ceux qui l'ont déjà le gardent) ; « Fondateur » est désormais réservé à 3 comptes.
    if not c.execute("SELECT 1 FROM parametres WHERE cle = 'badges_v3'").fetchone():
        if not c.execute("SELECT 1 FROM shop_items WHERE code = 'pionnier'").fetchone():
            c.execute("UPDATE shop_items SET code = 'pionnier' WHERE code = 'fondateur'")
            c.execute("UPDATE users SET badge_equipe = 'pionnier' WHERE badge_equipe = 'fondateur'")
        c.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES ('badges_v3', '1')")

    # Objets exclusifs gratuits
    for code, nom, description, prix, type_ in SHOP_DEFAUT:
        if c.execute('SELECT 1 FROM shop_items WHERE code = ?', (code,)).fetchone():
            c.execute('UPDATE shop_items SET nom = ?, description = ?, prix = ?, type = ? WHERE code = ?',
                      (nom, description, prix, type_, code))
        else:
            c.execute('INSERT INTO shop_items (code, nom, description, prix, type) VALUES (?, ?, ?, ?, ?)',
                      (code, nom, description, prix, type_))

    # Migration unique : cadres et avatars au choix supprimés (contour violet pour tout le monde)
    if not c.execute("SELECT 1 FROM parametres WHERE cle = 'cosmetiques_v4'").fetchone():
        c.execute('DELETE FROM inventory WHERE item_id IN (SELECT id FROM shop_items WHERE code IN (%s))' % ','.join('?' * len(OBJETS_RETIRES)), OBJETS_RETIRES)
        c.execute('DELETE FROM shop_items WHERE code IN (%s)' % ','.join('?' * len(OBJETS_RETIRES)), OBJETS_RETIRES)
        c.execute('UPDATE users SET cadre_equipe = NULL, profile_pic = ?', (AVATAR_DEFAUT,))
        c.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES ('cosmetiques_v4', '1')")

    # Le badge Admin suit les droits admin
    c.execute("""INSERT OR IGNORE INTO inventory (user_id, item_id, bought_at)
                 SELECT u.id, s.id, datetime('now') FROM users u, shop_items s
                 WHERE u.is_admin = 1 AND s.code = 'admin'
                 AND NOT EXISTS (SELECT 1 FROM inventory i WHERE i.user_id = u.id AND i.item_id = s.id)""")
    c.execute("""DELETE FROM inventory WHERE item_id IN (SELECT id FROM shop_items WHERE code = 'admin')
                 AND user_id IN (SELECT id FROM users WHERE COALESCE(is_admin, 0) = 0)""")
    c.execute("UPDATE users SET badge_equipe = NULL WHERE badge_equipe = 'admin' AND COALESCE(is_admin, 0) = 0")

    # Migration unique : fin du boost et de l'argent de départ
    if not c.execute("SELECT 1 FROM parametres WHERE cle = 'shop_v2'").fetchone():
        # Les anciens « Skin Premium » gardent leurs avantages (cadre or + avatars rares)
        anciens_skins = [r[0] for r in c.execute(
            "SELECT DISTINCT i.user_id FROM inventory i JOIN shop_items s ON s.id = i.item_id WHERE s.nom = 'Skin Premium'")]
        nouveaux = {code: r[0] for code in ('cadre_or', 'pack_rare')
                    for r in [c.execute('SELECT id FROM shop_items WHERE code = ?', (code,)).fetchone()] if r}
        for uid in anciens_skins:
            for code, item_id in nouveaux.items():
                c.execute("INSERT OR IGNORE INTO inventory (user_id, item_id, bought_at) VALUES (?, ?, datetime('now'))", (uid, item_id))
            c.execute("UPDATE users SET cadre_equipe = 'cadre_or' WHERE id = ?", (uid,))
        c.execute('DELETE FROM inventory WHERE item_id IN (SELECT id FROM shop_items WHERE code IS NULL)')
        c.execute('DELETE FROM shop_items WHERE code IS NULL')   # Badge Officiel, Skin Premium, Boost 2x
        c.execute('DROP TABLE IF EXISTS boosts')
        c.execute('UPDATE users SET solde = ?', (SOLDE_DEPART,))
        c.execute("INSERT INTO parametres (cle, valeur) VALUES ('shop_v2', datetime('now'))")

    # Profils : photo, bannière, identifiant @
    ajouter_colonne(c, 'users', 'photo', 'TEXT')
    ajouter_colonne(c, 'users', 'banniere', 'TEXT')
    ajouter_colonne(c, 'users', 'banniere_pos', 'INTEGER DEFAULT 50')
    ajouter_colonne(c, 'users', 'banniere_zoom', 'INTEGER DEFAULT 100')
    ajouter_colonne(c, 'users', 'handle', 'TEXT')
    ajouter_colonne(c, 'users', 'handle_changements', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'users', 'handle_modifie_at', 'TEXT')
    # Migration unique : pseudo et @identifiant ne font plus qu'un ; chacun repart avec ses 3 changements
    if not c.execute("SELECT 1 FROM parametres WHERE cle = 'pseudo_unique'").fetchone():
        c.execute('UPDATE users SET handle_changements = 0, handle_modifie_at = NULL')
        c.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES ('pseudo_unique', '1')")

    for col, definition in (('logo', 'TEXT'), ('embleme', 'TEXT'), ('banniere', 'TEXT'),
                            ('banniere_pos', 'INTEGER DEFAULT 50'), ('banniere_zoom', 'INTEGER DEFAULT 100')):
        ajouter_colonne(c, 'teams', col, definition)
    ajouter_colonne(c, 'users', 'bloque', 'INTEGER DEFAULT 0')
    c.execute("""CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        texte TEXT NOT NULL,
        lien TEXT,
        lu INTEGER DEFAULT 0,
        created_at TEXT)""")
    c.execute('CREATE INDEX IF NOT EXISTS idx_notifs_user ON notifications(user_id, lu)')
    c.execute("""CREATE TABLE IF NOT EXISTS reinitialisations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        jeton_hash TEXT NOT NULL,
        expire_at TEXT NOT NULL,
        utilise INTEGER DEFAULT 0,
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS rencontres (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        jeu_id INTEGER NOT NULL,
        saison INTEGER NOT NULL,
        ligue_num INTEGER NOT NULL,
        poule TEXT NOT NULL,
        journee INTEGER,
        team1_id INTEGER,
        team2_id INTEGER,
        date TEXT,
        match_id INTEGER,
        created_at TEXT)""")
    # Calendrier : jamais deux fois la même rencontre dans une saison (double clic sur « Générer »)
    if not c.execute(
            'SELECT 1 FROM rencontres GROUP BY jeu_id, saison, min(team1_id, team2_id), max(team1_id, team2_id) HAVING COUNT(*) > 1').fetchone():
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS rencontres_unique ON rencontres (jeu_id, saison, min(team1_id, team2_id), max(team1_id, team2_id))')
    for r in c.execute('SELECT id, username FROM users WHERE handle IS NULL').fetchall():
        attribuer_handle(c, r['id'], r['username'])
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_handle ON users(handle)')

    reparer_coherence(c)
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# Session / utilisateur connecté
# ---------------------------------------------------------------------------
def utilisateur_connecte():
    uid = session.get('user_id')
    if uid is None:
        return None
    if 'user' not in g:
        g.user = get_db().execute('SELECT id, username, email, profile_pic, is_admin, photo, handle, bloque, session_v, email_verifie FROM users WHERE id = ?', (uid,)).fetchone()
        # Compte supprimé, suspendu, ou mot de passe changé depuis l'ouverture de cette session : on déconnecte
        if g.user is None or g.user['bloque'] or version_session(uid) != (g.user['session_v'] or 0):
            session.clear()
            g.user = None
    return g.user


def liste_jeux(db):
    return [dict(r) for r in db.execute('SELECT id, code, nom, format FROM jeux ORDER BY ordre, id')]


def reglage(cle, db=None):
    """Valeur d'un réglage de la ligue (Admin → Réglages), mise en cache le temps de la requête."""
    cache = g.setdefault('reglages', {}) if has_request_context() else {}
    if cle not in cache:
        db = db or get_db()
        r = db.execute('SELECT valeur FROM parametres WHERE cle = ?', (f'reglage_{cle}',)).fetchone()
        defaut, mini, maxi, _ = REGLAGES[cle]
        try:
            cache[cle] = min(maxi, max(mini, int(r['valeur']))) if r else defaut
        except (TypeError, ValueError):
            cache[cle] = defaut
    return cache[cle]


def max_membres_jeu(db, jeu_id):
    """Joueurs maximum par team pour ce jeu (réglé dans Admin → Automatique → Jeux)."""
    r = db.execute('SELECT max_membres FROM jeux WHERE id = ?', (jeu_id,)).fetchone() if jeu_id else None
    return (r['max_membres'] if r and r['max_membres'] else MAX_MEMBRES)


def trouver_jeu(db, code=None):
    """Jeu demandé (paramètre ?jeu=code ou champ « jeu » du JSON), sinon le jeu par défaut. None si inconnu."""
    if code is None and has_request_context():
        data = request.get_json(silent=True)
        code = request.args.get('jeu') or (data.get('jeu') if isinstance(data, dict) else None)
    code = code or JEU_DEFAUT
    row = db.execute('SELECT id, code, nom, format FROM jeux WHERE code = ?', (str(code),)).fetchone()
    return dict(row) if row else None


def jeu_ou_erreur(db):
    jeu = trouver_jeu(db)
    if not jeu:
        return None, (jsonify({'success': False, 'error': 'Jeu inconnu'}), 404)
    return jeu, None


def jeu_de_team(db, team_id):
    row = db.execute('SELECT jeu_id FROM teams WHERE id = ?', (team_id,)).fetchone()
    return row['jeu_id'] if row else None


def jeu_defaut_id(db):
    return db.execute('SELECT id FROM jeux WHERE code = ?', (JEU_DEFAUT,)).fetchone()['id']


def logo_valide(logo):
    return logo if logo in AVATARS or logo in AVATARS_PREMIUM else AVATAR_DEFAUT


# ---------------------------------------------------------------------------
# Effets du shop
# ---------------------------------------------------------------------------
def possede(db, user_id, code):
    """Le joueur a-t-il récupéré cet objet ?"""
    return db.execute("""SELECT 1 FROM inventory i JOIN shop_items s ON s.id = i.item_id
                         WHERE i.user_id = ? AND s.code = ?""", (user_id, code)).fetchone() is not None


def apparence(db, user_id):
    """Badge et cadre équipés (seulement s'ils sont bien possédés)."""
    u = db.execute('SELECT badge_equipe, cadre_equipe FROM users WHERE id = ?', (user_id,)).fetchone()
    badge = u['badge_equipe'] if u and u['badge_equipe'] and possede(db, user_id, u['badge_equipe']) else None
    cadre = CADRE_POUR_TOUS     # contour violet pour tout le monde
    nom_badge = None
    if badge:
        nom_badge = db.execute('SELECT nom FROM shop_items WHERE code = ?', (badge,)).fetchone()['nom']
    return {'badge': nom_badge, 'badge_code': badge, 'cadre': cadre}


def disponibilite(db, user_id, code):
    """(disponible, raison) pour récupérer un objet."""
    perso = db.execute("""SELECT id, mode, quantite_max, debut_at, fin_at, COALESCE(actif, 1) AS actif FROM shop_items
                          WHERE code = ? AND COALESCE(perso, 0) = 1""", (code,)).fetchone()
    if perso:
        maintenant = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        if not perso['actif']:
            return False, 'Plus disponible'
        if perso['mode'] == 'reserve':
            return False, "Donné par l'orga"
        if perso['debut_at'] and maintenant < perso['debut_at']:
            return False, f"Disponible à partir du {date_paris(perso['debut_at'])}"
        if perso['fin_at'] and maintenant >= perso['fin_at']:
            return False, 'La période est terminée'
        if perso['mode'] == 'limite' and perso['quantite_max'] is not None and porteurs(db, code) >= perso['quantite_max']:
            return False, f"Les {perso['quantite_max']} exemplaires sont partis"
        return True, None
    if code == 'pionnier':
        # Seuls les comptes confirmés comptent : des faux comptes non confirmés ne peuvent pas « pousser »
        # les vrais joueurs hors des 100 premiers
        rang = db.execute('SELECT COUNT(*) FROM users WHERE id <= ? AND COALESCE(email_verifie, 1) = 1', (user_id,)).fetchone()[0]
        if rang > PIONNIERS_MAX:
            return False, f'Réservé aux {PIONNIERS_MAX} premiers inscrits'
    if code == 'fondateur':
        return False, 'Réservé aux 3 fondateurs'
    if code == 'admin':
        return False, "Réservé à l'équipe d'orga"
    if code == 'champion_tournoi':
        return False, 'Gagne un tournoi XHQ pour le débloquer'
    if code == 'saison1' and saison_actuelle(db, jeu_defaut_id(db)) != 1:
        return False, 'Uniquement pendant la saison 1'
    return True, None


@app.context_processor
def injecter_utilisateur():
    user = utilisateur_connecte()
    current_user = None
    if user:
        current_user = {'username': user['username'], 'email': user['email'], 'logo': logo_valide(user['profile_pic']),
                        'is_admin': bool(user['is_admin']), 'photo': url_media('avatars', user['photo']),
                        'handle': user['handle'], 'autres_comptes': autres_comptes(get_db(), user['id']),
                        'notifs': get_db().execute('SELECT COUNT(*) FROM notifications WHERE user_id = ? AND lu = 0',
                                                   (user['id'],)).fetchone()[0],
                        'email_a_confirmer': email_a_confirmer(user),
                        'email_masque': user['email'][:3] + '•' * max(3, len(user['email'].split('@')[0]) - 3) + '@' + user['email'].split('@')[-1],
                        **apparence(get_db(), user['id'])}
    db = get_db()
    jeux = liste_jeux(db)
    jeu = None
    try:
        jeu = trouver_jeu(db)
    except Exception:
        pass
    return {'current_user': current_user, 'jeux': jeux, 'jeu': jeu or (jeux[0] if jeux else None),
            'contact_email': CONTACT_EMAIL, 'discord_url': lien_discord(db)}


def page_connectee(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not utilisateur_connecte():
            return redirect('/login')
        return f(*args, **kwargs)
    return wrapper


def api_connectee(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not utilisateur_connecte():
            return jsonify({'success': False, 'error': 'Non connecté'}), 401
        return f(*args, **kwargs)
    return wrapper


def est_admin():
    user = utilisateur_connecte()
    return bool(user and user['is_admin'])


def page_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not utilisateur_connecte():
            return redirect('/login')
        if not est_admin():
            abort(404)  # la page admin n'existe pas pour les joueurs
        return f(*args, **kwargs)
    return wrapper


def est_protege(db, user_id):
    """Compte admin ou fondateur : seul un fondateur peut y toucher (mot de passe, suspension, droits…)."""
    u = db.execute('SELECT is_admin FROM users WHERE id = ?', (user_id,)).fetchone()
    return bool(u and (u['is_admin'] or possede(db, user_id, 'fondateur')))


def fondateur_requis(db):
    """Message d'erreur si l'admin connecté n'est pas fondateur (alors qu'il existe des fondateurs), sinon None.
    Tant que personne n'a le badge Fondateur, n'importe quel admin peut le donner (mise en route)."""
    if porteurs(db, 'fondateur') == 0 or possede(db, session['user_id'], 'fondateur'):
        return None
    return 'Réservé aux fondateurs (les comptes admin et fondateurs sont protégés)'


def ecrire_journal(c, admin_id, admin_nom, action, cible='', ip=''):
    """Ajoute une ligne scellée au journal. L'insertion prend le verrou d'écriture de la base,
    donc deux actions simultanées ne peuvent pas casser la chaîne."""
    c.execute("INSERT INTO journal_admin (admin_id, admin_nom, action, cible, ip, created_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
              (admin_id, admin_nom, action[:120], str(cible)[:200], ip))
    nouvelle = c.execute('SELECT * FROM journal_admin WHERE id = ?', (c.lastrowid,)).fetchone()
    avant = c.execute('SELECT hachage FROM journal_admin WHERE id < ? ORDER BY id DESC LIMIT 1', (nouvelle['id'],)).fetchone()
    c.execute('UPDATE journal_admin SET hachage = ? WHERE id = ?', (sceau_journal(avant['hachage'] if avant else 'debut', nouvelle), nouvelle['id']))


def verifier_journal(db):
    """(intact, id de la première ligne trafiquée ou None, nombre de lignes)."""
    precedent, n = 'debut', 0
    for ligne in db.execute('SELECT * FROM journal_admin ORDER BY id'):
        n += 1
        if ligne['hachage'] != sceau_journal(precedent, ligne):
            return False, ligne['id'], n
        precedent = ligne['hachage']
    return True, None, n


def journaliser(db, action, cible=''):
    """Garde une trace des actions sensibles des admins (qui, quoi, quand), consultable dans l'admin."""
    u = utilisateur_connecte()
    ecrire_journal(db.cursor() if hasattr(db, 'cursor') else db, u['id'] if u else None, u['username'] if u else 'console',
                   action, cible, ip_client() if has_request_context() else '')


def nom_joueur(db, user_id):
    r = db.execute('SELECT username FROM users WHERE id = ?', (user_id,)).fetchone()
    return f"{r['username']} (#{user_id})" if r else f'#{user_id}'


def nom_team(db, team_id):
    r = db.execute('SELECT nom FROM teams WHERE id = ?', (team_id,)).fetchone()
    return f"{r['nom']} (#{team_id})" if r else f'#{team_id}'


# Ce qu'un admin (non fondateur) peut faire : uniquement la compétition (résultats, litiges, tournois,
# calendrier) et consulter la saison. TOUT le reste est réservé aux fondateurs, y compris toute nouvelle
# action admin ajoutée plus tard tant qu'elle n'est pas inscrite ici (interdit par défaut).
ACTIONS_ADMIN = {
    'api_admin_stats',                                                        # tableau de bord
    'api_admin_teams',                                                        # liste des teams (formulaires de match)
    'api_admin_create_match', 'api_admin_delete_match',                       # résultats
    'api_admin_declarations', 'api_admin_trancher',                           # scores déclarés et litiges
    'api_admin_creer_tournoi', 'api_admin_tournoi_poules', 'api_admin_tournoi_bracket',
    'api_admin_tournoi_score', 'api_admin_supprimer_tournoi',                 # tournois
    'api_admin_generer_calendrier', 'api_admin_modifier_rencontre',          # calendrier
    'api_admin_saison', 'api_admin_lancement',                                # saison (lecture seule)
}
ONGLETS_ADMIN = ('matchs', 'calendrier', 'saison')                            # onglets visibles par un admin


def est_fondateur():
    """Fondateur (ou admin tant qu'aucun fondateur n'existe : mise en route du site)."""
    return est_admin() and fondateur_requis(get_db()) is None


def api_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not utilisateur_connecte():
            return jsonify({'success': False, 'error': 'Non connecté'}), 401
        if not est_admin():
            return jsonify({'success': False, 'error': 'Réservé aux admins'}), 403
        if f.__name__ not in ACTIONS_ADMIN and not est_fondateur():
            return jsonify({'success': False, 'error': 'Réservé aux fondateurs : les admins gèrent les résultats, les tournois et le calendrier'}), 403
        return f(*args, **kwargs)
    return wrapper


def donnees_json():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def sans_invisibles(t):
    """Retire les caractères invisibles ou de contrôle (espaces de largeur nulle, inversion de sens
    d'écriture…) qui servent à imiter un autre nom. Garde les retours à la ligne."""
    import unicodedata
    return ''.join(ch for ch in t if ch in '\n\t' or unicodedata.category(ch) not in ('Cc', 'Cf', 'Co', 'Cs'))


def texte(data, cle, max_len=None):
    valeur = data.get(cle)
    valeur = sans_invisibles(valeur).strip() if isinstance(valeur, str) else ''
    return valeur[:max_len] if max_len else valeur


EMAIL_VALIDE = re.compile(r'^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$')


# Noms (pseudos, teams) : alphabet latin seulement, pour empêcher les sosies (Η grec, а cyrillique…)
NOM_AUTORISE = re.compile(r"^[0-9A-Za-zÀ-ÖØ-öø-ÿŒœ _.'\-!?&#]+$")


def simplifier(texte_brut):
    """Texte ramené à des lettres simples (sans accents, majuscules ni « l33t ») pour repérer les mots interdits."""
    import unicodedata
    sosies = str.maketrans('аеорсухкмтвнАЕОРСУХКМТВНіІјЈѕЅԁɡαοερτυνκιΑΟΕΡΤΥΝΚΙ', 'aeopcyxkmtbhAEOPCYXKMTBHiIjJsSdgaoeptuvkiAOEPTYNKI')
    t = unicodedata.normalize('NFKD', (texte_brut or '').translate(sosies)).encode('ascii', 'ignore').decode().lower()
    return t.translate(str.maketrans({'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't', '@': 'a', '$': 's', '!': 'i'}))


def mot_interdit(texte_brut, db=None):
    """Le premier mot interdit trouvé (liste réglée dans Admin → Réglages), sinon None.
    Les mots de 4 lettres ou moins doivent être des mots entiers ; les plus longs sont cherchés partout
    (« s.a.l.o.p.e » ou « saaaalope » y compris, en ignorant les séparateurs et les lettres répétées)."""
    if not has_request_context() and db is None:
        return None
    db = db or get_db()
    r = db.execute("SELECT valeur FROM parametres WHERE cle = 'mots_interdits'").fetchone()
    mots = [simplifier(m.strip()) for m in (r['valeur'] if r else MOTS_INTERDITS_DEFAUT).splitlines() if m.strip()]
    if not mots:
        return None
    simple = simplifier(texte_brut)
    serre = re.sub(r'(.)\1+', r'\1', re.sub(r'[^a-z]', '', simple))
    for mot in mots:
        mot_serre = re.sub(r'(.)\1+', r'\1', re.sub(r'[^a-z]', '', mot))
        if not mot_serre:
            continue
        if len(mot_serre) <= 4:
            if re.search(r'(?<![a-z])' + re.escape(mot) + r'(?![a-z])', simple):
                return mot
        elif mot_serre in serre:
            return mot
    return None


def nom_refuse(nom, quoi, admin=False):
    """Message d'erreur si ce nom (pseudo ou team) n'est pas acceptable, sinon None."""
    if not NOM_AUTORISE.match(nom):
        return f"{quoi} : lettres, chiffres, espaces et . _ - ' ! ? & # seulement"
    if mot_interdit(nom):
        return f"{quoi} refusé : il contient un mot interdit"
    import unicodedata
    simple = re.sub(r'[^a-z]', '', unicodedata.normalize('NFKD', nom).encode('ascii', 'ignore').decode().lower())
    if not admin and simple.startswith('xhq'):
        return f"{quoi} réservé à l'équipe d'orga XHQ"
    return None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def chiffres_jeu(db, jeu_id):
    saison = saison_actuelle(db, jeu_id)
    return {
        'saison': saison,
        'teams': db.execute('SELECT COUNT(*) FROM ligue l JOIN teams t ON t.id = l.team_id WHERE t.jeu_id = ?',
                            (jeu_id,)).fetchone()[0],
        'joueurs': db.execute('SELECT COUNT(*) FROM team_members WHERE jeu_id = ?', (jeu_id,)).fetchone()[0],
        'matchs': db.execute('SELECT COUNT(*) FROM matchs WHERE jeu_id = ? AND saison = ?',
                             (jeu_id, saison)).fetchone()[0],
        'places': sum(c['poules'] for c in LIGUES.values()) * TEAMS_PAR_POULE,
        'attente': db.execute('SELECT COUNT(*) FROM teams WHERE jeu_id = ? AND attente_depuis IS NOT NULL', (jeu_id,)).fetchone()[0],
    }


JOURS_FR = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']
MOIS_FR = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août', 'sept.', 'oct.', 'nov.', 'déc.']


def date_fr(iso):
    """'2026-12-14T18:00' -> 'samedi 14 déc. à 18h00'."""
    d = datetime.strptime(iso, '%Y-%m-%dT%H:%M')
    return f"{JOURS_FR[d.weekday()]} {d.day} {MOIS_FR[d.month - 1]} à {d.hour}h{d.minute:02d}"


def il_y_a(sqlite_date):
    """Date SQLite (UTC) -> « il y a 5 min », « il y a 2 h », « il y a 3 j »."""
    if not sqlite_date:
        return ''
    try:
        d = datetime.strptime(sqlite_date[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return ''
    s = max(0, int((datetime.utcnow() - d).total_seconds()))
    if s < 60:
        return "à l'instant"
    if s < 3600:
        return f'il y a {s // 60} min'
    if s < 86400:
        return f'il y a {s // 3600} h'
    return f'il y a {s // 86400} j'


def forme_des_teams(db, jeu_id, saison):
    """{team_id: ['V', 'D', ...]} : résultats de la saison, du plus récent au plus ancien."""
    forme = {}
    for m in db.execute('SELECT team1_id, team2_id, score1, score2 FROM matchs WHERE jeu_id = ? AND saison = ? ORDER BY id DESC',
                        (jeu_id, saison)):
        forme.setdefault(m['team1_id'], []).append('V' if m['score1'] > m['score2'] else 'D')
        forme.setdefault(m['team2_id'], []).append('V' if m['score2'] > m['score1'] else 'D')
    return forme


def serie_en_cours(resultats):
    n = 0
    for r in resultats:
        if r != 'V':
            break
        n += 1
    return n


def fil_actu(db, jeu_id, limite=8):
    """Derniers évènements du jeu : résultats, nouvelles teams, inscriptions et vainqueurs de tournois."""
    ev = []
    for m in db.execute(REQ_MATCHS + ' WHERE m.jeu_id = ? ORDER BY m.id DESC LIMIT ?', (jeu_id, limite)):
        g, p = (m['team1_nom'], m['team2_nom']) if m['score1'] > m['score2'] else (m['team2_nom'], m['team1_nom'])
        ev.append((m['created_at'], 'match', f"{g} bat {p}", f"{max(m['score1'], m['score2'])}-{min(m['score1'], m['score2'])} · Ligue {m['ligue_num']}"))
    for t in db.execute('SELECT nom, created_at FROM teams WHERE jeu_id = ? ORDER BY id DESC LIMIT ?', (jeu_id, limite)):
        ev.append((t['created_at'], 'team', f"{t['nom']} rejoint la ligue", f'Départ en Ligue {LIGUE_DEPART}'))
    for r in db.execute("""SELECT COALESCE(te.nom, i.team_nom) AS team, t.nom AS tournoi, i.created_at FROM tournoi_inscriptions i
                           JOIN tournois t ON t.id = i.tournoi_id LEFT JOIN teams te ON te.id = i.team_id
                           WHERE t.jeu_id = ? ORDER BY i.id DESC LIMIT ?""", (jeu_id, limite)):
        ev.append((r['created_at'], 'inscription', f"{r['team']} s'inscrit à {r['tournoi']}", 'Tournoi'))
    for t in db.execute("SELECT nom, champion_nom, termine_at FROM tournois WHERE jeu_id = ? AND statut = 'termine' ORDER BY id DESC LIMIT 3",
                        (jeu_id,)):
        ev.append((t['termine_at'], 'champion', f"{t['champion_nom']} remporte {t['nom']}", 'Badge Champion débloqué'))
    ev.sort(key=lambda e: e[0] or '', reverse=True)
    return [{'quand': il_y_a(d), 'type': ty, 'titre': ti, 'detail': de} for d, ty, ti, de in ev[:limite]]


@app.route('/')
def index():
    """Accueil : prochain tournoi, teams à la une, classement de Ligue 1 avec la forme, fil d'actu."""
    db = get_db()
    jeu = trouver_jeu(db) or trouver_jeu(db, JEU_DEFAUT)
    chiffres = chiffres_jeu(db, jeu['id'])
    forme = forme_des_teams(db, jeu['id'], chiffres['saison'])

    ligue1 = classement_poule(db, jeu['id'], 1, lettres_poules(1)[0])
    for p in ligue1:
        p['forme'] = forme.get(p['team_id'], [])[:5]

    # À la une
    leader = next((p for p in ligue1 if p['team_id'] and p['joues']), None)
    en_forme = None
    for r in db.execute('SELECT t.id, t.nom, l.ligue_num FROM teams t JOIN ligue l ON l.team_id = t.id WHERE t.jeu_id = ?', (jeu['id'],)):
        serie = serie_en_cours(forme.get(r['id'], []))
        if serie >= 2 and (not en_forme or serie > en_forme['serie']):
            en_forme = {'nom': r['nom'], 'ligue': r['ligue_num'], 'serie': serie, 'forme': forme[r['id']][:5]}
    dernier_champion = db.execute("""SELECT nom, champion_nom FROM tournois WHERE jeu_id = ? AND statut = 'termine'
                                     ORDER BY termine_at DESC LIMIT 1""", (jeu['id'],)).fetchone()

    # Prochain tournoi : d'abord ceux en cours, puis les inscriptions ouvertes (date la plus proche)
    actifs = [resume_tournoi(db, t) for t in db.execute(
        "SELECT * FROM tournois WHERE jeu_id = ? AND statut != 'termine'", (jeu['id'],))]
    ordre = {'bracket': 0, 'poules': 1, 'inscriptions': 2}
    actifs.sort(key=lambda t: (ordre[t['statut']], t['debut'] or '9999', -t['id']))
    prochain = actifs[0] if actifs else None

    derniers = [dict(r) for r in db.execute(REQ_MATCHS + ' WHERE m.jeu_id = ? AND m.saison = ? ORDER BY m.id DESC LIMIT 6',
                                             (jeu['id'], chiffres['saison']))]
    nb_tournois = db.execute('SELECT COUNT(*) FROM tournois WHERE jeu_id = ?', (jeu['id'],)).fetchone()[0]
    # Les meilleures teams, toutes ligues confondues (la ligue la plus haute d'abord)
    top = [dict(r) for r in db.execute("""SELECT t.id, t.nom, l.ligue_num, l.points FROM ligue l JOIN teams t ON t.id = l.team_id
                                          WHERE t.jeu_id = ? ORDER BY l.ligue_num, l.points DESC, l.id LIMIT 5""", (jeu['id'],))]
    for t in top:
        t['forme'] = forme.get(t['id'], [])[:5]
    prochains_matchs = rencontres_a_venir(db, jeu['id'], limite=6)
    annonce = (annonces_actives(db, 1) or [None])[0]
    textes_accueil = {k: contenu(k, v) for k, v in ACCUEIL_DEFAUT.items()}
    return render_template('index.html', jeu_actuel=jeu, textes=textes_accueil, chiffres=chiffres, ligue1=ligue1, leader=leader, prochains_matchs=prochains_matchs,
                           en_forme=en_forme, dernier_champion=dernier_champion, prochain=prochain,
                           autres_tournois=actifs[1:3], derniers=derniers, top=top, fil=fil_actu(db, jeu['id']),
                           nb_tournois=nb_tournois, nb_ligues=LIGUE_MAX, par_poule=TEAMS_PAR_POULE,
                           ligue_depart=LIGUE_DEPART, palmares=palmares(db, jeu['id'], 1), annonce=annonce)


@app.route('/js-requis', methods=['GET', 'POST'])
def js_requis():
    """Formulaires envoyés sans JavaScript : on ne lit rien (le mot de passe n'est jamais dans une adresse)."""
    return ('<p style="font-family:sans-serif;padding:2rem">Le site a besoin de JavaScript pour se connecter. '
            'Active-le (ou mets ton navigateur à jour), puis <a href="/login">réessaie</a>.</p>'), 400


# ---------------------------------------------------------------------------
# Contenus modifiables dans Admin → Contenu : textes de l'accueil, règlement, FAQ
# ---------------------------------------------------------------------------
REGLEMENT_DEFAUT = """# Les teams
- Une team compte de 3 à {max_membres} joueurs (titulaires et remplaçants). Le capitaine gère la team et déclare les scores.
- Un joueur ne joue que dans une seule team par jeu. Pas de compte multiple, pas de joueur prêté.

# Les ligues
- Il y a 5 ligues : Ligue 1 (la meilleure), 2, 3, puis les Ligues 4 et 5 avec 2 poules chacune. Une poule = 8 teams.
- Une team entre en ligue dès qu'elle a {joueurs_min} joueurs (avant, elle est « en formation »). Elle prend alors la plus haute ligue encore libre : la Ligue 1 se remplit en premier, puis la 2, et ainsi de suite.
- Une team qui repasse sous {joueurs_min} joueurs avant le début de sa ligue perd sa place et repasse en formation.
- Quand toutes les places sont prises, les nouvelles teams passent en liste d'attente (les premières inscrites entrent en premier).

# Le lancement d'une ligue
- Une ligue démarre quand elle est pleine. Le calendrier est publié le dimanche à 15h et les matchs commencent le lundi à 9h.
- Chaque team affronte une fois chaque autre team de sa poule : 7 matchs, un tous les 3 jours. Une saison dure 3 semaines.
- Quand toutes les ligues tournent, elles finissent et recommencent ensemble : fin le dimanche à 15h, reprise le lundi à 9h.

# Les matchs et les scores
- Un match se joue entre la date prévue et la journée suivante. Mettez-vous d'accord sur l'heure entre capitaines.
- Une victoire rapporte {points_victoire} points, une défaite {points_defaite}. Pas de match nul.
- Après le match, le capitaine gagnant déclare le score. L'équipe adverse confirme ou conteste.
- Sans réponse sous {delai_h} heures, le score est validé automatiquement.
- En cas de désaccord, un admin tranche. Gardez toujours des captures d'écran de vos matchs.

# Fin de saison
- Les premiers de chaque poule montent, les derniers descendent. Le champion de Ligue 1 gagne le titre de la saison.
- Une team qui n'a joué aucun match de la saison est retirée du classement.
- Chaque match non joué coûte {penalite_par_match} point de pénalité (jusqu'à {penalite_max}), doublé en cas de récidive.
- S'il y a des teams en liste d'attente, le dernier de chaque poule de Ligue 5 leur laisse sa place.

# Fair-play
- Pas d'insultes, de triche, de bug exploité volontairement ni de comptes multiples.
- L'orga peut retirer des points, annuler un match, exclure une team ou suspendre un compte.
- Un doute, un litige ? Écris-nous par la page Contact ou sur le Discord.
"""

FAQ_DEFAUT = """C'est payant ?
Non. Tout est gratuit : l'inscription, les ligues, les tournois et les badges du Shop.

Comment inscrire ma team ?
Crée ton compte, confirme ton email, puis va dans Teams → Créer une team. Tes amis te rejoignent depuis la même page.

Dans quelle ligue va ma team ?
Dès qu'elle a 3 joueurs, dans la plus haute ligue encore libre : la Ligue 1 se remplit en premier, puis la 2, la 3… Si tout est plein, ta team est en liste d'attente et entre dès qu'une place se libère.

Quand commencent les matchs ?
Dès que ta ligue est pleine : le calendrier sort le dimanche à 15h et le premier match a lieu le lundi à 9h. Tu reçois une notification.

Combien de temps dure une saison ?
3 semaines : 7 matchs, un tous les 3 jours. Ensuite, les premiers montent, les derniers descendent, et tout repart le lundi suivant.

Qui déclare les scores ?
Le capitaine, juste après le match. L'équipe adverse confirme ou conteste ; sans réponse, le score est validé tout seul. En cas de litige, un admin tranche.

Et si on ne peut pas jouer un match ?
Préviens l'autre capitaine pour trouver un autre moment. Un match jamais joué coûte un point de pénalité en fin de saison.

Je n'ai pas reçu l'email de confirmation.
Regarde dans tes spams, puis clique sur « Renvoyer l'email » dans le bandeau en haut du site.

J'ai oublié mon mot de passe.
Sur la page de connexion, clique sur « Mot de passe oublié » : tu reçois un lien par email.

Comment contacter l'orga ?
Par la page Contact du site ou sur notre Discord."""

# Liste de départ des mots interdits (Admin → Réglages, modifiable). Les variantes en l33t, avec des points
# ou des lettres répétées sont attrapées automatiquement : inutile de les écrire.
MOTS_INTERDITS_DEFAUT = """connard
connasse
salope
salaud
encule
enculer
batard
fdp
ntm
niquer
pute
putain
pd
pede
tapette
gouine
negro
negre
bougnoule
youpin
bicot
chinetoque
nazi
hitler
mongol
abruti
debile
cul
bite
couille
chatte
porn
porno
sexe
viol
violer
pedophile
terroriste
fuck
fucker
bitch
nigger
nigga
faggot
cunt
whore
slut
dick
pussy
rape
"""

ACCUEIL_DEFAUT = {'accueil_ligne1': 'La ligue où', 'accueil_ligne2': 'chaque match', 'accueil_ligne3': 'compte',
                  'accueil_texte': "Monte ta team, prends ta place dans la ligue et vise la Ligue 1. Des saisons de 3 semaines, "
                                   "des résultats vérifiés par l'orga, et un palmarès qui reste."}


def contenu(cle, defaut=''):
    r = get_db().execute('SELECT valeur FROM parametres WHERE cle = ?', (f'contenu_{cle}',)).fetchone()
    return r['valeur'] if r and r['valeur'] is not None else defaut


def valeurs_reglement():
    return {'points_victoire': reglage('points_victoire'), 'points_defaite': reglage('points_defaite'),
            'delai_h': reglage('delai_confirmation_h'), 'penalite_par_match': reglage('penalite_par_match'),
            'penalite_max': reglage('penalite_max'), 'joueurs_min': reglage('joueurs_min'), 'max_membres': max_membres_jeu(get_db(), (trouver_jeu(get_db()) or {'id': None})['id'])}


def mettre_en_forme(texte_brut):
    """Texte simple -> blocs sûrs (titres « # », listes « - », paragraphes). {points_victoire}… remplacés
    par les vrais réglages. Aucun HTML n'est jamais interprété : le gabarit échappe tout."""
    valeurs = valeurs_reglement()
    t = re.sub(r'\{(\w+)\}', lambda m: str(valeurs.get(m.group(1), m.group(0))), texte_brut or '')
    blocs = []
    for ligne in t.splitlines():
        ligne = ligne.rstrip()
        if not ligne.strip():
            blocs.append({'type': 'vide'})
        elif ligne.startswith('# '):
            blocs.append({'type': 'titre', 'texte': ligne[2:].strip()})
        elif ligne.lstrip().startswith(('- ', '• ')):
            if blocs and blocs[-1]['type'] == 'liste':
                blocs[-1]['items'].append(ligne.lstrip()[2:])
            else:
                blocs.append({'type': 'liste', 'items': [ligne.lstrip()[2:]]})
        elif blocs and blocs[-1]['type'] == 'para':
            blocs[-1]['texte'] += '\n' + ligne
        else:
            blocs.append({'type': 'para', 'texte': ligne})
    return [b for b in blocs if b['type'] != 'vide']


def faq_en_questions(texte_brut):
    """« Question ?\nRéponse » séparées par une ligne vide -> [{'q', 'r'}]."""
    sortie = []
    for bloc in re.split(r'\n\s*\n', (texte_brut or '').strip()):
        lignes = [l for l in bloc.strip().splitlines() if l.strip()]
        if lignes:
            sortie.append({'q': lignes[0], 'r': '\n'.join(lignes[1:])})
    return sortie


@app.route('/reglement')
def reglement_page():
    return render_template('page_texte.html', titre='Règlement', kicker='Les règles', blocs=mettre_en_forme(contenu('reglement', REGLEMENT_DEFAUT)))


@app.route('/faq')
def faq_page():
    return render_template('page_texte.html', titre='Questions fréquentes', kicker='FAQ', faq=faq_en_questions(contenu('faq', FAQ_DEFAUT)))


@app.route('/api/admin/contenu', methods=['GET', 'POST'])
@api_admin
def api_admin_contenu():
    db = get_db()
    cles = {'reglement': (REGLEMENT_DEFAUT, 8000), 'faq': (FAQ_DEFAUT, 8000), **{k: (v, 200 if k != 'accueil_texte' else 400) for k, v in ACCUEIL_DEFAUT.items()}}
    if request.method == 'POST':
        data = donnees_json()
        for cle, (defaut, longueur) in cles.items():
            if cle in data:
                valeur = sans_invisibles(data[cle] if isinstance(data[cle], str) else '').strip()[:longueur] or defaut
                if valeur == contenu(cle, defaut):
                    continue
                db.execute('INSERT OR REPLACE INTO parametres (cle, valeur) VALUES (?, ?)', (f'contenu_{cle}', valeur))
                journaliser(db, 'Texte modifié', {'reglement': 'Règlement', 'faq': 'FAQ'}.get(cle, 'Accueil'))
        db.commit()
        return jsonify({'success': True}), 200
    return jsonify({cle: contenu(cle, defaut) for cle, (defaut, _) in cles.items()}), 200


@app.route('/api/admin/reglages', methods=['GET', 'POST'])
@api_admin
def api_admin_reglages():
    db = get_db()
    if request.method == 'POST':
        if fondateur_requis(db):
            return jsonify({'success': False, 'error': 'Seuls les fondateurs peuvent changer les réglages'}), 403
        data = donnees_json()
        for cle, (defaut, mini, maxi, libelle) in REGLAGES.items():   # tout vérifier avant d'écrire quoi que ce soit
            if cle in data and (entier(data[cle]) is None or not mini <= entier(data[cle]) <= maxi):
                return jsonify({'success': False, 'error': f'{libelle} : entre {mini} et {maxi}'}), 400
        for cle, (defaut, mini, maxi, libelle) in REGLAGES.items():
            if cle in data and entier(data[cle]) != reglage(cle):
                v = entier(data[cle])
                db.execute('INSERT OR REPLACE INTO parametres (cle, valeur) VALUES (?, ?)', (f'reglage_{cle}', str(v)))
                journaliser(db, f'Réglage : {libelle} = {v}')
        if 'discord_invite' in data:
            lien = sans_invisibles(str(data['discord_invite'] or '')).strip()
            if lien.startswith('discord.gg/'):
                lien = 'https://' + lien
            if lien and not DISCORD_INVITE_REGEX.match(lien):
                return jsonify({'success': False, 'error': 'Lien Discord invalide : il doit ressembler à https://discord.gg/AbCd123 ou https://discord.com/invite/AbCd123'}), 400
            if lien:
                db.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES ('discord_invite', ?)", (lien,))
            else:
                db.execute("DELETE FROM parametres WHERE cle = 'discord_invite'")
            journaliser(db, 'Lien d\'invitation Discord changé', lien or 'lien d\'origine')
        if 'mots_interdits' in data:
            mots = '\n'.join(sorted({m.strip().lower() for m in str(data['mots_interdits']).splitlines() if m.strip()}))[:5000]
            db.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES ('mots_interdits', ?)", (mots,))
            journaliser(db, f"Mots interdits mis à jour ({len(mots.splitlines()) if mots else 0} mot(s))")
        db.commit()
        g.pop('reglages', None)
        return jsonify({'success': True}), 200
    mots = db.execute("SELECT valeur FROM parametres WHERE cle = 'mots_interdits'").fetchone()
    return jsonify({'reglages': [{'cle': k, 'valeur': reglage(k), 'defaut': d, 'min': mi, 'max': ma, 'libelle': l}
                                 for k, (d, mi, ma, l) in REGLAGES.items()],
                    'mots_interdits': mots['valeur'] if mots else MOTS_INTERDITS_DEFAUT.strip(),
                    'discord_invite': lien_discord(db)}), 200


@app.route('/api/admin/statistiques', methods=['GET'])
@api_admin
def api_admin_statistiques():
    """Chiffres de la ligue : totaux et évolution sur 12 semaines (inscriptions, matchs)."""
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    compter = lambda req, *p: db.execute(req, p).fetchone()[0]
    saison = saison_actuelle(db, jeu['id'])
    lundi = (datetime.utcnow() - timedelta(days=datetime.utcnow().weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    semaines = []
    for i in range(11, -1, -1):
        debut = lundi - timedelta(weeks=i)
        fin = debut + timedelta(weeks=1)
        a, b = debut.strftime('%Y-%m-%d %H:%M:%S'), fin.strftime('%Y-%m-%d %H:%M:%S')
        semaines.append({'semaine': debut.strftime('%d/%m'),
                         'inscriptions': compter('SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at < ?', a, b),
                         'matchs': compter('SELECT COUNT(*) FROM matchs WHERE jeu_id = ? AND created_at >= ? AND created_at < ?', jeu['id'], a, b)})
    joueurs = compter('SELECT COUNT(*) FROM users')
    en_team = compter('SELECT COUNT(DISTINCT user_id) FROM team_members WHERE jeu_id = ?', jeu['id'])
    teams = compter('SELECT COUNT(*) FROM teams WHERE jeu_id = ?', jeu['id'])
    actives = compter("""SELECT COUNT(*) FROM teams t WHERE t.jeu_id = ? AND EXISTS (SELECT 1 FROM matchs m WHERE m.saison = ?
                         AND m.jeu_id = t.jeu_id AND (m.team1_id = t.id OR m.team2_id = t.id))""", jeu['id'], saison)
    decl = compter("SELECT COUNT(*) FROM declarations WHERE jeu_id = ? AND statut IN ('valide', 'refuse')", jeu['id'])
    litiges = compter("SELECT COUNT(*) FROM declarations WHERE jeu_id = ? AND (motif IS NOT NULL AND reponse = 'admin')", jeu['id'])
    return jsonify({'jeu': jeu['nom'], 'saison': saison, 'joueurs': joueurs, 'en_team': en_team, 'teams': teams, 'teams_actives': actives,
                    'matchs_saison': compter('SELECT COUNT(*) FROM matchs WHERE jeu_id = ? AND saison = ?', jeu['id'], saison),
                    'tournois': compter("SELECT COUNT(*) FROM tournois WHERE jeu_id = ? AND statut = 'termine'", jeu['id']),
                    'part_litiges': round(100 * litiges / decl) if decl else 0, 'semaines': semaines}), 200


@app.route('/annonces')
def annonces_page():
    return render_template('annonces.html', annonces=annonces_actives(get_db()))


@app.route('/login')
def login_page():
    if utilisateur_connecte() and not request.args.get('ajout'):
        return redirect('/')
    return render_template('login.html')


@app.route('/signup')
def signup_page():
    if utilisateur_connecte() and not request.args.get('ajout'):
        return redirect('/')
    return render_template('signup.html')


@app.route('/profil')
@page_connectee
def profil_page():
    return render_template('profil.html')


@app.route('/teams')
@page_connectee
def teams_page():
    return render_template('teams.html')


@app.route('/ligue')
def ligue_page():
    # Les classements sont publics : pas besoin de compte pour les consulter
    if not trouver_jeu(get_db()):
        abort(404)
    return render_template('ligue.html', ligues=LIGUES)


@app.route('/shop')
@page_connectee
def shop_page():
    return render_template('shop.html')


@app.route('/cgv')
def cgv_page():
    return render_template('cgv.html')


@app.route('/confidentialite')
def confidentialite_page():
    return render_template('confidentialite.html')


@app.route('/contact')
def contact_page():
    return render_template('contact.html', sujet=request.args.get('sujet', '')[:120])


COMPTES_MAX = 5   # comptes mémorisés sur un même navigateur


def version_session(user_id):
    return session.get('v', {}).get(str(user_id), 0)


def connecter(user_id):
    """Ouvre la session sur ce compte en gardant les autres comptes déjà connectés sur ce navigateur."""
    comptes = [c for c in session.get('comptes', []) if isinstance(c, int) and c != user_id]
    versions = dict(session.get('v', {}))
    session.clear()
    row = get_db().execute('SELECT session_v FROM users WHERE id = ?', (user_id,)).fetchone()
    versions[str(user_id)] = (row['session_v'] or 0) if row else 0
    session['user_id'] = user_id
    session['comptes'] = [user_id] + comptes[:COMPTES_MAX - 1]
    session['v'] = {k: v for k, v in versions.items() if int(k) in session['comptes']}


def invalider_sessions(db, user_id):
    """Après un changement de mot de passe : toutes les sessions ouvertes de ce compte sont fermées,
    et les liens « mot de passe oublié » encore valables ne marchent plus."""
    db.execute('UPDATE users SET session_v = COALESCE(session_v, 0) + 1 WHERE id = ?', (user_id,))
    db.execute('UPDATE reinitialisations SET utilise = 1 WHERE user_id = ?', (user_id,))


def autres_comptes(db, user_id):
    ids = [c for c in session.get('comptes', []) if isinstance(c, int) and c != user_id]
    if not ids:
        return []
    rows = {r['id']: r for r in db.execute('SELECT id, username, handle, photo, profile_pic FROM users WHERE id IN (%s)'
                                            % ','.join('?' * len(ids)), ids)}
    return [{'id': i, 'username': rows[i]['username'], 'handle': rows[i]['handle'],
             'photo': url_media('avatars', rows[i]['photo']), 'logo': logo_valide(rows[i]['profile_pic'])}
            for i in ids if i in rows]


@app.route('/api/comptes/basculer', methods=['POST'])
@api_connectee
def api_basculer_compte():
    cible = entier(donnees_json().get('user_id'))
    row = get_db().execute('SELECT session_v, bloque FROM users WHERE id = ?', (cible,)).fetchone()
    if cible not in session.get('comptes', []) or not row or row['bloque'] or (row['session_v'] or 0) != version_session(cible):
        session['comptes'] = [c for c in session.get('comptes', []) if c != cible]
        return jsonify({'success': False, 'error': 'Reconnecte-toi à ce compte (session expirée)'}), 403
    connecter(cible)
    return jsonify({'success': True}), 200


@app.route('/logout')
def logout():
    """Déconnecte le compte actuel ; s'il en reste un autre sur ce navigateur, on bascule dessus."""
    from urllib.parse import urlparse
    venant_de = request.headers.get('Referer')
    if venant_de and urlparse(venant_de).netloc != request.host:
        return redirect('/')   # lien piégé sur un autre site : on ne déconnecte pas
    restants = [c for c in session.get('comptes', []) if isinstance(c, int) and c != session.get('user_id')]
    session.clear()
    if request.args.get('tous'):    # ordinateur partagé : on ferme tous les comptes d'un coup
        return redirect('/login')
    if restants:
        connecter(restants[0])
        session['comptes'] = restants
        return redirect('/')
    return redirect('/login')


# ---------------------------------------------------------------------------
# API : comptes
# ---------------------------------------------------------------------------
def email_canonique(email):
    """Même boîte mail = même adresse : « jo+2@x.fr » -> « jo@x.fr » ; chez Gmail les points ne comptent pas
    (« j.o@gmail.com » = « jo@gmail.com »). Empêche de créer plein de comptes avec une seule boîte."""
    local, _, domaine = (email or '').strip().lower().partition('@')
    local = local.split('+', 1)[0]
    if domaine in ('gmail.com', 'googlemail.com'):
        local, domaine = local.replace('.', ''), 'gmail.com'
    return f'{local}@{domaine}'


def email_deja_pris(db, email):
    cible = email_canonique(email)
    return any(email_canonique(r[0]) == cible for r in db.execute('SELECT email FROM users'))


@app.route('/api/signup', methods=['POST'])
def api_signup():
    data = donnees_json()
    email = texte(data, 'email').lower()
    username = texte(data, 'username')
    password = data.get('password') if isinstance(data.get('password'), str) else ''

    if not email or not username or not password:
        return jsonify({'success': False, 'error': 'Tous les champs sont requis'}), 400
    if not EMAIL_VALIDE.match(email) or len(email) > 120:
        return jsonify({'success': False, 'error': 'Email invalide'}), 400
    username = re.sub(r'\s+', ' ', username).strip()
    if not 3 <= len(username) <= 20:
        return jsonify({'success': False, 'error': 'Le pseudo doit faire entre 3 et 20 caractères'}), 400
    if not MOT_DE_PASSE_MIN <= len(password) <= 128:
        return jsonify({'success': False, 'error': f'Le mot de passe doit faire entre {MOT_DE_PASSE_MIN} et 128 caractères'}), 400

    db = get_db()
    if limite_atteinte('signup:' + ip_client(), 20, 60):
        return jsonify({'success': False, 'error': TROP}), 429
    if db.execute('SELECT 1 FROM users WHERE lower(email) = ?', (email,)).fetchone() or email_deja_pris(db, email):
        return jsonify({'success': False, 'error': 'Cet email est déjà utilisé'}), 400
    if pseudo_refuse(db, username):
        return jsonify({'success': False, 'error': pseudo_refuse(db, username)}), 400
    try:
        cur = db.execute(
            "INSERT INTO users (email, username, password, bio, profile_pic, solde, created_at) "
            "VALUES (?, ?, ?, '', ?, ?, datetime('now'))",
            (email, username, generate_password_hash(password), AVATAR_DEFAUT, SOLDE_DEPART))
        attribuer_handle(db, cur.lastrowid, username)
        noter_action('signup:' + ip_client())
        if config_mail():
            db.execute('UPDATE users SET email_verifie = 0 WHERE id = ?', (cur.lastrowid,))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Email ou nom d\'utilisateur déjà pris'}), 400

    connecter(cur.lastrowid)
    envoye = envoyer_confirmation_email(db, cur.lastrowid) if config_mail() else False
    return jsonify({'success': True, 'confirmation': bool(config_mail()), 'mail_envoye': envoye}), 201


# ---------------------------------------------------------------------------
# Confirmation de l'email : sans elle, pas de team ni de badge (bloque les faux comptes en série)
# ---------------------------------------------------------------------------
def email_a_confirmer(user):
    """Vrai si ce compte doit encore confirmer son email (seulement quand l'envoi d'emails est activé)."""
    return bool(user) and not user['email_verifie'] and config_mail() is not None


def exiger_email_confirme():
    """Réponse d'erreur si le joueur connecté n'a pas encore confirmé son email, sinon None."""
    if email_a_confirmer(utilisateur_connecte()):
        return jsonify({'success': False, 'email_a_confirmer': True,
                        'error': "Confirme d'abord ton email : clique sur le lien qu'on t'a envoyé (pense aux spams)."}), 403
    return None


CONFIRMATION_JOURS = 7    # durée du lien de confirmation ; un compte jamais confirmé est supprimé après


def purger_comptes_non_confirmes(db):
    """Supprime les comptes jamais confirmés depuis plus de 7 jours (faux comptes, pseudos réservés pour rien)."""
    if not config_mail():
        return 0
    c = db.cursor()
    ids = [r[0] for r in c.execute(f"""SELECT id FROM users WHERE email_verifie = 0 AND COALESCE(is_admin, 0) = 0
                                       AND created_at < datetime('now', '-{CONFIRMATION_JOURS} days')""").fetchall()]
    for uid in ids:
        handle = c.execute('SELECT handle FROM users WHERE id = ?', (uid,)).fetchone()
        supprimer_compte(db, c, uid)
        if handle and handle['handle']:   # un faux compte ne bloque pas son @ pendant 90 jours
            c.execute('DELETE FROM anciens_handles WHERE handle = ? AND user_id = 0', (handle['handle'],))
    if ids:
        ecrire_journal(c, None, 'automatique', f'{len(ids)} compte(s) jamais confirmé(s) supprimé(s)')
    db.commit()
    return len(ids)


def envoyer_confirmation_email(db, user_id):
    u = db.execute('SELECT username, email FROM users WHERE id = ?', (user_id,)).fetchone()
    conf = config_mail()
    if not u or not conf:
        return False
    # 200 confirmations par jour au plus (le quota Gmail du site est de 300) : il en reste toujours
    # pour les « mot de passe oublié », même si quelqu'un crée des comptes en masse
    if has_request_context():
        if limite_atteinte('mail-confirmation', 200, 24 * 60):
            app.logger.error('Quota quotidien des emails de confirmation atteint')
            return False
        noter_action('mail-confirmation')
    jeton = secrets.token_urlsafe(32)
    db.execute("UPDATE users SET verif_jeton = ?, verif_envoye_at = datetime('now') WHERE id = ?", (hash_jeton(jeton), user_id))
    db.commit()
    site = (conf.get('site') or SITE_URL_DEFAUT).rstrip('/')
    return envoyer_mail(u['email'], 'XHQ League · Confirme ton email', f"""Salut {u['username']},

Bienvenue sur XHQ League ! Clique sur ce lien pour confirmer ton email et débloquer ton compte
(créer ou rejoindre une team, récupérer tes badges) :

{site}/confirmer-email/{jeton}

Si tu ne t'es pas inscrit, ignore ce message.

L'équipe XHQ""")


@app.route('/confirmer-email/<jeton>')
def confirmer_email(jeton):
    db = get_db()
    u = db.execute(f"""SELECT id FROM users WHERE verif_jeton = ? AND email_verifie = 0
                       AND verif_envoye_at > datetime('now', '-{CONFIRMATION_JOURS} days')""", (hash_jeton(jeton[:100]),)).fetchone()
    if u:
        db.execute('UPDATE users SET email_verifie = 1, verif_jeton = NULL WHERE id = ?', (u['id'],))
        db.commit()
    texte_page = ("# C'est confirmé !\nTon email est validé : tu peux créer ou rejoindre une team et récupérer tes badges dans le Shop."
                  if u else "# Lien expiré\nCe lien a déjà servi ou n'est plus valable. Connecte-toi : si ton email n'est pas encore confirmé, "
                            "le bandeau en haut du site te permet de recevoir un nouveau lien.")
    return render_template('page_texte.html', titre='Confirmation de l\'email', kicker='Compte', blocs=mettre_en_forme(texte_page))


@app.route('/api/email/renvoyer', methods=['POST'])
@api_connectee
def api_renvoyer_confirmation():
    db = get_db()
    if not email_a_confirmer(utilisateur_connecte()):
        return jsonify({'success': True, 'deja': True}), 200
    if limite_atteinte(f"verif:{session['user_id']}", 3, 60):
        return jsonify({'success': False, 'error': 'Déjà 3 emails envoyés : attends un peu, et regarde dans tes spams.'}), 429
    noter_action(f"verif:{session['user_id']}")
    if not envoyer_confirmation_email(db, session['user_id']):
        return jsonify({'success': False, 'error': "L'email n'a pas pu partir. Réessaie dans quelques minutes."}), 503
    return jsonify({'success': True}), 200


@app.route('/api/login', methods=['POST'])
def api_login():
    """Connexion. Protection contre la force brute, compte par compte :
    10 essais ratés -> compte bloqué 7 jours (débloquable par « Mot de passe oublié ») ;
    10 essais ratés de plus -> verrouillage définitif, seul un admin peut le lever."""
    data = donnees_json()
    email = texte(data, 'email').lower()
    password = data.get('password') if isinstance(data.get('password'), str) else ''
    password = password[:200]   # un mot de passe géant ne doit pas faire travailler le serveur pour rien
    if limite_atteinte('login-ip:' + ip_client(), 40, 15):
        return jsonify({'success': False, 'error': 'Trop d\'essais depuis ta connexion : réessaie dans 15 minutes.'}), 429
    db = get_db()
    user = db.execute("""SELECT id, password, bloque, echecs_connexion, verrou_niveau, verrou_definitif,
                                bloque_jusqua > datetime('now') AS bloque_temp, bloque_jusqua
                         FROM users WHERE lower(email) = ?""", (email,)).fetchone()
    # Même temps de calcul que le compte existe ou non (on ne révèle pas quels emails sont inscrits)
    bon = check_password_hash(user['password'] if user else HASH_FACTICE, password or ' ') and bool(user)
    if not user:
        noter_action('login-ip:' + ip_client())
        return jsonify({'success': False, 'error': 'Email ou mot de passe incorrect'}), 401
    if user['verrou_definitif']:
        return jsonify({'success': False, 'verrou': 'definitif',
                        'error': 'Compte verrouillé après trop d\'essais ratés. Écris à l\'orga (page Contact) pour le débloquer.'}), 423
    etat_compte = lambda: db.execute("""SELECT bloque, echecs_connexion, verrou_niveau, verrou_definitif,
                                               bloque_jusqua > datetime('now') AS bloque_temp, bloque_jusqua
                                        FROM users WHERE id = ?""", (user['id'],)).fetchone()
    if bon:
        # On relit l'état APRÈS le calcul du mot de passe : si des essais ratés lancés en même temps
        # ont bloqué le compte entre-temps, même le bon mot de passe est refusé.
        frais = etat_compte()
        if frais['verrou_definitif']:
            return jsonify({'success': False, 'verrou': 'definitif',
                            'error': 'Compte verrouillé après trop d\'essais ratés. Écris à l\'orga (page Contact) pour le débloquer.'}), 423
        if frais['bloque_temp']:
            # Bon mot de passe pendant le blocage : refusé, mais ne compte pas comme un essai raté
            return jsonify({'success': False, 'verrou': 'temporaire',
                            'error': f'Compte bloqué {BLOCAGE_JOURS} jours après trop d\'essais ratés. Utilise « Mot de passe oublié » pour le débloquer tout de suite.'}), 423
        if frais['bloque']:
            return jsonify({'success': False, 'error': 'Ce compte est suspendu. Contacte l\'organisation sur le Discord.'}), 403
        db.execute('UPDATE users SET echecs_connexion = 0, verrou_niveau = 0, bloque_jusqua = NULL WHERE id = ?', (user['id'],))
        db.commit()
        connecter(user['id'])
        return jsonify({'success': True}), 200
    # Essai raté : compteur augmenté en une seule opération (des essais simultanés comptent tous)
    noter_action('login-ip:' + ip_client())
    db.execute('UPDATE users SET echecs_connexion = COALESCE(echecs_connexion, 0) + 1 WHERE id = ?', (user['id'],))
    frais = etat_compte()
    if frais['echecs_connexion'] >= ESSAIS_MAX and (frais['verrou_niveau'] or 0) >= 1:
        db.execute('UPDATE users SET verrou_definitif = 1, echecs_connexion = 0 WHERE id = ?', (user['id'],))
        prevenir_par_mail(db, user['id'], 'Ton compte est verrouillé',
                          'Encore trop d\'essais de mot de passe ratés : ton compte est verrouillé. Écris à l\'orga (page Contact) pour le débloquer.')
        notifier(db, [r[0] for r in db.execute('SELECT id FROM users WHERE is_admin = 1').fetchall()],
                 f"Compte verrouillé (trop d'essais de mot de passe) : {user['id']}. À vérifier dans Admin → Joueurs.", '/admin#joueurs')
        db.commit()
        return jsonify({'success': False, 'verrou': 'definitif',
                        'error': 'Compte verrouillé après trop d\'essais ratés. Écris à l\'orga (page Contact) pour le débloquer.'}), 423
    if frais['echecs_connexion'] >= ESSAIS_MAX:
        db.execute("""UPDATE users SET verrou_niveau = 1, echecs_connexion = 0, bloque_jusqua = datetime('now', ?)
                      WHERE id = ?""", (f'+{BLOCAGE_JOURS} days', user['id']))
        db.commit()
        prevenir_par_mail(db, user['id'], 'Ton compte est bloqué 7 jours',
                          f'Quelqu\'un a raté {ESSAIS_MAX} fois ton mot de passe : ton compte est bloqué {BLOCAGE_JOURS} jours pour le protéger. '
                          'Si c\'était toi, « Mot de passe oublié » le débloque tout de suite.')
        return jsonify({'success': False, 'verrou': 'temporaire',
                        'error': f'Trop d\'essais ratés : compte bloqué {BLOCAGE_JOURS} jours. Si c\'est bien ton compte, utilise « Mot de passe oublié » pour le débloquer tout de suite.'}), 423
    db.commit()
    if frais['bloque_temp']:
        return jsonify({'success': False, 'verrou': 'temporaire',
                        'error': f'Compte bloqué jusqu\'au {date_fr(frais["bloque_jusqua"][:16].replace(" ", "T"))} (heure UTC) après trop d\'essais ratés. Utilise « Mot de passe oublié » pour le débloquer.'}), 423
    return jsonify({'success': False, 'error': 'Email ou mot de passe incorrect'}), 401


@app.route('/api/profil', methods=['GET'])
@api_connectee
def api_profil():
    db = get_db()
    user = db.execute("""SELECT id, email, username, bio, profile_pic, solde, created_at, photo, banniere, banniere_pos,
                             banniere_zoom, handle FROM users WHERE id = ?""", (session['user_id'],)).fetchone()
    mes_teams = [dict(r) for r in db.execute("""
        SELECT j.code AS jeu, j.nom AS jeu_nom, t.nom AS team_name, l.points, l.ligue_num
        FROM team_members tm JOIN teams t ON t.id = tm.team_id JOIN jeux j ON j.id = tm.jeu_id
        LEFT JOIN ligue l ON l.team_id = t.id
        WHERE tm.user_id = ? ORDER BY j.ordre""", (session['user_id'],))]
    principale = mes_teams[0] if mes_teams else {}
    return jsonify({
        'id': user['id'],
        'email': user['email'],
        'username': user['username'],
        'bio': user['bio'] or '',
        'logo': logo_valide(user['profile_pic']),
        'solde': user['solde'] if user['solde'] is not None else 0,
        'team_name': principale.get('team_name'),
        'points': principale.get('points') or 0,
        'ligue_num': principale.get('ligue_num'),
        'teams': mes_teams,
        'created_at': user['created_at'],
        'photo': url_media('avatars', user['photo']),
        'banniere': url_media('bannieres', user['banniere']),
        'banniere_pos': user['banniere_pos'] if user['banniere_pos'] is not None else 50,
        'banniere_zoom': user['banniere_zoom'] or 100,
        'handle': user['handle'],
        'handle_info': info_handle(db, user['id']),
        **inventaire(get_db(), user['id']),
    }), 200


def inventaire(db, user_id):
    objets = [dict(r) for r in db.execute("""
        SELECT s.code, s.nom, s.type FROM inventory i JOIN shop_items s ON s.id = i.item_id
        WHERE i.user_id = ? AND s.code IS NOT NULL ORDER BY i.id""", (user_id,))]
    return {'objets': objets, **apparence(db, user_id)}


@app.route('/api/profil/equiper', methods=['POST'])
@api_connectee
def api_profil_equiper():
    data = donnees_json()
    type_ = data.get('type')
    code = data.get('code')
    if type_ != 'badge':
        return jsonify({'success': False, 'error': 'Seuls les badges se choisissent'}), 400
    db = get_db()
    uid = session['user_id']
    if code is not None:
        item = db.execute('SELECT type FROM shop_items WHERE code = ?', (code,)).fetchone()
        if not item or item['type'] != type_ or not possede(db, uid, code):
            return jsonify({'success': False, 'error': "Tu ne possèdes pas cet objet"}), 400
    db.execute(f'UPDATE users SET {type_}_equipe = ? WHERE id = ?', (code, uid))
    db.commit()
    return jsonify({'success': True, **inventaire(db, uid)}), 200


@app.route('/api/profil/update', methods=['POST'])
@api_connectee
def api_profil_update():
    data = donnees_json()
    db = get_db()
    user = db.execute('SELECT bio, profile_pic FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    bio = data.get('bio', user['bio'] or '')
    if not isinstance(bio, str):
        return jsonify({'success': False, 'error': 'Bio invalide'}), 400
    bio = sans_invisibles(bio).strip()[:500]
    if mot_interdit(bio):
        return jsonify({'success': False, 'error': 'Ta bio contient un mot interdit'}), 400

    logo = AVATAR_DEFAUT   # l'avatar ne se choisit plus : photo de profil ou avatar par défaut

    db.execute('UPDATE users SET bio = ?, profile_pic = ? WHERE id = ?', (bio, logo, session['user_id']))
    db.commit()
    return jsonify({'success': True, 'bio': bio, 'logo': logo}), 200


@app.route('/api/profil/password', methods=['POST'])
@api_connectee
def api_profil_password():
    data = donnees_json()
    ancien = data.get('ancien') if isinstance(data.get('ancien'), str) else ''
    nouveau = data.get('nouveau') if isinstance(data.get('nouveau'), str) else ''
    db = get_db()
    erreur = verifier_mot_de_passe_actuel(db, ancien)
    if erreur:
        return erreur
    if not MOT_DE_PASSE_MIN <= len(nouveau) <= 128:
        return jsonify({'success': False, 'error': f'Le nouveau mot de passe doit faire au moins {MOT_DE_PASSE_MIN} caractères'}), 400
    db.execute('UPDATE users SET password = ? WHERE id = ?', (generate_password_hash(nouveau), session['user_id']))
    invalider_sessions(db, session['user_id'])
    db.commit()
    connecter(session['user_id'])
    prevenir_par_mail(db, session['user_id'], 'Ton mot de passe a été changé',
                      'Le mot de passe de ton compte XHQ League vient d\'être changé. Tes autres appareils ont été déconnectés.')
    return jsonify({'success': True}), 200


def verifier_mot_de_passe_actuel(db, mot_de_passe):
    """Pour les actions sensibles (changer le mot de passe, supprimer le compte) : 5 essais ratés
    par 15 minutes, pour qu'une session volée ne permette pas de deviner le mot de passe."""
    cle = f"mdp-actuel:{session['user_id']}"
    if limite_atteinte(cle, 5, 15):
        return jsonify({'success': False, 'error': 'Trop d\'essais : réessaie dans 15 minutes'}), 429
    user = db.execute('SELECT password FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not mot_de_passe or not check_password_hash(user['password'], mot_de_passe):
        noter_action(cle)
        return jsonify({'success': False, 'error': 'Mot de passe actuel incorrect'}), 400
    return None


def supprimer_compte(db, c, uid):
    """Supprime un compte : il quitte ses teams (le capitanat passe à un autre membre,
    ou la team disparaît s'il était seul), puis ses objets, images et notifications."""
    for j in liste_jeux(db):
        retirer_membre(c, uid, j['id'])
    c.execute('DELETE FROM inventory WHERE user_id = ?', (uid,))
    fichiers = c.execute('SELECT photo, banniere FROM users WHERE id = ?', (uid,)).fetchone()
    if fichiers:
        supprimer_media('avatars', fichiers['photo'])
        supprimer_media('bannieres', fichiers['banniere'])
    email = c.execute('SELECT email FROM users WHERE id = ?', (uid,)).fetchone()
    c.execute('DELETE FROM contact_messages WHERE user_id = ? OR lower(email) = lower(?)', (uid, email['email'] if email else ''))
    c.execute('DELETE FROM notifications WHERE user_id = ?', (uid,))
    ancien = c.execute('SELECT handle FROM users WHERE id = ?', (uid,)).fetchone()
    if ancien and ancien['handle']:
        c.execute("INSERT INTO anciens_handles (handle, user_id, libere_at) VALUES (?, 0, datetime('now'))", (ancien['handle'],))
    c.execute('DELETE FROM users WHERE id = ?', (uid,))


@app.route('/api/admin/users/<int:user_id>/delete', methods=['POST'])
@api_admin
def api_admin_delete_user(user_id):
    """Supprimer un compte (comptes de test, faux comptes…). Pas soi-même, pas un autre admin."""
    db = get_db()
    u = db.execute('SELECT is_admin FROM users WHERE id = ?', (user_id,)).fetchone()
    if not u:
        return jsonify({'success': False, 'error': 'Joueur introuvable'}), 404
    if user_id == session['user_id']:
        return jsonify({'success': False, 'error': 'Pour supprimer ton propre compte, passe par ton profil'}), 400
    if u['is_admin']:
        return jsonify({'success': False, 'error': "Retire d'abord ses droits admin"}), 400
    if est_protege(db, user_id) and fondateur_requis(db):
        return jsonify({'success': False, 'error': fondateur_requis(db)}), 403
    journaliser(db, 'Compte supprimé', nom_joueur(db, user_id))
    supprimer_compte(db, db.cursor(), user_id)
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/profil/export', methods=['GET'])
@api_connectee
def api_profil_export():
    """Droit d'accès / portabilité (RGPD) : toutes les données du joueur dans un fichier, sans aucun secret
    (ni mot de passe, ni compteurs de sécurité)."""
    db = get_db()
    uid = session['user_id']
    lignes = lambda req, *p: [dict(r) for r in db.execute(req, p)]
    compte = lignes("""SELECT username AS pseudo, handle AS identifiant, email, bio, profile_pic AS avatar, created_at AS inscrit_le,
                             badge_equipe, cadre_equipe FROM users WHERE id = ?""", uid)[0]
    donnees = {
        'genere_le': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
        'compte': compte,
        'photo': url_media('avatars', db.execute('SELECT photo FROM users WHERE id = ?', (uid,)).fetchone()['photo']),
        'teams': lignes("""SELECT j.nom AS jeu, t.nom AS team, tm.joined_at AS rejoint_le, t.createur_id = ? AS capitaine
                            FROM team_members tm JOIN teams t ON t.id = tm.team_id JOIN jeux j ON j.id = tm.jeu_id
                            WHERE tm.user_id = ?""", uid, uid),
        'objets': lignes('SELECT s.nom, i.bought_at AS obtenu_le FROM inventory i JOIN shop_items s ON s.id = i.item_id WHERE i.user_id = ?', uid),
        'notifications': lignes('SELECT texte, created_at FROM notifications WHERE user_id = ? ORDER BY id DESC', uid),
        'messages_de_contact': lignes("""SELECT sujet, message, created_at FROM contact_messages
                                         WHERE user_id = ? OR lower(email) = lower(?)""", uid, compte['email']),
        'resultats_declares': lignes('SELECT score1, score2, statut, created_at FROM declarations WHERE par_user_id = ?', uid),
    }
    import json
    reponse = app.response_class(json.dumps(donnees, ensure_ascii=False, indent=2), mimetype='application/json')
    reponse.headers['Content-Disposition'] = 'attachment; filename="mes-donnees-xhq.json"'
    return reponse


@app.route('/api/profil/delete', methods=['POST'])
@api_connectee
def api_profil_delete():
    uid = session['user_id']
    db = get_db()
    mdp = donnees_json().get('mot_de_passe')
    erreur = verifier_mot_de_passe_actuel(db, mdp if isinstance(mdp, str) else '')
    if erreur:
        return erreur
    if possede(db, uid, 'fondateur') and porteurs(db, 'fondateur') <= 1:
        # Sans fondateur, le site repasserait en « mise en route » : tous les admins auraient tous les droits
        return jsonify({'success': False, 'error': "Tu es le dernier fondateur : donne d'abord le badge Fondateur à quelqu'un de confiance (Admin → Joueurs)"}), 400
    for t in db.execute('SELECT team_id FROM team_members WHERE user_id = ?', (uid,)).fetchall():
        seul = db.execute('SELECT COUNT(*) FROM team_members WHERE team_id = ?', (t['team_id'],)).fetchone()[0] == 1
        tournoi = en_tournoi_actif(db, t['team_id'])
        if seul and tournoi:
            return jsonify({'success': False, 'error': f"Tu es le dernier membre d'une team qui joue « {tournoi['nom']} » : attends la fin du tournoi"}), 400
    supprimer_compte(db, db.cursor(), uid)
    db.commit()
    restants = [x for x in session.get('comptes', []) if x != uid]
    session.clear()
    if restants:
        connecter(restants[0])
        session['comptes'] = restants
    return jsonify({'success': True}), 200


# ---------------------------------------------------------------------------
# Profils : photo, bannière, identifiant @, page publique
# ---------------------------------------------------------------------------
NOM_MEDIA = re.compile(r'^[a-f0-9]{32}\.(webp|jpg)$')


def url_media(dossier, nom):
    return f'/media/{dossier}/{nom}' if nom else None


def supprimer_media(dossier, nom):
    if nom and NOM_MEDIA.match(nom):
        try:
            os.remove(os.path.join(UPLOAD_DIR, dossier, nom))
        except OSError:
            pass


def enregistrer_image(fichier, dossier, mode):
    """Vérifie, recadre et enregistre une image envoyée. Renvoie (nom_du_fichier, erreur)."""
    import warnings
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 40_000_000
    warnings.simplefilter('error', Image.DecompressionBombWarning)   # image géante = refusée, pas seulement signalée
    if not fichier or not fichier.filename:
        return None, 'Aucune image reçue'
    contenu = fichier.read(IMAGE_MAX_OCTETS + 1)
    if len(contenu) > IMAGE_MAX_OCTETS:
        return None, 'Image trop lourde (8 Mo maximum)'
    try:
        Image.open(io.BytesIO(contenu)).verify()
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(contenu))).convert('RGB')
    except Exception:
        return None, 'Image illisible : utilise un fichier JPG, PNG, WEBP ou GIF'
    if mode == 'avatar':
        if min(image.size) < 64:
            return None, 'Image trop petite (64 px minimum)'
        image = ImageOps.fit(image, (AVATAR_TAILLE, AVATAR_TAILLE), Image.LANCZOS)
    else:
        if image.width < 600:
            return None, 'Bannière trop petite : 600 px de large minimum (1500 x 500 conseillé)'
        if image.width > BANNIERE_LARGEUR:
            image = image.resize((BANNIERE_LARGEUR, round(image.height * BANNIERE_LARGEUR / image.width)), Image.LANCZOS)
    os.makedirs(os.path.join(UPLOAD_DIR, dossier), exist_ok=True)
    base = secrets.token_hex(16)
    try:
        nom = base + '.webp'
        image.save(os.path.join(UPLOAD_DIR, dossier, nom), 'WEBP', quality=86, method=4)
    except (OSError, KeyError, ValueError):
        nom = base + '.jpg'
        image.save(os.path.join(UPLOAD_DIR, dossier, nom), 'JPEG', quality=88, optimize=True)
    return nom, None


@app.route('/media/<dossier>/<nom>')
def media(dossier, nom):
    if dossier not in ('avatars', 'bannieres', 'logos') or not NOM_MEDIA.match(nom):
        abort(404)
    return send_from_directory(os.path.join(UPLOAD_DIR, dossier), nom, max_age=31536000)


UPLOADS_MAX_MO = 200
_taille_uploads = {'t': 0.0, 'octets': 0}


def taille_uploads():
    """Taille du dossier des images (recalculée au plus une fois par minute)."""
    import time
    if time.time() - _taille_uploads['t'] > 60:
        total = 0
        for dossier, _, fichiers in os.walk(UPLOAD_DIR):
            total += sum(os.path.getsize(os.path.join(dossier, f)) for f in fichiers)
        _taille_uploads.update(t=time.time(), octets=total)
    return _taille_uploads['octets']


def remplacer_image(colonne, dossier, mode, table='users', ligne_id=None):
    """Enregistre l'image envoyée dans la colonne voulue (profil ou team) et supprime l'ancienne."""
    ligne_id = ligne_id or session['user_id']
    if limite_atteinte(f"upload:{session['user_id']}", 20, 60):
        return jsonify({'success': False, 'error': 'Trop d\'images envoyées : réessaie dans une heure'}), 429
    noter_action(f"upload:{session['user_id']}")
    if taille_uploads() > UPLOADS_MAX_MO * 1024 * 1024:
        app.logger.error('Stockage des images plein (%s Mo)', UPLOADS_MAX_MO)
        return jsonify({'success': False, 'error': 'Stockage plein pour le moment : préviens l\'orga (page Contact)'}), 507
    nom, erreur = enregistrer_image(request.files.get('fichier'), dossier, mode)
    if erreur:
        return jsonify({'success': False, 'error': erreur}), 400
    _taille_uploads['t'] = 0     # recalcul au prochain envoi
    db = get_db()
    ancien = db.execute(f'SELECT {colonne} FROM {table} WHERE id = ?', (ligne_id,)).fetchone()[0]
    extra = ', banniere_pos = 50, banniere_zoom = 100' if colonne == 'banniere' else ''
    db.execute(f'UPDATE {table} SET {colonne} = ?{extra} WHERE id = ?', (nom, ligne_id))
    db.commit()
    supprimer_media(dossier, ancien)
    return jsonify({'success': True, 'url': url_media(dossier, nom)}), 200


def retirer_image(colonne, dossier, table='users', ligne_id=None):
    ligne_id = ligne_id or session['user_id']
    db = get_db()
    ancien = db.execute(f'SELECT {colonne} FROM {table} WHERE id = ?', (ligne_id,)).fetchone()[0]
    db.execute(f'UPDATE {table} SET {colonne} = NULL WHERE id = ?', (ligne_id,))
    db.commit()
    supprimer_media(dossier, ancien)
    return jsonify({'success': True}), 200


def cadrage_valide():
    data = donnees_json()
    pos, zoom = entier(data.get('pos')), entier(data.get('zoom'))
    if pos is None or zoom is None or not (0 <= pos <= 100 and 100 <= zoom <= 250):
        return None, None
    return pos, zoom


@app.route('/api/profil/photo', methods=['POST'])
@api_connectee
def api_profil_photo():
    return exiger_email_confirme() or remplacer_image('photo', 'avatars', 'avatar')


@app.route('/api/profil/photo/supprimer', methods=['POST'])
@api_connectee
def api_profil_photo_supprimer():
    return retirer_image('photo', 'avatars')


@app.route('/api/profil/banniere', methods=['POST'])
@api_connectee
def api_profil_banniere():
    return exiger_email_confirme() or remplacer_image('banniere', 'bannieres', 'banniere')


@app.route('/api/profil/banniere/supprimer', methods=['POST'])
@api_connectee
def api_profil_banniere_supprimer():
    return retirer_image('banniere', 'bannieres')


@app.route('/api/profil/banniere/cadrage', methods=['POST'])
@api_connectee
def api_profil_banniere_cadrage():
    pos, zoom = cadrage_valide()
    if pos is None:
        return jsonify({'success': False, 'error': 'Cadrage invalide'}), 400
    db = get_db()
    db.execute('UPDATE users SET banniere_pos = ?, banniere_zoom = ? WHERE id = ?', (pos, zoom, session['user_id']))
    db.commit()
    return jsonify({'success': True}), 200


def info_handle(db, user_id):
    """Peut-on changer d'identifiant maintenant ? Sinon, à partir de quand."""
    u = db.execute('SELECT handle_changements, handle_modifie_at FROM users WHERE id = ?', (user_id,)).fetchone()
    restants = max(0, HANDLE_MAX_CHANGEMENTS - (u['handle_changements'] or 0))
    prochain = None
    if u['handle_modifie_at']:
        date = datetime.strptime(u['handle_modifie_at'][:19], '%Y-%m-%d %H:%M:%S') + timedelta(days=HANDLE_DELAI_JOURS)
        if date > datetime.utcnow():
            prochain = date.strftime('%d/%m/%Y')
    return {'restants': restants, 'prochain': prochain, 'possible': restants > 0 and not prochain}


def pseudo_refuse(db, pseudo, user_id=None):
    """Message d'erreur si ce pseudo n'est pas utilisable, sinon None."""
    if not 3 <= len(pseudo) <= 20:
        return 'Le pseudo doit faire entre 3 et 20 caractères'
    est_admin_ = bool(user_id and db.execute('SELECT is_admin FROM users WHERE id = ?', (user_id,)).fetchone()['is_admin'])
    erreur = nom_refuse(pseudo, 'Pseudo', est_admin_)
    if erreur:
        return erreur
    if db.execute('SELECT 1 FROM users WHERE lower(username) = lower(?) AND id != ?', (pseudo, user_id or 0)).fetchone():
        return 'Ce pseudo est déjà pris'
    return None


@app.route('/api/profil/pseudo', methods=['POST'])
@api_connectee
def api_profil_pseudo():
    """Un seul nom à choisir : le pseudo. Le @identifiant (lien du profil) en découle automatiquement."""
    db = get_db()
    uid = session['user_id']
    pseudo = re.sub(r'\s+', ' ', texte(donnees_json(), 'pseudo', 40)).strip()
    actuel = db.execute('SELECT username, handle FROM users WHERE id = ?', (uid,)).fetchone()
    if pseudo == actuel['username']:
        return jsonify({'success': True, 'username': pseudo, 'handle': actuel['handle'], 'handle_info': info_handle(db, uid)}), 200
    erreur = pseudo_refuse(db, pseudo, uid)
    if erreur:
        return jsonify({'success': False, 'error': erreur}), 400
    c = db.cursor()
    if pseudo.lower() == actuel['username'].lower():
        # Simple changement de majuscules : gratuit, le lien ne change pas
        try:
            c.execute('UPDATE users SET username = ? WHERE id = ?', (pseudo, uid))
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'error': 'Ce pseudo est déjà pris'}), 400
        db.commit()
        return jsonify({'success': True, 'username': pseudo, 'handle': actuel['handle'], 'handle_info': info_handle(db, uid)}), 200
    info = info_handle(db, uid)
    if not info['restants']:
        return jsonify({'success': False, 'error': 'Tu as déjà utilisé tous tes changements de pseudo'}), 400
    if info['prochain']:
        return jsonify({'success': False, 'error': f"Tu pourras changer de pseudo à partir du {info['prochain']}"}), 400
    try:
        c.execute("""UPDATE users SET username = ?, handle = NULL, handle_changements = COALESCE(handle_changements, 0) + 1,
                     handle_modifie_at = datetime('now') WHERE id = ?""", (pseudo, uid))
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({'success': False, 'error': 'Ce pseudo est déjà pris'}), 400
    c.execute("INSERT INTO anciens_handles (handle, user_id, libere_at) VALUES (?, ?, datetime('now'))", (actuel['handle'], uid))
    handle = attribuer_handle(c, uid, pseudo)
    db.commit()
    return jsonify({'success': True, 'username': pseudo, 'handle': handle, 'handle_info': info_handle(db, uid)}), 200


@app.route('/u/<handle>')
def profil_public(handle):
    db = get_db()
    u = db.execute("""SELECT id, username, bio, profile_pic, photo, banniere, banniere_pos, banniere_zoom, handle, created_at
                      FROM users WHERE handle = ? AND COALESCE(bloque, 0) = 0""", (handle.lower().lstrip('@'),)).fetchone()
    if not u:
        abort(404)
    teams = [dict(r) for r in db.execute("""
        SELECT j.nom AS jeu, t.id, t.nom, t.createur_id, l.ligue_num, l.poule, l.points
        FROM team_members tm JOIN teams t ON t.id = tm.team_id JOIN jeux j ON j.id = tm.jeu_id
        LEFT JOIN ligue l ON l.team_id = t.id WHERE tm.user_id = ? ORDER BY j.ordre""", (u['id'],))]
    titres = [dict(r) for r in db.execute("""SELECT t.nom, t.termine_at FROM tournois t
        JOIN team_members tm ON tm.team_id = t.champion_team_id WHERE tm.user_id = ? AND t.statut = 'termine'
        ORDER BY t.termine_at DESC""", (u['id'],))]
    look = apparence(db, u['id'])
    moi = utilisateur_connecte()
    return render_template('profil_public.html', profil={
        'username': u['username'], 'handle': u['handle'], 'bio': u['bio'] or '', 'logo': logo_valide(u['profile_pic']),
        'photo': url_media('avatars', u['photo']), 'banniere': url_media('bannieres', u['banniere']),
        'banniere_pos': u['banniere_pos'] if u['banniere_pos'] is not None else 50, 'banniere_zoom': u['banniere_zoom'] or 100,
        'membre_depuis': (u['created_at'] or '')[:10], 'teams': teams, 'titres': titres, **look,
        'c_est_moi': bool(moi and moi['id'] == u['id'])})


# ---------------------------------------------------------------------------
# API : teams
# ---------------------------------------------------------------------------
@app.route('/api/teams', methods=['GET'])
@api_connectee
def api_teams():
    uid = session['user_id']
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    rows = db.execute("""
        SELECT t.id, t.nom, t.bio, t.createur_id, t.created_at, u.username AS createur, t.logo, t.embleme,
               t.banniere, t.banniere_pos, t.banniere_zoom,
               l.ligue_num, l.poule, l.points, t.attente_depuis, t.hors_classement, t.en_formation,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id) AS nb_membres
        FROM teams t
        LEFT JOIN users u ON u.id = t.createur_id
        LEFT JOIN ligue l ON l.team_id = t.id
        WHERE t.jeu_id = ?
        ORDER BY t.nom COLLATE NOCASE""", (jeu['id'],)).fetchall()
    teams_list = []
    for r in rows:
        team = dict(r)
        team['bio'] = team['bio'] or ''
        team['createur'] = team['createur'] or '—'
        team['logo'] = url_media('logos', team['logo'])
        team['banniere'] = url_media('bannieres', team['banniere'])
        team['banniere_pos'] = team['banniere_pos'] if team['banniere_pos'] is not None else 50
        team['banniere_zoom'] = team['banniere_zoom'] or 100
        team['embleme'] = team['embleme'] if team['embleme'] in EMBLEMES else None
        team['is_creator'] = team['createur_id'] == uid
        team['max_membres'] = max_membres_jeu(db, jeu['id'])
        team['pleine'] = team['nb_membres'] >= team['max_membres']
        team['attente'] = position_attente(db, team['id']) if team.pop('attente_depuis') else None
        team['hors_classement'] = bool(team['hors_classement'])
        team['formation'] = bool(team.pop('en_formation'))
        teams_list.append(team)

    mine = db.execute('SELECT team_id FROM team_members WHERE user_id = ? AND jeu_id = ?', (uid, jeu['id'])).fetchone()
    user_team_id = mine['team_id'] if mine else None
    saison = saison_actuelle(db, jeu['id'])
    membres = []
    if user_team_id:
        createur_id = db.execute('SELECT createur_id FROM teams WHERE id = ?', (user_team_id,)).fetchone()['createur_id']
        membres = [dict(m) for m in db.execute("""
            SELECT u.id, u.username, u.profile_pic AS logo, u.photo, u.handle FROM team_members tm JOIN users u ON u.id = tm.user_id
            WHERE tm.team_id = ? ORDER BY tm.joined_at, tm.id""", (user_team_id,))]
        for m in membres:
            m['logo'] = logo_valide(m['logo'])
            m['photo'] = url_media('avatars', m['photo'])
            m['is_capitaine'] = m['id'] == createur_id
            m['is_me'] = m['id'] == uid
            m.update(apparence(db, m['id']))
    matchs = matchs_de_team(db, user_team_id, limite=10) if user_team_id else []
    prochains = rencontres_a_venir(db, jeu['id'], limite=3, team_id=user_team_id) if user_team_id else []
    participation, derniere_penalite = None, None
    if user_team_id:
        ligue = db.execute('SELECT ligue_num, poule FROM ligue WHERE team_id = ?', (user_team_id,)).fetchone()
        if ligue:
            place = next(p for p in classement_poule(db, jeu['id'], ligue['ligue_num'], ligue['poule'])
                         if p['team_id'] == user_team_id)
            participation = {'joues': place['joues'], 'attendus': place['attendus'],
                             'manques': place['matchs_manques'], 'recidive_possible': deja_penalisee(
                                 db, user_team_id, saison - 1)}
        dp = db.execute("""SELECT saison, penalite, matchs_manques, recidive FROM saison_classements
                           WHERE team_id = ? AND saison = ? AND penalite > 0""",
                        (user_team_id, saison - 1)).fetchone()
        derniere_penalite = dict(dp) if dp else None
    return jsonify({'jeu': jeu, 'ligue_depart': LIGUE_DEPART,
                    'teams': teams_list, 'user_team_id': user_team_id, 'emblemes': EMBLEMES, 'user_id': uid, 'membres': membres,
                    'matchs': matchs, 'prochains': prochains, 'max_membres': max_membres_jeu(db, jeu['id']), 'participation': participation,
                    'derniere_penalite': derniere_penalite,
                    'lancement': etat_lancement(db, jeu['id']), 'joueurs_min': joueurs_min(db, jeu['id']),
                    'regle_penalite': {'par_match': reglage('penalite_par_match'), 'recidive': MULTIPLICATEUR_RECIDIVE,
                                       'max': reglage('penalite_max')}}), 200


@app.route('/api/teams', methods=['POST'])
@api_connectee
def api_create_team():
    uid = session['user_id']
    data = donnees_json()
    nom = re.sub(r'\s+', ' ', texte(data, 'nom'))
    bio = texte(data, 'bio', 300)
    if mot_interdit(bio):
        return jsonify({'success': False, 'error': 'La bio contient un mot interdit'}), 400
    if not 2 <= len(nom) <= 30:
        return jsonify({'success': False, 'error': 'Le nom de la team doit faire entre 2 et 30 caractères'}), 400
    if nom_refuse(nom, 'Nom de team', est_admin()):
        return jsonify({'success': False, 'error': nom_refuse(nom, 'Nom de team', est_admin())}), 400

    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    if exiger_email_confirme():
        return exiger_email_confirme()
    c = db.cursor()
    if c.execute('SELECT 1 FROM team_members WHERE user_id = ? AND jeu_id = ?', (uid, jeu['id'])).fetchone():
        return jsonify({'success': False, 'error': f"Vous êtes déjà dans une team {jeu['nom']}"}), 400
    if c.execute('SELECT 1 FROM teams WHERE lower(nom) = lower(?)', (nom,)).fetchone():
        return jsonify({'success': False, 'error': 'Ce nom de team est déjà pris'}), 400
    try:
        c.execute("INSERT INTO teams (nom, createur_id, bio, created_at, jeu_id) VALUES (?, ?, ?, datetime('now'), ?)",
                  (nom, uid, bio, jeu['id']))
        team_id = c.lastrowid
        c.execute("INSERT INTO team_members (team_id, user_id, jeu_id, joined_at) VALUES (?, ?, ?, datetime('now'))",
                  (team_id, uid, jeu['id']))
        minimum = joueurs_min(db, jeu['id'])
        if minimum <= 1:
            place, formation = inscrire_en_ligue(c, jeu['id'], team_id), False
        else:   # pas encore assez de joueurs : elle entrera en ligue quand elle en aura assez
            c.execute('UPDATE teams SET en_formation = 1 WHERE id = ?', (team_id,))
            place, formation = None, True
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({'success': False, 'error': 'Impossible de créer la team'}), 400
    return jsonify({'success': True, 'team_id': team_id, 'ligue_num': place[0] if place else None,
                    'poule': place[1] if place else None, 'attente': place is None and not formation,
                    'formation': formation, 'joueurs_min': minimum}), 201


@app.route('/api/teams/<int:team_id>/join', methods=['POST'])
@api_connectee
def api_join_team(team_id):
    uid = session['user_id']
    db = get_db()
    jeu_id = jeu_de_team(db, team_id)
    if jeu_id is None:
        return jsonify({'success': False, 'error': 'Team introuvable'}), 404
    if exiger_email_confirme():
        return exiger_email_confirme()
    if db.execute('SELECT 1 FROM team_members WHERE user_id = ? AND jeu_id = ?', (uid, jeu_id)).fetchone():
        return jsonify({'success': False, 'error': 'Vous êtes déjà dans une team pour ce jeu'}), 400
    if db.execute("""SELECT 1 FROM team_exclusions WHERE team_id = ? AND user_id = ?
                     AND created_at > datetime('now', '-7 days')""", (team_id, uid)).fetchone():
        return jsonify({'success': False, 'error': 'Le capitaine t\'a retiré de cette team il y a moins de 7 jours'}), 403
    tournoi = en_tournoi_actif(db, team_id)
    if tournoi:
        return jsonify({'success': False, 'error': f"Effectif bloqué : cette team joue le tournoi « {tournoi['nom']} ». Reviens après la fin."}), 400
    if limite_atteinte(f'rejoindre:{uid}', 10, 60):
        return jsonify({'success': False, 'error': TROP}), 429
    noter_action(f'rejoindre:{uid}')
    try:
        ajout = db.execute("""INSERT INTO team_members (team_id, user_id, jeu_id, joined_at)
                              SELECT ?, ?, ?, datetime('now')
                              WHERE (SELECT COUNT(*) FROM team_members WHERE team_id = ?) < ?""",
                           (team_id, uid, jeu_id, team_id, max_membres_jeu(db, jeu_id)))
        if not ajout.rowcount:
            return jsonify({'success': False, 'error': f'Cette team est complète ({max_membres_jeu(db, jeu_id)} joueurs maximum)'}), 400
        t = db.execute('SELECT createur_id FROM teams WHERE id = ?', (team_id,)).fetchone()
        notifier(db, [t['createur_id']], f"{utilisateur_connecte()['username']} a rejoint ta team", '/teams')
        entrer_en_ligue_si_complete(db.cursor(), team_id)
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({'success': False, 'error': 'Vous êtes déjà dans une team pour ce jeu'}), 400
    return jsonify({'success': True}), 200


@app.route('/api/teams/<int:team_id>/delete', methods=['POST'])
@api_connectee
def api_delete_team(team_id):
    db = get_db()
    team = db.execute('SELECT createur_id FROM teams WHERE id = ?', (team_id,)).fetchone()
    if not team:
        return jsonify({'success': False, 'error': 'Team introuvable'}), 404
    if team['createur_id'] != session['user_id']:
        return jsonify({'success': False, 'error': 'Seul le créateur peut supprimer la team'}), 403
    tournoi = en_tournoi_actif(db, team_id)
    if tournoi:
        return jsonify({'success': False, 'error': f"Impossible pendant le tournoi « {tournoi['nom']} » : attends la fin"}), 400
    supprimer_team(db.cursor(), team_id)
    db.commit()
    return jsonify({'success': True}), 200


def en_tournoi_actif(db, team_id):
    return db.execute("""SELECT t.nom FROM tournoi_inscriptions i JOIN tournois t ON t.id = i.tournoi_id
                         WHERE i.team_id = ? AND t.statut IN ('poules', 'bracket')""", (team_id,)).fetchone()


def team_du_capitaine(db, team_id):
    """Renvoie (team, erreur). Erreur si la team n'existe pas ou si l'utilisateur n'est pas le capitaine."""
    team = db.execute('SELECT id, nom, createur_id FROM teams WHERE id = ?', (team_id,)).fetchone()
    if not team:
        return None, (jsonify({'success': False, 'error': 'Team introuvable'}), 404)
    if team['createur_id'] != session['user_id']:
        return None, (jsonify({'success': False, 'error': 'Réservé au capitaine de la team'}), 403)
    return team, None


@app.route('/api/teams/<int:team_id>/leave', methods=['POST'])
@api_connectee
def api_leave_team(team_id):
    uid = session['user_id']
    db = get_db()
    if not db.execute('SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?', (team_id, uid)).fetchone():
        return jsonify({'success': False, 'error': "Vous n'êtes pas dans cette team"}), 400
    seul = db.execute('SELECT COUNT(*) FROM team_members WHERE team_id = ?', (team_id,)).fetchone()[0] == 1
    tournoi = en_tournoi_actif(db, team_id)
    if seul and tournoi:
        return jsonify({'success': False, 'error': f"Tu es le dernier membre et la team joue « {tournoi['nom']} » : attends la fin du tournoi"}), 400
    c = db.cursor()
    retirer_membre(c, uid, jeu_de_team(db, team_id))  # transfert du rôle de capitaine ou suppression si seul
    db.commit()
    supprimee = not db.execute('SELECT 1 FROM teams WHERE id = ?', (team_id,)).fetchone()
    return jsonify({'success': True, 'team_supprimee': supprimee}), 200


@app.route('/api/teams/<int:team_id>/kick/<int:user_id>', methods=['POST'])
@api_connectee
def api_kick_member(team_id, user_id):
    db = get_db()
    team, erreur = team_du_capitaine(db, team_id)
    if erreur:
        return erreur
    if user_id == session['user_id']:
        return jsonify({'success': False, 'error': 'Utilisez « Quitter la team » pour partir'}), 400
    if not db.execute('SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?', (team_id, user_id)).fetchone():
        return jsonify({'success': False, 'error': "Ce joueur n'est pas dans votre team"}), 404
    retirer_membre(db.cursor(), user_id, jeu_de_team(db, team_id))
    db.execute("INSERT INTO team_exclusions (team_id, user_id, created_at) VALUES (?, ?, datetime('now'))", (team_id, user_id))
    notifier(db, [user_id], f"Tu as été retiré de la team {team['nom']}", '/teams')
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/teams/<int:team_id>/capitaine/<int:user_id>', methods=['POST'])
@api_connectee
def api_transfer_captain(team_id, user_id):
    db = get_db()
    team, erreur = team_du_capitaine(db, team_id)
    if erreur:
        return erreur
    if user_id == session['user_id']:
        return jsonify({'success': False, 'error': 'Vous êtes déjà capitaine'}), 400
    if not db.execute('SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?', (team_id, user_id)).fetchone():
        return jsonify({'success': False, 'error': "Ce joueur n'est pas dans votre team"}), 404
    db.execute('UPDATE teams SET createur_id = ? WHERE id = ?', (user_id, team_id))
    notifier(db, [user_id], f"Tu es maintenant capitaine de {team['nom']}", '/teams')
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/teams/<int:team_id>/reintegrer', methods=['POST'])
@api_connectee
def api_reintegrer_team(team_id):
    db = get_db()
    team, erreur = team_du_capitaine(db, team_id)
    if erreur:
        return erreur
    c = db.cursor()
    if c.execute('SELECT 1 FROM ligue WHERE team_id = ?', (team_id,)).fetchone():
        return jsonify({'success': False, 'error': 'Votre team est déjà au classement'}), 400
    if c.execute('SELECT attente_depuis FROM teams WHERE id = ?', (team_id,)).fetchone()['attente_depuis']:
        return jsonify({'success': False, 'error': "Votre team est déjà en liste d'attente"}), 400
    if c.execute('SELECT en_formation FROM teams WHERE id = ?', (team_id,)).fetchone()['en_formation']:
        return jsonify({'success': False, 'error': 'Votre team est en formation : elle entrera en ligue dès qu\'elle aura assez de joueurs'}), 400
    jeu_id = jeu_de_team(db, team_id)
    if nb_membres(c, team_id) < joueurs_min(db, jeu_id):
        c.execute('UPDATE teams SET hors_classement = 0, en_formation = 1 WHERE id = ?', (team_id,))
        db.commit()
        return jsonify({'success': True, 'formation': True, 'joueurs_min': joueurs_min(db, jeu_id), 'ligue_num': None, 'poule': None, 'attente': False}), 200
    place = inscrire_en_ligue(c, jeu_id, team_id)
    db.commit()
    return jsonify({'success': True, 'ligue_num': place[0] if place else None, 'poule': place[1] if place else None,
                    'attente': place is None, 'formation': False}), 200


@app.route('/api/teams/<int:team_id>/update', methods=['POST'])
@api_connectee
def api_update_team(team_id):
    db = get_db()
    team, erreur = team_du_capitaine(db, team_id)
    if erreur:
        return erreur
    data = donnees_json()
    nom = re.sub(r'\s+', ' ', texte(data, 'nom')) if 'nom' in data else team['nom']
    if not 2 <= len(nom) <= 30:
        return jsonify({'success': False, 'error': 'Le nom de la team doit faire entre 2 et 30 caractères'}), 400
    if nom_refuse(nom, 'Nom de team', est_admin()):
        return jsonify({'success': False, 'error': nom_refuse(nom, 'Nom de team', est_admin())}), 400
    if db.execute('SELECT 1 FROM teams WHERE lower(nom) = lower(?) AND id != ?', (nom, team_id)).fetchone():
        return jsonify({'success': False, 'error': 'Ce nom de team est déjà pris'}), 400
    actuelle = db.execute('SELECT bio FROM teams WHERE id = ?', (team_id,)).fetchone()['bio'] or ''
    bio = texte(data, 'bio', 300) if 'bio' in data else actuelle
    if mot_interdit(bio):
        return jsonify({'success': False, 'error': 'La bio contient un mot interdit'}), 400
    try:
        db.execute('UPDATE teams SET nom = ?, bio = ? WHERE id = ?', (nom, bio, team_id))
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Ce nom de team est déjà pris'}), 400
    db.commit()
    return jsonify({'success': True, 'nom': nom, 'bio': bio}), 200


# ---------------------------------------------------------------------------
# Teams : logo, emblème, bannière, page publique
# ---------------------------------------------------------------------------
def capitaine_ou_erreur(team_id):
    team, erreur = team_du_capitaine(get_db(), team_id)
    return erreur


@app.route('/api/teams/<int:team_id>/logo', methods=['POST'])
@api_connectee
def api_team_logo(team_id):
    return capitaine_ou_erreur(team_id) or remplacer_image('logo', 'logos', 'avatar', 'teams', team_id)


@app.route('/api/teams/<int:team_id>/logo/supprimer', methods=['POST'])
@api_connectee
def api_team_logo_supprimer(team_id):
    return capitaine_ou_erreur(team_id) or retirer_image('logo', 'logos', 'teams', team_id)


@app.route('/api/teams/<int:team_id>/banniere', methods=['POST'])
@api_connectee
def api_team_banniere(team_id):
    return capitaine_ou_erreur(team_id) or remplacer_image('banniere', 'bannieres', 'banniere', 'teams', team_id)


@app.route('/api/teams/<int:team_id>/banniere/supprimer', methods=['POST'])
@api_connectee
def api_team_banniere_supprimer(team_id):
    return capitaine_ou_erreur(team_id) or retirer_image('banniere', 'bannieres', 'teams', team_id)


@app.route('/api/teams/<int:team_id>/banniere/cadrage', methods=['POST'])
@api_connectee
def api_team_banniere_cadrage(team_id):
    erreur = capitaine_ou_erreur(team_id)
    if erreur:
        return erreur
    pos, zoom = cadrage_valide()
    if pos is None:
        return jsonify({'success': False, 'error': 'Cadrage invalide'}), 400
    db = get_db()
    db.execute('UPDATE teams SET banniere_pos = ?, banniere_zoom = ? WHERE id = ?', (pos, zoom, team_id))
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/teams/<int:team_id>/embleme', methods=['POST'])
@api_connectee
def api_team_embleme(team_id):
    erreur = capitaine_ou_erreur(team_id)
    if erreur:
        return erreur
    embleme = donnees_json().get('embleme')
    if embleme is not None and embleme not in EMBLEMES:
        return jsonify({'success': False, 'error': 'Emblème inconnu'}), 400
    db = get_db()
    db.execute('UPDATE teams SET embleme = ? WHERE id = ?', (embleme, team_id))
    db.commit()
    return jsonify({'success': True, 'embleme': embleme}), 200


@app.route('/team/<int:team_id>')
def team_publique(team_id):
    db = get_db()
    t = db.execute("""SELECT t.*, j.nom AS jeu_nom, j.format, l.ligue_num, l.poule, l.points
                      FROM teams t JOIN jeux j ON j.id = t.jeu_id LEFT JOIN ligue l ON l.team_id = t.id
                      WHERE t.id = ?""", (team_id,)).fetchone()
    if not t:
        abort(404)
    membres = []
    for m in db.execute("""SELECT u.id, u.username, u.handle, u.photo, u.profile_pic FROM team_members tm
                           JOIN users u ON u.id = tm.user_id WHERE tm.team_id = ? ORDER BY tm.joined_at, tm.id""", (team_id,)):
        membres.append({'username': m['username'], 'handle': m['handle'], 'photo': url_media('avatars', m['photo']),
                        'logo': logo_valide(m['profile_pic']), 'capitaine': m['id'] == t['createur_id'],
                        **apparence(db, m['id'])})
    place = None
    if t['ligue_num']:
        place = next((p for p in classement_poule(db, t['jeu_id'], t['ligue_num'], t['poule']) if p['team_id'] == team_id), None)
    titres = [r['nom'] for r in db.execute("SELECT nom FROM tournois WHERE champion_team_id = ? AND statut = 'termine' ORDER BY termine_at DESC", (team_id,))]
    moi = utilisateur_connecte()
    return render_template('team_publique.html', team={
        'id': t['id'], 'nom': t['nom'], 'bio': t['bio'] or '', 'jeu': t['jeu_nom'], 'format': t['format'],
        'logo': url_media('logos', t['logo']), 'embleme': t['embleme'] if t['embleme'] in EMBLEMES else None,
        'banniere': url_media('bannieres', t['banniere']), 'banniere_pos': t['banniere_pos'] if t['banniere_pos'] is not None else 50,
        'banniere_zoom': t['banniere_zoom'] or 100, 'ligue_num': t['ligue_num'], 'poule': t['poule'],
        'hors_classement': bool(t['hors_classement']), 'formation': bool(t['en_formation']), 'joueurs_min': joueurs_min(db, t['jeu_id']), 'place': place, 'membres': membres, 'max_membres': max_membres_jeu(db, t['jeu_id']),
        'matchs': matchs_de_team(db, team_id, limite=6), 'titres': titres,
        'cree_le': (t['created_at'] or '')[:10], 'c_est_moi': bool(moi and moi['id'] == t['createur_id'])})


# ---------------------------------------------------------------------------
# API : ligue
# ---------------------------------------------------------------------------
def saison_actuelle(db, jeu_id):
    return db.execute('SELECT COALESCE(MAX(numero), 0) + 1 FROM saisons WHERE jeu_id = ?', (jeu_id,)).fetchone()[0]


def deja_penalisee(db, team_id, saison):
    """La team a-t-elle été pénalisée (ou retirée du classement) lors de la saison indiquée ?"""
    return db.execute("""SELECT 1 FROM saison_classements WHERE team_id = ? AND saison = ?
                         AND (penalite > 0 OR mouvement = 'retiree')""", (team_id, saison)).fetchone() is not None


def calculer_penalite(db, team_id, joues, attendus, saison):
    """(pénalité, matchs manqués, récidive) pour une team en fin de saison."""
    manques = max(0, attendus - joues)
    if joues == 0 or manques == 0:  # 0 match = retirée du classement, pas de pénalité en points
        return 0, manques, False
    penalite = manques * reglage('penalite_par_match', db)
    recidive = deja_penalisee(db, team_id, saison - 1)
    if recidive:
        penalite *= MULTIPLICATEUR_RECIDIVE
    return min(penalite, reglage('penalite_max', db)), manques, recidive


def classement_poule(db, jeu_id, ligue_num, poule, penalites=False):
    """Classement d'une poule. Avec penalites=True (fin de saison), les points des matchs non joués
    sont retirés avant de classer, et les teams sans aucun match passent en dernier."""
    config = LIGUES[ligue_num]
    saison = saison_actuelle(db, jeu_id)
    rows = db.execute("""
        SELECT t.id AS team_id, t.nom AS team_name, l.points, l.id AS ligue_id, t.logo, t.embleme
        FROM ligue l JOIN teams t ON t.id = l.team_id
        WHERE t.jeu_id = ? AND l.ligue_num = ? AND l.poule = ?""", (jeu_id, ligue_num, poule)).fetchall()

    # Stats calculées à partir des matchs joués dans CETTE ligue / poule
    stats = {r['team_id']: {'joues': 0, 'victoires': 0, 'defaites': 0, 'diff': 0} for r in rows}
    for m in db.execute('SELECT team1_id, team2_id, score1, score2 FROM matchs '
                        'WHERE jeu_id = ? AND ligue_num = ? AND poule = ? AND saison = ?', (jeu_id, ligue_num, poule, saison)):
        for tid, pour, contre in ((m['team1_id'], m['score1'], m['score2']), (m['team2_id'], m['score2'], m['score1'])):
            if tid in stats:
                s = stats[tid]
                s['joues'] += 1
                s['victoires' if pour > contre else 'defaites'] += 1
                s['diff'] += pour - contre

    # Pénalités (fin de saison uniquement)
    attendus = len(rows) - 1
    finaux = {}
    for r in rows:
        s = stats[r['team_id']]
        s['attendus'] = attendus
        s['points_avant'] = r['points'] or 0
        if penalites:
            s['penalite'], s['matchs_manques'], s['recidive'] = calculer_penalite(db, r['team_id'], s['joues'], attendus, saison)
        else:
            s['penalite'], s['matchs_manques'], s['recidive'] = 0, max(0, attendus - s['joues']), False
        finaux[r['team_id']] = s['points_avant'] - s['penalite']

    # Classement : (en fin de saison, teams sans match en dernier) puis points, différence de manches,
    # victoires, ancienneté
    rows = sorted(rows, key=lambda r: (penalites and stats[r['team_id']]['joues'] == 0,
                                       -finaux[r['team_id']], -stats[r['team_id']]['diff'],
                                       -stats[r['team_id']]['victoires'], r['ligue_id']))

    places = []
    for i, r in enumerate(rows):
        rang = i + 1
        statut = ''
        a_joue = stats[r['team_id']]['joues'] >= MATCHS_MIN_POUR_MONTER
        if config['champion'] and rang == 1 and a_joue:
            statut = 'champion'
        elif rang in config['monte'] and a_joue:
            statut = 'monte'
        elif rang in config['descend']:
            statut = 'descend'
        places.append({'rang': rang, 'team_id': r['team_id'], 'team_name': r['team_name'],
                       'logo': url_media('logos', r['logo']), 'embleme': r['embleme'] if r['embleme'] in EMBLEMES else None,
                       **stats[r['team_id']], 'points': finaux[r['team_id']], 'statut': statut})
    while len(places) < TEAMS_PAR_POULE:
        places.append({'rang': len(places) + 1, 'team_id': None, 'team_name': 'XXXXX', 'points': 0, 'statut': '',
                       'joues': 0, 'victoires': 0, 'defaites': 0, 'diff': 0, 'attendus': attendus,
                       'points_avant': 0, 'penalite': 0, 'matchs_manques': 0, 'recidive': False})
    return places


@app.route('/api/jeux', methods=['GET'])
def api_jeux():
    db = get_db()
    return jsonify([{**j, **chiffres_jeu(db, j['id'])} for j in liste_jeux(db)]), 200


@app.route('/api/ligue', methods=['GET'])
def api_ligue():
    ligue_num = request.args.get('ligue_num', 1, type=int)
    if ligue_num not in LIGUES:
        return jsonify({'success': False, 'error': f'Ligue invalide (1 à {LIGUE_MAX})'}), 400
    lettres = lettres_poules(ligue_num)
    poule = request.args.get('poule', '').upper()
    if poule and poule not in lettres:
        return jsonify({'success': False, 'error': f'Poule invalide pour la Ligue {ligue_num}'}), 400

    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    poules = [{'nom': l, 'teams': classement_poule(db, jeu['id'], ligue_num, l)} for l in (poule or lettres)]
    forme = forme_des_teams(db, jeu['id'], saison_actuelle(db, jeu['id']))
    for p in poules:
        for t in p['teams']:
            t['forme'] = forme.get(t['team_id'], [])[:5] if t['team_id'] else []
    config = LIGUES[ligue_num]
    return jsonify({
        'jeu': jeu,
        'nb_ligues': LIGUE_MAX,
        'ligue_num': ligue_num,
        'nb_poules': config['poules'],
        'teams_par_poule': TEAMS_PAR_POULE,
        'monte': config['monte'],
        'descend': config['descend'],
        'champion': config['champion'],
        'poules': poules,
        'saison': saison_actuelle(db, jeu['id']),
        'palmares': palmares(db, jeu['id'], 5),
        'etat': etat_lancement(db, jeu['id'])['ligues'][ligue_num],
    }), 200


# ---------------------------------------------------------------------------
# API : shop
# ---------------------------------------------------------------------------
@app.route('/api/shop', methods=['GET'])
@api_connectee
def api_shop():
    uid = session['user_id']
    db = get_db()
    equipe = apparence(db, uid)
    nb_users = db.execute('SELECT COUNT(*) FROM users WHERE COALESCE(email_verifie, 1) = 1').fetchone()[0]
    items = []
    for r in db.execute("""SELECT id, code, nom, description, prix, type, mode, quantite_max, style, COALESCE(perso, 0) AS perso
                           FROM shop_items WHERE code IS NOT NULL AND COALESCE(actif, 1) = 1
                           AND code NOT IN ('fondateur', 'admin') ORDER BY id""").fetchall():   # ces 2 badges sont donnés par le fondateur
        item = dict(r)
        item['possede'] = possede(db, uid, item['code'])
        item['disponible'], item['raison'] = disponibilite(db, uid, item['code'])
        item['equipe'] = item['code'] in (equipe['badge_code'], equipe['cadre'])
        if item['code'] == 'pionnier':
            item['places_restantes'] = max(0, PIONNIERS_MAX - nb_users)
        if item['perso'] and item['mode'] == 'limite' and item['quantite_max'] is not None:
            item['places_restantes'] = max(0, item['quantite_max'] - porteurs(db, item['code']))
        items.append(item)
    return jsonify(items), 200


@app.route('/api/shop/buy/<int:item_id>', methods=['POST'])
@app.route('/api/shop/recuperer/<int:item_id>', methods=['POST'])
@api_connectee
def api_shop_buy(item_id):
    """Récupérer un objet gratuit (l'ancienne route /buy reste valable)."""
    uid = session['user_id']
    db = get_db()
    item = db.execute('SELECT id, code, nom, type FROM shop_items WHERE id = ? AND code IS NOT NULL', (item_id,)).fetchone()
    if not item:
        return jsonify({'success': False, 'error': 'Objet introuvable'}), 404
    if exiger_email_confirme():
        return exiger_email_confirme()
    if possede(db, uid, item['code']):
        return jsonify({'success': False, 'error': 'Tu as déjà cet objet'}), 400
    dispo, raison = disponibilite(db, uid, item['code'])
    if not dispo:
        return jsonify({'success': False, 'error': raison}), 400
    if not db.execute("INSERT OR IGNORE INTO inventory (user_id, item_id, bought_at) VALUES (?, ?, datetime('now'))", (uid, item_id)).rowcount:
        return jsonify({'success': False, 'error': 'Tu as déjà cet objet'}), 400
    # Premier badge / premier cadre : équipé automatiquement
    if item['type'] in ('badge', 'cadre'):
        db.execute(f"UPDATE users SET {item['type']}_equipe = ? WHERE id = ? AND {item['type']}_equipe IS NULL",
                   (item['code'], uid))
    db.commit()
    return jsonify({'success': True, 'item': item['nom'], **inventaire(db, uid)}), 200


# ---------------------------------------------------------------------------
# API : contact (remplace aussi "mot de passe oublié")
# ---------------------------------------------------------------------------
@app.route('/api/contact', methods=['POST'])
def api_contact():
    data = donnees_json()
    nom = texte(data, 'nom', 100)
    email = texte(data, 'email', 120)
    sujet = texte(data, 'sujet', 150)
    message = texte(data, 'message', 5000)
    if not nom or not email or not sujet or not message:
        return jsonify({'success': False, 'error': 'Tous les champs sont requis'}), 400
    if not EMAIL_VALIDE.match(email):
        return jsonify({'success': False, 'error': 'Email invalide'}), 400
    if limite_atteinte('contact:' + ip_client(), 5, 60):
        return jsonify({'success': False, 'error': TROP}), 429
    noter_action('contact:' + ip_client())
    db = get_db()
    db.execute("INSERT INTO contact_messages (user_id, nom, email, sujet, message, created_at) "
               "VALUES (?, ?, ?, ?, ?, datetime('now'))", (session.get('user_id'), nom, email, sujet, message))
    db.commit()
    return jsonify({'success': True}), 201


# ---------------------------------------------------------------------------
# Matchs
# ---------------------------------------------------------------------------
REQ_MATCHS = """
    SELECT m.id, m.ligue_num, m.poule, m.team1_id, m.team2_id,
           COALESCE(t1.nom, m.team1_nom) AS team1_nom, COALESCE(t2.nom, m.team2_nom) AS team2_nom,
           m.score1, m.score2, m.points1, m.points2, m.created_at, m.saison, m.jeu_id
    FROM matchs m
    LEFT JOIN teams t1 ON t1.id = m.team1_id
    LEFT JOIN teams t2 ON t2.id = m.team2_id"""


def matchs_de_team(db, team_id, limite=10):
    resultats = []
    saison = saison_actuelle(db, jeu_de_team(db, team_id))
    for r in db.execute(REQ_MATCHS + ' WHERE (m.team1_id = ? OR m.team2_id = ?) AND m.saison = ? '
                        'ORDER BY m.id DESC LIMIT ?', (team_id, team_id, saison, limite)):
        m = dict(r)
        domicile = m['team1_id'] == team_id
        m['adversaire'] = m['team2_nom'] if domicile else m['team1_nom']
        m['score_pour'], m['score_contre'] = (m['score1'], m['score2']) if domicile else (m['score2'], m['score1'])
        m['victoire'] = m['score_pour'] > m['score_contre']
        m['points_gagnes'] = m['points1'] if domicile else m['points2']
        resultats.append(m)
    return resultats


def ajuster_points(c, team_id, ligue_num, poule, delta):
    """Ajoute (ou retire) des points à une team, seulement si elle est toujours dans cette ligue/poule."""
    c.execute('UPDATE ligue SET points = MAX(0, points + ?) WHERE team_id = ? AND ligue_num = ? AND poule = ?',
              (delta, team_id, ligue_num, poule))


@app.route('/api/matchs', methods=['GET'])
def api_matchs():
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    ligue_num = request.args.get('ligue_num', type=int)
    poule = request.args.get('poule', '').upper()
    limite = max(1, min(request.args.get('limit', 20, type=int), 100))
    saison = request.args.get('saison', type=int) or saison_actuelle(db, jeu['id'])
    conditions, params = ['m.jeu_id = ?', 'm.saison = ?'], [jeu['id'], saison]
    if ligue_num is not None:
        if ligue_num not in LIGUES:
            return jsonify({'success': False, 'error': f'Ligue invalide (1 à {LIGUE_MAX})'}), 400
        conditions.append('m.ligue_num = ?')
        params.append(ligue_num)
        if poule:
            if poule not in lettres_poules(ligue_num):
                return jsonify({'success': False, 'error': f'Poule invalide pour la Ligue {ligue_num}'}), 400
            conditions.append('m.poule = ?')
            params.append(poule)
    where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
    rows = db.execute(REQ_MATCHS + where + ' ORDER BY m.id DESC LIMIT ?', (*params, limite)).fetchall()
    return jsonify([dict(r) for r in rows]), 200


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.route('/admin')
@page_admin
def admin_page():
    return render_template('admin.html', ligues=LIGUES, points_victoire=reglage('points_victoire'), points_defaite=reglage('points_defaite'),
                           ligue_depart=LIGUE_DEPART, fondateur=est_fondateur(), onglets_admin=ONGLETS_ADMIN)


def entier(valeur):
    """Nombre entier « raisonnable » envoyé par le navigateur, sinon None (jamais d'erreur)."""
    if isinstance(valeur, bool):
        return None
    if isinstance(valeur, str) and valeur.strip().lstrip('-').isdigit() and len(valeur.strip()) <= 18:
        valeur = int(valeur.strip())
    if isinstance(valeur, int) and -2**62 < valeur < 2**62:
        return valeur
    return None


@app.route('/api/admin/stats', methods=['GET'])
@api_admin
def api_admin_stats():
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    compter = lambda req, *p: db.execute(req, p).fetchone()[0]
    saison = saison_actuelle(db, jeu['id'])
    valider_declarations_expirees(db)
    fondateur = est_fondateur()
    a_faire = a_faire_admin(db, jeu['id'], saison)
    if not fondateur:   # un admin ne voit que la compétition (pas les versions du serveur ni les sauvegardes)
        a_faire = {k: v for k, v in a_faire.items() if k not in ('outils_vieux', 'sauvegarde_jours')}
        a_faire['sauvegarde_jours'] = 0
    return jsonify({
        'jeu': jeu, 'fondateur': fondateur,
        'joueurs': compter('SELECT COUNT(*) FROM users'),
        'teams': compter('SELECT COUNT(*) FROM teams WHERE jeu_id = ?', jeu['id']),
        'matchs': compter('SELECT COUNT(*) FROM matchs WHERE jeu_id = ? AND saison = ?', jeu['id'], saison),
        'saison': saison,
        'messages_non_traites': compter('SELECT COUNT(*) FROM contact_messages WHERE traite = 0 OR traite IS NULL') if fondateur else 0,
        'litiges': compter("SELECT COUNT(*) FROM declarations WHERE jeu_id = ? AND statut = 'conteste'", jeu['id']),
        'verrouilles': compter("SELECT COUNT(*) FROM users WHERE verrou_definitif = 1") if fondateur else 0,
        'a_faire': a_faire,
    }), 200


VERSIONS_SURES = {'flask': (3, 1, 1), 'werkzeug': (3, 0, 6), 'pillow': (10, 3, 0)}


def outils_a_mettre_a_jour():
    """Outils installés sur le serveur plus vieux que les versions qui corrigent les failles connues."""
    from importlib.metadata import version, PackageNotFoundError
    vieux = []
    for nom, mini in VERSIONS_SURES.items():
        try:
            v = tuple(int(x) for x in re.findall(r'\d+', version(nom))[:3])
        except (PackageNotFoundError, ValueError):
            continue
        if v < mini:
            vieux.append(f"{nom} {version(nom)}")
    return vieux


def fuseau_paris():
    """Fuseau horaire de la France (heure d'été comprise). Secours : UTC+1 si le serveur n'a pas les fuseaux."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo('Europe/Paris')
    except Exception:
        from datetime import timezone
        return timezone(timedelta(hours=1))


def paris_vers_utc(texte_date):
    """'2026-12-14T18:00' (heure de France, tapée dans l'admin) -> '2026-12-14 17:00:00' (UTC, en base)."""
    from datetime import timezone
    d = datetime.strptime(texte_date, '%Y-%m-%dT%H:%M').replace(tzinfo=fuseau_paris())
    return d.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def date_paris(utc_texte, format_court=False):
    """'2026-12-14 17:00:00' (UTC) -> 'samedi 14 déc. à 18h00' (heure de France)."""
    from datetime import timezone
    if not utc_texte:
        return ''
    d = datetime.strptime(utc_texte[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone(fuseau_paris())
    return d.strftime('%Y-%m-%dT%H:%M') if format_court else date_fr(d.strftime('%Y-%m-%dT%H:%M'))


DISCORD_WEBHOOK_REGEX = re.compile(r'^https://(discord\.com|discordapp\.com)/api/webhooks/\d+/[A-Za-z0-9_\-]+$')


UA_DISCORD = 'DiscordBot (https://xhqtounament.pythonanywhere.com, 1.0)'


RELAIS_REGEX = re.compile(r'^https://script\.google\.com/macros/s/[A-Za-z0-9_\-]+/exec$')


def relais_google():
    """(adresse du relais, secret) si un relais Google est réglé, sinon (None, None)."""
    try:
        db = get_db()
    except RuntimeError:      # hors du site (console) : pas de relais
        return None, None
    r = dict(db.execute("SELECT cle, valeur FROM parametres WHERE cle IN ('relais_url', 'relais_secret')").fetchall())
    return (r.get('relais_url'), r.get('relais_secret')) if r.get('relais_url') and r.get('relais_secret') else (None, None)


def code_relais(secret):
    """Script à coller dans Google Apps Script : il reçoit le message du site et le poste sur Discord."""
    return """// Relais XHQ League -> Discord (à coller dans script.google.com, voir Admin -> Annonces)
const SECRET = '%s';

function doPost(e) {
  const d = JSON.parse(e.postData.contents);
  if (d.secret !== SECRET) return ContentService.createTextOutput('refus');
  if (!/^https:\\/\\/(discord\\.com|discordapp\\.com)\\/api\\/webhooks\\//.test(d.webhook)) return ContentService.createTextOutput('adresse');
  const r = UrlFetchApp.fetch(d.webhook, {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    payload: JSON.stringify({ content: d.content, allowed_mentions: { parse: [] } })
  });
  return ContentService.createTextOutput(String(r.getResponseCode()));
}
""" % secret


def poster_via_relais(relais, secret, url, contenu, delai=15):
    """Envoi par le relais Google. Google répond par une redirection vers la page de résultat :
    le message est déjà parti à ce moment-là, on lit le résultat si on peut."""
    import json
    import urllib.error
    import urllib.request

    class SansRedirection(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    corps = json.dumps({'secret': secret, 'webhook': url, 'content': contenu[:1900]}).encode()
    req = urllib.request.Request(relais, data=corps, headers={'Content-Type': 'application/json', 'User-Agent': UA_DISCORD})
    try:
        rep = urllib.request.build_opener(SansRedirection).open(req, timeout=delai)
        resultat = rep.read(200).decode('utf-8', 'replace').strip()
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307):
            return False, f"Le relais Google a répondu une erreur (code {e.code}). Vérifie qu'il est déployé en « Application Web », accès « Tout le monde »."
        suite = e.headers.get('Location', '')
        if not re.match(r'^https://script\.googleusercontent\.com/', suite):
            return True, ''          # le script a tourné ; on ne suit jamais une redirection vers un autre site
        try:   # page de résultat du script
            with urllib.request.urlopen(urllib.request.Request(suite, headers={'User-Agent': UA_DISCORD}), timeout=delai) as r2:
                resultat = r2.read(200).decode('utf-8', 'replace').strip()
        except Exception:
            return True, ''          # le script a tourné, mais sa réponse n'est pas lisible depuis le serveur
    except Exception as e:
        return False, f"Impossible de joindre le relais Google ({e.__class__.__name__}). Réessaie dans une minute."
    if resultat == 'refus':
        return False, 'Le relais Google refuse le message : le code du script n\'est pas le bon (recopie-le depuis Admin → Annonces).'
    if resultat == 'adresse':
        return False, "Le relais Google trouve l'adresse du webhook invalide : recolle-la dans « Relier le Discord »."
    if resultat.isdigit() and not 200 <= int(resultat) < 300:
        if resultat in ('401', '404'):
            return False, f"Discord ne connaît pas ce webhook (code {resultat}) : recrée-le et recolle l'adresse."
        return False, f'Discord a refusé le message envoyé par le relais Google (code {resultat}).'
    if resultat and not resultat.isdigit() and '<' in resultat:
        return False, "Le relais Google demande une connexion : redéploie-le avec l'accès « Tout le monde »."
    return True, ''


def poster_discord(url, contenu, delai=8):
    """Envoie un message au webhook (par le relais Google s'il est réglé).
    Renvoie (True, '') ou (False, raison en clair pour l'admin)."""
    relais, secret = relais_google()
    if relais:
        return poster_via_relais(relais, secret, url, contenu)
    import json
    import urllib.error
    import urllib.request
    corps = json.dumps({'content': contenu[:1900], 'allowed_mentions': {'parse': []}}).encode()
    req = urllib.request.Request(url, data=corps, headers={'Content-Type': 'application/json', 'User-Agent': UA_DISCORD})
    try:
        with urllib.request.urlopen(req, timeout=delai) as rep:
            return 200 <= rep.status < 300, ''
    except urllib.error.HTTPError as e:
        app.logger.warning('Discord a refusé le message : %s', e)
        if e.code in (401, 404):
            return False, ("Discord ne connaît pas ce webhook (code %d) : il a été supprimé ou l'adresse est incomplète. "
                           "Recrée-le et recolle l'adresse en entier." % e.code)
        if e.code == 429:
            return False, 'Discord limite les envois (code 429) : réessaie dans quelques minutes.'
        return False, f"Discord a refusé le message (code {e.code}). Réessaie dans quelques minutes ; si ça continue, envoie-moi ce code."
    except urllib.error.URLError as e:
        app.logger.warning('Discord injoignable : %s', e)
        raison = str(getattr(e, 'reason', e))
        if 'Tunnel' in raison or '403' in raison or 'proxy' in raison.lower():
            return False, ("Le serveur n'a pas le droit de joindre discord.com (compte gratuit PythonAnywhere : "
                           f"seuls certains sites sont autorisés). Détail : {raison}")
        return False, f"Impossible de joindre discord.com depuis le serveur. Détail : {raison}"
    except Exception as e:   # délai dépassé, coupure…
        app.logger.warning('Discord : %s', e)
        return False, f"Discord n'a pas répondu à temps ({e.__class__.__name__}). Réessaie dans une minute."


def envoyer_discord(titre, texte, lien):
    """Publie l'annonce sur le salon Discord relié (webhook réglé par un fondateur). Vrai si c'est parti."""
    db = get_db()
    r = db.execute("SELECT valeur FROM parametres WHERE cle = 'discord_webhook'").fetchone()
    if not r or not DISCORD_WEBHOOK_REGEX.match(r['valeur'] or ''):
        return False
    return poster_discord(r['valeur'], message_annonce(titre, texte, lien))[0]


def message_annonce(titre, texte, lien):
    """Texte d'une annonce pour Discord ; un lien du site (« /teams ») devient une adresse complète, cliquable."""
    if lien and lien.startswith('/'):
        lien = ((config_mail() or {}).get('site') or SITE_URL_DEFAUT).rstrip('/') + lien
    return f"📢 **{titre}**\n{texte or ''}" + (f"\n{lien}" if lien else '')


AUTO_DISCORD = {'resultats': 'Résultats des matchs', 'tournois': 'Nouveaux tournois et champions', 'saison': 'Fin de saison (champions, montées)'}


def auto_discord_actif(db, sujet):
    r = db.execute('SELECT valeur FROM parametres WHERE cle = ?', (f'discord_auto_{sujet}',)).fetchone()
    return bool(r and r['valeur'] == '1')


def enfiler_discord(db, sujet, message):
    """Met un message dans la file Discord (envoyé en arrière-plan, sans ralentir l'action en cours)."""
    if auto_discord_actif(db, sujet):
        db.execute("INSERT INTO discord_file (contenu, created_at) VALUES (?, datetime('now'))", (message[:1900],))


def envoyer_file_discord(db, maximum=2):
    """Envoie quelques messages en attente (appelé pendant les visites, au plus toutes les 30 s)."""
    w = db.execute("SELECT valeur FROM parametres WHERE cle = 'discord_webhook'").fetchone()
    if not w or not DISCORD_WEBHOOK_REGEX.match(w['valeur'] or ''):
        return 0
    pause = db.execute("SELECT valeur FROM parametres WHERE cle = 'discord_pause'").fetchone()
    if pause and pause['valeur'] > datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'):
        return 0          # Discord nous a demandé d'attendre : on ne le relance pas avant la fin de la pause
    envoyes = 0
    for m in db.execute("""SELECT id, contenu FROM discord_file WHERE envoye_at IS NULL AND essais < 5
                           ORDER BY id LIMIT ?""", (maximum,)).fetchall():
        # On « prend » le message d'abord : deux visites simultanées ne l'envoient jamais deux fois
        if not db.execute('UPDATE discord_file SET essais = essais + 1 WHERE id = ? AND envoye_at IS NULL', (m['id'],)).rowcount:
            continue
        db.commit()
        ok, raison = poster_discord(w['valeur'], m['contenu'], delai=4)
        if ok:
            db.execute("UPDATE discord_file SET envoye_at = datetime('now') WHERE id = ?", (m['id'],))
            db.execute("DELETE FROM parametres WHERE cle = 'discord_erreur'")
            envoyes += 1
        else:   # gardée pour l'admin (Admin → Automatique), le message sera réessayé
            db.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES ('discord_erreur', ?)", (raison[:400],))
            if '429' in raison:
                # Discord limite le serveur (fréquent sur un compte gratuit) : cet essai ne compte pas,
                # on attend 15 minutes avant de réessayer
                db.execute('UPDATE discord_file SET essais = essais - 1 WHERE id = ?', (m['id'],))
                db.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES ('discord_pause', datetime('now', '+15 minutes'))")
                db.commit()
                break
        db.commit()
    db.execute("DELETE FROM discord_file WHERE created_at < datetime('now', '-7 days')")
    db.commit()
    return envoyes


def publier_annonces_dues(db):
    """Publie les annonces dont l'heure est arrivée : notification à tous les joueurs et Discord si demandé."""
    dues = db.execute("SELECT * FROM annonces WHERE publiee = 0 AND publier_at <= datetime('now') ORDER BY publier_at").fetchall()
    for a in dues:
        # On « prend » l'annonce d'abord : deux visites simultanées ne la publient jamais deux fois
        if not db.execute('UPDATE annonces SET publiee = 1 WHERE id = ? AND publiee = 0', (a['id'],)).rowcount:
            continue
        if a['notifier']:
            db.execute("""INSERT INTO notifications (user_id, texte, lien, created_at)
                          SELECT id, ?, ?, datetime('now') FROM users WHERE COALESCE(bloque, 0) = 0""",
                       (f"📢 {a['titre']}"[:200], f"/annonces#annonce-{a['id']}"))
        db.commit()
        if a['discord']:
            ok = envoyer_discord(a['titre'], a['texte'], a['lien'])
            db.execute('UPDATE annonces SET discord_ok = ? WHERE id = ?', (1 if ok else None, a['id']))
            if not ok:   # Discord indisponible : l'annonce part plus tard, toute seule, avec les messages automatiques
                db.execute("INSERT INTO discord_file (contenu, created_at) VALUES (?, datetime('now'))",
                           (message_annonce(a['titre'], a['texte'], a['lien'])[:1900],))
            db.commit()
    return len(dues)


_controle_annonces = {'t': 0.0}


def annonces_actives(db, limite=None):
    req = """SELECT id, titre, texte, lien, publier_at FROM annonces WHERE publiee = 1
             AND (fin_at IS NULL OR fin_at > datetime('now')) ORDER BY publier_at DESC"""
    lignes = [dict(r) for r in db.execute(req + (' LIMIT ?' if limite else ''), (limite,) if limite else ())]
    for l in lignes:
        l['quand'] = date_paris(l['publier_at'])
    return lignes


def a_faire_admin(db, jeu_id, saison):
    """Ce qui attend l'organisateur : résultats en retard, matchs sans date, teams inactives, tournois, sauvegarde."""
    maintenant = datetime.now()
    compter = lambda req, *p: db.execute(req, p).fetchone()[0]
    base = ('FROM rencontres r JOIN teams t1 ON t1.id = r.team1_id JOIN teams t2 ON t2.id = r.team2_id '
            'WHERE r.jeu_id = ? AND r.saison = ? AND r.match_id IS NULL')
    dans7 = (maintenant + timedelta(days=7)).strftime('%Y-%m-%dT%H:%M')
    ici = maintenant.strftime('%Y-%m-%dT%H:%M')
    tournois = [dict(r) for r in db.execute(
        """SELECT t.id, t.nom, t.statut, t.places, (SELECT COUNT(*) FROM tournoi_inscriptions i WHERE i.tournoi_id = t.id) AS inscrits
           FROM tournois t WHERE t.jeu_id = ? AND t.statut != 'termine' ORDER BY t.id""", (jeu_id,))]
    recompenses = [dict(r) for r in db.execute(
        """SELECT id, nom, champion_nom, recompense FROM tournois WHERE jeu_id = ? AND statut = 'termine'
           AND COALESCE(recompense, '') != '' AND recompense_envoyee_at IS NULL ORDER BY id""", (jeu_id,))]
    derniere = None
    if os.path.isdir(SAUVEGARDE_DIR):
        zips = [os.path.join(SAUVEGARDE_DIR, f) for f in os.listdir(SAUVEGARDE_DIR) if f.startswith('xhq-') and f.endswith('.zip')]
        if zips:
            derniere = (maintenant - datetime.fromtimestamp(max(os.path.getmtime(z) for z in zips))).days
    return {
        'en_retard': compter(f'SELECT COUNT(*) {base} AND r.date IS NOT NULL AND r.date < ?', jeu_id, saison, ici),
        'sans_date': compter(f'SELECT COUNT(*) {base} AND r.date IS NULL', jeu_id, saison),
        'cette_semaine': compter(f'SELECT COUNT(*) {base} AND r.date >= ? AND r.date <= ?', jeu_id, saison, ici, dans7),
        'prevus': compter(f'SELECT COUNT(*) {base}', jeu_id, saison),
        'teams_inactives': compter("""SELECT COUNT(*) FROM teams t JOIN ligue l ON l.team_id = t.id WHERE t.jeu_id = ? AND NOT EXISTS (
                                        SELECT 1 FROM matchs m WHERE m.saison = ? AND (m.team1_id = t.id OR m.team2_id = t.id))""",
                                   jeu_id, saison),
        'suspendus': compter('SELECT COUNT(*) FROM users WHERE bloque = 1'),
        'tournois': tournois,
        'recompenses': recompenses,
        'sauvegarde_jours': derniere,
        'outils_vieux': outils_a_mettre_a_jour(),
    }


def enregistrer_resultat(c, db, team1_id, team2_id, score1, score2, saisi_par):
    """Enregistre un match de ligue (points, calendrier, notifications).
    Renvoie (infos, None) ou (None, (message d'erreur, code HTTP)). Ne fait pas le commit."""
    if team1_id is None or team2_id is None:
        return None, ('Choisissez les deux teams', 400)
    if team1_id == team2_id:
        return None, ('Une team ne peut pas jouer contre elle-même', 400)
    if score1 is None or score2 is None or not (0 <= score1 <= SCORE_MAX and 0 <= score2 <= SCORE_MAX):
        return None, (f'Les scores doivent être des nombres entre 0 et {SCORE_MAX}', 400)
    if score1 == score2:
        return None, ("Pas de match nul : il faut un gagnant", 400)
    t1 = c.execute('SELECT t.nom, t.jeu_id, l.ligue_num, l.poule FROM teams t JOIN ligue l ON l.team_id = t.id '
                   'WHERE t.id = ?', (team1_id,)).fetchone()
    t2 = c.execute('SELECT t.nom, t.jeu_id, l.ligue_num, l.poule FROM teams t JOIN ligue l ON l.team_id = t.id '
                   'WHERE t.id = ?', (team2_id,)).fetchone()
    if not t1 or not t2:
        return None, ('Team introuvable', 404)
    if t1['jeu_id'] != t2['jeu_id']:
        return None, ('Les deux teams ne jouent pas au même jeu', 400)
    if (t1['ligue_num'], t1['poule']) != (t2['ligue_num'], t2['poule']):
        return None, ('Les deux teams doivent être dans la même ligue et la même poule', 400)

    ligue_num, poule, jeu_id = t1['ligue_num'], t1['poule'], t1['jeu_id']
    saison = saison_actuelle(db, jeu_id)
    points1 = reglage('points_victoire', db) if score1 > score2 else reglage('points_defaite', db)
    points2 = reglage('points_victoire', db) if score2 > score1 else reglage('points_defaite', db)
    c.execute("""INSERT INTO matchs (ligue_num, poule, team1_id, team2_id, team1_nom, team2_nom,
                                     score1, score2, points1, points2, saisi_par, created_at, saison, jeu_id)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
              (ligue_num, poule, team1_id, team2_id, t1['nom'], t2['nom'],
               score1, score2, points1, points2, saisi_par, saison, jeu_id))
    match_id = c.lastrowid
    # Le résultat « remplit » le match prévu au calendrier entre ces deux teams (le plus ancien)
    c.execute("""UPDATE rencontres SET match_id = ? WHERE id = (
                    SELECT id FROM rencontres WHERE match_id IS NULL AND jeu_id = ? AND saison = ?
                    AND ((team1_id = ? AND team2_id = ?) OR (team1_id = ? AND team2_id = ?))
                    ORDER BY date LIMIT 1)""", (match_id, jeu_id, saison, team1_id, team2_id, team2_id, team1_id))
    ajuster_points(c, team1_id, ligue_num, poule, points1)
    ajuster_points(c, team2_id, ligue_num, poule, points2)
    for moi, adv, a, b, pts in ((team1_id, t2['nom'], score1, score2, points1), (team2_id, t1['nom'], score2, score1, points2)):
        notifier(c, membres_de(c, moi), f"{'Victoire' if a > b else 'Défaite'} {a}-{b} contre {adv} ({'+' if pts else ''}{pts} pts)",
                 f'/team/{moi}')
    gagnant, perdant = (t1['nom'], t2['nom']) if score1 > score2 else (t2['nom'], t1['nom'])
    enfiler_discord(db, 'resultats', f"⚔️ **{gagnant}** bat **{perdant}** {max(score1, score2)}-{min(score1, score2)} · Ligue {ligue_num} poule {poule}")
    return {'match_id': match_id, 'gagnant': t1['nom'] if score1 > score2 else t2['nom'],
            'ligue_num': ligue_num, 'poule': poule}, None


@app.route('/api/admin/matchs', methods=['POST'])
@api_admin
def api_admin_create_match():
    data = donnees_json()
    db = get_db()
    c = db.cursor()
    infos, erreur = enregistrer_resultat(c, db, entier(data.get('team1_id')), entier(data.get('team2_id')),
                                         entier(data.get('score1')), entier(data.get('score2')), session['user_id'])
    if erreur:
        return jsonify({'success': False, 'error': erreur[0]}), erreur[1]
    journaliser(db, 'Résultat saisi', f"{nom_team(db, entier(data.get('team1_id')))} {entier(data.get('score1'))}-{entier(data.get('score2'))} {nom_team(db, entier(data.get('team2_id')))}")
    # Un résultat saisi par l'admin clôt une éventuelle déclaration en cours entre ces deux teams
    c.execute("""UPDATE declarations SET statut = 'valide', reponse = 'admin', match_id = ?, traite_at = datetime('now')
                 WHERE statut IN ('attente', 'conteste') AND ((team1_id = ? AND team2_id = ?) OR (team1_id = ? AND team2_id = ?))""",
              (infos['match_id'], entier(data.get('team1_id')), entier(data.get('team2_id')),
               entier(data.get('team2_id')), entier(data.get('team1_id'))))
    db.commit()
    return jsonify({'success': True, **infos}), 201


# ---------------------------------------------------------------------------
# Résultats déclarés par les capitaines : l'un déclare, l'autre confirme ou conteste.
# Sans réponse sous 48 h, le résultat est validé. Un litige est tranché par un admin.
# ---------------------------------------------------------------------------
DELAI_CONFIRMATION_H = 48


def capitaine_de(c, team_id):
    r = c.execute('SELECT createur_id FROM teams WHERE id = ?', (team_id,)).fetchone()
    return r['createur_id'] if r else None


def valider_declaration(c, db, d, score1=None, score2=None, reponse='confirme'):
    """Transforme une déclaration en match. Renvoie un message d'erreur ou None."""
    s1 = d['score1'] if score1 is None else score1
    s2 = d['score2'] if score2 is None else score2
    if d['saison'] != saison_actuelle(db, d['jeu_id']):
        c.execute("UPDATE declarations SET statut = 'annule', motif = 'Saison terminée', traite_at = datetime('now') WHERE id = ?", (d['id'],))
        return "Ce résultat date d'une saison terminée"
    # On « réserve » la déclaration d'abord : deux clics simultanés ne créent jamais deux matchs
    if not c.execute("UPDATE declarations SET statut = 'traitement' WHERE id = ? AND statut IN ('attente', 'conteste')",
                     (d['id'],)).rowcount:
        return 'Ce résultat a déjà été traité'
    infos, erreur = enregistrer_resultat(c, db, d['team1_id'], d['team2_id'], s1, s2, d['par_user_id'])
    if erreur:
        c.execute("UPDATE declarations SET statut = 'annule', motif = ?, traite_at = datetime('now') WHERE id = ?",
                  (erreur[0], d['id']))
        return erreur[0]
    c.execute("""UPDATE declarations SET statut = 'valide', reponse = ?, score1 = ?, score2 = ?, match_id = ?,
                 traite_at = datetime('now') WHERE id = ?""", (reponse, s1, s2, infos['match_id'], d['id']))
    return None


def valider_declarations_expirees(db):
    """Les déclarations restées sans réponse 48 h sont validées automatiquement."""
    c = db.cursor()
    rows = c.execute("""SELECT * FROM declarations WHERE statut = 'attente'
                        AND created_at <= datetime('now', ?)""", (f"-{reglage('delai_confirmation_h', db)} hours",)).fetchall()
    for d in rows:
        valider_declaration(c, db, d, reponse='auto')
    if rows:
        db.commit()


def declaration_publique(c, d):
    d = dict(d)
    noms = {r['id']: r['nom'] for r in c.execute('SELECT id, nom FROM teams WHERE id IN (?, ?)', (d['team1_id'], d['team2_id']))}
    d['team1_nom'] = noms.get(d['team1_id'], 'Team supprimée')
    d['team2_nom'] = noms.get(d['team2_id'], 'Team supprimée')
    d['date_fr'] = il_y_a(d['created_at']) if d.get('created_at') else ''
    if d['statut'] == 'attente' and d.get('created_at'):
        fin = datetime.strptime(d['created_at'][:19], '%Y-%m-%d %H:%M:%S') + timedelta(hours=reglage('delai_confirmation_h'))
        d['heures_restantes'] = max(0, int((fin - datetime.utcnow()).total_seconds() // 3600))
    return d


@app.route('/api/resultats', methods=['GET'])
@api_connectee
def api_resultats():
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    valider_declarations_expirees(db)
    team = ma_team_dans_jeu(db, jeu['id'])
    if not team:
        return jsonify({'team': None}), 200
    c = db.cursor()
    tid = team['id']
    ligue = c.execute('SELECT ligue_num, poule FROM ligue WHERE team_id = ?', (tid,)).fetchone()
    adversaires = []
    if ligue:
        saison = saison_actuelle(db, jeu['id'])
        for r in c.execute("""SELECT t.id, t.nom FROM ligue l JOIN teams t ON t.id = l.team_id
                              WHERE l.ligue_num = ? AND l.poule = ? AND t.id != ? AND t.jeu_id = ? ORDER BY t.nom""",
                           (ligue['ligue_num'], ligue['poule'], tid, jeu['id'])).fetchall():
            joues = c.execute("""SELECT COUNT(*) FROM matchs WHERE saison = ? AND jeu_id = ?
                                 AND ((team1_id = ? AND team2_id = ?) OR (team1_id = ? AND team2_id = ?))""",
                              (saison, jeu['id'], tid, r['id'], r['id'], tid)).fetchone()[0]
            adversaires.append({'id': r['id'], 'nom': r['nom'], 'joues': joues})
    liste = lambda cond, *p: [declaration_publique(c, d) for d in c.execute(
        f'SELECT * FROM declarations WHERE jeu_id = ? AND {cond} ORDER BY id DESC LIMIT 10', (jeu['id'], *p)).fetchall()]
    return jsonify({
        'team': team, 'capitaine': team['capitaine'], 'adversaires': adversaires, 'delai_h': reglage('delai_confirmation_h'),
        'a_confirmer': liste("statut = 'attente' AND team2_id = ?", tid),
        'en_attente': liste("statut = 'attente' AND team1_id = ?", tid),
        'litiges': liste("statut = 'conteste' AND (team1_id = ? OR team2_id = ?)", tid, tid),
        'recents': liste("statut IN ('valide', 'refuse', 'annule') AND (team1_id = ? OR team2_id = ?) AND traite_at >= datetime('now', '-7 days')", tid, tid),
    }), 200


@app.route('/api/resultats/declarer', methods=['POST'])
@api_connectee
def api_resultats_declarer():
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    team = ma_team_dans_jeu(db, jeu['id'])
    if not team or not team['capitaine']:
        return jsonify({'success': False, 'error': 'Seul le capitaine peut déclarer un résultat'}), 403
    if limite_atteinte(f"declarer:{session['user_id']}", 10, 60):
        return jsonify({'success': False, 'error': TROP}), 429
    noter_action(f"declarer:{session['user_id']}")
    data = donnees_json()
    adv_id = entier(data.get('adversaire_id'))
    pour, contre = entier(data.get('score_pour')), entier(data.get('score_contre'))
    if adv_id is None:
        return jsonify({'success': False, 'error': "Choisis l'équipe adverse"}), 400
    if pour is None or contre is None or not (0 <= pour <= SCORE_MAX and 0 <= contre <= SCORE_MAX):
        return jsonify({'success': False, 'error': f'Les scores doivent être des nombres entre 0 et {SCORE_MAX}'}), 400
    if pour == contre:
        return jsonify({'success': False, 'error': 'Pas de match nul : il faut un gagnant'}), 400
    c = db.cursor()
    l1 = c.execute('SELECT ligue_num, poule FROM ligue WHERE team_id = ?', (team['id'],)).fetchone()
    l2 = c.execute('SELECT l.ligue_num, l.poule, t.nom, t.jeu_id FROM ligue l JOIN teams t ON t.id = l.team_id WHERE t.id = ?', (adv_id,)).fetchone()
    if not l1 or not l2 or l2['jeu_id'] != jeu['id'] or (l1['ligue_num'], l1['poule']) != (l2['ligue_num'], l2['poule']) or adv_id == team['id']:
        return jsonify({'success': False, 'error': 'Cette équipe n\'est pas dans ta poule'}), 400
    saison = saison_actuelle(db, jeu['id'])
    paire = (team['id'], adv_id, adv_id, team['id'])
    joues = c.execute("""SELECT COUNT(*) FROM matchs WHERE jeu_id = ? AND saison = ?
                         AND ((team1_id = ? AND team2_id = ?) OR (team1_id = ? AND team2_id = ?))""", (jeu['id'], saison, *paire)).fetchone()[0]
    prevus = c.execute("""SELECT COUNT(*) FROM rencontres WHERE jeu_id = ? AND saison = ?
                          AND ((team1_id = ? AND team2_id = ?) OR (team1_id = ? AND team2_id = ?))""", (jeu['id'], saison, *paire)).fetchone()[0]
    if joues >= max(1, prevus):
        return jsonify({'success': False, 'error': 'Vous vous êtes déjà affrontés cette saison : demande à un admin si un match doit être rejoué'}), 400
    deja = c.execute("""SELECT 1 FROM declarations WHERE statut IN ('attente', 'conteste')
                        AND ((team1_id = ? AND team2_id = ?) OR (team1_id = ? AND team2_id = ?))""",
                     (team['id'], adv_id, adv_id, team['id'])).fetchone()
    if deja:
        return jsonify({'success': False, 'error': 'Un résultat est déjà en attente entre vos deux équipes'}), 400
    try:
        c.execute("""INSERT INTO declarations (jeu_id, saison, team1_id, team2_id, score1, score2, par_user_id, statut, created_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, 'attente', datetime('now'))""",
                  (jeu['id'], saison_actuelle(db, jeu['id']), team['id'], adv_id, pour, contre, session['user_id']))
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({'success': False, 'error': 'Un résultat est déjà en attente entre vos deux équipes'}), 400
    notifier(c, membres_de(c, adv_id),
             f"{team['nom']} déclare {contre}-{pour} pour vous ({'défaite' if pour > contre else 'victoire'}). "
             f"Confirme ou conteste sous {reglage('delai_confirmation_h')} h.", '/teams#resultats')
    db.commit()
    return jsonify({'success': True}), 201


def declaration_ou_erreur(c, declaration_id):
    d = c.execute('SELECT * FROM declarations WHERE id = ?', (declaration_id,)).fetchone()
    if not d:
        return None, (jsonify({'success': False, 'error': 'Résultat introuvable'}), 404)
    return d, None


@app.route('/api/resultats/<int:declaration_id>/<action>', methods=['POST'])
@api_connectee
def api_resultats_repondre(declaration_id, action):
    db = get_db()
    c = db.cursor()
    d, erreur = declaration_ou_erreur(c, declaration_id)
    if erreur:
        return erreur
    uid = session['user_id']
    if d['statut'] != 'attente':
        return jsonify({'success': False, 'error': 'Ce résultat a déjà été traité'}), 400
    noms = {r['id']: r['nom'] for r in c.execute('SELECT id, nom FROM teams WHERE id IN (?, ?)', (d['team1_id'], d['team2_id']))}
    if action == 'annuler':
        if capitaine_de(c, d['team1_id']) != uid:
            return jsonify({'success': False, 'error': 'Seul le capitaine qui a déclaré peut annuler'}), 403
        c.execute("UPDATE declarations SET statut = 'annule', traite_at = datetime('now') WHERE id = ?", (d['id'],))
        db.commit()
        return jsonify({'success': True}), 200
    if capitaine_de(c, d['team2_id']) != uid:
        return jsonify({'success': False, 'error': "Seul le capitaine de l'équipe adverse peut répondre"}), 403
    if action == 'confirmer':
        message = valider_declaration(c, db, d)
        if message:
            db.commit()
            return jsonify({'success': False, 'error': message}), 400
        notifier(c, [capitaine_de(c, d['team1_id'])], noms.get(d['team2_id'], "L'adversaire") + f" a confirmé le résultat {d['score1']}-{d['score2']}.",
                 '/teams#resultats')
        db.commit()
        return jsonify({'success': True}), 200
    if action == 'contester':
        motif = texte(donnees_json(), 'motif', 300)
        if not motif:
            return jsonify({'success': False, 'error': 'Explique en quelques mots ce qui ne va pas'}), 400
        c.execute("UPDATE declarations SET statut = 'conteste', motif = ?, traite_at = datetime('now') WHERE id = ?",
                  (motif, d['id']))
        a, b = noms.get(d['team1_id'], '?'), noms.get(d['team2_id'], '?')
        notifier(c, [capitaine_de(c, d['team1_id'])], f"{b} conteste le résultat {d['score1']}-{d['score2']} : un admin va trancher.", '/teams#resultats')
        notifier(c, [r[0] for r in c.execute('SELECT id FROM users WHERE is_admin = 1')],
                 f"Litige : {a} vs {b} ({d['score1']}-{d['score2']} contesté)", '/admin#matchs')
        db.commit()
        return jsonify({'success': True}), 200
    return jsonify({'success': False, 'error': 'Action inconnue'}), 400


@app.route('/api/admin/declarations', methods=['GET'])
@api_admin
def api_admin_declarations():
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    valider_declarations_expirees(db)
    c = db.cursor()
    rows = c.execute("""SELECT * FROM declarations WHERE jeu_id = ? AND statut IN ('conteste', 'attente')
                        ORDER BY statut = 'attente', id""", (jeu['id'],)).fetchall()
    return jsonify([declaration_publique(c, d) for d in rows]), 200


@app.route('/api/admin/declarations/<int:declaration_id>', methods=['POST'])
@api_admin
def api_admin_trancher(declaration_id):
    db = get_db()
    c = db.cursor()
    d, erreur = declaration_ou_erreur(c, declaration_id)
    if erreur:
        return erreur
    if d['statut'] not in ('attente', 'conteste'):
        return jsonify({'success': False, 'error': 'Ce résultat a déjà été traité'}), 400
    data = donnees_json()
    capitaines = [capitaine_de(c, d['team1_id']), capitaine_de(c, d['team2_id'])]
    if data.get('action') == 'valider':
        s1 = entier(data.get('score1')) if data.get('score1') is not None else None
        s2 = entier(data.get('score2')) if data.get('score2') is not None else None
        message = valider_declaration(c, db, d, s1, s2, reponse='admin')
        journaliser(db, 'Litige tranché : résultat validé', f"{nom_team(db, d['team1_id'])} {s1 if s1 is not None else d['score1']}-{s2 if s2 is not None else d['score2']} {nom_team(db, d['team2_id'])}")
        db.commit()
        if message:
            return jsonify({'success': False, 'error': message}), 400
        return jsonify({'success': True}), 200
    if data.get('action') == 'refuser':
        c.execute("UPDATE declarations SET statut = 'refuse', reponse = 'admin', traite_at = datetime('now') WHERE id = ?", (d['id'],))
        journaliser(db, 'Litige tranché : résultat refusé', f"{nom_team(db, d['team1_id'])} vs {nom_team(db, d['team2_id'])}")
        notifier(c, capitaines, f"Un admin a refusé le résultat déclaré {d['score1']}-{d['score2']} : à rejouer ou à redéclarer.", '/teams#resultats')
        db.commit()
        return jsonify({'success': True}), 200
    return jsonify({'success': False, 'error': 'Action inconnue'}), 400


@app.route('/api/admin/matchs/<int:match_id>/delete', methods=['POST'])
@api_admin
def api_admin_delete_match(match_id):
    db = get_db()
    c = db.cursor()
    m = c.execute('SELECT * FROM matchs WHERE id = ?', (match_id,)).fetchone()
    if not m:
        return jsonify({'success': False, 'error': 'Match introuvable'}), 404
    if m['saison'] != saison_actuelle(db, m['jeu_id']):
        return jsonify({'success': False, 'error': 'Ce match appartient à une saison terminée'}), 400
    journaliser(db, 'Match supprimé', f"{m['team1_nom']} {m['score1']}-{m['score2']} {m['team2_nom']}")
    ajuster_points(c, m['team1_id'], m['ligue_num'], m['poule'], -m['points1'])
    ajuster_points(c, m['team2_id'], m['ligue_num'], m['poule'], -m['points2'])
    c.execute('DELETE FROM matchs WHERE id = ?', (match_id,))
    c.execute('UPDATE rencontres SET match_id = NULL WHERE match_id = ?', (match_id,))
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/teams', methods=['GET'])
@api_admin
def api_admin_teams():
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    rows = db.execute("""
        SELECT t.id, t.nom, u.username AS createur, l.ligue_num, l.poule, l.points, t.logo, t.banniere, t.bio,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id) AS nb_membres
        FROM teams t
        LEFT JOIN users u ON u.id = t.createur_id
        LEFT JOIN ligue l ON l.team_id = t.id
        WHERE t.jeu_id = ?
        ORDER BY l.ligue_num, l.poule, t.nom COLLATE NOCASE""", (jeu['id'],)).fetchall()
    sortie = []
    for r in rows:
        t = dict(r)
        t['logo'], t['banniere'] = url_media('logos', t['logo']), url_media('bannieres', t['banniere'])
        sortie.append(t)
    return jsonify(sortie), 200


@app.route('/api/admin/teams/<int:team_id>/move', methods=['POST'])
@api_admin
def api_admin_move_team(team_id):
    ligue_num = entier(donnees_json().get('ligue_num'))
    if ligue_num not in LIGUES:
        return jsonify({'success': False, 'error': f'Ligue invalide (1 à {LIGUE_MAX})'}), 400
    db = get_db()
    c = db.cursor()
    jeu_id = jeu_de_team(db, team_id)
    if jeu_id is None:
        return jsonify({'success': False, 'error': 'Team introuvable'}), 404
    actuelle = c.execute('SELECT ligue_num FROM ligue WHERE team_id = ?', (team_id,)).fetchone()
    if not actuelle:  # team hors classement : on la remet dans la ligue choisie
        c.execute('INSERT INTO ligue (team_id, ligue_num, points, poule) VALUES (?, ?, 0, ?)',
                  (team_id, ligue_num, choisir_poule(c, jeu_id, ligue_num)))
        c.execute('UPDATE teams SET hors_classement = 0, attente_depuis = NULL, en_formation = 0 WHERE id = ?', (team_id,))
        db.commit()
        poule = c.execute('SELECT poule FROM ligue WHERE team_id = ?', (team_id,)).fetchone()['poule']
        return jsonify({'success': True, 'ligue_num': ligue_num, 'poule': poule}), 200
    if actuelle['ligue_num'] == ligue_num:
        return jsonify({'success': False, 'error': f'La team est déjà en Ligue {ligue_num}'}), 400
    journaliser(db, f'Team déplacée en Ligue {ligue_num}', nom_team(db, team_id))
    # Changement de ligue : nouvelle poule, les points repartent de 0
    c.execute('UPDATE ligue SET ligue_num = ?, poule = NULL, points = 0 WHERE team_id = ?', (ligue_num, team_id))
    poule = choisir_poule(c, jeu_id, ligue_num)
    c.execute('UPDATE ligue SET poule = ? WHERE team_id = ?', (poule, team_id))
    db.commit()
    return jsonify({'success': True, 'ligue_num': ligue_num, 'poule': poule}), 200


@app.route('/api/admin/teams/<int:team_id>/delete', methods=['POST'])
@api_admin
def api_admin_delete_team(team_id):
    db = get_db()
    if not db.execute('SELECT 1 FROM teams WHERE id = ?', (team_id,)).fetchone():
        return jsonify({'success': False, 'error': 'Team introuvable'}), 404
    journaliser(db, 'Team supprimée', nom_team(db, team_id))
    supprimer_team(db.cursor(), team_id)
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/users', methods=['GET'])
@api_admin
def api_admin_users():
    q = request.args.get('q', '').strip().lower()[:100]
    rows = get_db().execute("""
        SELECT u.id, u.username, u.email, u.solde, u.is_admin, u.created_at, u.handle, u.photo, u.banniere, u.bio, u.bloque,
               (SELECT GROUP_CONCAT(t.nom, ', ') FROM team_members tm JOIN teams t ON t.id = tm.team_id
                WHERE tm.user_id = u.id) AS team_name
        FROM users u
        WHERE lower(u.username) LIKE ? OR lower(u.email) LIKE ?
        ORDER BY u.id DESC LIMIT 50""", (f'%{q}%', f'%{q}%')).fetchall()
    users = []
    for r in rows:
        u = dict(r)
        u['is_admin'] = bool(u['is_admin'])
        u['bloque'] = bool(u['bloque'])
        u['fondateur'] = possede(get_db(), u['id'], 'fondateur')
        v = get_db().execute("SELECT verrou_definitif, bloque_jusqua > datetime('now') AS temp FROM users WHERE id = ?", (u['id'],)).fetchone()
        u['verrou'] = 'definitif' if v['verrou_definitif'] else ('temporaire' if v['temp'] else None)
        u['is_me'] = u['id'] == session['user_id']
        u['photo'] = url_media('avatars', u['photo'])
        u['banniere'] = url_media('bannieres', u['banniere'])
        users.append(u)
    return jsonify(users), 200


@app.route('/api/admin/users/<int:user_id>/reset-password', methods=['POST'])
@api_admin
def api_admin_reset_password(user_id):
    db = get_db()
    if est_protege(db, user_id) and user_id != session['user_id'] and fondateur_requis(db):
        return jsonify({'success': False, 'error': fondateur_requis(db)}), 403
    user = db.execute('SELECT username, email FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'success': False, 'error': 'Joueur introuvable'}), 404
    if config_mail():
        # On n'affiche jamais de mot de passe : le lien part sur l'email DU COMPTE (pas celui qui a écrit)
        if not envoyer_lien_reinitialisation(db, user_id):
            return jsonify({'success': False, 'error': "L'email n'a pas pu partir, réessaie plus tard"}), 502
        journaliser(db, 'Lien de nouveau mot de passe envoyé', nom_joueur(db, user_id))
        db.commit()
        return jsonify({'success': True, 'email_envoye': True, 'username': user['username'], 'email': user['email']}), 200
    journaliser(db, 'Mot de passe temporaire créé', nom_joueur(db, user_id))
    temporaire = secrets.token_urlsafe(8)
    db.execute('UPDATE users SET password = ? WHERE id = ?', (generate_password_hash(temporaire), user_id))
    invalider_sessions(db, user_id)
    db.commit()
    return jsonify({'success': True, 'username': user['username'], 'email': user['email'],
                    'mot_de_passe': temporaire}), 200


@app.route('/api/admin/users/<int:user_id>/admin', methods=['POST'])
@api_admin
def api_admin_set_admin(user_id):
    valeur = donnees_json().get('is_admin')
    if not isinstance(valeur, bool):
        return jsonify({'success': False, 'error': 'Valeur invalide'}), 400
    if user_id == session['user_id'] and not valeur:
        return jsonify({'success': False, 'error': 'Vous ne pouvez pas retirer vos propres droits admin'}), 400
    db = get_db()
    c = db.cursor()
    u = c.execute('SELECT is_admin FROM users WHERE id = ?', (user_id,)).fetchone()
    if not u:
        return jsonify({'success': False, 'error': 'Joueur introuvable'}), 404
    if fondateur_requis(db):
        return jsonify({'success': False, 'error': 'Seuls les fondateurs peuvent nommer ou retirer un admin'}), 403
    if valeur and not u['is_admin'] and c.execute('SELECT COUNT(*) FROM users WHERE is_admin = 1').fetchone()[0] >= ADMINS_MAX:
        return jsonify({'success': False, 'error': f'Il y a déjà {ADMINS_MAX} admins (le maximum). Retire les droits à quelqu\'un d\'abord.'}), 400
    c.execute('UPDATE users SET is_admin = ? WHERE id = ?', (1 if valeur else 0, user_id))
    journaliser(db, 'Droits admin donnés' if valeur else 'Droits admin retirés', nom_joueur(db, user_id))
    if valeur:
        donner_objet(c, user_id, 'admin', equiper=not c.execute('SELECT 1 FROM users WHERE id = ? AND badge_equipe = ?', (user_id, 'fondateur')).fetchone())
        notifier(c, [user_id], "Tu fais maintenant partie de l'équipe d'orga : badge Admin débloqué.", '/admin')
    else:
        retirer_objet(c, user_id, 'admin')
    db.commit()
    return jsonify({'success': True}), 200


def lire_annonce(data):
    """Valide le formulaire d'annonce. Renvoie (valeurs, erreur)."""
    titre = texte(data, 'titre', 100)
    corps = texte(data, 'texte', 1500)
    lien = texte(data, 'lien', 300)
    if len(titre) < 3:
        return None, 'Le titre doit faire au moins 3 caractères'
    if lien and not re.match(r'^(https://|/)[^\s<>"]*$', lien):
        return None, 'Le lien doit commencer par https:// ou / (une page du site)'
    try:
        publier = paris_vers_utc(texte(data, 'publier_at', 16)) if texte(data, 'publier_at', 16) else datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        fin = paris_vers_utc(texte(data, 'fin_at', 16)) if texte(data, 'fin_at', 16) else None
    except ValueError:
        return None, 'Date invalide'
    if fin and fin <= publier:
        return None, 'La fin doit être après la publication'
    return {'titre': titre, 'texte': corps, 'lien': lien or None, 'publier_at': publier, 'fin_at': fin,
            'notifier': 1 if data.get('notifier', True) else 0, 'discord': 1 if data.get('discord') else 0}, None


@app.route('/api/admin/annonces', methods=['GET', 'POST'])
@api_admin
def api_admin_annonces():
    db = get_db()
    if request.method == 'POST':
        valeurs, erreur = lire_annonce(donnees_json())
        if erreur:
            return jsonify({'success': False, 'error': erreur}), 400
        cur = db.execute("""INSERT INTO annonces (titre, texte, lien, publier_at, fin_at, notifier, discord, auteur_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                         (valeurs['titre'], valeurs['texte'], valeurs['lien'], valeurs['publier_at'], valeurs['fin_at'],
                          valeurs['notifier'], valeurs['discord'], session['user_id']))
        journaliser(db, 'Annonce créée', valeurs['titre'])
        db.commit()
        publier_annonces_dues(db)
        return jsonify({'success': True, 'id': cur.lastrowid}), 201
    lignes = []
    for a in db.execute('SELECT a.*, u.username AS auteur FROM annonces a LEFT JOIN users u ON u.id = a.auteur_id ORDER BY a.publier_at DESC LIMIT 100'):
        a = dict(a)
        a['publier_fr'], a['publier_court'] = date_paris(a['publier_at']), date_paris(a['publier_at'], True)
        a['fin_fr'], a['fin_court'] = date_paris(a['fin_at']), date_paris(a['fin_at'], True)
        a['etat'] = 'programmee' if not a['publiee'] else ('terminee' if a['fin_at'] and a['fin_at'] <= datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') else 'en_ligne')
        lignes.append(a)
    w = db.execute("SELECT valeur FROM parametres WHERE cle = 'discord_webhook'").fetchone()
    return jsonify({'annonces': lignes, 'discord_relie': bool(w and w['valeur']),
                    'relais_actif': bool(db.execute("SELECT 1 FROM parametres WHERE cle = 'relais_url'").fetchone())}), 200


@app.route('/api/admin/annonces/<int:annonce_id>', methods=['POST'])
@api_admin
def api_admin_annonce(annonce_id):
    db = get_db()
    a = db.execute('SELECT * FROM annonces WHERE id = ?', (annonce_id,)).fetchone()
    if not a:
        return jsonify({'success': False, 'error': 'Annonce introuvable'}), 404
    data = donnees_json()
    if data.get('supprimer'):
        db.execute('DELETE FROM annonces WHERE id = ?', (annonce_id,))
        db.execute('DELETE FROM notifications WHERE lien = ?', (f'/annonces#annonce-{annonce_id}',))
        journaliser(db, 'Annonce supprimée', a['titre'])
        db.commit()
        return jsonify({'success': True}), 200
    if data.get('discord'):
        # (Re)publier sur Discord une annonce déjà en ligne, avec la raison si ça échoue
        if not a['publiee']:
            return jsonify({'success': False, 'error': "L'annonce n'est pas encore publiée : elle partira sur Discord à l'heure prévue."}), 400
        w = db.execute("SELECT valeur FROM parametres WHERE cle = 'discord_webhook'").fetchone()
        if not w or not DISCORD_WEBHOOK_REGEX.match(w['valeur'] or ''):
            return jsonify({'success': False, 'error': "Aucun Discord relié : colle d'abord l'adresse du webhook (plus bas)."}), 400
        ok, raison = poster_discord(w['valeur'], message_annonce(a['titre'], a['texte'], a['lien']))
        db.execute('UPDATE annonces SET discord = 1, discord_ok = ? WHERE id = ?', (1 if ok else 0, annonce_id))
        journaliser(db, 'Annonce envoyée sur Discord' if ok else 'Annonce : envoi Discord raté', a['titre'])
        db.commit()
        return jsonify({'success': True, 'test': ok, 'raison': raison}), 200
    if data.get('terminer'):
        db.execute("UPDATE annonces SET fin_at = datetime('now') WHERE id = ?", (annonce_id,))
        journaliser(db, 'Annonce retirée de l\'accueil', a['titre'])
        db.commit()
        return jsonify({'success': True}), 200
    valeurs, erreur = lire_annonce(data)
    if erreur:
        return jsonify({'success': False, 'error': erreur}), 400
    if a['publiee']:
        valeurs['publier_at'] = a['publier_at']   # déjà publiée : on corrige le texte, pas la date de publication
    db.execute('UPDATE annonces SET titre = ?, texte = ?, lien = ?, publier_at = ?, fin_at = ?, notifier = ?, discord = ? WHERE id = ?',
               (valeurs['titre'], valeurs['texte'], valeurs['lien'], valeurs['publier_at'], valeurs['fin_at'],
                valeurs['notifier'], valeurs['discord'], annonce_id))
    journaliser(db, 'Annonce modifiée', valeurs['titre'])
    db.commit()
    publier_annonces_dues(db)
    return jsonify({'success': True}), 200


@app.route('/api/admin/discord', methods=['POST'])
@api_admin
def api_admin_discord():
    """Relier un salon Discord (adresse de webhook). Réservé aux fondateurs : l'adresse permet de poster."""
    db = get_db()
    if fondateur_requis(db):
        return jsonify({'success': False, 'error': 'Seuls les fondateurs peuvent relier le Discord'}), 403
    url = texte(donnees_json(), 'webhook', 300)
    if url and not DISCORD_WEBHOOK_REGEX.match(url):
        return jsonify({'success': False, 'error': 'Adresse invalide : elle doit commencer par https://discord.com/api/webhooks/'}), 400
    db.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES ('discord_webhook', ?)", (url,))
    journaliser(db, 'Discord relié' if url else 'Discord délié')
    db.commit()
    if url and donnees_json().get('tester'):
        ok, raison = poster_discord(url, '✅ **Test XHQ League**\nLe site est bien relié à ce salon.')
        return jsonify({'success': True, 'test': ok, 'raison': raison}), 200
    return jsonify({'success': True}), 200


@app.route('/api/admin/discord/relais', methods=['GET', 'POST'])
@api_admin
def api_admin_relais():
    """Relais Google (quand Discord bloque le serveur gratuit) : code du script et adresse. Fondateurs seulement."""
    db = get_db()
    if fondateur_requis(db):
        return jsonify({'success': False, 'error': 'Seuls les fondateurs peuvent régler le relais'}), 403
    secret = db.execute("SELECT valeur FROM parametres WHERE cle = 'relais_secret'").fetchone()
    if not secret:
        db.execute("INSERT INTO parametres (cle, valeur) VALUES ('relais_secret', ?)", (secrets.token_urlsafe(24),))
        db.commit()
        secret = db.execute("SELECT valeur FROM parametres WHERE cle = 'relais_secret'").fetchone()
    if request.method == 'GET':
        url = db.execute("SELECT valeur FROM parametres WHERE cle = 'relais_url'").fetchone()
        return jsonify({'code': code_relais(secret['valeur']), 'actif': bool(url and url['valeur'])}), 200
    url = texte(donnees_json(), 'url', 300)
    if url and not RELAIS_REGEX.match(url):
        return jsonify({'success': False, 'error': "Adresse invalide : elle doit ressembler à https://script.google.com/macros/s/…/exec"}), 400
    if url:
        db.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES ('relais_url', ?)", (url,))
    else:
        db.execute("DELETE FROM parametres WHERE cle = 'relais_url'")
    db.execute("DELETE FROM parametres WHERE cle IN ('discord_pause', 'discord_erreur')")
    db.execute('UPDATE discord_file SET essais = 0 WHERE envoye_at IS NULL')    # on retente tout avec le relais
    journaliser(db, 'Relais Google réglé' if url else 'Relais Google retiré')
    db.commit()
    w = db.execute("SELECT valeur FROM parametres WHERE cle = 'discord_webhook'").fetchone()
    if url and w and DISCORD_WEBHOOK_REGEX.match(w['valeur'] or ''):
        ok, raison = poster_discord(w['valeur'], '✅ **Test XHQ League**\nLe relais Google fonctionne : les messages du site arrivent ici.')
        return jsonify({'success': True, 'test': ok, 'raison': raison}), 200
    return jsonify({'success': True}), 200


@app.route('/api/admin/discord/tester', methods=['POST'])
@api_admin
def api_admin_discord_tester():
    """Renvoie un message de test sur le Discord déjà relié, et dit pourquoi si ça échoue."""
    db = get_db()
    r = db.execute("SELECT valeur FROM parametres WHERE cle = 'discord_webhook'").fetchone()
    if not r or not DISCORD_WEBHOOK_REGEX.match(r['valeur'] or ''):
        return jsonify({'success': False, 'error': "Aucun Discord relié : colle d'abord l'adresse du webhook."}), 400
    if limite_atteinte(f"test-discord:{session['user_id']}", 10, 10):
        return jsonify({'success': False, 'error': TROP}), 429
    noter_action(f"test-discord:{session['user_id']}")
    ok, raison = poster_discord(r['valeur'], '✅ **Test XHQ League**\nLe site est bien relié à ce salon.')
    return jsonify({'success': True, 'test': ok, 'raison': raison}), 200


# ---------- admin : boutique (badges créés depuis l'admin) ----------
STYLES_BADGE = ('violet', 'sombre', 'contour')
MODES_BADGE = ('libre', 'limite', 'reserve')


def lire_badge(data):
    nom = re.sub(r'\s+', ' ', texte(data, 'nom', 20))
    if not 2 <= len(nom) <= 20:
        return None, 'Le nom du badge doit faire entre 2 et 20 caractères'
    if not NOM_AUTORISE.match(nom):
        return None, "Nom : lettres, chiffres, espaces et . _ - ' ! ? & # seulement"
    mode = data.get('mode') if data.get('mode') in MODES_BADGE else 'libre'
    quantite = entier(data.get('quantite_max')) if mode == 'limite' else None
    if mode == 'limite' and (quantite is None or not 1 <= quantite <= 100000):
        return None, 'Indique combien d\'exemplaires (1 ou plus)'
    try:
        debut = paris_vers_utc(texte(data, 'debut_at', 16)) if texte(data, 'debut_at', 16) else None
        fin = paris_vers_utc(texte(data, 'fin_at', 16)) if texte(data, 'fin_at', 16) else None
    except ValueError:
        return None, 'Date invalide'
    if debut and fin and fin <= debut:
        return None, 'La fin doit être après le début'
    return {'nom': nom, 'description': texte(data, 'description', 200), 'mode': mode, 'quantite_max': quantite,
            'debut_at': debut, 'fin_at': fin, 'style': data.get('style') if data.get('style') in STYLES_BADGE else 'violet'}, None


@app.route('/api/admin/boutique', methods=['GET', 'POST'])
@api_admin
def api_admin_boutique():
    db = get_db()
    if request.method == 'POST':
        valeurs, erreur = lire_badge(donnees_json())
        if erreur:
            return jsonify({'success': False, 'error': erreur}), 400
        if db.execute('SELECT 1 FROM shop_items WHERE lower(nom) = lower(?)', (valeurs['nom'],)).fetchone():
            return jsonify({'success': False, 'error': 'Un badge porte déjà ce nom'}), 400
        cur = db.execute("""INSERT INTO shop_items (nom, description, prix, type, mode, quantite_max, debut_at, fin_at, style, perso, actif)
                            VALUES (?, ?, 0, 'badge', ?, ?, ?, ?, ?, 1, 1)""",
                         (valeurs['nom'], valeurs['description'], valeurs['mode'], valeurs['quantite_max'],
                          valeurs['debut_at'], valeurs['fin_at'], valeurs['style']))
        db.execute('UPDATE shop_items SET code = ? WHERE id = ?', (f'perso_{cur.lastrowid}', cur.lastrowid))
        journaliser(db, 'Badge créé', valeurs['nom'])
        db.commit()
        return jsonify({'success': True, 'id': cur.lastrowid}), 201
    lignes = []
    for r in db.execute("""SELECT id, code, nom, description, mode, quantite_max, debut_at, fin_at, style, COALESCE(actif, 1) AS actif,
                                  COALESCE(perso, 0) AS perso FROM shop_items WHERE code IS NOT NULL ORDER BY perso, id"""):
        b = dict(r)
        b['porteurs'] = porteurs(db, b['code'])
        b['debut_court'], b['fin_court'] = date_paris(b['debut_at'], True), date_paris(b['fin_at'], True)
        b['debut_fr'], b['fin_fr'] = date_paris(b['debut_at']), date_paris(b['fin_at'])
        lignes.append(b)
    return jsonify(lignes), 200


@app.route('/api/admin/boutique/<int:item_id>', methods=['POST'])
@api_admin
def api_admin_badge(item_id):
    db = get_db()
    b = db.execute('SELECT * FROM shop_items WHERE id = ?', (item_id,)).fetchone()
    if not b:
        return jsonify({'success': False, 'error': 'Badge introuvable'}), 404
    data = donnees_json()
    if data.get('donner'):
        # Donner le badge à un joueur (par son @identifiant ou son pseudo)
        cible = texte(data, 'donner', 40).lstrip('@').lower()
        u = db.execute('SELECT id, username FROM users WHERE handle = ? OR lower(username) = ?', (cible, cible)).fetchone()
        if not u:
            return jsonify({'success': False, 'error': 'Joueur introuvable (tape son @identifiant)'}), 404
        if b['code'] in ('fondateur', 'admin'):
            return jsonify({'success': False, 'error': 'Ce badge se donne avec les droits (onglet Joueurs)'}), 400
        if possede(db, u['id'], b['code']):
            return jsonify({'success': False, 'error': f"{u['username']} a déjà ce badge"}), 400
        donner_objet(db.cursor(), u['id'], b['code'], equiper=False)
        notifier(db, [u['id']], f"Nouveau badge : {b['nom']} (offert par l'orga)", '/profil')
        journaliser(db, f"Badge « {b['nom']} » donné", nom_joueur(db, u['id']))
        db.commit()
        return jsonify({'success': True, 'joueur': u['username']}), 200
    if not b['perso']:
        return jsonify({'success': False, 'error': 'Les badges de base (OG, Saison 1, Champion, Fondateur, Admin) ne se modifient pas'}), 400
    if data.get('supprimer'):
        db.execute('DELETE FROM inventory WHERE item_id = ?', (item_id,))
        db.execute("UPDATE users SET badge_equipe = NULL WHERE badge_equipe = ?", (b['code'],))
        db.execute('DELETE FROM shop_items WHERE id = ?', (item_id,))
        journaliser(db, 'Badge supprimé', b['nom'])
        db.commit()
        return jsonify({'success': True}), 200
    if 'actif' in data and len(data) == 1:
        db.execute('UPDATE shop_items SET actif = ? WHERE id = ?', (1 if data.get('actif') else 0, item_id))
        journaliser(db, 'Badge affiché dans la boutique' if data.get('actif') else 'Badge masqué de la boutique', b['nom'])
        db.commit()
        return jsonify({'success': True}), 200
    valeurs, erreur = lire_badge(data)
    if erreur:
        return jsonify({'success': False, 'error': erreur}), 400
    if db.execute('SELECT 1 FROM shop_items WHERE lower(nom) = lower(?) AND id != ?', (valeurs['nom'], item_id)).fetchone():
        return jsonify({'success': False, 'error': 'Un badge porte déjà ce nom'}), 400
    db.execute('UPDATE shop_items SET nom = ?, description = ?, mode = ?, quantite_max = ?, debut_at = ?, fin_at = ?, style = ? WHERE id = ?',
               (valeurs['nom'], valeurs['description'], valeurs['mode'], valeurs['quantite_max'], valeurs['debut_at'],
                valeurs['fin_at'], valeurs['style'], item_id))
    journaliser(db, 'Badge modifié', valeurs['nom'])
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/auto', methods=['GET'])
@api_admin
def api_admin_auto():
    db = get_db()
    w = db.execute("SELECT valeur FROM parametres WHERE cle = 'discord_webhook'").fetchone()
    jeux = []
    for j in db.execute('SELECT id, code, nom, format, COALESCE(max_membres, ?) AS max_membres FROM jeux ORDER BY ordre, id', (MAX_MEMBRES,)):
        j = dict(j)
        j['teams'] = db.execute('SELECT COUNT(*) FROM teams WHERE jeu_id = ?', (j['id'],)).fetchone()[0]
        j['de_base'] = j['code'] in [x[0] for x in JEUX_DEFAUT]
        jeux.append(j)
    recurrents = []
    maintenant = datetime.now(fuseau_paris()).replace(tzinfo=None)
    for r in db.execute('SELECT r.*, j.nom AS jeu_nom FROM tournois_recurrents r LEFT JOIN jeux j ON j.id = r.jeu_id ORDER BY r.id'):
        r = dict(r)
        prochaine = prochaine_date(r['jour'], r['heure'], maintenant)
        r['jour_nom'] = JOURS_SEMAINE[r['jour']]
        r['prochaine'] = date_fr(prochaine.strftime('%Y-%m-%dT%H:%M'))
        r['ouverture'] = date_fr((prochaine - timedelta(days=r['ouvrir_avant_jours'])).strftime('%Y-%m-%dT%H:%M'))
        recurrents.append(r)
    return jsonify({'discord_relie': bool(w and w['valeur']), 'auto': {k: auto_discord_actif(db, k) for k in AUTO_DISCORD},
                    'auto_noms': AUTO_DISCORD, 'en_attente': db.execute('SELECT COUNT(*) FROM discord_file WHERE envoye_at IS NULL AND essais < 5').fetchone()[0],
                    'abandonnes': db.execute('SELECT COUNT(*) FROM discord_file WHERE envoye_at IS NULL AND essais >= 5').fetchone()[0],
                    'envoyes': db.execute('SELECT COUNT(*) FROM discord_file WHERE envoye_at IS NOT NULL').fetchone()[0],
                    'erreur': (db.execute("SELECT valeur FROM parametres WHERE cle = 'discord_erreur'").fetchone() or [None])[0],
                    'jeux': jeux, 'recurrents': recurrents, 'places': TOURNOI_PLACES}), 200


@app.route('/api/admin/auto/discord', methods=['POST'])
@api_admin
def api_admin_auto_discord():
    data = donnees_json()
    db = get_db()
    for k in AUTO_DISCORD:
        if k in data:
            db.execute('INSERT OR REPLACE INTO parametres (cle, valeur) VALUES (?, ?)', (f'discord_auto_{k}', '1' if data[k] else '0'))
            journaliser(db, f"Discord automatique : {AUTO_DISCORD[k]} {'activé' if data[k] else 'désactivé'}")
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/jeux', methods=['POST'])
@api_admin
def api_admin_jeux():
    """Ajouter un jeu (Clash Royale 1v1, Valorant 5v5…) : il a ses propres teams, ligues, saisons et tournois."""
    db = get_db()
    data = donnees_json()
    nom = re.sub(r'\s+', ' ', texte(data, 'nom', 30))
    format_ = texte(data, 'format', 10)
    max_m = entier(data.get('max_membres'))
    if not 2 <= len(nom) <= 30 or not NOM_AUTORISE.match(nom):
        return jsonify({'success': False, 'error': 'Nom du jeu : 2 à 30 caractères (lettres, chiffres, espaces)'}), 400
    if not re.match(r'^\d{1,2}v\d{1,2}$', format_):
        return jsonify({'success': False, 'error': 'Format : par exemple 1v1, 3v3 ou 5v5'}), 400
    if max_m is None or not 1 <= max_m <= 20:
        return jsonify({'success': False, 'error': 'Joueurs maximum par team : entre 1 et 20'}), 400
    code = handle_depuis(nom).replace('.', '').replace('_', '')[:20] or 'jeu'
    if db.execute('SELECT 1 FROM jeux WHERE code = ? OR lower(nom) = lower(?)', (code, nom)).fetchone():
        return jsonify({'success': False, 'error': 'Ce jeu existe déjà'}), 400
    ordre = db.execute('SELECT COALESCE(MAX(ordre), 0) + 1 FROM jeux').fetchone()[0]
    db.execute('INSERT INTO jeux (code, nom, format, ordre, max_membres) VALUES (?, ?, ?, ?, ?)', (code, nom, format_, ordre, max_m))
    journaliser(db, 'Jeu ajouté', f'{nom} ({format_})')
    db.commit()
    return jsonify({'success': True, 'code': code}), 201


@app.route('/api/admin/jeux/<int:jeu_id>', methods=['POST'])
@api_admin
def api_admin_jeu(jeu_id):
    db = get_db()
    j = db.execute('SELECT * FROM jeux WHERE id = ?', (jeu_id,)).fetchone()
    if not j:
        return jsonify({'success': False, 'error': 'Jeu introuvable'}), 404
    data = donnees_json()
    if data.get('supprimer'):
        if j['code'] in [x[0] for x in JEUX_DEFAUT]:
            return jsonify({'success': False, 'error': 'Le jeu principal ne se supprime pas'}), 400
        if db.execute('SELECT 1 FROM teams WHERE jeu_id = ?', (jeu_id,)).fetchone() or db.execute('SELECT 1 FROM tournois WHERE jeu_id = ?', (jeu_id,)).fetchone():
            return jsonify({'success': False, 'error': 'Ce jeu a déjà des teams ou des tournois : on ne peut plus le supprimer'}), 400
        db.execute('DELETE FROM tournois_recurrents WHERE jeu_id = ?', (jeu_id,))
        db.execute('DELETE FROM jeux WHERE id = ?', (jeu_id,))
        journaliser(db, 'Jeu supprimé', j['nom'])
        db.commit()
        return jsonify({'success': True}), 200
    max_m = entier(data.get('max_membres'))
    if max_m is None or not 1 <= max_m <= 20:
        return jsonify({'success': False, 'error': 'Joueurs maximum par team : entre 1 et 20'}), 400
    db.execute('UPDATE jeux SET max_membres = ? WHERE id = ?', (max_m, jeu_id))
    journaliser(db, f'Jeu modifié : {max_m} joueurs max par team', j['nom'])
    db.commit()
    return jsonify({'success': True}), 200


def lire_recurrent(db, data):
    nom = texte(data, 'nom', 50)
    if len(nom) < 3:
        return None, 'Le nom doit faire au moins 3 caractères'
    jeu_id, jour, places, avant = entier(data.get('jeu_id')), entier(data.get('jour')), entier(data.get('places')), entier(data.get('ouvrir_avant_jours'))
    heure = texte(data, 'heure', 5)
    if not db.execute('SELECT 1 FROM jeux WHERE id = ?', (jeu_id,)).fetchone():
        return None, 'Choisis le jeu'
    if jour is None or not 0 <= jour <= 6 or not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', heure):
        return None, 'Choisis le jour et l\'heure'
    if places not in TOURNOI_PLACES:
        return None, 'Nombre de places : ' + ', '.join(map(str, TOURNOI_PLACES))
    if avant is None or not 1 <= avant <= 14:
        return None, 'Ouverture des inscriptions : entre 1 et 14 jours avant'
    return {'nom': nom, 'jeu_id': jeu_id, 'jour': jour, 'heure': heure, 'places': places, 'ouvrir_avant_jours': avant,
            'description': texte(data, 'description', 500), 'recompense': texte(data, 'recompense', RECOMPENSE_MAX)}, None


@app.route('/api/admin/recurrents', methods=['POST'])
@api_admin
def api_admin_recurrents():
    db = get_db()
    v, erreur = lire_recurrent(db, donnees_json())
    if erreur:
        return jsonify({'success': False, 'error': erreur}), 400
    db.execute("""INSERT INTO tournois_recurrents (jeu_id, nom, places, description, jour, heure, ouvrir_avant_jours, created_at, recompense)
                  VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
               (v['jeu_id'], v['nom'], v['places'], v['description'], v['jour'], v['heure'], v['ouvrir_avant_jours'], v['recompense']))
    journaliser(db, 'Tournoi récurrent programmé', f"{v['nom']} · chaque {JOURS_SEMAINE[v['jour']]} à {v['heure']}")
    db.commit()
    creer_tournois_recurrents(db)   # si les inscriptions doivent déjà être ouvertes, le 1er tournoi est créé tout de suite
    return jsonify({'success': True}), 201


@app.route('/api/admin/recurrents/<int:rec_id>', methods=['POST'])
@api_admin
def api_admin_recurrent(rec_id):
    db = get_db()
    r = db.execute('SELECT * FROM tournois_recurrents WHERE id = ?', (rec_id,)).fetchone()
    if not r:
        return jsonify({'success': False, 'error': 'Introuvable'}), 404
    data = donnees_json()
    if data.get('supprimer'):
        db.execute('DELETE FROM tournois_recurrents WHERE id = ?', (rec_id,))
        journaliser(db, 'Tournoi récurrent arrêté', r['nom'])
    else:
        db.execute('UPDATE tournois_recurrents SET actif = ? WHERE id = ?', (1 if data.get('actif') else 0, rec_id))
        journaliser(db, 'Tournoi récurrent ' + ('repris' if data.get('actif') else 'mis en pause'), r['nom'])
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/journal', methods=['GET'])
@api_admin
def api_admin_journal():
    db = get_db()
    rows = db.execute('SELECT admin_nom, action, cible, created_at FROM journal_admin ORDER BY id DESC LIMIT 200').fetchall()
    intact, ligne, total = verifier_journal(db)
    return jsonify({'lignes': [{**dict(r), 'quand': il_y_a(r['created_at'])} for r in rows],
                    'intact': intact, 'ligne_trafiquee': ligne, 'total': total}), 200


@app.route('/api/admin/users/<int:user_id>/deverrouiller', methods=['POST'])
@api_admin
def api_admin_deverrouiller(user_id):
    """Lève le blocage (7 jours ou définitif) d'un compte après trop d'essais de mot de passe ratés."""
    db = get_db()
    if est_protege(db, user_id) and user_id != session['user_id'] and fondateur_requis(db):
        return jsonify({'success': False, 'error': fondateur_requis(db)}), 403
    if not db.execute("""UPDATE users SET verrou_definitif = 0, verrou_niveau = 0, echecs_connexion = 0, bloque_jusqua = NULL
                         WHERE id = ?""", (user_id,)).rowcount:
        return jsonify({'success': False, 'error': 'Joueur introuvable'}), 404
    notifier(db, [user_id], 'Ton compte a été débloqué par un admin. Pense à choisir un mot de passe solide.', '/profil')
    journaliser(db, 'Compte déverrouillé', nom_joueur(db, user_id))
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/users/<int:user_id>/fondateur', methods=['POST'])
@api_admin
def api_admin_set_fondateur(user_id):
    """Donne ou retire le badge Fondateur (3 comptes maximum)."""
    valeur = donnees_json().get('fondateur')
    if not isinstance(valeur, bool):
        return jsonify({'success': False, 'error': 'Valeur invalide'}), 400
    db = get_db()
    c = db.cursor()
    if not c.execute('SELECT 1 FROM users WHERE id = ?', (user_id,)).fetchone():
        return jsonify({'success': False, 'error': 'Joueur introuvable'}), 404
    if fondateur_requis(db):
        return jsonify({'success': False, 'error': 'Seuls les fondateurs peuvent donner ou retirer ce badge'}), 403
    if not valeur and porteurs(db, 'fondateur') <= 1 and possede(db, user_id, 'fondateur'):
        return jsonify({'success': False, 'error': 'Il doit toujours rester au moins un fondateur'}), 400
    if valeur:
        if possede(db, user_id, 'fondateur'):
            return jsonify({'success': True}), 200
        if porteurs(db, 'fondateur') >= FONDATEURS_MAX:
            return jsonify({'success': False, 'error': f'Les {FONDATEURS_MAX} badges Fondateur sont déjà donnés. Retire-le à quelqu\'un d\'abord.'}), 400
        donner_objet(c, user_id, 'fondateur')
        journaliser(db, 'Badge Fondateur donné', nom_joueur(db, user_id))
        notifier(c, [user_id], 'Badge Fondateur débloqué : merci d\'avoir lancé la XHQ League !', '/shop')
    else:
        retirer_objet(c, user_id, 'fondateur')
        journaliser(db, 'Badge Fondateur retiré', nom_joueur(db, user_id))
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/messages', methods=['GET'])
@api_admin
def api_admin_messages():
    rows = get_db().execute("""
        SELECT m.id, m.nom, m.email, m.sujet, m.message, m.created_at, COALESCE(m.traite, 0) AS traite, m.user_id,
               (SELECT u.id FROM users u WHERE lower(u.email) = lower(m.email)) AS compte_id
        FROM contact_messages m
        ORDER BY COALESCE(m.traite, 0) ASC, m.id DESC LIMIT 200""").fetchall()
    messages = []
    for r in rows:
        m = dict(r)
        m['traite'] = bool(m['traite'])
        # Le message vient-il vraiment de ce compte (envoyé en étant connecté dessus) ? Sinon, n'importe qui
        # a pu taper cet email : ne jamais accorder de droits ou d'infos sur la seule foi d'un message.
        m['verifie'] = bool(m['compte_id'] and m['user_id'] == m['compte_id'])
        del m['user_id']
        messages.append(m)
    return jsonify(messages), 200


@app.route('/api/admin/messages/<int:message_id>/traite', methods=['POST'])
@api_admin
def api_admin_message_traite(message_id):
    valeur = donnees_json().get('traite', True)
    db = get_db()
    cur = db.execute('UPDATE contact_messages SET traite = ? WHERE id = ?', (1 if valeur else 0, message_id))
    if cur.rowcount == 0:
        return jsonify({'success': False, 'error': 'Message introuvable'}), 404
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/messages/<int:message_id>/delete', methods=['POST'])
@api_admin
def api_admin_message_delete(message_id):
    db = get_db()
    cur = db.execute('DELETE FROM contact_messages WHERE id = ?', (message_id,))
    if cur.rowcount == 0:
        return jsonify({'success': False, 'error': 'Message introuvable'}), 404
    db.commit()
    return jsonify({'success': True}), 200


# ---------------------------------------------------------------------------
# Fin de saison
# ---------------------------------------------------------------------------
def palmares(db, jeu_id, limite=20):
    rows = db.execute("""
        SELECT s.numero, s.cloturee_at, sc.team_nom AS champion
        FROM saisons s
        LEFT JOIN saison_classements sc ON sc.saison = s.numero AND sc.jeu_id = s.jeu_id AND sc.mouvement = 'champion'
        WHERE s.jeu_id = ?
        ORDER BY s.numero DESC LIMIT ?""", (jeu_id, limite)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Remplissage des ligues, liste d'attente et lancement automatique
# ---------------------------------------------------------------------------
def places_ligue(ligue_num):
    return LIGUES[ligue_num]['poules'] * TEAMS_PAR_POULE


def ligues_lancees(db, jeu_id, saison=None, avec_matchs=False):
    """Ligues lancées cette saison (calendrier publié) -> 1re date de match. avec_matchs : compte aussi
    les ligues où un match a été saisi à la main (pour la fin de saison)."""
    saison = saison or saison_actuelle(db, jeu_id)
    lancees = {r[0]: r[1] or '' for r in db.execute(
        'SELECT ligue_num, MIN(date) FROM rencontres WHERE jeu_id = ? AND saison = ? GROUP BY ligue_num', (jeu_id, saison))}
    if avec_matchs:
        for r in db.execute('SELECT DISTINCT ligue_num FROM matchs WHERE jeu_id = ? AND saison = ?', (jeu_id, saison)):
            lancees.setdefault(r[0], '')
    return lancees


def compte_par_poule(db, jeu_id, ligue_num):
    compte = {l: 0 for l in lettres_poules(ligue_num)}
    for r in db.execute("""SELECT l.poule, COUNT(*) FROM ligue l JOIN teams t ON t.id = l.team_id
                           WHERE t.jeu_id = ? AND l.ligue_num = ? GROUP BY l.poule""", (jeu_id, ligue_num)):
        if r[0] in compte:
            compte[r[0]] = r[1]
    return compte


def ligue_pleine(db, jeu_id, ligue_num):
    return all(n >= TEAMS_PAR_POULE for n in compte_par_poule(db, jeu_id, ligue_num).values())


def place_libre(c, jeu_id):
    """Plus haute ligue pas encore lancée qui a de la place : (ligue, poule), sinon None (tout est pris)."""
    lancees = ligues_lancees(c, jeu_id)
    for n in sorted(LIGUES):
        if n not in lancees and not ligue_pleine(c, jeu_id, n):
            return n, choisir_poule(c, jeu_id, n)
    return None


def verrou_ecriture(c):
    """Prend tout de suite le verrou d'écriture de la base (si ce n'est pas déjà fait) : ce qu'on lit
    ensuite ne peut plus changer sous nos pieds avant notre enregistrement."""
    if not c.connection.in_transaction:
        c.execute('BEGIN IMMEDIATE')


def inscrire_en_ligue(c, jeu_id, team_id):
    """Met la team dans la plus haute ligue libre (Ligue 1 d'abord), sinon en liste d'attente. Renvoie (ligue, poule) ou None."""
    verrou_ecriture(c)
    place = place_libre(c, jeu_id)
    if place:
        c.execute('INSERT INTO ligue (team_id, ligue_num, points, poule) VALUES (?, ?, 0, ?)', (team_id, place[0], place[1]))
        c.execute('UPDATE teams SET hors_classement = 0, attente_depuis = NULL WHERE id = ?', (team_id,))
    else:
        c.execute("UPDATE teams SET hors_classement = 0, attente_depuis = COALESCE(attente_depuis, datetime('now')) WHERE id = ?", (team_id,))
    return place


def joueurs_min(db, jeu_id):
    """Joueurs qu'il faut pour entrer en ligue : le réglage (3), sans dépasser le format du jeu (1 en 1v1)."""
    r = db.execute('SELECT format FROM jeux WHERE id = ?', (jeu_id,)).fetchone()
    m = re.match(r'\s*(\d+)\s*v', (r['format'] if r else '') or '')
    titulaires = int(m.group(1)) if m else 3
    return max(1, min(reglage('joueurs_min', db), titulaires, max_membres_jeu(db, jeu_id)))


def nb_membres(c, team_id):
    return c.execute('SELECT COUNT(*) FROM team_members WHERE team_id = ?', (team_id,)).fetchone()[0]


def entrer_en_ligue_si_complete(c, team_id):
    """Une team en formation qui atteint le nombre de joueurs voulu prend sa place (ou passe en liste d'attente)."""
    t = c.execute('SELECT jeu_id, en_formation FROM teams WHERE id = ?', (team_id,)).fetchone()
    if not t or not t['en_formation'] or nb_membres(c, team_id) < joueurs_min(c, t['jeu_id']):
        return None
    c.execute('UPDATE teams SET en_formation = 0 WHERE id = ?', (team_id,))
    place = inscrire_en_ligue(c, t['jeu_id'], team_id)
    notifier(c, membres_de(c, team_id), f"Ta team est complète : elle entre en Ligue {place[0]} (poule {place[1]}) !" if place
             else "Ta team est complète, mais toutes les ligues sont pleines : elle est en liste d'attente.", '/teams')
    return place or 'attente'


def verifier_effectif(c, team_id):
    """Plus assez de joueurs avant le début de sa ligue : la team repasse « en formation » et libère sa place
    (impossible de réserver une place à 3 puis de partir). Une team qui a déjà commencé à jouer la garde."""
    t = c.execute('SELECT jeu_id, en_formation, hors_classement FROM teams WHERE id = ?', (team_id,)).fetchone()
    if not t or t['en_formation'] or t['hors_classement'] or nb_membres(c, team_id) >= joueurs_min(c, t['jeu_id']):
        return
    l = c.execute('SELECT ligue_num FROM ligue WHERE team_id = ?', (team_id,)).fetchone()
    if l and (l['ligue_num'] in ligues_lancees(c, t['jeu_id']) or c.execute(
            'SELECT 1 FROM matchs WHERE saison = ? AND (team1_id = ? OR team2_id = ?)',
            (saison_actuelle(c, t['jeu_id']), team_id, team_id)).fetchone()):
        return
    c.execute('DELETE FROM ligue WHERE team_id = ?', (team_id,))
    c.execute('UPDATE teams SET en_formation = 1, attente_depuis = NULL WHERE id = ?', (team_id,))
    notifier(c, membres_de(c, team_id), f"Ta team n'a plus assez de joueurs ({joueurs_min(c, t['jeu_id'])} minimum) : "
             "elle repasse en formation et reprendra une place dès qu'elle sera complète.", '/teams')


def position_attente(db, team_id):
    r = db.execute('SELECT jeu_id, attente_depuis FROM teams WHERE id = ?', (team_id,)).fetchone()
    if not r or not r['attente_depuis']:
        return None
    return db.execute("""SELECT COUNT(*) FROM teams WHERE jeu_id = ? AND attente_depuis IS NOT NULL
                         AND (attente_depuis < ? OR (attente_depuis = ? AND id <= ?))""",
                      (r['jeu_id'], r['attente_depuis'], r['attente_depuis'], team_id)).fetchone()[0]


def placer_liste_attente(c, jeu_id, exclure=()):
    """Les teams en attente (les plus anciennes d'abord) prennent les places libres. Renvoie leurs noms.
    `exclure` : teams qui viennent de sortir à cette fin de saison (elles ne reprennent pas leur place)."""
    entrees = []
    if not c.execute('SELECT 1 FROM teams WHERE jeu_id = ? AND attente_depuis IS NOT NULL LIMIT 1', (jeu_id,)).fetchone():
        return entrees
    verrou_ecriture(c)
    for t in c.execute("""SELECT id, nom FROM teams WHERE jeu_id = ? AND attente_depuis IS NOT NULL
                          ORDER BY attente_depuis, id""", (jeu_id,)).fetchall():
        if t['id'] in exclure:
            continue
        place = place_libre(c, jeu_id)
        if not place:
            break
        inscrire_en_ligue(c, jeu_id, t['id'])
        notifier(c, membres_de(c, t['id']), f"Bonne nouvelle : ta team quitte la liste d'attente et entre en Ligue {place[0]} (poule {place[1]}) !", '/ligue')
        entrees.append(t['nom'])
    return entrees


def sortie_derniers_ligue_max(c, jeu_id, saison, classement):
    """Fin de saison : s'il y a des teams en attente, le dernier de chaque poule de la dernière ligue
    leur laisse sa place (le moins bon d'abord) et passe en liste d'attente."""
    attente = c.execute('SELECT COUNT(*) FROM teams WHERE jeu_id = ? AND attente_depuis IS NOT NULL', (jeu_id,)).fetchone()[0]
    if not attente:
        return []
    derniers = []
    for poule in lettres_poules(LIGUE_MAX):
        restants = [p for p in classement if p['ligue_num'] == LIGUE_MAX and p['poule'] == poule and p['destination'] == LIGUE_MAX]
        if restants:
            derniers.append(max(restants, key=lambda p: p['rang']))
    derniers.sort(key=lambda p: (p['points'], p['diff'], p['victoires']))
    sorties = []
    for p in derniers[:attente]:
        c.execute('DELETE FROM ligue WHERE team_id = ?', (p['team_id'],))
        c.execute("UPDATE teams SET attente_depuis = datetime('now') WHERE id = ?", (p['team_id'],))
        c.execute("UPDATE saison_classements SET mouvement = 'sortie' WHERE saison = ? AND team_id = ? AND jeu_id = ?", (saison, p['team_id'], jeu_id))
        notifier(c, membres_de(c, p['team_id']), f"Fin de saison : dernière de Ligue {LIGUE_MAX}, ta team laisse sa place et passe en liste d'attente.", '/teams')
        sorties.append((p['team_id'], p['team_name']))
    return sorties


def maintenant_paris():
    return datetime.now(fuseau_paris()).replace(tzinfo=None, microsecond=0)


def prochain(apres, jour, heure):
    """Premier « jour de la semaine à heure:00 » (heure de France) à partir de `apres` (inclus)."""
    d = apres.replace(hour=heure, minute=0, second=0, microsecond=0) + timedelta(days=(jour - apres.weekday()) % 7)
    return d if d >= apres else d + timedelta(days=7)


def debut_saison_paris(db, jeu_id):
    """Heure (France) à laquelle la saison actuelle a commencé = clôture de la précédente, sinon None."""
    r = db.execute('SELECT MAX(cloturee_at) FROM saisons WHERE jeu_id = ?', (jeu_id,)).fetchone()[0]
    return datetime.strptime(date_paris(r, True), '%Y-%m-%dT%H:%M') if r else None


def programme_ligue(db, jeu_id, saison, ligue_num):
    """(calendrier, coup d'envoi) prévus pour une ligue pleine pas encore lancée, en heure de France."""
    if not ligue_pleine(db, jeu_id, ligue_num):
        return None, None
    r = db.execute('SELECT valeur FROM parametres WHERE cle = ?', (f'pleine_{jeu_id}_{saison}_{ligue_num}',)).fetchone()
    if r:
        pleine = datetime.strptime(r['valeur'], '%Y-%m-%d %H:%M:%S')
    else:
        pleine = maintenant_paris()      # vient d'être remplie : la tâche automatique la notera à la prochaine visite
    debut_saison = debut_saison_paris(db, jeu_id)
    if debut_saison and pleine <= debut_saison + timedelta(hours=2):
        calendrier = pleine        # pleine dès la fin de la saison d'avant : on repart tous ensemble tout de suite
    else:
        calendrier = prochain(pleine + timedelta(hours=DELAI_LIGUE_PLEINE_H), JOUR_CALENDRIER, HEURE_CALENDRIER)
    return calendrier, prochain(calendrier + timedelta(minutes=1), JOUR_DEBUT, HEURE_DEBUT)


def lancer_ligue(c, jeu, saison, ligue_num, coup_envoi):
    """Publie le calendrier d'une ligue : 7 journées, une tous les 3 jours à partir du coup d'envoi."""
    crees = 0
    for poule in lettres_poules(ligue_num):
        teams = [r[0] for r in c.execute("""SELECT l.team_id FROM ligue l JOIN teams t ON t.id = l.team_id
                                             WHERE t.jeu_id = ? AND l.ligue_num = ? AND l.poule = ? ORDER BY l.id""",
                                          (jeu['id'], ligue_num, poule)).fetchall()]
        for n, tour in enumerate(journees(teams)):
            date = (coup_envoi + timedelta(days=JOURS_ENTRE_JOURNEES * n)).strftime('%Y-%m-%dT%H:%M')
            for a, b in tour:
                crees += c.execute("""INSERT OR IGNORE INTO rencontres (jeu_id, saison, ligue_num, poule, journee, team1_id, team2_id, date, created_at)
                                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                                   (jeu['id'], saison, ligue_num, poule, n + 1, a, b, date)).rowcount
    quand = date_fr(coup_envoi.strftime('%Y-%m-%dT%H:%M'))
    joueurs = [r[0] for r in c.execute("""SELECT tm.user_id FROM team_members tm JOIN ligue l ON l.team_id = tm.team_id
                                          WHERE tm.jeu_id = ? AND l.ligue_num = ?""", (jeu['id'], ligue_num)).fetchall()]
    notifier(c, joueurs, f"📅 Calendrier de la Ligue {ligue_num} publié ! Premier match {quand}.", '/calendrier?team=moi')
    enfiler_discord(c, 'saison', f"📅 **Ligue {ligue_num}** ({jeu['nom']}) : le calendrier est publié, coup d'envoi {quand} !")
    ecrire_journal(c, None, 'automatique', f'Ligue {ligue_num} lancée ({crees} matchs)', jeu['nom'])
    return crees


def lancement_auto(db):
    """À chaque visite (au plus toutes les 30 s) : liste d'attente -> places libres, ligues pleines -> calendrier
    le dimanche 15h et début le lundi 9h, et quand toutes les ligues tournent, fin de saison commune programmée."""
    faites = 0
    maintenant = maintenant_paris()
    for jeu in [dict(j) for j in db.execute('SELECT id, code, nom FROM jeux').fetchall()]:
        c = db.cursor()
        saison = saison_actuelle(db, jeu['id'])
        for t in c.execute('SELECT id FROM teams WHERE jeu_id = ? AND en_formation = 1', (jeu['id'],)).fetchall():
            entrer_en_ligue_si_complete(c, t['id'])     # (si le minimum de joueurs a été baissé dans les réglages)
        placer_liste_attente(c, jeu['id'])
        lancees = ligues_lancees(db, jeu['id'], saison)
        for n in LIGUES:
            cle = f"pleine_{jeu['id']}_{saison}_{n}"
            if n in lancees:
                c.execute('DELETE FROM parametres WHERE cle = ?', (cle,))
                continue
            if not ligue_pleine(db, jeu['id'], n):
                # On garde l'heure où elle a été pleine la 1re fois : une team qui part et revient
                # (ou un capitaine qui supprime puis recrée sa team) ne peut pas repousser le lancement sans fin
                continue
            c.execute('INSERT OR IGNORE INTO parametres (cle, valeur) VALUES (?, ?)', (cle, maintenant.strftime('%Y-%m-%d %H:%M:%S')))
            calendrier, coup_envoi = programme_ligue(db, jeu['id'], saison, n)
            # « On prend » le lancement d'abord : deux visites en même temps ne lancent jamais deux fois
            if calendrier and maintenant >= calendrier and c.execute(
                    'INSERT OR IGNORE INTO parametres (cle, valeur) VALUES (?, ?)', (f"lancee_{jeu['id']}_{saison}_{n}", 'oui')).rowcount:
                lancer_ligue(c, jeu, saison, n, coup_envoi)
                faites += 1
        db.commit()
        # Toutes les ligues ont leur calendrier : fin de saison commune le dimanche 15h après la dernière journée
        lancees = ligues_lancees(db, jeu['id'], saison)
        cle_fin = f"fin_planifiee_{jeu['id']}_{saison}"
        if all(n in lancees for n in LIGUES) and not db.execute('SELECT 1 FROM parametres WHERE cle = ?', (cle_fin,)).fetchone():
            derniere = db.execute('SELECT MAX(date) FROM rencontres WHERE jeu_id = ? AND saison = ?', (jeu['id'], saison)).fetchone()[0]
            c.execute('INSERT INTO parametres (cle, valeur) VALUES (?, ?)', (cle_fin, 'oui'))
            if derniere:
                fin = prochain(datetime.strptime(derniere, '%Y-%m-%dT%H:%M') + timedelta(days=1), JOUR_CALENDRIER, HEURE_CALENDRIER)
                if c.execute('INSERT OR IGNORE INTO parametres (cle, valeur) VALUES (?, ?)',
                             (f"cloture_auto_{jeu['id']}", paris_vers_utc(fin.strftime('%Y-%m-%dT%H:%M')))).rowcount:
                    ecrire_journal(c, None, 'automatique', f"Fin de saison {saison} programmée : {date_fr(fin.strftime('%Y-%m-%dT%H:%M'))}", jeu['nom'])
            db.commit()
    return faites


def etat_lancement(db, jeu_id):
    """Où en est chaque ligue (pour l'accueil, les classements et l'admin)."""
    saison = saison_actuelle(db, jeu_id)
    lancees = ligues_lancees(db, jeu_id, saison)
    ligues = {}
    for n in LIGUES:
        inscrites = sum(compte_par_poule(db, jeu_id, n).values())
        etat = {'ligue_num': n, 'places': places_ligue(n), 'inscrites': inscrites}
        if n in lancees:
            premier = lancees[n]
            etat['statut'] = 'en_cours' if not premier or premier <= maintenant_paris().strftime('%Y-%m-%dT%H:%M') else 'programmee'
            etat['debut'] = date_fr(premier) if premier else None
        else:
            calendrier, coup_envoi = programme_ligue(db, jeu_id, saison, n)
            etat['statut'] = 'pleine' if calendrier else 'remplissage'
            if calendrier:
                etat['calendrier'] = date_fr(calendrier.strftime('%Y-%m-%dT%H:%M'))
                etat['debut'] = date_fr(coup_envoi.strftime('%Y-%m-%dT%H:%M'))
        ligues[n] = etat
    fin = db.execute('SELECT valeur FROM parametres WHERE cle = ?', (f'cloture_auto_{jeu_id}',)).fetchone()
    return {'ligues': ligues, 'attente': db.execute('SELECT COUNT(*) FROM teams WHERE jeu_id = ? AND attente_depuis IS NOT NULL',
                                                    (jeu_id,)).fetchone()[0],
            'fin_saison': date_paris(fin['valeur']) if fin else None}


@app.route('/api/admin/lancement', methods=['GET'])
@api_admin
def api_admin_lancement():
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    attente = [{'id': r['id'], 'nom': r['nom'], 'depuis': date_paris(r['attente_depuis'])}
               for r in db.execute("""SELECT id, nom, attente_depuis FROM teams WHERE jeu_id = ? AND attente_depuis IS NOT NULL
                                      ORDER BY attente_depuis, id""", (jeu['id'],))]
    return jsonify({**etat_lancement(db, jeu['id']), 'liste_attente': attente, 'saison': saison_actuelle(db, jeu['id'])}), 200


def calculer_fin_saison(db, jeu_id):
    """Classement final de chaque poule + destination de chaque team (sans rien modifier)."""
    resultats = []
    # Ligues vraiment commencées : premier match déjà passé (ou match saisi à la main). Une ligue dont le
    # calendrier vient de sortir n'a encore rien pu jouer : ses teams ne sont ni retirées ni pénalisées.
    maintenant = maintenant_paris().strftime('%Y-%m-%dT%H:%M')
    lancees = {n for n, d in ligues_lancees(db, jeu_id).items() if d <= maintenant}
    lancees |= {r[0] for r in db.execute('SELECT DISTINCT ligue_num FROM matchs WHERE jeu_id = ? AND saison = ?',
                                         (jeu_id, saison_actuelle(db, jeu_id)))}
    for ligue_num in LIGUES:
        for poule in lettres_poules(ligue_num):
            for place in classement_poule(db, jeu_id, ligue_num, poule, penalites=ligue_num in lancees):
                if not place['team_id']:
                    continue
                destination, mouvement = ligue_num, place['statut'] or 'reste'
                if ligue_num not in lancees:
                    # Ligue pas encore lancée (pas pleine) : ses teams gardent leur place, sans pénalité
                    destination, mouvement = ligue_num, 'reste'
                elif place['joues'] == 0:
                    # Inactive (aucun match de toute la saison) : retirée du classement, quelle que
                    # soit sa ligue. Sa place est libérée pour le repêchage.
                    destination, mouvement = None, 'retiree'
                elif mouvement == 'monte' and ligue_num > 1:
                    destination = ligue_num - 1
                elif mouvement == 'descend' and ligue_num < max(LIGUES):
                    destination = ligue_num + 1
                elif mouvement not in ('champion',):
                    mouvement = 'reste'
                resultats.append({**place, 'ligue_num': ligue_num, 'poule': poule,
                                  'mouvement': mouvement, 'destination': destination})

    # Repêchage : les places libres d'une ligue (inactives parties, XXXXX) sont données aux
    # meilleures teams ACTIVES de la ligue juste en dessous. On remplit de haut en bas,
    # et une team ne monte jamais de plus d'une ligue par saison.
    for ligue_num in sorted(LIGUES)[:-1]:
        places = LIGUES[ligue_num]['poules'] * TEAMS_PAR_POULE
        libres = places - sum(r['destination'] == ligue_num for r in resultats)
        if libres <= 0:
            continue
        candidates = [r for r in resultats
                      if r['ligue_num'] == ligue_num + 1 and r['destination'] == ligue_num + 1
                      and r['joues'] >= MATCHS_MIN_POUR_MONTER]
        candidates.sort(key=lambda r: (r['rang'], -r['points'], -r['diff'], -r['victoires'], r['poule']))
        for r in candidates[:libres]:
            r['destination'], r['mouvement'] = ligue_num, 'repechage'
    return resultats


@app.route('/api/admin/saison', methods=['GET'])
@api_admin
def api_admin_saison():
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    saison = saison_actuelle(db, jeu['id'])
    classement = calculer_fin_saison(db, jeu['id'])
    mouvements = [m for m in classement if m['mouvement'] != 'reste' or m['penalite'] > 0]
    mouvements.sort(key=lambda m: (m['ligue_num'], m['poule'], m['rang']))
    matchs_joues = db.execute('SELECT COUNT(*) FROM matchs WHERE jeu_id = ? AND saison = ?',
                              (jeu['id'], saison)).fetchone()[0]
    prog = db.execute('SELECT valeur FROM parametres WHERE cle = ?', (f"cloture_auto_{jeu['id']}",)).fetchone()
    return jsonify({
        'jeu': jeu,
        'saison': saison,
        'cloture_programmee': date_paris(prog['valeur']) if prog else None,
        'cloture_programmee_court': date_paris(prog['valeur'], True) if prog else None,
        'penalite_par_match': reglage('penalite_par_match'), 'penalite_max': reglage('penalite_max'),
        'matchs_joues': matchs_joues,
        'teams': len(classement),
        'mouvements': mouvements,
        'nb_montees': sum(m['mouvement'] in ('monte', 'repechage') for m in mouvements),
        'nb_descentes': sum(m['mouvement'] == 'descend' for m in mouvements),
        'nb_repechages': sum(m['mouvement'] == 'repechage' for m in mouvements),
        'nb_retirees': sum(m['mouvement'] == 'retiree' for m in mouvements),
        'nb_penalisees': sum(m['penalite'] > 0 for m in mouvements),
        'historique': palmares(db, jeu['id']),
    }), 200


@app.route('/api/admin/saison/cloturer', methods=['POST'])
@api_admin
def api_admin_cloturer_saison():
    if donnees_json().get('confirmation') != 'CLOTURER':
        return jsonify({'success': False, 'error': 'Tapez CLOTURER pour confirmer'}), 400
    db = get_db()
    if fondateur_requis(db):
        return jsonify({'success': False, 'error': 'Seuls les fondateurs peuvent clôturer une saison'}), 403
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    journaliser(db, f"Saison {saison_actuelle(db, jeu['id'])} clôturée", jeu['nom'])
    resultat, message = cloturer_saison(db, jeu, session['user_id'])
    if message:
        db.rollback()
        return jsonify({'success': False, 'error': message}), 400
    return jsonify({'success': True, 'jeu': jeu, **resultat}), 200


def cloturer_saison(db, jeu, par_user_id):
    """Clôture la saison en cours de ce jeu (classement archivé, montées / descentes, points à 0).
    Renvoie (résumé, None) ou (None, message d'erreur). Fait le commit en cas de succès."""
    c = db.cursor()
    saison = saison_actuelle(db, jeu['id'])
    if not c.execute('SELECT 1 FROM matchs WHERE jeu_id = ? AND saison = ?', (jeu['id'], saison)).fetchone():
        return None, 'Aucun match joué cette saison : rien à clôturer'
    classement = calculer_fin_saison(db, jeu['id'])
    c.execute("""UPDATE declarations SET statut = 'annule', motif = 'Saison terminée', traite_at = datetime('now')
                 WHERE jeu_id = ? AND statut IN ('attente', 'conteste')""", (jeu['id'],))
    try:
        c.execute("INSERT INTO saisons (jeu_id, numero, cloturee_at, cloturee_par) VALUES (?, ?, datetime('now'), ?)",
                  (jeu['id'], saison, par_user_id))
        for p in classement:
            c.execute("""INSERT INTO saison_classements (saison, team_id, team_nom, ligue_num, poule, rang, points,
                                                         joues, victoires, defaites, diff, mouvement, destination,
                                                         penalite, matchs_manques, points_avant, recidive, jeu_id)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (saison, p['team_id'], p['team_name'], p['ligue_num'], p['poule'], p['rang'], p['points'],
                       p['joues'], p['victoires'], p['defaites'], p['diff'], p['mouvement'], p['destination'],
                       p['penalite'], p['matchs_manques'], p['points_avant'], 1 if p['recidive'] else 0, jeu['id']))

        # 1) on sort toutes les teams qui changent de ligue de leur poule
        # 0) les teams inactives sortent du classement (elles pourront réintégrer la Ligue 10)
        retirees = [p for p in classement if p['mouvement'] == 'retiree']
        for p in retirees:
            c.execute('DELETE FROM ligue WHERE team_id = ?', (p['team_id'],))
            c.execute('UPDATE teams SET hors_classement = 1 WHERE id = ?', (p['team_id'],))
        # 1) on sort de leur poule toutes les teams qui changent de ligue
        partants = [p for p in classement if p['destination'] is not None and p['destination'] != p['ligue_num']]
        for p in partants:
            c.execute('UPDATE ligue SET ligue_num = ?, poule = NULL WHERE team_id = ?', (p['destination'], p['team_id']))
        # 2) on les place dans les places libérées de leur nouvelle ligue (les mieux classées d'abord)
        for p in sorted(partants, key=lambda p: (p['destination'], p['rang'], p['poule'])):
            c.execute('UPDATE ligue SET poule = ? WHERE team_id = ?',
                      (choisir_poule(c, jeu['id'], p['destination']), p['team_id']))
        # 3) liste d'attente : les derniers de chaque poule de Ligue 5 laissent leur place aux teams qui attendent
        sorties = sortie_derniers_ligue_max(c, jeu['id'], saison, classement)
        # 4) nouvelle saison : toutes les teams de ce jeu repartent à 0
        c.execute('UPDATE ligue SET points = 0 WHERE team_id IN (SELECT id FROM teams WHERE jeu_id = ?)', (jeu['id'],))
        entrees = placer_liste_attente(c, jeu['id'], exclure={tid for tid, _ in sorties})
        sorties = [nom for _, nom in sorties]
        champion = next((p['team_name'] for p in classement if p['mouvement'] == 'champion'), None)
        montees = [p['team_name'] for p in classement if p['mouvement'] in ('monte', 'repechage')]
        enfiler_discord(c, 'saison', f"🏁 **Fin de la saison {saison}** ({jeu['nom']})" + (f"\n👑 Champion de Ligue 1 : **{champion}**" if champion else '')
                        + (f"\n⬆️ Montent : {', '.join(montees[:15])}" if montees else '') + f"\nLa saison {saison + 1} commence maintenant !")
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return None, 'Cette saison a déjà été clôturée'
    c.execute("DELETE FROM parametres WHERE cle = ?", (f"cloture_auto_{jeu['id']}",))
    db.commit()
    return {
        'saison_cloturee': saison,
        'nouvelle_saison': saison + 1,
        'montees': sum(p['mouvement'] in ('monte', 'repechage') for p in partants),
        'descentes': sum(p['mouvement'] == 'descend' for p in partants),
        'repechages': sum(p['mouvement'] == 'repechage' for p in partants),
        'retirees': len(retirees),
        'sorties': sorties, 'entrees': entrees,
        'penalisees': sum(p['penalite'] > 0 for p in classement),
        'champion': next((p['team_name'] for p in classement if p['mouvement'] == 'champion'), None),
    }, None


def clotures_programmees(db):
    """Clôture toute seule la saison d'un jeu quand l'heure programmée (Admin → Saison) est arrivée."""
    faites = 0
    for r in db.execute("SELECT cle, valeur FROM parametres WHERE cle LIKE 'cloture_auto_%' AND valeur <= datetime('now')").fetchall():
        jeu_id = entier(r['cle'].rsplit('_', 1)[1])
        jeu = db.execute('SELECT id, code, nom FROM jeux WHERE id = ?', (jeu_id,)).fetchone()
        # On « prend » la clôture d'abord : deux visites simultanées ne clôturent jamais deux fois
        if not db.execute('DELETE FROM parametres WHERE cle = ? AND valeur = ?', (r['cle'], r['valeur'])).rowcount or not jeu:
            db.commit()
            continue
        db.commit()
        saison = saison_actuelle(db, jeu['id'])
        resultat, message = cloturer_saison(db, dict(jeu), None)
        admins = [x[0] for x in db.execute('SELECT id FROM users WHERE is_admin = 1').fetchall()]
        if message:
            notifier(db, admins, f"Clôture automatique de la saison {saison} ({jeu['nom']}) impossible : {message}", '/admin#saison')
        else:
            ecrire_journal(db.cursor(), None, 'automatique', f'Saison {saison} clôturée', jeu['nom'])
            notifier(db, admins, f"Saison {saison} ({jeu['nom']}) clôturée automatiquement", '/admin#saison')
            joueurs = [x[0] for x in db.execute('SELECT user_id FROM team_members WHERE jeu_id = ?', (jeu['id'],)).fetchall()]
            notifier(db, joueurs, f"Fin de la saison {saison} : la saison {saison + 1} commence ! Regarde où joue ta team.", '/ligue')
            faites += 1
        db.commit()
    return faites


@app.route('/api/admin/saison/programmer', methods=['POST'])
@api_admin
def api_admin_programmer_cloture():
    db = get_db()
    if fondateur_requis(db):
        return jsonify({'success': False, 'error': 'Seuls les fondateurs peuvent programmer la fin de saison'}), 403
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    quand = texte(donnees_json(), 'quand', 16)
    cle = f"cloture_auto_{jeu['id']}"
    if not quand:
        db.execute('DELETE FROM parametres WHERE cle = ?', (cle,))
        journaliser(db, 'Fin de saison programmée annulée', jeu['nom'])
        db.commit()
        return jsonify({'success': True}), 200
    try:
        utc = paris_vers_utc(quand)
    except ValueError:
        return jsonify({'success': False, 'error': 'Date invalide'}), 400
    if utc <= datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'):
        return jsonify({'success': False, 'error': 'Choisis une date dans le futur'}), 400
    db.execute('INSERT OR REPLACE INTO parametres (cle, valeur) VALUES (?, ?)', (cle, utc))
    journaliser(db, f'Fin de saison programmée : {date_paris(utc)}', jeu['nom'])
    db.commit()
    return jsonify({'success': True, 'quand_fr': date_paris(utc)}), 200


# ---------------------------------------------------------------------------
# Tournois : inscriptions -> poules -> bracket (élimination directe)
# ---------------------------------------------------------------------------
TOURNOI_PLACES = (8, 16, 32)         # tailles proposées à la création
TOURNOI_TAILLE_POULE = 4             # nombre visé de teams par poule
TOURNOI_MIN_TEAMS = 4                # minimum pour lancer les poules
STATUTS_TOURNOI = {
    'inscriptions': 'Inscriptions ouvertes',
    'poules': 'Phase de poules',
    'bracket': 'Phase finale',
    'termine': 'Terminé',
}


def ordre_tetes_de_serie(taille):
    """Placement classique d'un bracket : 1 contre le dernier, 2 contre l'avant-dernier... (taille = puissance de 2)."""
    ordre = [1]
    while len(ordre) < taille:
        m = len(ordre) * 2
        ordre = [x for s in ordre for x in (s, m + 1 - s)]
    return ordre


def journees_poule(teams):
    """Toutes les rencontres d'une poule, rangées par journée (méthode du tourniquet)."""
    liste = list(teams) + ([None] if len(teams) % 2 else [])
    n = len(liste)
    matchs = []
    for _ in range(n - 1):
        for i in range(n // 2):
            a, b = liste[i], liste[n - 1 - i]
            if a is not None and b is not None:
                matchs.append((a, b))
        liste = [liste[0], liste[-1]] + liste[1:-1]
    return matchs


def nom_tour(tour, nb_tours):
    reste = nb_tours - tour
    return {0: 'Finale', 1: 'Demi-finales', 2: 'Quarts de finale', 3: 'Huitièmes de finale'}.get(
        reste, f'1/{2 ** reste}e de finale')


def noms_teams_tournoi(db, tournoi_id):
    """Nom affiché de chaque team : nom actuel, sinon celui gardé à l'inscription (team supprimée depuis)."""
    return {r['team_id']: r['nom'] for r in db.execute(
        """SELECT i.team_id, COALESCE(t.nom, i.team_nom) AS nom FROM tournoi_inscriptions i
           LEFT JOIN teams t ON t.id = i.team_id WHERE i.tournoi_id = ?""", (tournoi_id,))}


def classement_poule_tournoi(db, tournoi_id, poule, noms):
    lignes = {r['team_id']: {'team_id': r['team_id'], 'nom': noms.get(r['team_id'], '?'), 'joues': 0, 'victoires': 0,
                             'defaites': 0, 'pour': 0, 'contre': 0, 'points': 0}
              for r in db.execute('SELECT team_id FROM tournoi_inscriptions WHERE tournoi_id = ? AND poule = ?',
                                  (tournoi_id, poule))}
    for m in db.execute("""SELECT * FROM tournoi_matchs WHERE tournoi_id = ? AND phase = 'poule' AND poule = ?
                           AND vainqueur_id IS NOT NULL""", (tournoi_id, poule)):
        for moi, adv, s_moi, s_adv in ((m['team1_id'], m['team2_id'], m['score1'], m['score2']),
                                       (m['team2_id'], m['team1_id'], m['score2'], m['score1'])):
            if moi not in lignes:
                continue
            l = lignes[moi]
            l['joues'] += 1
            l['pour'] += s_moi
            l['contre'] += s_adv
            if m['vainqueur_id'] == moi:
                l['victoires'] += 1
                l['points'] += reglage('points_victoire', db)
            else:
                l['defaites'] += 1
                l['points'] += reglage('points_defaite', db)
    classement = sorted(lignes.values(), key=lambda l: (-l['points'], -(l['pour'] - l['contre']), -l['pour'],
                                                        l['nom'].lower()))
    for i, l in enumerate(classement, 1):
        l['rang'] = i
        l['diff'] = l['pour'] - l['contre']
    return classement


def qualifies_par_poule(nb_poules):
    # Une seule poule : les 4 premiers vont en demi-finales. Sinon les 2 premiers de chaque poule.
    return 4 if nb_poules == 1 else 2


def porteurs(db, code):
    return db.execute("""SELECT COUNT(*) FROM inventory i JOIN shop_items s ON s.id = i.item_id
                         WHERE s.code = ?""", (code,)).fetchone()[0]


def donner_objet(c, user_id, code, equiper=True):
    """Ajoute un objet à l'inventaire (s'il n'y est pas) et l'équipe si demandé."""
    item = c.execute('SELECT id, type FROM shop_items WHERE code = ?', (code,)).fetchone()
    if not item:
        return
    if not c.execute('SELECT 1 FROM inventory WHERE user_id = ? AND item_id = ?', (user_id, item['id'])).fetchone():
        c.execute("INSERT OR IGNORE INTO inventory (user_id, item_id, bought_at) VALUES (?, ?, datetime('now'))", (user_id, item['id']))
    if equiper and item['type'] in ('badge', 'cadre'):
        c.execute(f"UPDATE users SET {item['type']}_equipe = ? WHERE id = ?", (code, user_id))


def retirer_objet(c, user_id, code):
    item = c.execute('SELECT id, type FROM shop_items WHERE code = ?', (code,)).fetchone()
    if not item:
        return
    c.execute('DELETE FROM inventory WHERE user_id = ? AND item_id = ?', (user_id, item['id']))
    if item['type'] in ('badge', 'cadre'):
        c.execute(f"UPDATE users SET {item['type']}_equipe = NULL WHERE id = ? AND {item['type']}_equipe = ?", (user_id, code))


def donner_badge_champion(c, team_id, depuis=None):
    """Badge Champion pour les joueurs de la team gagnante présents AVANT le début du tournoi."""
    item = c.execute("SELECT id FROM shop_items WHERE code = 'champion_tournoi'").fetchone()
    if not item:
        return
    for m in c.execute('SELECT user_id FROM team_members WHERE team_id = ? AND (? IS NULL OR joined_at <= ?)',
                       (team_id, depuis, depuis)).fetchall():
        deja = c.execute('SELECT 1 FROM inventory WHERE user_id = ? AND item_id = ?', (m['user_id'], item['id'])).fetchone()
        if not deja:
            c.execute("INSERT OR IGNORE INTO inventory (user_id, item_id, bought_at) VALUES (?, ?, datetime('now'))",
                      (m['user_id'], item['id']))
            c.execute("UPDATE users SET badge_equipe = 'champion_tournoi' WHERE id = ?", (m['user_id'],))


def faire_avancer(c, match):
    """Envoie le vainqueur d'un match de bracket dans le match suivant (ou le sacre champion)."""
    t = c.execute('SELECT id, lance_at FROM tournois WHERE id = ?', (match['tournoi_id'],)).fetchone()
    suivant = c.execute("""SELECT * FROM tournoi_matchs WHERE tournoi_id = ? AND phase = 'bracket'
                           AND tour = ? AND position = ?""",
                        (match['tournoi_id'], match['tour'] + 1, match['position'] // 2)).fetchone()
    if suivant:
        colonne = 'team1_id' if match['position'] % 2 == 0 else 'team2_id'
        c.execute(f'UPDATE tournoi_matchs SET {colonne} = ? WHERE id = ?', (match['vainqueur_id'], suivant['id']))
        return
    nom = c.execute("""SELECT COALESCE(t.nom, i.team_nom) FROM tournoi_inscriptions i LEFT JOIN teams t ON t.id = i.team_id
                       WHERE i.tournoi_id = ? AND i.team_id = ?""", (t['id'], match['vainqueur_id'])).fetchone()[0]
    c.execute("""UPDATE tournois SET statut = 'termine', champion_team_id = ?, champion_nom = ?,
                 termine_at = datetime('now') WHERE id = ?""", (match['vainqueur_id'], nom, t['id']))
    donner_badge_champion(c, match['vainqueur_id'], t['lance_at'])
    titre_tournoi, recompense = c.execute('SELECT nom, recompense FROM tournois WHERE id = ?', (t['id'],)).fetchone()
    enfiler_discord(c, 'tournois', f"🏆 **{nom}** remporte **{titre_tournoi}** ! Bravo à toute l'équipe."
                    + (f"\n🎁 Récompense : {recompense}" if recompense else ''))
    notifier(c, membres_de(c, match['vainqueur_id']), f"Ta team remporte {titre_tournoi} ! Badge Champion débloqué",
             f"/tournois/{t['id']}")
    if recompense:
        capitaine = c.execute('SELECT createur_id FROM teams WHERE id = ?', (match['vainqueur_id'],)).fetchone()
        if capitaine:
            notifier(c, [capitaine[0]], f"🎁 Récompense gagnée : {recompense}. Ouvre un ticket sur le Discord XHQ pour la recevoir",
                     f"/tournois/{t['id']}")


def nom_tournoi(c, tournoi_id):
    return c.execute('SELECT nom FROM tournois WHERE id = ?', (tournoi_id,)).fetchone()[0]


def resume_tournoi(db, t):
    nb = db.execute('SELECT COUNT(*) FROM tournoi_inscriptions WHERE tournoi_id = ?', (t['id'],)).fetchone()[0]
    jeu = db.execute('SELECT code, nom, format FROM jeux WHERE id = ?', (t['jeu_id'],)).fetchone()
    return {'id': t['id'], 'nom': t['nom'], 'description': t['description'], 'quand': t['quand'],
            'places': t['places'], 'inscrits': nb, 'statut': t['statut'], 'statut_label': STATUTS_TOURNOI[t['statut']],
            'champion': t['champion_nom'], 'jeu': dict(jeu) if jeu else None, 'debut': t['debut'],
            'recompense': t['recompense'] or '', 'recompense_envoyee': bool(t['recompense_envoyee_at']),
            'champion_team_id': t['champion_team_id']}


def ma_team_dans_jeu(db, jeu_id):
    user = utilisateur_connecte()
    if not user:
        return None
    row = db.execute("""SELECT t.id, t.nom, t.createur_id FROM team_members m JOIN teams t ON t.id = m.team_id
                        WHERE m.user_id = ? AND m.jeu_id = ?""", (user['id'], jeu_id)).fetchone()
    return {'id': row['id'], 'nom': row['nom'], 'capitaine': row['createur_id'] == user['id']} if row else None


def tournoi_ou_404(db, tournoi_id):
    t = db.execute('SELECT * FROM tournois WHERE id = ?', (tournoi_id,)).fetchone()
    if not t:
        return None, (jsonify({'success': False, 'error': 'Tournoi introuvable'}), 404)
    return t, None


@app.route('/tournois')
def tournois_page():
    if not trouver_jeu(get_db()):
        abort(404)
    return render_template('tournois.html')


@app.route('/tournois/<int:tournoi_id>')
def tournoi_page(tournoi_id):
    t = get_db().execute('SELECT id, nom FROM tournois WHERE id = ?', (tournoi_id,)).fetchone()
    if not t:
        abort(404)
    return render_template('tournoi.html', tournoi_id=t['id'], tournoi_nom=t['nom'])


@app.route('/api/tournois', methods=['GET'])
def api_tournois():
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    lignes = db.execute("""SELECT * FROM tournois WHERE jeu_id = ?
                           ORDER BY CASE statut WHEN 'termine' THEN 1 ELSE 0 END, id DESC""", (jeu['id'],)).fetchall()
    return jsonify({'jeu': jeu, 'tournois': [resume_tournoi(db, t) for t in lignes],
                    'admin': est_admin(), 'fondateur': est_fondateur(), 'places_possibles': TOURNOI_PLACES}), 200


@app.route('/api/tournois/<int:tournoi_id>', methods=['GET'])
def api_tournoi(tournoi_id):
    db = get_db()
    t, erreur = tournoi_ou_404(db, tournoi_id)
    if erreur:
        return erreur
    noms = noms_teams_tournoi(db, t['id'])
    equipe = lambda tid: {'id': tid, 'nom': noms.get(tid, '?')} if tid else None

    inscrits = [{'team_id': r['team_id'], 'nom': noms.get(r['team_id'], '?'), 'poule': r['poule']}
                for r in db.execute('SELECT team_id, poule FROM tournoi_inscriptions WHERE tournoi_id = ? ORDER BY id',
                                    (t['id'],))]

    poules = []
    lettres = sorted({i['poule'] for i in inscrits if i['poule']})
    nb_q = qualifies_par_poule(len(lettres))
    for p in lettres:
        classement = classement_poule_tournoi(db, t['id'], p, noms)
        for l in classement:
            l['qualifie'] = l['rang'] <= nb_q
        matchs = [{'id': m['id'], 'team1': equipe(m['team1_id']), 'team2': equipe(m['team2_id']),
                   'score1': m['score1'], 'score2': m['score2'], 'vainqueur_id': m['vainqueur_id']}
                  for m in db.execute("""SELECT * FROM tournoi_matchs WHERE tournoi_id = ? AND phase = 'poule'
                                         AND poule = ? ORDER BY id""", (t['id'], p))]
        poules.append({'nom': p, 'classement': classement, 'matchs': matchs})

    bracket = []
    rows = db.execute("""SELECT * FROM tournoi_matchs WHERE tournoi_id = ? AND phase = 'bracket'
                         ORDER BY tour, position""", (t['id'],)).fetchall()
    nb_tours = max((r['tour'] for r in rows), default=0)
    joues = {(r['tour'], r['position']): r['vainqueur_id'] is not None and not r['bye'] for r in rows}
    for tour in range(1, nb_tours + 1):
        matchs = []
        for r in (r for r in rows if r['tour'] == tour):
            suivant_joue = joues.get((tour + 1, r['position'] // 2), False)
            matchs.append({'id': r['id'], 'position': r['position'], 'team1': equipe(r['team1_id']),
                           'team2': equipe(r['team2_id']), 'score1': r['score1'], 'score2': r['score2'],
                           'vainqueur_id': r['vainqueur_id'], 'bye': bool(r['bye']),
                           'modifiable': bool(r['team1_id'] and r['team2_id'] and not r['bye'] and not suivant_joue)})
        bracket.append({'tour': tour, 'nom': nom_tour(tour, nb_tours), 'matchs': matchs})

    ma_team = ma_team_dans_jeu(db, t['jeu_id'])
    inscrit = bool(ma_team and any(i['team_id'] == ma_team['id'] for i in inscrits))
    raison = None
    if t['statut'] != 'inscriptions':
        raison = 'Les inscriptions sont fermées'
    elif not utilisateur_connecte():
        raison = 'Connecte-toi pour inscrire ta team'
    elif not ma_team:
        raison = "Tu n'as pas de team pour ce jeu"
    elif not ma_team['capitaine']:
        raison = 'Seul le capitaine peut inscrire la team'
    elif not inscrit and len(inscrits) >= t['places']:
        raison = 'Le tournoi est complet'

    poules_jouees = all(m['vainqueur_id'] for p in poules for m in p['matchs'])
    return jsonify({
        'tournoi': resume_tournoi(db, t),
        'inscrits': inscrits,
        'poules': poules,
        'qualifies_par_poule': nb_q,
        'bracket': bracket,
        'moi': {'connecte': bool(utilisateur_connecte()), 'team': ma_team, 'inscrit': inscrit,
                'peut_agir': raison is None, 'raison': raison},
        'admin': est_admin(), 'fondateur': est_fondateur(),
        'actions': {
            'lancer_poules': t['statut'] == 'inscriptions' and len(inscrits) >= TOURNOI_MIN_TEAMS,
            'lancer_bracket': t['statut'] == 'poules' and poules_jouees,
        },
        'min_teams': TOURNOI_MIN_TEAMS,
    }), 200


@app.route('/api/tournois/<int:tournoi_id>/inscription', methods=['POST'])
@api_connectee
def api_tournoi_inscription(tournoi_id):
    db = get_db()
    t, erreur = tournoi_ou_404(db, tournoi_id)
    if erreur:
        return erreur
    if t['statut'] != 'inscriptions':
        return jsonify({'success': False, 'error': 'Les inscriptions sont fermées'}), 400
    ma_team = ma_team_dans_jeu(db, t['jeu_id'])
    if not ma_team:
        return jsonify({'success': False, 'error': "Tu n'as pas de team pour ce jeu"}), 400
    if not ma_team['capitaine']:
        return jsonify({'success': False, 'error': 'Seul le capitaine peut inscrire la team'}), 403
    minimum = joueurs_min(db, t['jeu_id'])
    if nb_membres(db, ma_team['id']) < minimum:   # pas de team fantôme qui prend une place de tournoi
        return jsonify({'success': False, 'error': f'Il faut au moins {minimum} joueurs dans la team pour s\'inscrire'}), 400
    if db.execute('SELECT 1 FROM tournoi_inscriptions WHERE tournoi_id = ? AND team_id = ?',
                  (t['id'], ma_team['id'])).fetchone():
        return jsonify({'success': False, 'error': 'Ta team est déjà inscrite'}), 400
    nb = db.execute('SELECT COUNT(*) FROM tournoi_inscriptions WHERE tournoi_id = ?', (t['id'],)).fetchone()[0]
    if nb >= t['places']:
        return jsonify({'success': False, 'error': 'Le tournoi est complet'}), 400
    if not db.execute("""INSERT OR IGNORE INTO tournoi_inscriptions (tournoi_id, team_id, team_nom, created_at)
                         SELECT ?, ?, ?, datetime('now')
                         WHERE (SELECT COUNT(*) FROM tournoi_inscriptions WHERE tournoi_id = ?) < ?""",
                      (t['id'], ma_team['id'], ma_team['nom'], t['id'], t['places'])).rowcount:
        return jsonify({'success': False, 'error': 'Le tournoi est complet'}), 400
    db.commit()
    return jsonify({'success': True, 'message': f"{ma_team['nom']} est inscrite à {t['nom']}"}), 200


@app.route('/api/tournois/<int:tournoi_id>/desinscription', methods=['POST'])
@api_connectee
def api_tournoi_desinscription(tournoi_id):
    db = get_db()
    t, erreur = tournoi_ou_404(db, tournoi_id)
    if erreur:
        return erreur
    if t['statut'] != 'inscriptions':
        return jsonify({'success': False, 'error': 'Le tournoi a commencé : impossible de se désinscrire'}), 400
    data = donnees_json()
    team_id = entier(data.get('team_id'))
    if est_admin() and team_id:
        cible = team_id                     # l'admin peut retirer n'importe quelle team
    else:
        ma_team = ma_team_dans_jeu(db, t['jeu_id'])
        if not ma_team or not ma_team['capitaine']:
            return jsonify({'success': False, 'error': 'Seul le capitaine peut désinscrire la team'}), 403
        cible = ma_team['id']
    if not db.execute('DELETE FROM tournoi_inscriptions WHERE tournoi_id = ? AND team_id = ?', (t['id'], cible)).rowcount:
        return jsonify({'success': False, 'error': "Cette team n'est pas inscrite"}), 400
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/tournois', methods=['POST'])
@api_admin
def api_admin_creer_tournoi():
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    data = donnees_json()
    nom = texte(data, 'nom', 60)
    places = entier(data.get('places'))
    if len(nom) < 3:
        return jsonify({'success': False, 'error': 'Le nom doit faire au moins 3 caractères'}), 400
    if places not in TOURNOI_PLACES:
        return jsonify({'success': False, 'error': 'Nombre de places : ' + ', '.join(map(str, TOURNOI_PLACES))}), 400
    debut = texte(data, 'debut', 16) or None
    if debut:
        try:
            debut = datetime.strptime(debut, '%Y-%m-%dT%H:%M').strftime('%Y-%m-%dT%H:%M')
        except ValueError:
            return jsonify({'success': False, 'error': 'Date de début invalide'}), 400
    quand = texte(data, 'quand', 80) or (date_fr(debut) if debut else '')
    tournoi_id = creer_tournoi(db, jeu, nom, places, texte(data, 'description', 500), debut, quand,
                               texte(data, 'recompense', RECOMPENSE_MAX) if est_fondateur() else '')   # le fondateur paie : lui seul promet
    journaliser(db, 'Tournoi créé', nom)
    db.commit()
    return jsonify({'success': True, 'tournoi_id': tournoi_id}), 201


RECOMPENSE_MAX = 100


def creer_tournoi(db, jeu, nom, places, description, debut, quand, recompense=''):
    """Crée un tournoi (inscriptions ouvertes), prévient les capitaines et Discord. Ne fait pas le commit."""
    c = db.cursor()
    c.execute("""INSERT INTO tournois (jeu_id, nom, description, quand, places, created_at, debut, recompense)
                 VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?)""", (jeu['id'], nom, description, quand, places, debut, recompense or ''))
    tournoi_id = c.lastrowid
    capitaines = [r[0] for r in db.execute('SELECT createur_id FROM teams WHERE jeu_id = ?', (jeu['id'],))]
    notifier(db, capitaines, f"Nouveau tournoi : {nom}{' (' + quand + ')' if quand else ''}, inscriptions ouvertes", f'/tournois/{tournoi_id}')
    enfiler_discord(db, 'tournois', f"🎮 **Nouveau tournoi : {nom}** ({jeu['nom']}, {places} places)" + (f"\n🗓️ {quand}" if quand else '')
                    + (f"\n🎁 À gagner : {recompense}" if recompense else '')
                    + f"\nInscriptions ouvertes : {SITE_URL_DEFAUT}/tournois/{tournoi_id}")
    return tournoi_id


JOURS_SEMAINE = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']


def prochaine_date(jour, heure, apres):
    """Prochaine date (heure de France, naïve) tombant ce jour de la semaine à cette heure, strictement après « apres »."""
    h, m = map(int, heure.split(':'))
    d = apres.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=(jour - apres.weekday()) % 7)
    return d if d > apres else d + timedelta(days=7)


def creer_tournois_recurrents(db):
    """Crée les tournois récurrents dont la date d'ouverture des inscriptions est arrivée (un par semaine)."""
    maintenant = datetime.now(fuseau_paris()).replace(tzinfo=None)
    crees = 0
    for r in db.execute('SELECT * FROM tournois_recurrents WHERE actif = 1').fetchall():
        prochaine = prochaine_date(r['jour'], r['heure'], maintenant)
        cle = prochaine.strftime('%Y-%m-%dT%H:%M')
        if maintenant < prochaine - timedelta(days=r['ouvrir_avant_jours']) or r['dernier_pour'] == cle:
            continue
        # On « prend » cette date d'abord : jamais deux tournois pour la même semaine
        if not db.execute('UPDATE tournois_recurrents SET dernier_pour = ?, compteur = compteur + 1 WHERE id = ? AND COALESCE(dernier_pour, \'\') != ?',
                          (cle, r['id'], cle)).rowcount:
            continue
        jeu = db.execute('SELECT id, nom, code FROM jeux WHERE id = ?', (r['jeu_id'],)).fetchone()
        if not jeu:
            continue
        numero = r['compteur'] + 1
        nom = (r['nom'] + ' #' + str(numero))[:60]
        tid = creer_tournoi(db, dict(jeu), nom, r['places'], r['description'] or '', cle, date_fr(cle), r['recompense'] or '')
        ecrire_journal(db.cursor(), None, 'automatique', 'Tournoi récurrent créé', nom)
        db.commit()
        crees += 1
    return crees


_controle_auto = {'t': 0.0}


@app.route('/api/admin/tournois/<int:tournoi_id>/poules', methods=['POST'])
@api_admin
def api_admin_tournoi_poules(tournoi_id):
    import random
    db = get_db()
    t, erreur = tournoi_ou_404(db, tournoi_id)
    if erreur:
        return erreur
    if t['statut'] != 'inscriptions':
        return jsonify({'success': False, 'error': 'Les poules sont déjà lancées'}), 400
    teams = [r['team_id'] for r in db.execute('SELECT team_id FROM tournoi_inscriptions WHERE tournoi_id = ?', (t['id'],))]
    if len(teams) < TOURNOI_MIN_TEAMS:
        return jsonify({'success': False, 'error': f'Il faut au moins {TOURNOI_MIN_TEAMS} teams inscrites'}), 400
    # On « prend » le tournoi d'abord : un double clic ne crée jamais deux fois les poules
    if not db.execute("UPDATE tournois SET statut = 'poules', lance_at = datetime('now') WHERE id = ? AND statut = 'inscriptions'",
                      (t['id'],)).rowcount:
        return jsonify({'success': False, 'error': 'Les poules sont déjà lancées'}), 400
    random.shuffle(teams)                                   # tirage au sort
    nb_poules = max(1, round(len(teams) / TOURNOI_TAILLE_POULE))
    lettres = 'ABCDEFGHIJKLMNOP'[:nb_poules]
    repartition = {l: teams[i::nb_poules] for i, l in enumerate(lettres)}
    c = db.cursor()
    for lettre, membres in repartition.items():
        for tid in membres:
            c.execute('UPDATE tournoi_inscriptions SET poule = ? WHERE tournoi_id = ? AND team_id = ?', (lettre, t['id'], tid))
        for a, b in journees_poule(membres):
            c.execute("""INSERT INTO tournoi_matchs (tournoi_id, phase, poule, team1_id, team2_id)
                         VALUES (?, 'poule', ?, ?, ?)""", (t['id'], lettre, a, b))
    c.execute("UPDATE tournois SET statut = 'poules', lance_at = datetime('now') WHERE id = ?", (t['id'],))
    for tid in teams:
        notifier(c, membres_de(c, tid), f"{t['nom']} : les poules sont tirées, à vous de jouer", f"/tournois/{t['id']}")
    db.commit()
    return jsonify({'success': True, 'poules': {l: len(m) for l, m in repartition.items()}}), 200


@app.route('/api/admin/tournois/<int:tournoi_id>/bracket', methods=['POST'])
@api_admin
def api_admin_tournoi_bracket(tournoi_id):
    db = get_db()
    t, erreur = tournoi_ou_404(db, tournoi_id)
    if erreur:
        return erreur
    if t['statut'] != 'poules':
        return jsonify({'success': False, 'error': "Le tournoi n'est pas en phase de poules"}), 400
    if db.execute("""SELECT 1 FROM tournoi_matchs WHERE tournoi_id = ? AND phase = 'poule'
                     AND vainqueur_id IS NULL""", (t['id'],)).fetchone():
        return jsonify({'success': False, 'error': 'Tous les matchs de poule doivent être joués'}), 400

    if not db.execute("UPDATE tournois SET statut = 'bracket' WHERE id = ? AND statut = 'poules'", (t['id'],)).rowcount:
        return jsonify({'success': False, 'error': 'La phase finale est déjà lancée'}), 400
    noms = noms_teams_tournoi(db, t['id'])
    lettres = sorted({r['poule'] for r in db.execute(
        'SELECT DISTINCT poule FROM tournoi_inscriptions WHERE tournoi_id = ? AND poule IS NOT NULL', (t['id'],))})
    nb_q = qualifies_par_poule(len(lettres))
    classements = {p: classement_poule_tournoi(db, t['id'], p, noms) for p in lettres}
    # Têtes de série : tous les 1ers, puis tous les 2es (...), chacun avec sa poule d'origine
    tetes = [(cl[rang]['team_id'], p) for rang in range(nb_q) for p, cl in classements.items() if rang < len(cl)]
    taille = 2
    while taille < len(tetes):
        taille *= 2
    ordre = ordre_tetes_de_serie(taille)
    places = [tetes[s - 1] if s <= len(tetes) else None for s in ordre]
    # Évite qu'une poule se retrouve contre elle-même dès le premier tour (si possible)
    if len(lettres) > 1:
        for i in range(0, taille, 2):
            a, b = places[i], places[i + 1]
            if a and b and a[1] == b[1]:
                for j in range(1, taille, 2):
                    autre, voisin = places[j], places[j - 1]
                    if j != i + 1 and autre and voisin and autre[1] != a[1] and b[1] != voisin[1]:
                        places[i + 1], places[j] = autre, b
                        break

    c = db.cursor()
    nb_tours = taille.bit_length() - 1
    for tour in range(1, nb_tours + 1):
        for pos in range(taille >> tour):
            if tour == 1:
                a, b = places[2 * pos], places[2 * pos + 1]
                t1, t2 = (a[0] if a else None), (b[0] if b else None)
                bye = (t1 is None) != (t2 is None)
                c.execute("""INSERT INTO tournoi_matchs (tournoi_id, phase, tour, position, team1_id, team2_id,
                             vainqueur_id, bye) VALUES (?, 'bracket', 1, ?, ?, ?, ?, ?)""",
                          (t['id'], pos, t1, t2, (t1 or t2) if bye else None, int(bye)))
            else:
                c.execute("INSERT INTO tournoi_matchs (tournoi_id, phase, tour, position) VALUES (?, 'bracket', ?, ?)",
                          (t['id'], tour, pos))
    for m in c.execute("SELECT * FROM tournoi_matchs WHERE tournoi_id = ? AND phase = 'bracket' AND bye = 1",
                       (t['id'],)).fetchall():
        faire_avancer(c, m)
    c.execute("UPDATE tournois SET statut = 'bracket' WHERE id = ?", (t['id'],))
    db.commit()
    return jsonify({'success': True, 'qualifies': len(tetes), 'taille': taille}), 200


@app.route('/api/admin/tournois/<int:tournoi_id>/matchs/<int:match_id>', methods=['POST'])
@api_admin
def api_admin_tournoi_score(tournoi_id, match_id):
    db = get_db()
    t, erreur = tournoi_ou_404(db, tournoi_id)
    if erreur:
        return erreur
    m = db.execute('SELECT * FROM tournoi_matchs WHERE id = ? AND tournoi_id = ?', (match_id, t['id'])).fetchone()
    if not m:
        return jsonify({'success': False, 'error': 'Match introuvable'}), 404
    data = donnees_json()
    s1, s2 = entier(data.get('score1')), entier(data.get('score2'))
    if s1 is None or s2 is None or not (0 <= s1 <= SCORE_MAX and 0 <= s2 <= SCORE_MAX):
        return jsonify({'success': False, 'error': f'Les scores doivent être des nombres entre 0 et {SCORE_MAX}'}), 400
    if s1 == s2:
        return jsonify({'success': False, 'error': 'Pas de match nul : il faut un gagnant'}), 400
    if not m['team1_id'] or not m['team2_id'] or m['bye']:
        return jsonify({'success': False, 'error': "Ce match n'a pas encore ses deux teams"}), 400
    if t['statut'] == 'termine':
        return jsonify({'success': False, 'error': 'Tournoi terminé : le résultat est définitif'}), 400
    if m['phase'] == 'poule' and t['statut'] != 'poules':
        return jsonify({'success': False, 'error': 'La phase de poules est terminée'}), 400
    if m['phase'] == 'bracket':
        suivant = db.execute("""SELECT vainqueur_id, bye FROM tournoi_matchs WHERE tournoi_id = ? AND phase = 'bracket'
                                AND tour = ? AND position = ?""", (t['id'], m['tour'] + 1, m['position'] // 2)).fetchone()
        if suivant and suivant['vainqueur_id'] and not suivant['bye']:
            return jsonify({'success': False, 'error': 'Le match suivant est déjà joué : corrige-le d\'abord'}), 400
    vainqueur = m['team1_id'] if s1 > s2 else m['team2_id']
    c = db.cursor()
    c.execute('UPDATE tournoi_matchs SET score1 = ?, score2 = ?, vainqueur_id = ? WHERE id = ?', (s1, s2, vainqueur, m['id']))
    if m['phase'] == 'bracket':
        faire_avancer(c, c.execute('SELECT * FROM tournoi_matchs WHERE id = ?', (m['id'],)).fetchone())
    db.commit()
    return jsonify({'success': True, 'vainqueur_id': vainqueur}), 200


@app.route('/api/admin/tournois/<int:tournoi_id>/delete', methods=['POST'])
@api_admin
def api_admin_supprimer_tournoi(tournoi_id):
    db = get_db()
    t, erreur = tournoi_ou_404(db, tournoi_id)
    if erreur:
        return erreur
    for table in ('tournoi_matchs', 'tournoi_inscriptions'):
        db.execute(f'DELETE FROM {table} WHERE tournoi_id = ?', (t['id'],))
    journaliser(db, 'Tournoi supprimé', t['nom'])
    db.execute('DELETE FROM tournois WHERE id = ?', (t['id'],))
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/tournois/<int:tournoi_id>/recompense', methods=['POST'])
@api_admin
def api_admin_recompense_envoyee(tournoi_id):
    """Le fondateur note qu'il a remis la récompense au champion (ou annule s'il s'est trompé)."""
    db = get_db()
    t, erreur = tournoi_ou_404(db, tournoi_id)
    if erreur:
        return erreur
    if t['statut'] != 'termine' or not t['recompense']:
        return jsonify({'success': False, 'error': "Ce tournoi n'a pas de récompense à remettre"}), 400
    envoyee = donnees_json().get('envoyee', True) is True   # seul un vrai « true » compte (pas « 'false' » ni 1)
    db.execute("UPDATE tournois SET recompense_envoyee_at = CASE WHEN ? THEN datetime('now') END WHERE id = ?", (envoyee, t['id']))
    journaliser(db, 'Récompense remise' if envoyee else 'Récompense : remise annulée',
                f"{t['nom']} · {t['champion_nom'] or '?'} · {t['recompense']}")
    db.commit()
    return jsonify({'success': True}), 200


# ---------------------------------------------------------------------------
# Mot de passe oublié : lien de réinitialisation envoyé par email (valable 1 heure)
# Réglages : variables d'environnement XHQ_SMTP_* ou fichier config_mail.json (non versionné) :
#   {"hote": "smtp.gmail.com", "port": 587, "utilisateur": "...@gmail.com",
#    "mot_de_passe": "mot de passe d'application", "expediteur": "XHQ League <...@gmail.com>"}
# ---------------------------------------------------------------------------
REINIT_DUREE_MINUTES = 60
REINIT_MAX_PAR_HEURE = 3


def config_mail():
    import json
    conf = {}
    chemin = os.path.join(BASE_DIR, 'config_mail.json')
    if os.path.exists(chemin):
        try:
            with open(chemin, encoding='utf-8') as f:
                conf = json.load(f)
        except (OSError, ValueError):
            conf = {}
    for cle, env in (('hote', 'XHQ_SMTP_HOST'), ('port', 'XHQ_SMTP_PORT'), ('utilisateur', 'XHQ_SMTP_USER'),
                     ('mot_de_passe', 'XHQ_SMTP_PASSWORD'), ('expediteur', 'XHQ_SMTP_FROM'), ('site', 'XHQ_SITE_URL'),
                     ('tls', 'XHQ_SMTP_TLS')):
        if os.environ.get(env):
            conf[cle] = os.environ[env]
    if conf.get('mot_de_passe'):
        # Google affiche le code d'application par blocs (« abcd efgh ijkl mnop ») : on retire les espaces
        conf['mot_de_passe'] = ''.join(str(conf['mot_de_passe']).split())
    return conf if conf.get('hote') and conf.get('utilisateur') and conf.get('mot_de_passe') else None


def envoyer_mail(destinataire, sujet, texte_brut):
    import smtplib
    from email.message import EmailMessage
    conf = config_mail()
    if not conf:
        return False
    # Quota : Gmail limite les envois quotidiens. Au-delà de 300 mails en 24 h, on arrête (et on le note)
    # pour qu'un attaquant ne puisse pas épuiser le quota et bloquer les vrais « mot de passe oublié ».
    if has_request_context():
        if limite_atteinte('mail-global', 300, 24 * 60):
            app.logger.error('Quota quotidien de mails atteint : envoi refusé')
            return False
        noter_action('mail-global')
    msg = EmailMessage()
    msg['Subject'] = sujet
    msg['From'] = conf.get('expediteur') or conf['utilisateur']
    msg['To'] = destinataire
    msg.set_content(texte_brut)
    try:
        with smtplib.SMTP(conf['hote'], int(conf.get('port') or 587), timeout=15) as smtp:
            if str(conf.get('tls', True)).lower() not in ('0', 'false', 'non'):
                smtp.starttls()
                smtp.login(conf['utilisateur'], conf['mot_de_passe'])
            smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as e:
        app.logger.error('Envoi du mail impossible : %s', e)
        return False


def hash_jeton(jeton):
    import hashlib
    return hashlib.sha256(jeton.encode()).hexdigest()


@app.route('/mot-de-passe-oublie')
def mot_de_passe_oublie_page():
    return render_template('mot_de_passe.html', etape='demande', mail_actif=bool(config_mail()), jeton='')


def envoyer_lien_reinitialisation(db, user_id):
    """Crée un lien à usage unique (1 h) et l'envoie à l'email du compte. Vrai si le mail est parti."""
    user = db.execute('SELECT id, username, email FROM users WHERE id = ?', (user_id,)).fetchone()
    conf = config_mail()
    if not user or not conf:
        return False
    jeton = secrets.token_urlsafe(32)
    expire = (datetime.utcnow() + timedelta(minutes=REINIT_DUREE_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
    cur = db.execute("""INSERT INTO reinitialisations (user_id, jeton_hash, expire_at, created_at)
                        VALUES (?, ?, ?, datetime('now'))""", (user['id'], hash_jeton(jeton), expire))
    db.commit()
    site = (conf.get('site') or SITE_URL_DEFAUT).rstrip('/')
    envoye = envoyer_mail(user['email'], 'XHQ League · Réinitialiser ton mot de passe', f"""Salut {user['username']},

Tu as demandé à changer ton mot de passe XHQ League. Clique sur ce lien (valable {REINIT_DUREE_MINUTES} minutes) :

{site}/reinitialiser/{jeton}

Si ce n'est pas toi, ignore ce message : ton mot de passe actuel reste valable.

L'équipe XHQ""")
    if not envoye:
        # L'envoi a échoué : la demande ne compte pas dans la limite, on pourra réessayer
        db.execute('DELETE FROM reinitialisations WHERE id = ?', (cur.lastrowid,))
        db.commit()
    return envoye


def prevenir_par_mail(db, user_id, sujet, message):
    """Alerte de sécurité envoyée au joueur (si l'envoi d'emails est configuré). Ne bloque jamais l'action."""
    u = db.execute('SELECT username, email FROM users WHERE id = ?', (user_id,)).fetchone()
    if not u or not config_mail():
        return
    try:
        envoyer_mail(u['email'], f'XHQ League · {sujet}', f"""Salut {u['username']},

{message}

Si ce n'est pas toi : utilise tout de suite « Mot de passe oublié » sur le site, puis préviens l'orga (page Contact).

L'équipe XHQ""")
    except Exception as e:   # un souci d'email ne doit jamais empêcher l'action
        app.logger.warning('Alerte de sécurité non envoyée : %s', e)


_duree_envoi = {'moyenne': 1.5}   # durée habituelle d'un envoi de mail (mise à jour à chaque envoi)


@app.route('/api/mot-de-passe/demande', methods=['POST'])
def api_demande_reinitialisation():
    email = texte(donnees_json(), 'email', 120).lower()
    if limite_atteinte('reinit:' + ip_client(), 10, 60):
        return jsonify({'success': False, 'error': TROP}), 429
    noter_action('reinit:' + ip_client())
    conf = config_mail()
    if not conf:
        return jsonify({'success': False, 'mail': False,
                        'error': "L'envoi d'emails n'est pas encore activé : écris-nous via la page Contact."}), 503
    import time
    import random
    debut = time.time()
    db = get_db()
    user = db.execute('SELECT id, username, email FROM users WHERE lower(email) = ? AND COALESCE(bloque, 0) = 0 AND COALESCE(verrou_definitif, 0) = 0', (email,)).fetchone()
    envoye = False
    if user:
        recentes = db.execute("""SELECT COUNT(*) FROM reinitialisations WHERE user_id = ?
                                  AND created_at > datetime('now', '-1 hour')""", (user['id'],)).fetchone()[0]
        if recentes < REINIT_MAX_PAR_HEURE:
            envoye = envoyer_lien_reinitialisation(db, user['id'])
    # Même durée de réponse que l'email existe ou non : sinon, en chronométrant, on saurait qui est inscrit
    if envoye:
        _duree_envoi['moyenne'] = 0.5 * _duree_envoi['moyenne'] + 0.5 * (time.time() - debut)
    else:
        time.sleep(max(0.0, _duree_envoi['moyenne'] * random.uniform(0.85, 1.15) - (time.time() - debut)))
    # Même réponse que le compte existe ou non (on ne révèle pas quels emails sont inscrits)
    return jsonify({'success': True, 'mail': True}), 200


def reinitialisation_valide(db, jeton):
    return db.execute("""SELECT r.id, r.user_id FROM reinitialisations r JOIN users u ON u.id = r.user_id
                         WHERE r.jeton_hash = ? AND r.utilise = 0 AND r.expire_at > ? AND COALESCE(u.bloque, 0) = 0
                         AND COALESCE(u.verrou_definitif, 0) = 0""",
                      (hash_jeton(jeton), datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))).fetchone()


@app.route('/reinitialiser/<jeton>')
def reinitialiser_page(jeton):
    valide = reinitialisation_valide(get_db(), jeton) is not None
    return render_template('mot_de_passe.html', etape='nouveau' if valide else 'expire', jeton=jeton if valide else '')


@app.route('/api/mot-de-passe/reinitialiser', methods=['POST'])
def api_reinitialiser():
    data = donnees_json()
    db = get_db()
    r = reinitialisation_valide(db, texte(data, 'jeton', 100))
    if not r:
        return jsonify({'success': False, 'error': 'Ce lien a expiré ou a déjà servi. Refais une demande.'}), 400
    nouveau = data.get('nouveau') if isinstance(data.get('nouveau'), str) else ''
    if not MOT_DE_PASSE_MIN <= len(nouveau) <= 128:
        return jsonify({'success': False, 'error': f'Le mot de passe doit faire au moins {MOT_DE_PASSE_MIN} caractères'}), 400
    db.execute('UPDATE users SET password = ? WHERE id = ?', (generate_password_hash(nouveau), r['user_id']))
    db.execute('UPDATE reinitialisations SET utilise = 1 WHERE user_id = ?', (r['user_id'],))
    db.execute('UPDATE users SET echecs_connexion = 0, verrou_niveau = 0, bloque_jusqua = NULL, email_verifie = 1, verif_jeton = NULL WHERE id = ?', (r['user_id'],))
    invalider_sessions(db, r['user_id'])
    db.commit()
    prevenir_par_mail(db, r['user_id'], 'Ton mot de passe a été réinitialisé',
                      'Le mot de passe de ton compte XHQ League vient d\'être réinitialisé avec le lien reçu par email.')
    connecter(r['user_id'])
    return jsonify({'success': True}), 200


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
@app.route('/api/notifications', methods=['GET'])
@api_connectee
def api_notifications():
    db = get_db()
    rows = db.execute('SELECT id, texte, lien, lu, created_at FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 20',
                      (session['user_id'],)).fetchall()
    return jsonify({'non_lues': sum(1 for r in rows if not r['lu']),
                    'items': [{**dict(r), 'lu': bool(r['lu']), 'quand': il_y_a(r['created_at'])} for r in rows]}), 200


@app.route('/api/notifications/lues', methods=['POST'])
@api_connectee
def api_notifications_lues():
    db = get_db()
    db.execute('UPDATE notifications SET lu = 1 WHERE user_id = ? AND lu = 0', (session['user_id'],))
    # On ne garde que les 100 dernières par joueur
    db.execute('''DELETE FROM notifications WHERE user_id = ? AND id NOT IN
                  (SELECT id FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 100)''',
               (session['user_id'], session['user_id']))
    db.commit()
    return jsonify({'success': True}), 200


# ---------------------------------------------------------------------------
# Sauvegardes : base de données + images dans un fichier .zip
# ---------------------------------------------------------------------------
SAUVEGARDE_DIR = os.path.join(BASE_DIR, 'sauvegardes')
SAUVEGARDES_GARDEES = 7    # on garde les 7 dernières (une semaine)
SAUVEGARDES_MAX_MO = 150   # et jamais plus de 150 Mo au total (le compte gratuit n'a que 512 Mo)


def creer_sauvegarde():
    """Copie cohérente de la base (même si le site tourne) + dossier uploads, dans sauvegardes/xhq-AAAAMMJJ-HHMM.zip."""
    import zipfile
    import tempfile
    os.makedirs(SAUVEGARDE_DIR, exist_ok=True)
    nom = datetime.now().strftime('xhq-%Y%m%d-%H%M%S.zip')
    chemin = os.path.join(SAUVEGARDE_DIR, nom)
    with tempfile.TemporaryDirectory() as tmp:
        copie = os.path.join(tmp, 'tournois.db')
        source = sqlite3.connect(DB_PATH)
        cible = sqlite3.connect(copie)
        source.backup(cible)
        cible.close()
        source.close()
        with zipfile.ZipFile(chemin, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(copie, 'tournois.db')
            if os.path.isdir(UPLOAD_DIR):
                for dossier, _, fichiers in os.walk(UPLOAD_DIR):
                    for f in fichiers:
                        complet = os.path.join(dossier, f)
                        z.write(complet, os.path.join('uploads', os.path.relpath(complet, UPLOAD_DIR)))
    anciennes = sorted(f for f in os.listdir(SAUVEGARDE_DIR) if f.startswith('xhq-') and f.endswith('.zip'))
    for f in anciennes[:-SAUVEGARDES_GARDEES]:
        os.remove(os.path.join(SAUVEGARDE_DIR, f))
    restantes = sorted(f for f in os.listdir(SAUVEGARDE_DIR) if f.startswith('xhq-') and f.endswith('.zip'))
    while len(restantes) > 1 and sum(os.path.getsize(os.path.join(SAUVEGARDE_DIR, f)) for f in restantes) > SAUVEGARDES_MAX_MO * 1024 * 1024:
        os.remove(os.path.join(SAUVEGARDE_DIR, restantes.pop(0)))
    return chemin


# Sauvegarde automatique sans tâche planifiée (payante sur PythonAnywhere) :
# au plus une vérification toutes les 10 min ; si la dernière sauvegarde a plus de 24 h, on en fait une.
_controle_sauvegarde = {'t': 0.0}


def sauvegarde_auto_si_besoin():
    import time
    maintenant = time.time()
    if maintenant - _controle_sauvegarde['t'] < 600 or os.environ.get('XHQ_SAUVEGARDE_AUTO') == '0':
        return
    _controle_sauvegarde['t'] = maintenant
    try:
        derniere = 0
        if os.path.isdir(SAUVEGARDE_DIR):
            zips = [os.path.join(SAUVEGARDE_DIR, f) for f in os.listdir(SAUVEGARDE_DIR) if f.startswith('xhq-') and f.endswith('.zip')]
            derniere = max((os.path.getmtime(z) for z in zips), default=0)
        if maintenant - derniere > 24 * 3600:
            creer_sauvegarde()
            # Ménage quotidien : vieilles notifications (plus de 90 jours)
            with app.app_context():
                db = get_db()
                db.execute("DELETE FROM notifications WHERE created_at < datetime('now', '-90 days')")
                db.commit()
                purger_comptes_non_confirmes(db)
    except Exception as e:   # une sauvegarde ratée ne doit jamais casser le site
        app.logger.warning('Sauvegarde automatique impossible : %s', e)


@app.before_request
def _sauvegarde_quotidienne():
    if not request.path.startswith(('/static/', '/media/')):
        sauvegarde_auto_si_besoin()
        import time
        if time.time() - _controle_annonces['t'] > 30:
            _controle_annonces['t'] = time.time()
            for tache_auto in (publier_annonces_dues, creer_tournois_recurrents, clotures_programmees, lancement_auto, envoyer_file_discord):
                try:
                    tache_auto(get_db())
                except Exception as e:   # une tâche automatique ratée ne doit jamais casser une page
                    app.logger.warning('Tâche automatique impossible (%s) : %s', tache_auto.__name__, e)


@app.route('/admin/sauvegarde')
@page_admin
def admin_sauvegarde():
    # Une sauvegarde contient TOUTE la base (emails, mots de passe chiffrés) : réservée aux fondateurs
    db = get_db()
    if fondateur_requis(db):
        return 'Téléchargement des sauvegardes réservé aux fondateurs.', 403
    journaliser(db, 'Sauvegarde téléchargée')
    db.commit()
    chemin = creer_sauvegarde()
    return send_from_directory(SAUVEGARDE_DIR, os.path.basename(chemin), as_attachment=True)


@app.route('/api/admin/sauvegardes', methods=['GET'])
@api_admin
def api_admin_sauvegardes():
    liste = []
    if os.path.isdir(SAUVEGARDE_DIR):
        for f in sorted(os.listdir(SAUVEGARDE_DIR), reverse=True):
            if f.startswith('xhq-') and f.endswith('.zip'):
                liste.append({'nom': f, 'taille_ko': round(os.path.getsize(os.path.join(SAUVEGARDE_DIR, f)) / 1024)})
    return jsonify(liste), 200


# ---------------------------------------------------------------------------
# Modération : retirer une image / une bio, suspendre un compte
# ---------------------------------------------------------------------------
MODERATION = {
    'users': {'photo': 'avatars', 'banniere': 'bannieres', 'bio': None},
    'teams': {'logo': 'logos', 'banniere': 'bannieres', 'bio': None},
}


@app.route('/api/admin/moderer', methods=['POST'])
@api_admin
def api_admin_moderer():
    data = donnees_json()
    table, champ, cible = data.get('type'), data.get('champ'), entier(data.get('id'))
    if table not in MODERATION or champ not in MODERATION[table] or cible is None:
        return jsonify({'success': False, 'error': 'Demande invalide'}), 400
    db = get_db()
    ligne = db.execute(f'SELECT {champ} FROM {table} WHERE id = ?', (cible,)).fetchone()
    if not ligne:
        return jsonify({'success': False, 'error': 'Introuvable'}), 404
    dossier = MODERATION[table][champ]
    journaliser(db, f'Modération : {champ} retiré(e)', nom_joueur(db, cible) if table == 'users' else nom_team(db, cible))
    db.execute(f"UPDATE {table} SET {champ} = {'NULL' if dossier else chr(39) * 2} WHERE id = ?", (cible,))
    db.commit()
    if dossier:
        supprimer_media(dossier, ligne[0])
    return jsonify({'success': True}), 200


@app.route('/api/admin/users/<int:user_id>/bloquer', methods=['POST'])
@api_admin
def api_admin_bloquer(user_id):
    if user_id == session['user_id']:
        return jsonify({'success': False, 'error': 'Tu ne peux pas suspendre ton propre compte'}), 400
    bloque = bool(donnees_json().get('bloque'))
    db = get_db()
    if est_protege(db, user_id) and fondateur_requis(db):
        return jsonify({'success': False, 'error': fondateur_requis(db)}), 403
    journaliser(db, 'Compte suspendu' if bloque else 'Compte réactivé', nom_joueur(db, user_id))
    if not db.execute('UPDATE users SET bloque = ? WHERE id = ?', (int(bloque), user_id)).rowcount:
        return jsonify({'success': False, 'error': 'Joueur introuvable'}), 404
    db.commit()
    return jsonify({'success': True, 'bloque': bloque}), 200


# ---------------------------------------------------------------------------
# Calendrier : les matchs prévus de chaque poule
# ---------------------------------------------------------------------------
def journees(teams):
    """Rencontres d'une poule regroupées par journée (méthode du tourniquet) : [[(a, b), ...], ...]."""
    liste = list(teams) + ([None] if len(teams) % 2 else [])
    n, tours = len(liste), []
    for _ in range(n - 1):
        tours.append([(liste[i], liste[n - 1 - i]) for i in range(n // 2) if liste[i] and liste[n - 1 - i]])
        liste = [liste[0], liste[-1]] + liste[1:-1]
    return tours


def rencontres_a_venir(db, jeu_id, limite=50, team_id=None, ligue_num=None):
    saison = saison_actuelle(db, jeu_id)
    cond, params = ['r.jeu_id = ?', 'r.saison = ?', 'r.match_id IS NULL', 't1.id IS NOT NULL', 't2.id IS NOT NULL'], [jeu_id, saison]
    if team_id:
        cond.append('(r.team1_id = ? OR r.team2_id = ?)')
        params += [team_id, team_id]
    if ligue_num:
        cond.append('r.ligue_num = ?')
        params.append(ligue_num)
    rows = db.execute(f"""SELECT r.id, r.ligue_num, r.poule, r.journee, r.date, r.team1_id, r.team2_id,
                                 t1.nom AS team1_nom, t2.nom AS team2_nom, t1.logo AS logo1, t2.logo AS logo2,
                                 t1.embleme AS emb1, t2.embleme AS emb2
                          FROM rencontres r LEFT JOIN teams t1 ON t1.id = r.team1_id LEFT JOIN teams t2 ON t2.id = r.team2_id
                          WHERE {' AND '.join(cond)} ORDER BY r.date IS NULL, r.date, r.ligue_num, r.poule LIMIT ?""",
                      params + [limite]).fetchall()
    maintenant = datetime.now().strftime('%Y-%m-%dT%H:%M')
    sortie = []
    for r in rows:
        d = dict(r)
        d['logo1'], d['logo2'] = url_media('logos', d['logo1']), url_media('logos', d['logo2'])
        d['date_fr'] = date_fr(d['date']) if d['date'] else 'Date à fixer'
        d['en_retard'] = bool(d['date'] and d['date'] < maintenant)
        sortie.append(d)
    return sortie


@app.route('/calendrier')
def calendrier_page():
    db = get_db()
    jeu = trouver_jeu(db)
    if not jeu:
        abort(404)
    ligue_num = request.args.get('ligue', type=int)
    if ligue_num not in LIGUES:
        ligue_num = None
    ma_team = ma_team_dans_jeu(db, jeu['id'])
    filtre_team = ma_team['id'] if (ma_team and request.args.get('team') == 'moi') else None
    a_venir = rencontres_a_venir(db, jeu['id'], limite=200, team_id=filtre_team, ligue_num=ligue_num)
    jours = []
    for r in a_venir:
        cle = r['date'][:10] if r['date'] else 'a-fixer'
        if not jours or jours[-1]['cle'] != cle:
            titre = 'Date à fixer'
            if r['date']:
                d = datetime.strptime(r['date'], '%Y-%m-%dT%H:%M')
                titre = f"{JOURS_FR[d.weekday()].capitalize()} {d.day} {MOIS_FR[d.month - 1]}"
            jours.append({'cle': cle, 'titre': titre, 'matchs': []})
        jours[-1]['matchs'].append(r)
    return render_template('calendrier.html', jours=jours, ligues=LIGUES, ligue_num=ligue_num, ma_team=ma_team,
                           filtre_team=bool(filtre_team), nb=len(a_venir))


@app.route('/api/calendrier', methods=['GET'])
def api_calendrier():
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    return jsonify(rencontres_a_venir(db, jeu['id'], limite=max(1, min(request.args.get('limit', 50, type=int), 300)),
                                      team_id=request.args.get('team_id', type=int))), 200


@app.route('/api/admin/calendrier/generer', methods=['POST'])
@api_admin
def api_admin_generer_calendrier():
    """Prévoit toutes les rencontres pas encore jouées ni prévues de la saison, journée par journée."""
    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    data = donnees_json()
    try:
        debut = datetime.strptime(texte(data, 'debut', 16), '%Y-%m-%dT%H:%M')
    except ValueError:
        return jsonify({'success': False, 'error': 'Choisis la date et l\'heure de la 1re journée'}), 400
    ecart = entier(data.get('ecart_jours'))
    if ecart is None or not 1 <= ecart <= 30:
        return jsonify({'success': False, 'error': "L'écart entre deux journées doit être entre 1 et 30 jours"}), 400
    saison = saison_actuelle(db, jeu['id'])
    deja = set()
    for r in db.execute('SELECT team1_id, team2_id FROM rencontres WHERE jeu_id = ? AND saison = ?', (jeu['id'], saison)):
        deja.add(frozenset((r['team1_id'], r['team2_id'])))
    for m in db.execute('SELECT team1_id, team2_id FROM matchs WHERE jeu_id = ? AND saison = ?', (jeu['id'], saison)):
        deja.add(frozenset((m['team1_id'], m['team2_id'])))
    crees, par_team = 0, {}
    for ligue_num in LIGUES:
        for poule in lettres_poules(ligue_num):
            teams = [r['team_id'] for r in db.execute("""SELECT l.team_id FROM ligue l JOIN teams t ON t.id = l.team_id
                                                         WHERE t.jeu_id = ? AND l.ligue_num = ? AND l.poule = ? ORDER BY l.id""",
                                                      (jeu['id'], ligue_num, poule))]
            for n, tour in enumerate(journees(teams)):
                date = (debut + timedelta(days=ecart * n)).strftime('%Y-%m-%dT%H:%M')
                for a, b in tour:
                    if frozenset((a, b)) in deja:
                        continue
                    db.execute("""INSERT OR IGNORE INTO rencontres (jeu_id, saison, ligue_num, poule, journee, team1_id, team2_id, date, created_at)
                                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                               (jeu['id'], saison, ligue_num, poule, n + 1, a, b, date))
                    deja.add(frozenset((a, b)))
                    crees += 1
                    par_team[a] = par_team.get(a, 0) + 1
                    par_team[b] = par_team.get(b, 0) + 1
    for tid, n in par_team.items():
        notifier(db, membres_de(db, tid), f"Calendrier publié : {n} match{'s' if n > 1 else ''} de ta team au programme",
                 '/calendrier?team=moi')
    journaliser(db, 'Calendrier généré', f'{crees} match(s)')
    db.commit()
    return jsonify({'success': True, 'crees': crees}), 200


@app.route('/api/admin/calendrier/<int:rencontre_id>', methods=['POST'])
@api_admin
def api_admin_modifier_rencontre(rencontre_id):
    data = donnees_json()
    db = get_db()
    if data.get('supprimer'):
        db.execute('DELETE FROM rencontres WHERE id = ? AND match_id IS NULL', (rencontre_id,))
        db.commit()
        return jsonify({'success': True}), 200
    date = texte(data, 'date', 16)
    try:
        date = datetime.strptime(date, '%Y-%m-%dT%H:%M').strftime('%Y-%m-%dT%H:%M') if date else None
    except ValueError:
        return jsonify({'success': False, 'error': 'Date invalide'}), 400
    if not db.execute('UPDATE rencontres SET date = ? WHERE id = ? AND match_id IS NULL', (date, rencontre_id)).rowcount:
        return jsonify({'success': False, 'error': 'Match introuvable ou déjà joué'}), 404
    r = db.execute("""SELECT r.team1_id, r.team2_id, t1.nom AS n1, t2.nom AS n2 FROM rencontres r
                      JOIN teams t1 ON t1.id = r.team1_id JOIN teams t2 ON t2.id = r.team2_id WHERE r.id = ?""", (rencontre_id,)).fetchone()
    if r:
        notifier(db, membres_de(db, r['team1_id']) + membres_de(db, r['team2_id']),
                 f"Nouveau créneau : {r['n1']} vs {r['n2']}, {date_fr(date) if date else 'date à fixer'}", '/calendrier?team=moi')
    db.commit()
    return jsonify({'success': True, 'date_fr': date_fr(date) if date else 'Date à fixer'}), 200


# ---------------------------------------------------------------------------
# Site public (GitHub Pages) : export des données en JSON
# ---------------------------------------------------------------------------
SITE_PUBLIC_DIR = os.path.join(BASE_DIR, 'docs')


def exporter_site_public():
    """Écrit docs/data/ligue.json (classements, teams, résultats, palmarès) et recopie la feuille de style.
    Aucune donnée personnelle : ni email, ni pseudo de joueur, seulement les noms de teams."""
    import json
    import shutil
    from datetime import datetime, timezone
    with app.test_request_context():
        db = get_db()
        jeu_id = jeu_defaut_id(db)
        saison = saison_actuelle(db, jeu_id)
        ligues = []
        for n, config in LIGUES.items():
            poules = []
            for l in lettres_poules(n):
                places = []
                for p in classement_poule(db, jeu_id, n, l):
                    if p['team_id']:
                        places.append({k: p[k] for k in ('rang', 'team_name', 'points', 'joues', 'victoires',
                                                          'defaites', 'diff', 'statut')})
                    else:
                        places.append({'rang': p['rang'], 'team_name': None})
                poules.append({'nom': l, 'teams': places})
            ligues.append({'ligue_num': n, 'champion': config['champion'], 'poules': poules})
        teams = [dict(r) for r in db.execute("""
            SELECT t.nom, t.bio, l.ligue_num, l.poule, l.points,
                   (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id) AS nb_membres
            FROM teams t JOIN ligue l ON l.team_id = t.id
            WHERE t.jeu_id = ?
            ORDER BY l.ligue_num, l.points DESC, t.nom COLLATE NOCASE""", (jeu_id,))]
        resultats = [{k: r[k] for k in ('ligue_num', 'poule', 'team1_nom', 'team2_nom', 'score1', 'score2', 'created_at')}
                     for r in db.execute(REQ_MATCHS + ' WHERE m.jeu_id = ? AND m.saison = ? ORDER BY m.id DESC LIMIT 50',
                                         (jeu_id, saison))]
        donnees = {
            'genere_le': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'saison': saison,
            'chiffres': {
                'teams': len(teams),
                'joueurs': db.execute('SELECT COUNT(*) FROM team_members tm JOIN ligue l ON l.team_id = tm.team_id '
                                      'WHERE tm.jeu_id = ?', (jeu_id,)).fetchone()[0],
                'matchs': db.execute('SELECT COUNT(*) FROM matchs WHERE jeu_id = ? AND saison = ?', (jeu_id, saison)).fetchone()[0],
            },
            'regles': {'points_victoire': reglage('points_victoire', db), 'max_membres': MAX_MEMBRES, 'penalite_max': reglage('penalite_max', db),
                       'teams_par_poule': TEAMS_PAR_POULE, 'ligue_depart': LIGUE_DEPART},
            'ligues': ligues,
            'teams': teams,
            'resultats': resultats,
            'palmares': [p for p in palmares(db, jeu_id) if p['champion']],
        }
    os.makedirs(os.path.join(SITE_PUBLIC_DIR, 'data'), exist_ok=True)
    os.makedirs(os.path.join(SITE_PUBLIC_DIR, 'assets'), exist_ok=True)
    chemin = os.path.join(SITE_PUBLIC_DIR, 'data', 'ligue.json')
    with open(chemin, 'w', encoding='utf-8') as f:
        json.dump(donnees, f, ensure_ascii=False, indent=1)
    shutil.copyfile(os.path.join(BASE_DIR, 'static', 'style.css'), os.path.join(SITE_PUBLIC_DIR, 'assets', 'style.css'))
    for nom in ('favicon.png', 'logo-xhq.png', 'logo-xhq-clair.png'):
        shutil.copyfile(os.path.join(BASE_DIR, 'static', nom), os.path.join(SITE_PUBLIC_DIR, 'assets', nom))
    return chemin, donnees


@app.route('/api/admin/exporter', methods=['POST'])
@api_admin
def api_admin_exporter():
    chemin, donnees = exporter_site_public()
    return jsonify({'success': True, 'fichier': os.path.relpath(chemin, BASE_DIR),
                    'teams': donnees['chiffres']['teams'], 'matchs': donnees['chiffres']['matchs']}), 200


def promouvoir_admin(email):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    u = c.execute('SELECT id FROM users WHERE lower(email) = lower(?)', (email.strip(),)).fetchone()
    if u:
        c.execute('UPDATE users SET is_admin = 1 WHERE id = ?', (u['id'],))
        donner_objet(c, u['id'], 'admin', equiper=False)
        ecrire_journal(c, None, 'console', 'Droits admin donnés', f"{email.strip()} (#{u['id']})")
    conn.commit()
    conn.close()
    return u is not None


if __name__ == '__main__':
    # python app.py admin email@exemple.fr  -> donne les droits admin à ce compte
    if len(sys.argv) >= 2 and sys.argv[1] == 'admin':
        if len(sys.argv) != 3:
            print('Usage : python app.py admin email@exemple.fr')
            sys.exit(1)
        if promouvoir_admin(sys.argv[2]):
            print(f'OK : {sys.argv[2]} est maintenant admin. Va sur http://localhost:5000/admin')
        else:
            print(f"Erreur : aucun compte avec l'email {sys.argv[2]}. Inscris-toi d'abord sur le site.")
            sys.exit(1)
    elif len(sys.argv) >= 2 and sys.argv[1] == 'discord':
        # python app.py discord  -> teste l'envoi vers le Discord relié et affiche l'erreur exacte (jamais l'adresse)
        import json
        import urllib.error
        import urllib.request
        conn = sqlite3.connect(DB_PATH)
        w = conn.execute("SELECT valeur FROM parametres WHERE cle = 'discord_webhook'").fetchone()
        auto = dict(conn.execute("SELECT cle, valeur FROM parametres WHERE cle LIKE 'discord_auto_%'").fetchall())
        attente = conn.execute('SELECT COUNT(*) FROM discord_file WHERE envoye_at IS NULL').fetchone()[0]
        conn.close()
        print('1) Webhook enregistré :', 'oui' if w and w[0] else 'NON -> Admin → Annonces → Relier le Discord')
        if w and w[0]:
            print('   Adresse au bon format :', 'oui' if DISCORD_WEBHOOK_REGEX.match(w[0]) else 'NON (ce n\'est pas une adresse de webhook)')
        print('2) Envois automatiques cochés :', ', '.join(k.replace('discord_auto_', '') for k, v in auto.items() if v == '1') or 'aucun')
        print('   Messages en attente :', attente)
        print('3) Proxy du serveur :', 'oui' if (os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY')) else 'non')
        if w and w[0]:
            corps = json.dumps({'content': '🔧 Test depuis la console du serveur (XHQ League)', 'allowed_mentions': {'parse': []}}).encode()
            req = urllib.request.Request(w[0], data=corps, headers={'Content-Type': 'application/json', 'User-Agent': UA_DISCORD})
            try:
                with urllib.request.urlopen(req, timeout=15) as rep:
                    print('4) Envoi : OK, code', rep.status, '-> regarde ton salon Discord')
            except urllib.error.HTTPError as e:
                print('4) Envoi refusé par Discord : code', e.code, '-', (e.read() or b'')[:300].decode('utf-8', 'replace'))
            except Exception as e:
                print('4) Envoi impossible :', e.__class__.__name__, '-', str(e)[:300])
    elif len(sys.argv) >= 2 and sys.argv[1] == 'debloquer':
        # python app.py debloquer email@exemple.fr  -> lève le blocage (7 jours ou définitif) d'un compte, même admin
        if len(sys.argv) != 3:
            print('Usage : python app.py debloquer email@exemple.fr')
            sys.exit(1)
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("""UPDATE users SET verrou_definitif = 0, verrou_niveau = 0, echecs_connexion = 0, bloque_jusqua = NULL
                            WHERE lower(email) = lower(?)""", (sys.argv[2].strip(),)).rowcount
        conn.row_factory = sqlite3.Row
        ecrire_journal(conn.cursor(), None, 'console', 'Compte déverrouillé', sys.argv[2])
        conn.commit()
        conn.close()
        print(f'OK : {sys.argv[2]} est débloqué.' if n else f"Erreur : aucun compte avec l'email {sys.argv[2]}")
    elif len(sys.argv) >= 2 and sys.argv[1] == 'sauvegarde':
        # python app.py sauvegarde  -> crée sauvegardes/xhq-AAAAMMJJ-HHMMSS.zip (à lancer chaque jour)
        print('OK : sauvegarde créée :', os.path.relpath(creer_sauvegarde(), BASE_DIR))
    elif len(sys.argv) >= 2 and sys.argv[1] == 'export':
        # python app.py export  -> met à jour les données du site public (dossier docs/)
        chemin, donnees = exporter_site_public()
        print(f"OK : {os.path.relpath(chemin, BASE_DIR)} mis à jour ({donnees['chiffres']['teams']} teams, "
              f"{donnees['chiffres']['matchs']} matchs). Envoie-le en ligne avec : git add docs && git commit -m \"Mise à jour\" && git push")
    else:
        # Serveur de test local (http://localhost) : cookie sans HTTPS et limites coupées
        app.config['SESSION_COOKIE_SECURE'] = False
        LIMITES_ACTIVES = False
        app.run(debug=os.environ.get('XHQ_DEBUG') == '1')
