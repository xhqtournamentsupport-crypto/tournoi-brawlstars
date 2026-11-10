// XHQ Tournament : fonctions communes à toutes les pages

// Échappe un texte avant de l'insérer en HTML (évite les injections via un nom de team, une bio...)
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// Appel API JSON. Renvoie toujours { ok, status, data } (jamais d'exception).
// Si la session a expiré (401), redirige vers /login sauf si options.noRedirect est vrai.
async function api(url, options = {}) {
    const { noRedirect, body, ...rest } = options;
    // Le jeu choisi (Brawl Stars...) accompagne chaque appel, sauf s'il est déjà précisé
    if (window.XHQ_JEU && url.startsWith('/api/') && !/[?&]jeu=/.test(url)) {
        url += (url.includes('?') ? '&' : '?') + 'jeu=' + encodeURIComponent(window.XHQ_JEU);
    }
    const opts = { headers: { 'Content-Type': 'application/json' }, ...rest };
    if (body !== undefined) opts.body = JSON.stringify(body);

    let res;
    try {
        res = await fetch(url, opts);
    } catch (e) {
        return { ok: false, status: 0, data: { error: 'Erreur réseau' } };
    }
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }

    if (res.status === 401 && !noRedirect) {
        window.location.href = '/login';
    }
    return { ok: res.ok, status: res.status, data };
}

// ---------------------------------------------------------------------------
// Animations : apparition des blocs au défilement + compteurs qui défilent.
// Rien ne bouge si l'appareil demande moins d'animations.
// ---------------------------------------------------------------------------
(function () {
    const calme = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (calme || !('IntersectionObserver' in window)) return;
    document.documentElement.classList.add('anim');

    const CIBLES = '.reveal-auto, .section, .poule, .resultat, .team-card, .item-card, .inv-item, .stat, .carte-tournoi, '
        + '.bm, .inscrit, .vedette, .boite, .carte-match, .etape, .chiffres > div, .tuile, .discord, .profil-card, .info-section';

    const vus = new IntersectionObserver(entrees => {
        entrees.forEach(e => {
            if (!e.isIntersecting) return;
            e.target.classList.add('vu');
            vus.unobserve(e.target);
            const n = e.target.querySelector('[data-compte-a]') || (e.target.matches('[data-compte-a]') ? e.target : null);
            if (n) compter(n);
        });
    }, { threshold: 0.12, rootMargin: '0px 0px -30px 0px' });

    function preparer(racine) {
        const liste = [];
        if (racine.matches && racine.matches(CIBLES)) liste.push(racine);
        if (racine.querySelectorAll) liste.push(...racine.querySelectorAll(CIBLES));
        // Décalage entre éléments voisins pour un effet « cascade »
        liste.forEach(el => {
            if (el.classList.contains('reveal')) return;
            const freres = el.parentElement ? [...el.parentElement.children].filter(x => x.matches(CIBLES)) : [];
            const i = Math.max(0, freres.indexOf(el));
            el.style.setProperty('--d', Math.min(i, 8) * 0.06 + 's');
            el.classList.add('reveal');
            vus.observe(el);
        });
    }

    function compter(el) {
        const cible = parseInt(el.dataset.compteA, 10);
        if (!cible || el.dataset.fait) return;
        el.dataset.fait = '1';
        const t0 = performance.now();
        (function pas(t) {
            const p = Math.min((t - t0) / 1200, 1);
            el.textContent = Math.round(cible * (1 - Math.pow(1 - p, 3)));
            if (p < 1) requestAnimationFrame(pas);
        })(t0);
    }

    document.addEventListener('DOMContentLoaded', () => {
        preparer(document.body);
        document.querySelectorAll('[data-compte-a]').forEach(n => { if (!n.closest(CIBLES)) vus.observe(n); });
        // Les pages qui chargent leur contenu en JavaScript (classements, teams…) sont animées aussi
        new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => { if (n.nodeType === 1) preparer(n); })))
            .observe(document.body, { childList: true, subtree: true });
    });
})();

