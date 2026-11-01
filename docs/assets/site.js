// XHQ League — site public statique (GitHub Pages).
// Les données viennent de data/ligue.json, généré par « python app.py export » (ou le bouton Publier de l'admin).

const etat = { donnees: null, config: {}, ligue: 1 };
const app = document.getElementById('app');

function esc(v) {
    return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function dateFr(iso) {
    if (!iso) return '';
    const d = new Date(iso.includes('T') ? iso : iso.replace(' ', 'T') + 'Z');
    return isNaN(d) ? '' : d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' });
}

async function charger() {
    try {
        const [d, c] = await Promise.all([
            fetch('data/ligue.json', { cache: 'no-store' }).then(r => r.ok ? r.json() : null),
            fetch('data/config.json', { cache: 'no-store' }).then(r => r.ok ? r.json() : {}).catch(() => ({}))
        ]);
        etat.donnees = d;
        etat.config = c || {};
    } catch (e) {
        etat.donnees = null;
    }
    if (etat.donnees && etat.donnees.genere_le) {
        document.getElementById('maj').textContent = 'Classement mis à jour le ' +
            new Date(etat.donnees.genere_le).toLocaleDateString('fr-FR', { day: 'numeric', month: 'long', year: 'numeric' });
    }
    router();
}

// ---------------------------------------------------------------- pages

function pageAccueil() {
    const d = etat.donnees;
    const ch = d ? d.chiffres : { teams: 0, joueurs: 0, matchs: 0 };
    const saison = d ? d.saison : 1;
    const derniers = d ? d.resultats.slice(0, 5) : [];
    return `
    <section class="hero">
        <div>
            <span class="kicker">Brawl Stars · 3v3 · Saison ${Number(saison)}</span>
            <h1>Monte.<br>Reste.<br><span>Ou dégage.</span></h1>
            <p>Une ligue à dix divisions pour les teams Brawl Stars. Tu joues tes matchs, tu grimpes au classement, et chaque fin de saison décide qui monte et qui redescend.</p>
            <div class="hero-actions">
                <a href="#/rejoindre" class="hero-btn">Inscrire ma team</a>
                <a href="#/ligue" class="lien-discret">Voir le classement →</a>
            </div>
        </div>
        <div class="chiffres">
            <div class="chiffre"><strong>${Number(saison)}</strong><span>Saison en cours</span></div>
            <div class="chiffre"><strong>${Number(ch.teams)}</strong><span>Teams classées</span></div>
            <div class="chiffre"><strong>${Number(ch.joueurs)}</strong><span>Joueurs</span></div>
            <div class="chiffre"><strong>${Number(ch.matchs)}</strong><span>Matchs cette saison</span></div>
        </div>
    </section>

    ${derniers.length ? `
    <div class="bloc-titre"><h2>Derniers résultats</h2><a href="#/resultats">Tous les résultats →</a></div>
    ${listeResultats(derniers)}` : ''}

    <div class="bloc-titre"><h2>Comment ça marche</h2><a href="#/reglement">Le règlement complet →</a></div>
    <div class="stack">
        ${[
            ['#c8ff2e', '10 divisions', 'De la Ligue 10 à la Ligue 1. Chaque nouvelle team commence tout en bas.'],
            ['#6f8cff', 'Poules de 10', 'Une seule poule en haut, huit en Ligue 10. Tu affrontes les teams de ta poule.'],
            ['#3ddc84', 'Montées', 'Le premier de chaque poule monte. Les places libres sont repêchées par la division du dessous.'],
            ['#ff5146', 'Descentes', 'Les dernières places redescendent. En Ligue 1, le 10e tombe direct.'],
            ['#ffae1f', 'Absences', "Un match pas joué, c'est un point en moins. Aucun match sur la saison, et la team sort du classement."],
            ['#b08cff', 'Teams de 5', "Trois titulaires, deux remplaçants. Le capitaine gère l'effectif."],
            ['#36d6e7', 'Résultats vérifiés', "Les scores sont saisis par l'équipe d'organisation, pas par les joueurs."],
            ['#ff6fb1', 'Palmarès', 'Chaque champion de Ligue 1 reste inscrit au palmarès, saison après saison.']
        ].map(([c, t, txt], i) => `
            <div class="stack-card" style="--c:${c}">
                <div class="stack-num">0${i + 1}</div>
                <div class="stack-title">${t}</div>
                <div class="stack-desc">${txt}</div>
            </div>`).join('')}
    </div>`;
}

function enTete(kicker, titre, droite = '') {
    return `<div class="header-nav"><h1><span class="kicker">${kicker}</span>${titre}</h1>${droite}</div>`;
}

function rangClasse(r) { return r === 1 ? 'top1' : r === 2 ? 'top2' : r === 3 ? 'top3' : ''; }

const BADGES = { champion: 'Champion', monte: 'Monte', descend: 'Descend' };

function ligneClassement(p) {
    if (!p.team_name) {
        return `<li class="leaderboard-item vide"><span class="rank">#${p.rang}</span>
            <div class="player-info"><div class="player-name">XXXXX</div></div><div class="player-points">0 pts</div></li>`;
    }
    const diff = p.diff > 0 ? '+' + p.diff : p.diff;
    return `<li class="leaderboard-item ${esc(p.statut)}">
        <span class="rank ${rangClasse(p.rang)}">#${p.rang}</span>
        <div class="player-info">
            <div class="player-name">${esc(p.team_name)}</div>
            <div class="player-stats">J ${p.joues} · V ${p.victoires} · D ${p.defaites} · ${diff}</div>
        </div>
        <div class="player-points">${Number(p.points)} pts</div>
        ${p.statut ? `<span class="badge ${esc(p.statut)}">${BADGES[p.statut] || ''}</span>` : ''}
    </li>`;
}

function pageLigue() {
    const d = etat.donnees;
    if (!d) return enTete('Classement', 'Ligues') + '<p class="ligue-message">Le classement n\'est pas encore publié.</p>';
    const ligue = d.ligues.find(l => l.ligue_num === etat.ligue) || d.ligues[0];
    const unique = ligue.poules.length === 1;
    const palmares = d.palmares.slice(0, 5);
    const resultats = d.resultats.filter(r => r.ligue_num === ligue.ligue_num).slice(0, 10);
    return `
    ${enTete('Classement', 'Ligues')}
    <div class="selector-wrapper">
        <button class="selector-btn" type="button" id="selector-btn">Ligue ${ligue.ligue_num} <span class="dropdown-arrow">▼</span></button>
        <div class="dropdown-menu" id="dropdown-menu">
            ${d.ligues.map(l => `<button class="dropdown-item ${l.ligue_num === ligue.ligue_num ? 'active' : ''}" type="button" data-ligue="${l.ligue_num}">Ligue ${l.ligue_num}</button>`).join('')}
        </div>
    </div>
    <h2 class="ligue-title">Ligue ${ligue.ligue_num}</h2>
    <div class="saison-bandeau">
        <span class="saison-tag">Saison ${Number(d.saison)} en cours</span>
        ${palmares.length ? `<span>Derniers champions : ${palmares.map(p => `S${Number(p.numero)} <strong style="color:var(--gold)">${esc(p.champion)}</strong>`).join(' · ')}</span>` : ''}
    </div>
    <div class="poules-grid">
        ${ligue.poules.map(po => `
            <div class="poule">
                <div class="poule-title">${unique ? 'Poule unique' : 'Poule ' + esc(po.nom)}</div>
                <ul class="leaderboard">${po.teams.map(ligneClassement).join('')}</ul>
            </div>`).join('')}
    </div>
    <div class="resultats">
        <h3>Derniers résultats</h3>
        ${resultats.length ? listeResultats(resultats) : '<p class="ligue-message">Aucun match joué dans cette ligue pour le moment.</p>'}
    </div>`;
}

function listeResultats(liste) {
    return '<div class="liste-resultats">' + liste.map(m => `
        <div class="resultat">
            <span class="equipe ${m.score1 > m.score2 ? 'gagnant' : ''}">${esc(m.team1_nom)}</span>
            <span class="score">${Number(m.score1)} - ${Number(m.score2)}</span>
            <span class="equipe ${m.score2 > m.score1 ? 'gagnant' : ''}">${esc(m.team2_nom)}</span>
            <span class="meta">L${Number(m.ligue_num)} · ${esc(m.poule)} · ${dateFr(m.created_at)}</span>
        </div>`).join('') + '</div>';
}

function pageResultats() {
    const d = etat.donnees;
    const liste = d ? d.resultats : [];
    return enTete('Saison ' + (d ? Number(d.saison) : 1), 'Résultats') +
        (liste.length ? `<div class="resultats" style="margin-top:0">${listeResultats(liste)}</div>`
                      : '<p class="ligue-message">Aucun match joué pour le moment.</p>');
}

function pageTeams(recherche = '') {
    const d = etat.donnees;
    const teams = d ? d.teams : [];
    const q = recherche.trim().toLowerCase();
    const filtre = teams.filter(t => t.nom.toLowerCase().includes(q));
    return enTete('Effectifs', 'Teams', `<span class="muted">${teams.length} team(s) classée(s)</span>`) + `
    <input type="text" id="recherche" placeholder="Rechercher une team…" value="${esc(recherche)}" style="margin-bottom:1.4rem">
    <div class="teams-liste" id="teams-liste">
        ${filtre.length ? filtre.map(t => `
            <div class="team-tuile">
                <h3>${esc(t.nom)}</h3>
                <p>${esc(t.bio || 'Pas de description.')}</p>
                <div class="meta">
                    <span class="tag">Ligue ${Number(t.ligue_num)} · Poule ${esc(t.poule)}</span>
                    <span class="tag">${Number(t.points)} pts</span>
                    <span class="tag">${Number(t.nb_membres)} joueur(s)</span>
                </div>
            </div>`).join('')
        : `<p class="ligue-message">${teams.length ? 'Aucune team ne correspond.' : 'Aucune team inscrite pour le moment. <a href="#/rejoindre" style="color:var(--text)">Sois la première.</a>'}</p>`}
    </div>`;
}

function pagePalmares() {
    const d = etat.donnees;
    const liste = d ? d.palmares : [];
    return enTete('Hall of fame', 'Palmarès') + (liste.length
        ? liste.map(p => `<div class="palmares-ligne"><span>Saison ${Number(p.numero)}</span><strong>${esc(p.champion)}</strong></div>`).join('')
        : '<p class="ligue-message">Pas encore de champion : la première saison est en cours.</p>');
}

function blocContact() {
    const { contact_email: email, discord } = etat.config;
    const liens = [];
    if (discord) liens.push(`<a class="btn" href="${esc(discord)}" target="_blank" rel="noopener">Rejoindre le Discord</a>`);
    if (email) liens.push(`<a class="btn secondaire" href="mailto:${esc(email)}?subject=${encodeURIComponent('Inscription XHQ League')}">Écrire à ${esc(email)}</a>`);
    return liens.length
        ? `<div class="contact-bloc">${liens.join('')}</div>`
        : '<div class="contact-bloc"><span class="muted">Les inscriptions ouvrent très bientôt : le moyen de contact sera affiché ici.</span></div>';
}

function pageRejoindre() {
    const r = (etat.donnees && etat.donnees.regles) || { max_membres: 5 };
    return enTete('Inscriptions', 'Rejoindre la ligue') + `
    <div class="texte"><p>Pas besoin de compte sur ce site : l'inscription se fait directement avec l'organisation.</p></div>
    <div class="etapes">
        <div class="etape"><b>01</b><h3>Monte ta team</h3><p>De 3 à ${Number(r.max_membres)} joueurs : trois titulaires et jusqu'à deux remplaçants. Choisis un nom et un capitaine.</p></div>
        <div class="etape"><b>02</b><h3>Envoie ta demande</h3><p>Le nom de la team, le pseudo Brawl Stars du capitaine et ceux des joueurs.</p></div>
        <div class="etape"><b>03</b><h3>Départ en Ligue 10</h3><p>Ta team apparaît au classement à la prochaine mise à jour. Ensuite, tout se joue sur le terrain.</p></div>
    </div>
    ${blocContact()}`;
}

function pageContact() {
    return enTete('Support', 'Contact') + `
    <div class="texte"><p>Un score à corriger, une question sur le règlement, un changement dans ta team : passe par ici.</p></div>
    ${blocContact()}`;
}

function pageReglement() {
    const r = (etat.donnees && etat.donnees.regles) || { points_victoire: 3, max_membres: 5, penalite_max: 15 };
    return enTete('Règles', 'Règlement') + `
    <div class="texte">
        <h2>Les divisions</h2>
        <p>Dix ligues. La Ligue 1, 2 et 3 ont une poule unique de 10 teams, les Ligues 4 et 5 deux poules, les Ligues 6 à 9 quatre poules et la Ligue 10 huit poules. Une team n'est jamais dans deux ligues à la fois. Toute nouvelle team démarre en Ligue 10.</p>
        <h2>Les matchs</h2>
        <p>Les matchs se jouent en 3v3, entre teams de la même poule. Une victoire rapporte ${Number(r.points_victoire)} points, une défaite 0. Pas de match nul. Les résultats sont saisis par l'organisation.</p>
        <p>À égalité de points, on départage à la différence de manches, puis au nombre de victoires.</p>
        <h2>Fin de saison</h2>
        <ul>
            <li>Ligue 1 : le 1er est champion, le 10e descend.</li>
            <li>Ligue 2 : le 1er monte, le 10e descend. Ligue 3 : le 1er monte, les 9e et 10e descendent.</li>
            <li>Ligue 4 : le 1er de chaque poule monte, le 10e descend. Ligue 5 : le 1er monte, les 9e et 10e descendent.</li>
            <li>Ligues 6 à 8 : le 1er de chaque poule monte, le 10e descend. Ligue 9 : le 1er monte, les 9e et 10e descendent.</li>
            <li>Ligue 10 : le 1er de chaque poule monte, pas de descente.</li>
            <li>Les places libres d'une ligue sont données aux meilleures teams de la ligue du dessous (repêchage, une ligue maximum par saison).</li>
        </ul>
        <h2>Absences et pénalités</h2>
        <p>Chaque match non joué coûte 1 point en fin de saison. Si la team était déjà pénalisée la saison précédente, la pénalité est doublée, avec un maximum de ${Number(r.penalite_max)} points. Une team qui ne joue aucun match de la saison est retirée du classement ; elle peut se réinscrire en Ligue 10.</p>
        <h2>Les teams</h2>
        <p>${Number(r.max_membres)} joueurs maximum par team. Un joueur ne peut être que dans une seule team.</p>
    </div>`;
}

function pageCgv() {
    return enTete('Légal', 'Conditions générales') + `
    <div class="texte">
        <h2>Objet</h2>
        <p>Ce site présente la XHQ League, une ligue amateur de Brawl Stars : classements, résultats et règlement. Il ne vend rien et ne demande aucun paiement.</p>
        <h2>Participation</h2>
        <p>La participation est gratuite. En inscrivant une team, le capitaine s'engage à respecter le règlement et les autres joueurs. L'organisation peut retirer une team en cas de triche, de harcèlement ou de comportement abusif.</p>
        <h2>Brawl Stars</h2>
        <p>XHQ League est un projet indépendant, non affilié à Supercell. Brawl Stars est une marque de Supercell.</p>
        <h2>Contact</h2>
        <p>Pour toute question : voir la page <a href="#/contact">Contact</a>.</p>
    </div>`;
}

function pageConfidentialite() {
    return enTete('Légal', 'Confidentialité') + `
    <div class="texte">
        <h2>Ce que ce site collecte</h2>
        <p>Rien. Pas de compte, pas de formulaire, pas de cookie, pas d'outil de mesure d'audience.</p>
        <h2>Ce qui est affiché</h2>
        <p>Seuls les noms des teams, leurs descriptions, leurs résultats et le nombre de joueurs sont publiés. Aucun email ni pseudo de joueur n'apparaît.</p>
        <h2>Services tiers</h2>
        <p>Le site est hébergé par GitHub Pages et utilise les polices Google Fonts : ces services peuvent voir l'adresse IP des visiteurs pour livrer les fichiers.</p>
        <h2>Retirer une team</h2>
        <p>Le capitaine peut demander le retrait de sa team via la page <a href="#/contact">Contact</a>.</p>
    </div>`;
}

const ROUTES = {
    '': pageAccueil, 'ligue': pageLigue, 'teams': pageTeams, 'resultats': pageResultats, 'palmares': pagePalmares,
    'rejoindre': pageRejoindre, 'contact': pageContact, 'reglement': pageReglement, 'cgv': pageCgv,
    'confidentialite': pageConfidentialite
};

// ---------------------------------------------------------------- navigation

function router() {
    const route = location.hash.replace(/^#\/?/, '').split('?')[0];
    const page = ROUTES[route] || pageAccueil;
    app.innerHTML = page();
    document.querySelectorAll('.xhq-nav-btn[data-route]').forEach(a => a.classList.toggle('active', a.dataset.route === route));
    const titres = { ligue: 'Ligues', teams: 'Teams', resultats: 'Résultats', palmares: 'Palmarès', rejoindre: 'Rejoindre',
                     contact: 'Contact', reglement: 'Règlement', cgv: 'CGV', confidentialite: 'Confidentialité' };
    document.title = (titres[route] ? titres[route] + ' · ' : '') + 'XHQ League';
    brancher(route);
    window.scrollTo(0, 0);
}

function brancher(route) {
    if (route === 'ligue') {
        const menu = document.getElementById('dropdown-menu');
        const fleche = document.querySelector('.dropdown-arrow');
        if (!menu) return;
        document.getElementById('selector-btn').addEventListener('click', e => {
            e.stopPropagation();
            menu.classList.toggle('open');
            fleche.classList.toggle('open');
        });
        menu.querySelectorAll('.dropdown-item').forEach(b => b.addEventListener('click', () => {
            etat.ligue = Number(b.dataset.ligue);
            router();
        }));
    }
    if (route === 'teams') {
        const champ = document.getElementById('recherche');
        champ.addEventListener('input', () => {
            const pos = champ.selectionStart;
            app.innerHTML = pageTeams(champ.value);
            brancher('teams');
            const nouveau = document.getElementById('recherche');
            nouveau.focus();
            nouveau.setSelectionRange(pos, pos);
        });
    }
}

document.addEventListener('click', e => {
    const menu = document.getElementById('dropdown-menu');
    if (menu && !e.target.closest('.selector-wrapper')) {
        menu.classList.remove('open');
        document.querySelector('.dropdown-arrow')?.classList.remove('open');
    }
});

window.addEventListener('hashchange', router);
charger();
