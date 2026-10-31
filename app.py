import os
import secrets
import sys
import sqlite3
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, g, abort
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
AVATAR_DEFAUT = '👤'

# ---------------------------------------------------------------------------
# Ligues : nombre de poules, rangs qui montent / descendent (dans chaque poule)
# ---------------------------------------------------------------------------
TEAMS_PAR_POULE = 10
LETTRES_POULES = 'ABCDEFGH'
LIGUE_DEPART = 10

LIGUES = {
    1: {'poules': 1, 'monte': [], 'descend': [10], 'champion': True},
    2: {'poules': 1, 'monte': [1], 'descend': [10], 'champion': False},
    3: {'poules': 1, 'monte': [1], 'descend': [9, 10], 'champion': False},
    4: {'poules': 2, 'monte': [1], 'descend': [10], 'champion': False},
    5: {'poules': 2, 'monte': [1], 'descend': [9, 10], 'champion': False},
    6: {'poules': 4, 'monte': [1], 'descend': [10], 'champion': False},
    7: {'poules': 4, 'monte': [1], 'descend': [10], 'champion': False},
    8: {'poules': 4, 'monte': [1], 'descend': [10], 'champion': False},
    9: {'poules': 4, 'monte': [1], 'descend': [9, 10], 'champion': False},
    10: {'poules': 8, 'monte': [1], 'descend': [], 'champion': False},
}

SHOP_DEFAUT = [
    ('Badge Officiel', 'Affichez le badge officiel XHQ sur votre profil.', 50, 'badge'),
    ('Skin Premium', 'Un skin exclusif pour vous démarquer des autres joueurs.', 100, 'cosmetic'),
    ('Boost 2x Points', 'Doublez les points de ligue gagnés par votre team.', 200, 'boost'),
]
# Types qu'on ne peut posséder qu'une fois
TYPES_UNIQUES = {'badge', 'cosmetic'}

# Nombre maximum de joueurs par team (3 titulaires + 2 remplaçants en 3v3)
MAX_MEMBRES = 5

# Barème des matchs (pas de match nul : un BO3 a toujours un gagnant)
POINTS_VICTOIRE = 3
POINTS_DEFAITE = 0
SCORE_MAX = 99

# Fin de saison : nombre de matchs minimum pour monter (ou être champion)
MATCHS_MIN_POUR_MONTER = 1


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


def choisir_poule(c, ligue_num):
    """Première poule qui a encore de la place (A, puis B...). Si tout est plein, la moins remplie."""
    lettres = lettres_poules(ligue_num)
    compte = {l: 0 for l in lettres}
    for row in c.execute('SELECT poule, COUNT(*) AS n FROM ligue WHERE ligue_num = ? GROUP BY poule', (ligue_num,)):
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


def retirer_membre(c, user_id):
    """Retire un joueur de sa team. Si c'était le créateur : un autre membre prend la main,
    ou la team est supprimée s'il était seul."""
    row = c.execute('SELECT team_id FROM team_members WHERE user_id = ?', (user_id,)).fetchone()
    if not row:
        return
    team_id = row['team_id']
    c.execute('DELETE FROM team_members WHERE user_id = ?', (user_id,))
    c.execute('UPDATE users SET team_id = NULL WHERE id = ?', (user_id,))
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
    # Anciennes données : users.team_id -> team_members
    c.execute("""INSERT OR IGNORE INTO team_members (team_id, user_id, joined_at)
                 SELECT team_id, id, datetime('now') FROM users
                 WHERE team_id IS NOT NULL AND team_id IN (SELECT id FROM teams)""")
    # users.team_id est le miroir de team_members
    c.execute('UPDATE users SET team_id = (SELECT team_id FROM team_members WHERE team_members.user_id = users.id)')
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
    c.execute(f'UPDATE ligue SET ligue_num = {LIGUE_DEPART} WHERE ligue_num IS NULL OR ligue_num NOT BETWEEN 1 AND 10')
    c.execute(f"""INSERT INTO ligue (team_id, ligue_num, points)
                  SELECT id, {LIGUE_DEPART}, 0 FROM teams
                  WHERE COALESCE(hors_classement, 0) = 0 AND id NOT IN (SELECT team_id FROM ligue)""")
    for row in c.execute('SELECT id, ligue_num, poule FROM ligue ORDER BY id').fetchall():
        if not row['poule'] or row['poule'] not in lettres_poules(row['ligue_num']):
            c.execute('UPDATE ligue SET poule = ? WHERE id = ?', (choisir_poule(c, row['ligue_num']), row['id']))


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
            solde INTEGER DEFAULT 1000,
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
            ligue_num INTEGER DEFAULT 10,
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
    c.execute("UPDATE users SET created_at = datetime('now') WHERE created_at IS NULL")
    c.execute("UPDATE teams SET created_at = datetime('now') WHERE created_at IS NULL")
    c.execute("UPDATE users SET profile_pic = ? WHERE profile_pic IS NULL OR profile_pic NOT IN (%s)"
              % ','.join('?' * len(AVATARS)), (AVATAR_DEFAUT, *AVATARS))

    # Shop par défaut (l'ancien item "Badge" devient "Badge Officiel")
    if not c.execute("SELECT 1 FROM shop_items WHERE nom = 'Badge Officiel'").fetchone():
        c.execute("UPDATE shop_items SET nom = 'Badge Officiel' WHERE nom = 'Badge'")
    for nom, description, prix, type_ in SHOP_DEFAUT:
        if c.execute('SELECT 1 FROM shop_items WHERE nom = ?', (nom,)).fetchone():
            c.execute("UPDATE shop_items SET description = ?, type = ?, prix = ? WHERE nom = ? AND (type IS NULL OR type = '')",
                      (description, type_, prix, nom))
        else:
            c.execute('INSERT INTO shop_items (nom, description, prix, type) VALUES (?, ?, ?, ?)',
                      (nom, description, prix, type_))

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