// ---------------------------------------------------------------------------
// Barre du haut : verre dépoli au défilement, menu du compte, burger mobile
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    const nav = document.getElementById('xhq-nav');
    if (!nav) return;

    const majColle = () => nav.classList.toggle('colle', window.scrollY > 8);
    majColle();
    window.addEventListener('scroll', majColle, { passive: true });

    const btn = document.getElementById('compte-btn');
    const menu = document.getElementById('compte-menu');
    const fermerCompte = () => {
        if (!menu || menu.hidden) return;
        menu.hidden = true;
        btn.parentElement.classList.remove('ouvert');
        btn.setAttribute('aria-expanded', 'false');
    };
    if (btn && menu) {
        btn.addEventListener('click', e => {
            e.stopPropagation();
            const ouvrir = menu.hidden;
            menu.hidden = !ouvrir;
            btn.parentElement.classList.toggle('ouvert', ouvrir);
            btn.setAttribute('aria-expanded', String(ouvrir));
        });
        menu.addEventListener('click', e => e.stopPropagation());
    }

    // Changer de compte (comptes déjà connectés sur ce navigateur)
    document.querySelectorAll('.compte-autre[data-compte]').forEach(b => b.addEventListener('click', async () => {
        const { ok } = await api('/api/comptes/basculer', { method: 'POST', body: { user_id: Number(b.dataset.compte) } });
        if (ok) location.reload();
    }));

    const burger = document.getElementById('burger');
    if (burger) burger.addEventListener('click', e => {
        e.stopPropagation();
        const ouvert = nav.classList.toggle('ouvert');
        burger.setAttribute('aria-expanded', String(ouvert));
        fermerCompte();
    });

    document.addEventListener('click', () => { fermerCompte(); nav.classList.remove('ouvert'); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') { fermerCompte(); nav.classList.remove('ouvert'); } });
});

// ---------------------------------------------------------------------------
// Envoi d'une image (photo, logo, bannière) en multipart
// ---------------------------------------------------------------------------
async function envoyerFichier(url, fichier) {
    const form = new FormData();
    form.append('fichier', fichier);
    let res, data = {};
    try {
        res = await fetch(url, { method: 'POST', body: form });
        data = await res.json().catch(() => ({}));
    } catch (e) { return { ok: false, data: { error: 'Erreur réseau' } }; }
    if (res.status === 413) data.error = 'Image trop lourde (8 Mo maximum)';
    return { ok: res.ok && !!data.success, data };
}

// ---------------------------------------------------------------------------
// Éditeur de bannière réutilisable : glisser pour cadrer + curseur de taille.
// racine : élément qui contient img.eb-img, .eb-vide, input.eb-fichier, .eb-suppr, input.eb-zoom, .eb-reglages
// ---------------------------------------------------------------------------
function editeurBanniere(racine, { url, pos = 50, zoom = 100, routes, message }) {
    const img = racine.querySelector('.eb-img');
    const etat = { pos, zoom, minuteur: null };
    const appliquer = () => {
        img.style.objectPosition = `50% ${etat.pos}%`;
        img.style.transformOrigin = `50% ${etat.pos}%`;
        img.style.transform = `scale(${etat.zoom / 100})`;
    };
    const afficher = u => {
        img.hidden = !u;
        if (u) img.src = u;
        racine.querySelector('.eb-vide').hidden = !!u;
        racine.querySelectorAll('.eb-avec').forEach(el => { el.hidden = !u; });
        racine.querySelector('.eb-zoom').value = etat.zoom;
        appliquer();
    };
    const sauver = () => {
        clearTimeout(etat.minuteur);
        etat.minuteur = setTimeout(() => api(routes.cadrage, { method: 'POST', body: { pos: Math.round(etat.pos), zoom: Math.round(etat.zoom) } }), 400);
    };
    afficher(url);

    racine.querySelector('.eb-fichier').addEventListener('change', async e => {
        const f = e.target.files[0]; e.target.value = '';
        if (!f) return;
        const { ok, data } = await envoyerFichier(routes.envoi, f);
        if (ok) { etat.pos = 50; etat.zoom = 100; afficher(data.url); message && message('Bannière mise à jour. Glisse-la pour la cadrer.', 'success'); }
        else message && message(data.error || 'Erreur', 'error');
    });
    racine.querySelector('.eb-suppr').addEventListener('click', async () => {
        const { ok } = await api(routes.suppression, { method: 'POST' });
        if (ok) afficher(null);
    });
    racine.querySelector('.eb-zoom').addEventListener('input', e => { etat.zoom = Number(e.target.value); appliquer(); sauver(); });

    let depart = null;
    const debut = y => { depart = { y, pos: etat.pos }; img.classList.add('glisse'); };
    const bouge = y => {
        if (!depart) return;
        const h = img.getBoundingClientRect().height || 1;
        etat.pos = Math.max(0, Math.min(100, depart.pos - (y - depart.y) / h * 100));
        appliquer();
    };
    const fin = () => { if (!depart) return; depart = null; img.classList.remove('glisse'); sauver(); };
    img.addEventListener('mousedown', e => { e.preventDefault(); debut(e.clientY); });
    window.addEventListener('mousemove', e => bouge(e.clientY));
    window.addEventListener('mouseup', fin);
    img.addEventListener('touchstart', e => debut(e.touches[0].clientY), { passive: true });
    img.addEventListener('touchmove', e => { e.preventDefault(); bouge(e.touches[0].clientY); }, { passive: false });
    img.addEventListener('touchend', fin);
}
