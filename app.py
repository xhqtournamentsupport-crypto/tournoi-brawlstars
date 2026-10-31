import os
import sqlite3
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, g
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'xhq-tournament-secret-2026'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'tournois.db')

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
                  SELECT id, {LIGUE_DEPART}, 0 FROM teams WHERE id NOT IN (SELECT team_id FROM ligue)""")
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
        g.user = get_db().execute('SELECT id, username, email, profile_pic FROM users WHERE id = ?', (uid,)).fetchone()
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
        current_user = {'username': user['username'], 'email': user['email'], 'logo': logo_valide(user['profile_pic'])}
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
        teams_list.append(team)

    mine = db.execute('SELECT team_id FROM team_members WHERE user_id = ?', (uid,)).fetchone()
    user_team_id = mine['team_id'] if mine else None
    membres = []
    if user_team_id:
        membres = [dict(m) for m in db.execute("""
            SELECT u.username, u.profile_pic AS logo FROM team_members tm JOIN users u ON u.id = tm.user_id
            WHERE tm.team_id = ? ORDER BY tm.joined_at, tm.id""", (user_team_id,))]
        for m in membres:
            m['logo'] = logo_valide(m['logo'])
    return jsonify({'teams': teams_list, 'user_team_id': user_team_id, 'user_id': uid, 'membres': membres}), 200


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


# ---------------------------------------------------------------------------
# API : ligue
# ---------------------------------------------------------------------------
def classement_poule(db, ligue_num, poule):
    config = LIGUES[ligue_num]
    rows = db.execute("""
        SELECT t.id AS team_id, t.nom AS team_name, l.points
        FROM ligue l JOIN teams t ON t.id = l.team_id
        WHERE l.ligue_num = ? AND l.poule = ?
        ORDER BY l.points DESC, l.id ASC""", (ligue_num, poule)).fetchall()

    places = []
    for i, r in enumerate(rows):
        rang = i + 1
        statut = ''
        if config['champion'] and rang == 1:
            statut = 'champion'
        elif rang in config['monte']:
            statut = 'monte'
        elif rang in config['descend']:
            statut = 'descend'
        places.append({'rang': rang, 'team_id': r['team_id'], 'team_name': r['team_name'],
                       'points': r['points'] or 0, 'statut': statut})
    while len(places) < TEAMS_PAR_POULE:
        places.append({'rang': len(places) + 1, 'team_id': None, 'team_name': 'XXXXX', 'points': 0, 'statut': ''})
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


if __name__ == '__main__':
    app.run(debug=True)