def logo_valide(logo):
    return logo if logo in AVATARS else AVATAR_DEFAUT


@app.context_processor
def injecter_utilisateur():
    user = utilisateur_connecte()
    current_user = None
    if user:
        current_user = {'username': user['username'], 'email': user['email'], 'logo': logo_valide(user['profile_pic']),
                        'is_admin': bool(user['is_admin'])}
    return {'current_user': current_user}


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
@app.route('/')
def index():
    return render_template('index.html')


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
    return render_template('profil.html', avatars=AVATARS)


@app.route('/teams')
@page_connectee
def teams_page():
    return render_template('teams.html')


@app.route('/ligue')
@page_connectee
def ligue_page():
    return render_template('ligue.html')


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
            "VALUES (?, ?, ?, '', ?, 1000, datetime('now'))",
            (email, username, generate_password_hash(password), AVATAR_DEFAUT))
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
    user = get_db().execute("""
        SELECT u.id, u.email, u.username, u.bio, u.profile_pic, u.solde, u.created_at,
               t.nom AS team_name, l.points, l.ligue_num
        FROM users u
        LEFT JOIN team_members tm ON tm.user_id = u.id
        LEFT JOIN teams t ON t.id = tm.team_id
        LEFT JOIN ligue l ON l.team_id = t.id
        WHERE u.id = ?""", (session['user_id'],)).fetchone()
    return jsonify({
        'id': user['id'],
        'email': user['email'],
        'username': user['username'],
        'bio': user['bio'] or '',
        'logo': logo_valide(user['profile_pic']),
        'solde': user['solde'] if user['solde'] is not None else 0,
        'team_name': user['team_name'],
        'points': user['points'] or 0,
        'ligue_num': user['ligue_num'],
        'created_at': user['created_at'],
    }), 200


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
    if logo not in AVATARS:
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
    retirer_membre(c, uid)
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
    rows = db.execute("""
        SELECT t.id, t.nom, t.bio, t.createur_id, t.created_at, u.username AS createur,
               l.ligue_num, l.poule, l.points,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id) AS nb_membres
        FROM teams t
        LEFT JOIN users u ON u.id = t.createur_id
        LEFT JOIN ligue l ON l.team_id = t.id
        ORDER BY t.nom COLLATE NOCASE""").fetchall()
    teams_list = []
    for r in rows:
        team = dict(r)
        team['bio'] = team['bio'] or ''
        team['createur'] = team['createur'] or '—'
        team['is_creator'] = team['createur_id'] == uid
        team['max_membres'] = MAX_MEMBRES
        team['pleine'] = team['nb_membres'] >= MAX_MEMBRES
        teams_list.append(team)

    mine = db.execute('SELECT team_id FROM team_members WHERE user_id = ?', (uid,)).fetchone()
    user_team_id = mine['team_id'] if mine else None
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
    matchs = matchs_de_team(db, user_team_id, limite=10) if user_team_id else []
    return jsonify({'teams': teams_list, 'user_team_id': user_team_id, 'user_id': uid, 'membres': membres,
                    'matchs': matchs, 'max_membres': MAX_MEMBRES}), 200


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
    c = db.cursor()
    if c.execute('SELECT 1 FROM team_members WHERE user_id = ?', (uid,)).fetchone():
        return jsonify({'success': False, 'error': 'Vous êtes déjà dans une team'}), 400
    if c.execute('SELECT 1 FROM teams WHERE lower(nom) = lower(?)', (nom,)).fetchone():
        return jsonify({'success': False, 'error': 'Ce nom de team est déjà pris'}), 400
    try:
        c.execute("INSERT INTO teams (nom, createur_id, bio, created_at) VALUES (?, ?, ?, datetime('now'))", (nom, uid, bio))
        team_id = c.lastrowid
        c.execute("INSERT INTO team_members (team_id, user_id, joined_at) VALUES (?, ?, datetime('now'))", (team_id, uid))
        c.execute('UPDATE users SET team_id = ? WHERE id = ?', (team_id, uid))
        c.execute('INSERT INTO ligue (team_id, ligue_num, points, poule) VALUES (?, ?, 0, ?)',
                  (team_id, LIGUE_DEPART, choisir_poule(c, LIGUE_DEPART)))
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
    if not db.execute('SELECT 1 FROM teams WHERE id = ?', (team_id,)).fetchone():
        return jsonify({'success': False, 'error': 'Team introuvable'}), 404
    if db.execute('SELECT 1 FROM team_members WHERE user_id = ?', (uid,)).fetchone():
        return jsonify({'success': False, 'error': 'Vous êtes déjà dans une team'}), 400
    if db.execute('SELECT COUNT(*) FROM team_members WHERE team_id = ?', (team_id,)).fetchone()[0] >= MAX_MEMBRES:
        return jsonify({'success': False, 'error': f'Cette team est complète ({MAX_MEMBRES} joueurs maximum)'}), 400
    try:
        db.execute("INSERT INTO team_members (team_id, user_id, joined_at) VALUES (?, ?, datetime('now'))", (team_id, uid))
        db.execute('UPDATE users SET team_id = ? WHERE id = ?', (team_id, uid))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({'success': False, 'error': 'Vous êtes déjà dans une team'}), 400
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
    retirer_membre(c, uid)  # transfert du rôle de capitaine ou suppression si seul
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
    retirer_membre(db.cursor(), user_id)
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
    poule = choisir_poule(c, LIGUE_DEPART)
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
def saison_actuelle(db):
    return db.execute('SELECT COALESCE(MAX(numero), 0) + 1 FROM saisons').fetchone()[0]


