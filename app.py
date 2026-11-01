import os
import secrets
import sys
import sqlite3
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, g, abort, has_request_context
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'tournois.db')
SECRET_KEY_PATH = os.path.join(BASE_DIR, 'secret_key.txt')


def charger_secret_key():
    """Clé qui signe les sessions. Elle ne doit JAMAIS être dans le code (le dépôt est public).
    1) variable d'environnement XHQ_SECRET_KEY si elle existe (pour la mise en ligne)
    2) sinon le fichier secret_key.txt (créé automatiquement, ignoré par Git)"""
    cle = os.environ.get('XHQ_SECRET_KEY', '').strip()
    if cle:
        return cle
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, encoding='utf-8') as f:
            cle = f.read().strip()
        if cle:
            return cle
    cle = secrets.token_hex(32)
    with open(SECRET_KEY_PATH, 'w', encoding='utf-8') as f:
        f.write(cle)
    return cle


app = Flask(__name__)
app.secret_key = charger_secret_key()

AVATARS = ['👤', '🎮', '🏆', '⚡', '🔥', '💎', '👑', '🚀', '⭐', '🎯', '💪', '🌟']
# Avatars exclusifs débloqués par le Skin Premium
AVATARS_PREMIUM = ['🐉', '🦄', '🤖', '👽']
AVATAR_DEFAUT = '👤'

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

# ---------------------------------------------------------------------------
# Jeux : chaque jeu a ses teams, ses ligues, ses saisons et son palmarès.
# Pour ajouter un jeu plus tard : une ligne ici (code, nom, format), puis redémarrer.
# ---------------------------------------------------------------------------
JEUX_DEFAUT = [
    ('brawlstars', 'Brawl Stars', '3v3'),
]
JEU_DEFAUT = 'brawlstars'

# Tout le monde démarre à 0 €
SOLDE_DEPART = 0

# Objets exclusifs, gratuits et purement cosmétiques : (code, nom, description, prix, type)
SHOP_DEFAUT = [
    ('fondateur', 'Badge Fondateur', "Réservé aux 100 premiers inscrits. Une fois les 100 places prises, il ne revient pas.", 0, 'badge'),
    ('saison1', 'Badge Saison 1', "Disponible pendant la saison 1 uniquement. À la fin de la saison, il disparaît du shop.", 0, 'badge'),
    ('cadre_or', 'Cadre Or', "Contour or autour de ton avatar : profil, menu et liste des membres de ta team.", 0, 'cadre'),
    ('cadre_carbone', 'Cadre Carbone', "Contour noir mat avec liseré, plus discret que l'or.", 0, 'cadre'),
    ('pack_rare', 'Avatars rares', "Quatre avatars en plus dans le sélecteur du profil, introuvables autrement.", 0, 'avatars'),
]
FONDATEURS_MAX = 100

# Nombre maximum de joueurs par team (3 titulaires + 2 remplaçants en 3v3)
MAX_MEMBRES = 5