def classement_poule(db, ligue_num, poule):
    config = LIGUES[ligue_num]
    saison = saison_actuelle(db)
    rows = db.execute("""
        SELECT t.id AS team_id, t.nom AS team_name, l.points, l.id AS ligue_id
        FROM ligue l JOIN teams t ON t.id = l.team_id
        WHERE l.ligue_num = ? AND l.poule = ?""", (ligue_num, poule)).fetchall()

    # Stats calculées à partir des matchs joués dans CETTE ligue / poule
    stats = {r['team_id']: {'joues': 0, 'victoires': 0, 'defaites': 0, 'diff': 0} for r in rows}
    for m in db.execute('SELECT team1_id, team2_id, score1, score2 FROM matchs '
                        'WHERE ligue_num = ? AND poule = ? AND saison = ?', (ligue_num, poule, saison)):
        for tid, pour, contre in ((m['team1_id'], m['score1'], m['score2']), (m['team2_id'], m['score2'], m['score1'])):
            if tid in stats:
                s = stats[tid]
                s['joues'] += 1
                s['victoires' if pour > contre else 'defaites'] += 1
                s['diff'] += pour - contre

    # Classement : points, puis différence de manches, puis victoires, puis ancienneté
    rows = sorted(rows, key=lambda r: (-(r['points'] or 0), -stats[r['team_id']]['diff'],
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
                       'points': r['points'] or 0, 'statut': statut, **stats[r['team_id']]})
    while len(places) < TEAMS_PAR_POULE:
        places.append({'rang': len(places) + 1, 'team_id': None, 'team_name': 'XXXXX', 'points': 0, 'statut': '',
                       'joues': 0, 'victoires': 0, 'defaites': 0, 'diff': 0})
    return places


@app.route('/api/ligue', methods=['GET'])
@api_connectee
def api_ligue():
    ligue_num = request.args.get('ligue_num', 1, type=int)
    if ligue_num not in LIGUES:
        return jsonify({'success': False, 'error': 'Ligue invalide (1 à 10)'}), 400
    lettres = lettres_poules(ligue_num)
    poule = request.args.get('poule', '').upper()
    if poule and poule not in lettres:
        return jsonify({'success': False, 'error': f'Poule invalide pour la Ligue {ligue_num}'}), 400

    db = get_db()
    poules = [{'nom': l, 'teams': classement_poule(db, ligue_num, l)} for l in (poule or lettres)]
    config = LIGUES[ligue_num]
    return jsonify({
        'ligue_num': ligue_num,
        'nb_poules': config['poules'],
        'teams_par_poule': TEAMS_PAR_POULE,
        'monte': config['monte'],
        'descend': config['descend'],
        'champion': config['champion'],
        'poules': poules,
        'saison': saison_actuelle(db),
        'palmares': palmares(db, 5),
    }), 200


# ---------------------------------------------------------------------------
# API : shop
# ---------------------------------------------------------------------------
@app.route('/api/shop', methods=['GET'])
@api_connectee
def api_shop():
    rows = get_db().execute("""
        SELECT s.id, s.nom, s.description, s.prix, s.type,
               (SELECT COUNT(*) FROM inventory i WHERE i.item_id = s.id AND i.user_id = ?) AS possede
        FROM shop_items s ORDER BY s.prix, s.id""", (session['user_id'],)).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item['unique'] = item['type'] in TYPES_UNIQUES
        items.append(item)
    return jsonify(items), 200


@app.route('/api/shop/buy/<int:item_id>', methods=['POST'])
@api_connectee
def api_shop_buy(item_id):
    uid = session['user_id']
    db = get_db()
    item = db.execute('SELECT id, nom, prix, type FROM shop_items WHERE id = ?', (item_id,)).fetchone()
    if not item:
        return jsonify({'success': False, 'error': 'Item introuvable'}), 404
    if item['type'] in TYPES_UNIQUES and db.execute(
            'SELECT 1 FROM inventory WHERE user_id = ? AND item_id = ?', (uid, item_id)).fetchone():
        return jsonify({'success': False, 'error': 'Vous possédez déjà cet item'}), 400

    cur = db.execute('UPDATE users SET solde = solde - ? WHERE id = ? AND solde >= ?', (item['prix'], uid, item['prix']))
    if cur.rowcount == 0:
        db.rollback()
        return jsonify({'success': False, 'error': 'Solde insuffisant'}), 400
    db.execute("INSERT INTO inventory (user_id, item_id, bought_at) VALUES (?, ?, datetime('now'))", (uid, item_id))
    db.commit()
    solde = db.execute('SELECT solde FROM users WHERE id = ?', (uid,)).fetchone()['solde']
    return jsonify({'success': True, 'solde': solde, 'item': item['nom']}), 200


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
           m.score1, m.score2, m.points1, m.points2, m.created_at, m.saison
    FROM matchs m
    LEFT JOIN teams t1 ON t1.id = m.team1_id
    LEFT JOIN teams t2 ON t2.id = m.team2_id"""


def matchs_de_team(db, team_id, limite=10):
    resultats = []
    for r in db.execute(REQ_MATCHS + ' WHERE (m.team1_id = ? OR m.team2_id = ?) AND m.saison = ? '
                        'ORDER BY m.id DESC LIMIT ?', (team_id, team_id, saison_actuelle(db), limite)):
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
@api_connectee
def api_matchs():
    ligue_num = request.args.get('ligue_num', type=int)
    poule = request.args.get('poule', '').upper()
    limite = max(1, min(request.args.get('limit', 20, type=int), 100))
    saison = request.args.get('saison', type=int) or saison_actuelle(get_db())
    conditions, params = ['m.saison = ?'], [saison]
    if ligue_num is not None:
        if ligue_num not in LIGUES:
            return jsonify({'success': False, 'error': 'Ligue invalide (1 à 10)'}), 400
        conditions.append('m.ligue_num = ?')
        params.append(ligue_num)
        if poule:
            if poule not in lettres_poules(ligue_num):
                return jsonify({'success': False, 'error': f'Poule invalide pour la Ligue {ligue_num}'}), 400
            conditions.append('m.poule = ?')
            params.append(poule)
    where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
    rows = get_db().execute(REQ_MATCHS + where + ' ORDER BY m.id DESC LIMIT ?', (*params, limite)).fetchall()
    return jsonify([dict(r) for r in rows]), 200


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.route('/admin')
@page_admin
def admin_page():
    return render_template('admin.html', ligues=LIGUES, points_victoire=POINTS_VICTOIRE, points_defaite=POINTS_DEFAITE)


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
    compter = lambda req: db.execute(req).fetchone()[0]
    return jsonify({
        'joueurs': compter('SELECT COUNT(*) FROM users'),
        'teams': compter('SELECT COUNT(*) FROM teams'),
        'matchs': db.execute('SELECT COUNT(*) FROM matchs WHERE saison = ?', (saison_actuelle(db),)).fetchone()[0],
        'saison': saison_actuelle(db),
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
    t1 = c.execute('SELECT t.nom, l.ligue_num, l.poule FROM teams t JOIN ligue l ON l.team_id = t.id WHERE t.id = ?',
                   (team1_id,)).fetchone()
    t2 = c.execute('SELECT t.nom, l.ligue_num, l.poule FROM teams t JOIN ligue l ON l.team_id = t.id WHERE t.id = ?',
                   (team2_id,)).fetchone()
    if not t1 or not t2:
        return jsonify({'success': False, 'error': 'Team introuvable'}), 404
    if (t1['ligue_num'], t1['poule']) != (t2['ligue_num'], t2['poule']):
        return jsonify({'success': False,
                        'error': 'Les deux teams doivent être dans la même ligue et la même poule'}), 400

    ligue_num, poule = t1['ligue_num'], t1['poule']
    points1 = POINTS_VICTOIRE if score1 > score2 else POINTS_DEFAITE
    points2 = POINTS_VICTOIRE if score2 > score1 else POINTS_DEFAITE
    c.execute("""INSERT INTO matchs (ligue_num, poule, team1_id, team2_id, team1_nom, team2_nom,
                                     score1, score2, points1, points2, saisi_par, created_at, saison)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
              (ligue_num, poule, team1_id, team2_id, t1['nom'], t2['nom'],
               score1, score2, points1, points2, session['user_id'], saison_actuelle(db)))
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
    if m['saison'] != saison_actuelle(db):
        return jsonify({'success': False, 'error': 'Ce match appartient à une saison terminée'}), 400
    ajuster_points(c, m['team1_id'], m['ligue_num'], m['poule'], -m['points1'])
    ajuster_points(c, m['team2_id'], m['ligue_num'], m['poule'], -m['points2'])
    c.execute('DELETE FROM matchs WHERE id = ?', (match_id,))
    db.commit()
    return jsonify({'success': True}), 200