# Barème des matchs (pas de match nul : un BO3 a toujours un gagnant)
POINTS_VICTOIRE = 3
POINTS_DEFAITE = 0
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
    c.execute('DELETE FROM ligue WHERE team_id = ?', (team_id,))
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
                  WHERE COALESCE(hors_classement, 0) = 0 AND id NOT IN (SELECT team_id FROM ligue)""")
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
    ajouter_colonne(c, 'saison_classements', 'penalite', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'saison_classements', 'matchs_manques', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'saison_classements', 'points_avant', 'INTEGER')
    ajouter_colonne(c, 'saison_classements', 'recidive', 'INTEGER DEFAULT 0')
    ajouter_colonne(c, 'shop_items', 'code', 'TEXT')
    ajouter_colonne(c, 'users', 'badge_equipe', 'TEXT')
    ajouter_colonne(c, 'users', 'cadre_equipe', 'TEXT')
    c.execute('CREATE TABLE IF NOT EXISTS parametres (cle TEXT PRIMARY KEY, valeur TEXT)')
    migrer_multi_jeux(c)
    c.execute("UPDATE users SET created_at = datetime('now') WHERE created_at IS NULL")
    c.execute("UPDATE teams SET created_at = datetime('now') WHERE created_at IS NULL")
    tous_avatars = AVATARS + AVATARS_PREMIUM
    c.execute("UPDATE users SET profile_pic = ? WHERE profile_pic IS NULL OR profile_pic NOT IN (%s)"
              % ','.join('?' * len(tous_avatars)), (AVATAR_DEFAUT, *tous_avatars))

    # Objets exclusifs gratuits
    for code, nom, description, prix, type_ in SHOP_DEFAUT:
        if c.execute('SELECT 1 FROM shop_items WHERE code = ?', (code,)).fetchone():
            c.execute('UPDATE shop_items SET nom = ?, description = ?, prix = ?, type = ? WHERE code = ?',
                      (nom, description, prix, type_, code))
        else:
            c.execute('INSERT INTO shop_items (code, nom, description, prix, type) VALUES (?, ?, ?, ?, ?)',
                      (code, nom, description, prix, type_))

    # Migration unique : fin du boost et de l'argent de départ
    if not c.execute("SELECT 1 FROM parametres WHERE cle = 'shop_v2'").fetchone():
        # Les anciens « Skin Premium » gardent leurs avantages (cadre or + avatars rares)
        anciens_skins = [r[0] for r in c.execute(
            "SELECT DISTINCT i.user_id FROM inventory i JOIN shop_items s ON s.id = i.item_id WHERE s.nom = 'Skin Premium'")]
        nouveaux = {code: c.execute('SELECT id FROM shop_items WHERE code = ?', (code,)).fetchone()[0]
                    for code in ('cadre_or', 'pack_rare')}
        for uid in anciens_skins:
            for code, item_id in nouveaux.items():
                c.execute("INSERT INTO inventory (user_id, item_id, bought_at) VALUES (?, ?, datetime('now'))", (uid, item_id))
            c.execute("UPDATE users SET cadre_equipe = 'cadre_or' WHERE id = ?", (uid,))
        c.execute('DELETE FROM inventory WHERE item_id IN (SELECT id FROM shop_items WHERE code IS NULL)')
        c.execute('DELETE FROM shop_items WHERE code IS NULL')   # Badge Officiel, Skin Premium, Boost 2x
        c.execute('DROP TABLE IF EXISTS boosts')
        c.execute('UPDATE users SET solde = ?', (SOLDE_DEPART,))
        c.execute("INSERT INTO parametres (cle, valeur) VALUES ('shop_v2', datetime('now'))")

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
        g.user = get_db().execute('SELECT id, username, email, profile_pic, is_admin FROM users WHERE id = ?', (uid,)).fetchone()
        if g.user is None:  # compte supprimé entre-temps
            session.clear()
    return g.user


def liste_jeux(db):
    return [dict(r) for r in db.execute('SELECT id, code, nom, format FROM jeux ORDER BY ordre, id')]


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
    cadre = u['cadre_equipe'] if u and u['cadre_equipe'] and possede(db, user_id, u['cadre_equipe']) else None
    nom_badge = None
    if badge:
        nom_badge = db.execute('SELECT nom FROM shop_items WHERE code = ?', (badge,)).fetchone()['nom'].replace('Badge ', '')
    return {'badge': nom_badge, 'badge_code': badge, 'cadre': cadre}


def disponibilite(db, user_id, code):
    """(disponible, raison) pour récupérer un objet."""
    if code == 'fondateur':
        rang = db.execute('SELECT COUNT(*) FROM users WHERE id <= ?', (user_id,)).fetchone()[0]
        if rang > FONDATEURS_MAX:
            return False, f'Réservé aux {FONDATEURS_MAX} premiers inscrits'
    if code == 'saison1' and saison_actuelle(db, jeu_defaut_id(db)) != 1:
        return False, 'Uniquement pendant la saison 1'
    return True, None


@app.context_processor
def injecter_utilisateur():
    user = utilisateur_connecte()
    current_user = None
    if user:
        current_user = {'username': user['username'], 'email': user['email'], 'logo': logo_valide(user['profile_pic']),
                        'is_admin': bool(user['is_admin']),
                        **apparence(get_db(), user['id'])}
    db = get_db()
    jeux = liste_jeux(db)
    jeu = None
    try:
        jeu = trouver_jeu(db)
    except Exception:
        pass
    return {'current_user': current_user, 'jeux': jeux, 'jeu': jeu or (jeux[0] if jeux else None)}


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


def api_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not utilisateur_connecte():
            return jsonify({'success': False, 'error': 'Non connecté'}), 401
        if not est_admin():
            return jsonify({'success': False, 'error': 'Réservé aux admins'}), 403
        return f(*args, **kwargs)
    return wrapper


def donnees_json():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def texte(data, cle, max_len=None):
    valeur = data.get(cle)
    valeur = valeur.strip() if isinstance(valeur, str) else ''
    return valeur[:max_len] if max_len else valeur


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
    }


@app.route('/')
def index():
    db = get_db()
    cartes = [{**j, **chiffres_jeu(db, j['id'])} for j in liste_jeux(db)]
    return render_template('index.html', cartes=cartes, nb_ligues=LIGUE_MAX, par_poule=TEAMS_PAR_POULE,
                           joueurs_total=db.execute('SELECT COUNT(*) FROM users').fetchone()[0])


@app.route('/login')
def login_page():
    if utilisateur_connecte():
        return redirect('/')
    return render_template('login.html')


@app.route('/signup')
def signup_page():
    if utilisateur_connecte():
        return redirect('/')
    return render_template('signup.html')


@app.route('/profil')
@page_connectee
def profil_page():
    return render_template('profil.html', avatars=AVATARS, avatars_premium=AVATARS_PREMIUM,
                           avatars_rares=possede(get_db(), session['user_id'], 'pack_rare'))


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


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ---------------------------------------------------------------------------
# API : comptes
# ---------------------------------------------------------------------------
@app.route('/api/signup', methods=['POST'])
def api_signup():
    data = donnees_json()
    email = texte(data, 'email').lower()
    username = texte(data, 'username')
    password = data.get('password') if isinstance(data.get('password'), str) else ''

    if not email or not username or not password:
        return jsonify({'success': False, 'error': 'Tous les champs sont requis'}), 400
    if '@' not in email or len(email) > 120:
        return jsonify({'success': False, 'error': 'Email invalide'}), 400
    if not 3 <= len(username) <= 20:
        return jsonify({'success': False, 'error': "Le nom d'utilisateur doit faire entre 3 et 20 caractères"}), 400
    if len(password) < 6:
        return jsonify({'success': False, 'error': 'Le mot de passe doit faire au moins 6 caractères'}), 400

    db = get_db()
    if db.execute('SELECT 1 FROM users WHERE lower(email) = ?', (email,)).fetchone():
        return jsonify({'success': False, 'error': 'Cet email est déjà utilisé'}), 400
    if db.execute('SELECT 1 FROM users WHERE lower(username) = lower(?)', (username,)).fetchone():
        return jsonify({'success': False, 'error': "Ce nom d'utilisateur est déjà pris"}), 400
    try:
        cur = db.execute(
            "INSERT INTO users (email, username, password, bio, profile_pic, solde, created_at) "
            "VALUES (?, ?, ?, '', ?, ?, datetime('now'))",
            (email, username, generate_password_hash(password), AVATAR_DEFAUT, SOLDE_DEPART))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Email ou nom d\'utilisateur déjà pris'}), 400

    session.clear()
    session['user_id'] = cur.lastrowid
    return jsonify({'success': True}), 201


@app.route('/api/login', methods=['POST'])
def api_login():
    data = donnees_json()
    email = texte(data, 'email').lower()
    password = data.get('password') if isinstance(data.get('password'), str) else ''

    user = get_db().execute('SELECT id, password FROM users WHERE lower(email) = ?', (email,)).fetchone()
    if user and password and check_password_hash(user['password'], password):
        session.clear()
        session['user_id'] = user['id']
        return jsonify({'success': True}), 200
    return jsonify({'success': False, 'error': 'Email ou mot de passe incorrect'}), 401


@app.route('/api/profil', methods=['GET'])
@api_connectee
def api_profil():
    db = get_db()
    user = db.execute('SELECT id, email, username, bio, profile_pic, solde, created_at FROM users WHERE id = ?',
                      (session['user_id'],)).fetchone()
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
        **inventaire(get_db(), user['id']),
    }), 200


def inventaire(db, user_id):
    objets = [dict(r) for r in db.execute("""
        SELECT s.code, s.nom, s.type FROM inventory i JOIN shop_items s ON s.id = i.item_id
        WHERE i.user_id = ? AND s.code IS NOT NULL ORDER BY i.id""", (user_id,))]
    return {'objets': objets, 'avatars_rares': possede(db, user_id, 'pack_rare'), **apparence(db, user_id)}


@app.route('/api/profil/equiper', methods=['POST'])
@api_connectee
def api_profil_equiper():
    data = donnees_json()
    type_ = data.get('type')
    code = data.get('code')
    if type_ not in ('badge', 'cadre'):
        return jsonify({'success': False, 'error': 'Type invalide'}), 400
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
    bio = bio.strip()[:500]

    logo = data.get('logo', user['profile_pic'])
    if logo in AVATARS_PREMIUM and not possede(db, session['user_id'], 'pack_rare'):
        return jsonify({'success': False, 'error': 'Cet avatar se débloque avec « Avatars rares » (shop)'}), 403
    if logo not in AVATARS and logo not in AVATARS_PREMIUM:
        return jsonify({'success': False, 'error': 'Logo invalide'}), 400

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
    user = db.execute('SELECT password FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not ancien or not check_password_hash(user['password'], ancien):
        return jsonify({'success': False, 'error': 'Mot de passe actuel incorrect'}), 400
    if len(nouveau) < 6:
        return jsonify({'success': False, 'error': 'Le nouveau mot de passe doit faire au moins 6 caractères'}), 400
    db.execute('UPDATE users SET password = ? WHERE id = ?', (generate_password_hash(nouveau), session['user_id']))
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/profil/delete', methods=['POST'])
@api_connectee
def api_profil_delete():
    uid = session['user_id']
    db = get_db()
    c = db.cursor()
    for j in liste_jeux(db):
        retirer_membre(c, uid, j['id'])
    c.execute('DELETE FROM inventory WHERE user_id = ?', (uid,))
    c.execute('UPDATE contact_messages SET user_id = NULL WHERE user_id = ?', (uid,))
    c.execute('DELETE FROM users WHERE id = ?', (uid,))
    db.commit()
    session.clear()
    return jsonify({'success': True}), 200


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
        SELECT t.id, t.nom, t.bio, t.createur_id, t.created_at, u.username AS createur,
               l.ligue_num, l.poule, l.points,
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
        team['is_creator'] = team['createur_id'] == uid
        team['max_membres'] = MAX_MEMBRES
        team['pleine'] = team['nb_membres'] >= MAX_MEMBRES
        teams_list.append(team)

    mine = db.execute('SELECT team_id FROM team_members WHERE user_id = ? AND jeu_id = ?', (uid, jeu['id'])).fetchone()
    user_team_id = mine['team_id'] if mine else None
    saison = saison_actuelle(db, jeu['id'])
    membres = []
    if user_team_id:
        createur_id = db.execute('SELECT createur_id FROM teams WHERE id = ?', (user_team_id,)).fetchone()['createur_id']
        membres = [dict(m) for m in db.execute("""
            SELECT u.id, u.username, u.profile_pic AS logo FROM team_members tm JOIN users u ON u.id = tm.user_id
            WHERE tm.team_id = ? ORDER BY tm.joined_at, tm.id""", (user_team_id,))]
        for m in membres:
            m['logo'] = logo_valide(m['logo'])
            m['is_capitaine'] = m['id'] == createur_id
            m['is_me'] = m['id'] == uid
            m.update(apparence(db, m['id']))
    matchs = matchs_de_team(db, user_team_id, limite=10) if user_team_id else []
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
                    'teams': teams_list, 'user_team_id': user_team_id, 'user_id': uid, 'membres': membres,
                    'matchs': matchs, 'max_membres': MAX_MEMBRES, 'participation': participation,
                    'derniere_penalite': derniere_penalite,
                    'regle_penalite': {'par_match': PENALITE_PAR_MATCH, 'recidive': MULTIPLICATEUR_RECIDIVE,
                                       'max': PENALITE_MAX}}), 200


@app.route('/api/teams', methods=['POST'])
@api_connectee
def api_create_team():
    uid = session['user_id']
    data = donnees_json()
    nom = texte(data, 'nom')
    bio = texte(data, 'bio', 300)
    if not 2 <= len(nom) <= 30:
        return jsonify({'success': False, 'error': 'Le nom de la team doit faire entre 2 et 30 caractères'}), 400

    db = get_db()
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
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
        c.execute('INSERT INTO ligue (team_id, ligue_num, points, poule) VALUES (?, ?, 0, ?)',
                  (team_id, LIGUE_DEPART, choisir_poule(c, jeu['id'], LIGUE_DEPART)))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({'success': False, 'error': 'Impossible de créer la team'}), 400
    return jsonify({'success': True, 'team_id': team_id}), 201


@app.route('/api/teams/<int:team_id>/join', methods=['POST'])
@api_connectee
def api_join_team(team_id):
    uid = session['user_id']
    db = get_db()
    jeu_id = jeu_de_team(db, team_id)
    if jeu_id is None:
        return jsonify({'success': False, 'error': 'Team introuvable'}), 404
    if db.execute('SELECT 1 FROM team_members WHERE user_id = ? AND jeu_id = ?', (uid, jeu_id)).fetchone():
        return jsonify({'success': False, 'error': 'Vous êtes déjà dans une team pour ce jeu'}), 400
    if db.execute('SELECT COUNT(*) FROM team_members WHERE team_id = ?', (team_id,)).fetchone()[0] >= MAX_MEMBRES:
        return jsonify({'success': False, 'error': f'Cette team est complète ({MAX_MEMBRES} joueurs maximum)'}), 400
    try:
        db.execute("INSERT INTO team_members (team_id, user_id, jeu_id, joined_at) VALUES (?, ?, ?, datetime('now'))",
                   (team_id, uid, jeu_id))
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
    supprimer_team(db.cursor(), team_id)
    db.commit()
    return jsonify({'success': True}), 200


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
    poule = choisir_poule(c, jeu_de_team(db, team_id), LIGUE_DEPART)
    c.execute('INSERT INTO ligue (team_id, ligue_num, points, poule) VALUES (?, ?, 0, ?)', (team_id, LIGUE_DEPART, poule))
    c.execute('UPDATE teams SET hors_classement = 0 WHERE id = ?', (team_id,))
    db.commit()
    return jsonify({'success': True, 'ligue_num': LIGUE_DEPART, 'poule': poule}), 200


@app.route('/api/teams/<int:team_id>/update', methods=['POST'])
@api_connectee
def api_update_team(team_id):
    db = get_db()
    team, erreur = team_du_capitaine(db, team_id)
    if erreur:
        return erreur
    data = donnees_json()
    nom = texte(data, 'nom') if 'nom' in data else team['nom']
    if not 2 <= len(nom) <= 30:
        return jsonify({'success': False, 'error': 'Le nom de la team doit faire entre 2 et 30 caractères'}), 400
    if db.execute('SELECT 1 FROM teams WHERE lower(nom) = lower(?) AND id != ?', (nom, team_id)).fetchone():
        return jsonify({'success': False, 'error': 'Ce nom de team est déjà pris'}), 400
    actuelle = db.execute('SELECT bio FROM teams WHERE id = ?', (team_id,)).fetchone()['bio'] or ''
    bio = texte(data, 'bio', 300) if 'bio' in data else actuelle
    db.execute('UPDATE teams SET nom = ?, bio = ? WHERE id = ?', (nom, bio, team_id))
    db.commit()
    return jsonify({'success': True, 'nom': nom, 'bio': bio}), 200


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
    penalite = manques * PENALITE_PAR_MATCH
    recidive = deja_penalisee(db, team_id, saison - 1)
    if recidive:
        penalite *= MULTIPLICATEUR_RECIDIVE
    return min(penalite, PENALITE_MAX), manques, recidive


def classement_poule(db, jeu_id, ligue_num, poule, penalites=False):
    """Classement d'une poule. Avec penalites=True (fin de saison), les points des matchs non joués
    sont retirés avant de classer, et les teams sans aucun match passent en dernier."""
    config = LIGUES[ligue_num]
    saison = saison_actuelle(db, jeu_id)
    rows = db.execute("""
        SELECT t.id AS team_id, t.nom AS team_name, l.points, l.id AS ligue_id
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
    nb_users = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    items = []
    for r in db.execute('SELECT id, code, nom, description, prix, type FROM shop_items WHERE code IS NOT NULL ORDER BY id'):
        item = dict(r)
        item['possede'] = possede(db, uid, item['code'])
        item['disponible'], item['raison'] = disponibilite(db, uid, item['code'])
        item['equipe'] = item['code'] in (equipe['badge_code'], equipe['cadre'])
        if item['code'] == 'fondateur':
            item['places_restantes'] = max(0, FONDATEURS_MAX - nb_users)
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
    if possede(db, uid, item['code']):
        return jsonify({'success': False, 'error': 'Tu as déjà cet objet'}), 400
    dispo, raison = disponibilite(db, uid, item['code'])
    if not dispo:
        return jsonify({'success': False, 'error': raison}), 400
    db.execute("INSERT INTO inventory (user_id, item_id, bought_at) VALUES (?, ?, datetime('now'))", (uid, item_id))
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
    if '@' not in email:
        return jsonify({'success': False, 'error': 'Email invalide'}), 400
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
    return render_template('admin.html', ligues=LIGUES, points_victoire=POINTS_VICTOIRE, points_defaite=POINTS_DEFAITE,
                           ligue_depart=LIGUE_DEPART)