@app.route('/api/admin/teams', methods=['GET'])
@api_admin
def api_admin_teams():
    rows = get_db().execute("""
        SELECT t.id, t.nom, u.username AS createur, l.ligue_num, l.poule, l.points,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id) AS nb_membres
        FROM teams t
        LEFT JOIN users u ON u.id = t.createur_id
        LEFT JOIN ligue l ON l.team_id = t.id
        ORDER BY l.ligue_num, l.poule, t.nom COLLATE NOCASE""").fetchall()
    return jsonify([dict(r) for r in rows]), 200


@app.route('/api/admin/teams/<int:team_id>/move', methods=['POST'])
@api_admin
def api_admin_move_team(team_id):
    ligue_num = entier(donnees_json().get('ligue_num'))
    if ligue_num not in LIGUES:
        return jsonify({'success': False, 'error': 'Ligue invalide (1 à 10)'}), 400
    db = get_db()
    c = db.cursor()
    if not c.execute('SELECT 1 FROM teams WHERE id = ?', (team_id,)).fetchone():
        return jsonify({'success': False, 'error': 'Team introuvable'}), 404
    actuelle = c.execute('SELECT ligue_num FROM ligue WHERE team_id = ?', (team_id,)).fetchone()
    if not actuelle:  # team hors classement : on la remet dans la ligue choisie
        c.execute('INSERT INTO ligue (team_id, ligue_num, points, poule) VALUES (?, ?, 0, ?)',
                  (team_id, ligue_num, choisir_poule(c, ligue_num)))
        c.execute('UPDATE teams SET hors_classement = 0 WHERE id = ?', (team_id,))
        db.commit()
        poule = c.execute('SELECT poule FROM ligue WHERE team_id = ?', (team_id,)).fetchone()['poule']
        return jsonify({'success': True, 'ligue_num': ligue_num, 'poule': poule}), 200
    if actuelle['ligue_num'] == ligue_num:
        return jsonify({'success': False, 'error': f'La team est déjà en Ligue {ligue_num}'}), 400
    # Changement de ligue : nouvelle poule, les points repartent de 0
    c.execute('UPDATE ligue SET ligue_num = ?, poule = NULL, points = 0 WHERE team_id = ?', (ligue_num, team_id))
    poule = choisir_poule(c, ligue_num)
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
        SELECT u.id, u.username, u.email, u.solde, u.is_admin, u.created_at, t.nom AS team_name
        FROM users u
        LEFT JOIN team_members tm ON tm.user_id = u.id
        LEFT JOIN teams t ON t.id = tm.team_id
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
def palmares(db, limite=20):
    rows = db.execute("""
        SELECT s.numero, s.cloturee_at, sc.team_nom AS champion
        FROM saisons s
        LEFT JOIN saison_classements sc ON sc.saison = s.numero AND sc.mouvement = 'champion'
        ORDER BY s.numero DESC LIMIT ?""", (limite,)).fetchall()
    return [dict(r) for r in rows]


def calculer_fin_saison(db):
    """Classement final de chaque poule + destination de chaque team (sans rien modifier)."""
    resultats = []
    for ligue_num in LIGUES:
        for poule in lettres_poules(ligue_num):
            for place in classement_poule(db, ligue_num, poule):
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
    saison = saison_actuelle(db)
    classement = calculer_fin_saison(db)
    mouvements = [m for m in classement if m['mouvement'] != 'reste']
    mouvements.sort(key=lambda m: (m['ligue_num'], m['poule'], m['rang']))
    matchs_joues = db.execute('SELECT COUNT(*) FROM matchs WHERE saison = ?', (saison,)).fetchone()[0]
    return jsonify({
        'saison': saison,
        'matchs_joues': matchs_joues,
        'teams': len(classement),
        'mouvements': mouvements,
        'nb_montees': sum(m['mouvement'] in ('monte', 'repechage') for m in mouvements),
        'nb_descentes': sum(m['mouvement'] == 'descend' for m in mouvements),
        'nb_repechages': sum(m['mouvement'] == 'repechage' for m in mouvements),
        'nb_retirees': sum(m['mouvement'] == 'retiree' for m in mouvements),
        'historique': palmares(db),
    }), 200


@app.route('/api/admin/saison/cloturer', methods=['POST'])
@api_admin
def api_admin_cloturer_saison():
    if donnees_json().get('confirmation') != 'CLOTURER':
        return jsonify({'success': False, 'error': 'Tapez CLOTURER pour confirmer'}), 400
    db = get_db()
    c = db.cursor()
    saison = saison_actuelle(db)
    if not c.execute('SELECT 1 FROM matchs WHERE saison = ?', (saison,)).fetchone():
        return jsonify({'success': False, 'error': 'Aucun match joué cette saison : rien à clôturer'}), 400

    classement = calculer_fin_saison(db)
    try:
        c.execute("INSERT INTO saisons (numero, cloturee_at, cloturee_par) VALUES (?, datetime('now'), ?)",
                  (saison, session['user_id']))
        for p in classement:
            c.execute("""INSERT INTO saison_classements (saison, team_id, team_nom, ligue_num, poule, rang, points,
                                                         joues, victoires, defaites, diff, mouvement, destination)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (saison, p['team_id'], p['team_name'], p['ligue_num'], p['poule'], p['rang'], p['points'],
                       p['joues'], p['victoires'], p['defaites'], p['diff'], p['mouvement'], p['destination']))

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
            c.execute('UPDATE ligue SET poule = ? WHERE team_id = ?', (choisir_poule(c, p['destination']), p['team_id']))
        # 3) nouvelle saison : tout le monde repart à 0
        c.execute('UPDATE ligue SET points = 0')
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({'success': False, 'error': 'Cette saison a déjà été clôturée'}), 400

    return jsonify({
        'success': True,
        'saison_cloturee': saison,
        'nouvelle_saison': saison + 1,
        'montees': sum(p['mouvement'] in ('monte', 'repechage') for p in partants),
        'descentes': sum(p['mouvement'] == 'descend' for p in partants),
        'repechages': sum(p['mouvement'] == 'repechage' for p in partants),
        'retirees': len(retirees),
        'champion': next((p['team_name'] for p in classement if p['mouvement'] == 'champion'), None),
    }), 200


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
    else:
        app.run(debug=True)