def entier(valeur):
    if isinstance(valeur, bool):
        return None
    if isinstance(valeur, int):
        return valeur
    if isinstance(valeur, str) and valeur.strip().lstrip('-').isdigit():
        return int(valeur.strip())
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
    return jsonify({
        'jeu': jeu,
        'joueurs': compter('SELECT COUNT(*) FROM users'),
        'teams': compter('SELECT COUNT(*) FROM teams WHERE jeu_id = ?', jeu['id']),
        'matchs': compter('SELECT COUNT(*) FROM matchs WHERE jeu_id = ? AND saison = ?', jeu['id'], saison),
        'saison': saison,
        'messages_non_traites': compter('SELECT COUNT(*) FROM contact_messages WHERE traite = 0 OR traite IS NULL'),
    }), 200


@app.route('/api/admin/matchs', methods=['POST'])
@api_admin
def api_admin_create_match():
    data = donnees_json()
    team1_id, team2_id = entier(data.get('team1_id')), entier(data.get('team2_id'))
    score1, score2 = entier(data.get('score1')), entier(data.get('score2'))
    if team1_id is None or team2_id is None:
        return jsonify({'success': False, 'error': 'Choisissez les deux teams'}), 400
    if team1_id == team2_id:
        return jsonify({'success': False, 'error': 'Une team ne peut pas jouer contre elle-même'}), 400
    if score1 is None or score2 is None or not (0 <= score1 <= SCORE_MAX and 0 <= score2 <= SCORE_MAX):
        return jsonify({'success': False, 'error': f'Les scores doivent être des nombres entre 0 et {SCORE_MAX}'}), 400
    if score1 == score2:
        return jsonify({'success': False, 'error': "Pas de match nul : il faut un gagnant"}), 400

    db = get_db()
    c = db.cursor()
    t1 = c.execute('SELECT t.nom, t.jeu_id, l.ligue_num, l.poule FROM teams t JOIN ligue l ON l.team_id = t.id '
                   'WHERE t.id = ?', (team1_id,)).fetchone()
    t2 = c.execute('SELECT t.nom, t.jeu_id, l.ligue_num, l.poule FROM teams t JOIN ligue l ON l.team_id = t.id '
                   'WHERE t.id = ?', (team2_id,)).fetchone()
    if not t1 or not t2:
        return jsonify({'success': False, 'error': 'Team introuvable'}), 404
    if t1['jeu_id'] != t2['jeu_id']:
        return jsonify({'success': False, 'error': 'Les deux teams ne jouent pas au même jeu'}), 400
    if (t1['ligue_num'], t1['poule']) != (t2['ligue_num'], t2['poule']):
        return jsonify({'success': False,
                        'error': 'Les deux teams doivent être dans la même ligue et la même poule'}), 400

    ligue_num, poule, jeu_id = t1['ligue_num'], t1['poule'], t1['jeu_id']
    points1 = POINTS_VICTOIRE if score1 > score2 else POINTS_DEFAITE
    points2 = POINTS_VICTOIRE if score2 > score1 else POINTS_DEFAITE
    c.execute("""INSERT INTO matchs (ligue_num, poule, team1_id, team2_id, team1_nom, team2_nom,
                                     score1, score2, points1, points2, saisi_par, created_at, saison, jeu_id)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)""",
              (ligue_num, poule, team1_id, team2_id, t1['nom'], t2['nom'],
               score1, score2, points1, points2, session['user_id'], saison_actuelle(db, jeu_id), jeu_id))
    match_id = c.lastrowid
    ajuster_points(c, team1_id, ligue_num, poule, points1)
    ajuster_points(c, team2_id, ligue_num, poule, points2)
    db.commit()
    gagnant = t1['nom'] if score1 > score2 else t2['nom']
    return jsonify({'success': True, 'match_id': match_id, 'gagnant': gagnant,
                    'ligue_num': ligue_num, 'poule': poule}), 201


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
    ajuster_points(c, m['team1_id'], m['ligue_num'], m['poule'], -m['points1'])
    ajuster_points(c, m['team2_id'], m['ligue_num'], m['poule'], -m['points2'])
    c.execute('DELETE FROM matchs WHERE id = ?', (match_id,))
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
        SELECT t.id, t.nom, u.username AS createur, l.ligue_num, l.poule, l.points,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id) AS nb_membres
        FROM teams t
        LEFT JOIN users u ON u.id = t.createur_id
        LEFT JOIN ligue l ON l.team_id = t.id
        WHERE t.jeu_id = ?
        ORDER BY l.ligue_num, l.poule, t.nom COLLATE NOCASE""", (jeu['id'],)).fetchall()
    return jsonify([dict(r) for r in rows]), 200


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
        c.execute('UPDATE teams SET hors_classement = 0 WHERE id = ?', (team_id,))
        db.commit()
        poule = c.execute('SELECT poule FROM ligue WHERE team_id = ?', (team_id,)).fetchone()['poule']
        return jsonify({'success': True, 'ligue_num': ligue_num, 'poule': poule}), 200
    if actuelle['ligue_num'] == ligue_num:
        return jsonify({'success': False, 'error': f'La team est déjà en Ligue {ligue_num}'}), 400
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
    supprimer_team(db.cursor(), team_id)
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/users', methods=['GET'])
@api_admin
def api_admin_users():
    q = request.args.get('q', '').strip().lower()[:100]
    rows = get_db().execute("""
        SELECT u.id, u.username, u.email, u.solde, u.is_admin, u.created_at,
               (SELECT GROUP_CONCAT(t.nom, ', ') FROM team_members tm JOIN teams t ON t.id = tm.team_id
                WHERE tm.user_id = u.id) AS team_name
        FROM users u
        WHERE lower(u.username) LIKE ? OR lower(u.email) LIKE ?
        ORDER BY u.id DESC LIMIT 50""", (f'%{q}%', f'%{q}%')).fetchall()
    users = []
    for r in rows:
        u = dict(r)
        u['is_admin'] = bool(u['is_admin'])
        u['is_me'] = u['id'] == session['user_id']
        users.append(u)
    return jsonify(users), 200


@app.route('/api/admin/users/<int:user_id>/reset-password', methods=['POST'])
@api_admin
def api_admin_reset_password(user_id):
    db = get_db()
    user = db.execute('SELECT username, email FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'success': False, 'error': 'Joueur introuvable'}), 404
    temporaire = secrets.token_urlsafe(8)
    db.execute('UPDATE users SET password = ? WHERE id = ?', (generate_password_hash(temporaire), user_id))
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
    cur = db.execute('UPDATE users SET is_admin = ? WHERE id = ?', (1 if valeur else 0, user_id))
    if cur.rowcount == 0:
        return jsonify({'success': False, 'error': 'Joueur introuvable'}), 404
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/messages', methods=['GET'])
@api_admin
def api_admin_messages():
    rows = get_db().execute("""
        SELECT m.id, m.nom, m.email, m.sujet, m.message, m.created_at, COALESCE(m.traite, 0) AS traite,
               (SELECT u.id FROM users u WHERE lower(u.email) = lower(m.email)) AS compte_id
        FROM contact_messages m
        ORDER BY COALESCE(m.traite, 0) ASC, m.id DESC LIMIT 200""").fetchall()
    messages = []
    for r in rows:
        m = dict(r)
        m['traite'] = bool(m['traite'])
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


def calculer_fin_saison(db, jeu_id):
    """Classement final de chaque poule + destination de chaque team (sans rien modifier)."""
    resultats = []
    for ligue_num in LIGUES:
        for poule in lettres_poules(ligue_num):
            for place in classement_poule(db, jeu_id, ligue_num, poule, penalites=True):
                if not place['team_id']:
                    continue
                destination, mouvement = ligue_num, place['statut'] or 'reste'
                if place['joues'] == 0:
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
    return jsonify({
        'jeu': jeu,
        'saison': saison,
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
    jeu, erreur = jeu_ou_erreur(db)
    if erreur:
        return erreur
    c = db.cursor()
    saison = saison_actuelle(db, jeu['id'])
    if not c.execute('SELECT 1 FROM matchs WHERE jeu_id = ? AND saison = ?', (jeu['id'], saison)).fetchone():
        return jsonify({'success': False, 'error': 'Aucun match joué cette saison : rien à clôturer'}), 400

    classement = calculer_fin_saison(db, jeu['id'])
    try:
        c.execute("INSERT INTO saisons (jeu_id, numero, cloturee_at, cloturee_par) VALUES (?, ?, datetime('now'), ?)",
                  (jeu['id'], saison, session['user_id']))
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
        # 3) nouvelle saison : toutes les teams de ce jeu repartent à 0
        c.execute('UPDATE ligue SET points = 0 WHERE team_id IN (SELECT id FROM teams WHERE jeu_id = ?)', (jeu['id'],))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({'success': False, 'error': 'Cette saison a déjà été clôturée'}), 400

    return jsonify({
        'success': True,
        'jeu': jeu,
        'saison_cloturee': saison,
        'nouvelle_saison': saison + 1,
        'montees': sum(p['mouvement'] in ('monte', 'repechage') for p in partants),
        'descentes': sum(p['mouvement'] == 'descend' for p in partants),
        'repechages': sum(p['mouvement'] == 'repechage' for p in partants),
        'retirees': len(retirees),
        'penalisees': sum(p['penalite'] > 0 for p in classement),
        'champion': next((p['team_name'] for p in classement if p['mouvement'] == 'champion'), None),
    }), 200


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
            'regles': {'points_victoire': POINTS_VICTOIRE, 'max_membres': MAX_MEMBRES, 'penalite_max': PENALITE_MAX,
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
    cur = conn.execute('UPDATE users SET is_admin = 1 WHERE lower(email) = lower(?)', (email.strip(),))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


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
    elif len(sys.argv) >= 2 and sys.argv[1] == 'export':
        # python app.py export  -> met à jour les données du site public (dossier docs/)
        chemin, donnees = exporter_site_public()
        print(f"OK : {os.path.relpath(chemin, BASE_DIR)} mis à jour ({donnees['chiffres']['teams']} teams, "
              f"{donnees['chiffres']['matchs']} matchs). Envoie-le en ligne avec : git add docs && git commit -m \"Mise à jour\" && git push")
    else:
        app.run(debug=True)
