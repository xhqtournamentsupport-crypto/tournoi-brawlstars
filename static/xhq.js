// XHQ Tournament : fonctions communes à toutes les pages

// Échappe un texte avant de l'insérer en HTML (évite les injections via un nom de team, une bio...)
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// Email masqué à l'écran (« pau************ ») : un clic l'affiche en entier, un autre le recache
function emailMasque(email) {
    const e = String(email ?? '');
    return e ? e.slice(0, 3) + '*'.repeat(12) : '';
}
function htmlEmail(email) {
    return `<button type="button" class="email-cache" data-email="${escapeHtml(email)}" title="Cliquer pour afficher">${escapeHtml(emailMasque(email))}</button>`;
}
document.addEventListener('click', e => {
    const b = e.target.closest && e.target.closest('.email-cache');
    if (!b) return;
    const visible = b.classList.toggle('vu');
    b.textContent = visible ? b.dataset.email : emailMasque(b.dataset.email);
    b.title = visible ? 'Cliquer pour cacher' : 'Cliquer pour afficher';
});

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
        + '.bm, .bracket-wrap, .inscrit, .vedette, .boite, .carte-match, .etape, .chiffres > div, .tuile, .discord, .profil-card, .info-section';

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
        // Zones redessinées pendant qu'on les modifie (ex. la carte « Ma team » après un changement de logo) :
        // une fois la page affichée, on ne rejoue pas l'animation, sinon tout « sauterait » à chaque modification.
        const pageDejaAffichee = performance.now() > 1500;
        liste.forEach(el => {
            if (el.classList.contains('reveal')) return;
            if (pageDejaAffichee && el.closest('[data-sans-animation]')) return;
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

    // Cloche : charge les notifications à l'ouverture et les marque comme lues
    const cloche = document.getElementById('cloche-btn');
    const clocheMenu = document.getElementById('cloche-menu');
    if (cloche && clocheMenu) {
        cloche.addEventListener('click', async e => {
            e.stopPropagation();
            fermerCompte();
            const ouvrir = clocheMenu.hidden;
            clocheMenu.hidden = !ouvrir;
            cloche.parentElement.classList.toggle('ouvert', ouvrir);
            cloche.setAttribute('aria-expanded', String(ouvrir));
            if (!ouvrir) return;
            const { ok, data } = await api('/api/notifications', { noRedirect: true });
            const liste = document.getElementById('cloche-liste');
            if (!ok) { liste.innerHTML = '<p class="cloche-vide">Impossible de charger les notifications.</p>'; return; }
            liste.innerHTML = data.items.length ? data.items.map(n => `
                <a class="cloche-item${n.lu ? '' : ' nouveau'}" href="${escapeHtml(n.lien || '#')}">${escapeHtml(n.texte)}<small>${escapeHtml(n.quand)}</small></a>`).join('')
                : '<p class="cloche-vide">Rien de neuf pour l\'instant. Tu seras prévenu ici des matchs, résultats et tournois.</p>';
            if (data.non_lues) {
                await api('/api/notifications/lues', { method: 'POST', noRedirect: true });
                const pastille = document.getElementById('cloche-compte');
                if (pastille) pastille.remove();
            }
        });
        clocheMenu.addEventListener('click', e => e.stopPropagation());
        document.addEventListener('click', () => { clocheMenu.hidden = true; cloche.parentElement.classList.remove('ouvert'); });
    }

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

// ---------------------------------------------------------------------------
// Effets : lueur et inclinaison 3D des cartes, onde sur les boutons, barre de
// lecture, bulle qui glisse dans la barre du haut, éclats de particules,
// confettis. Tout est coupé si l'appareil demande moins d'animations ;
// les effets de survol ne concernent que la souris (pas le tactile).
// ---------------------------------------------------------------------------
(function () {
    const calme = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
    const souris = window.matchMedia && matchMedia('(hover: hover) and (pointer: fine)').matches;
    const VIOLETS = ['#7c6cff', '#ab9fff', '#ffffff', '#8e80ff'];

    // ---- Particules (un seul canvas, créé à la demande) ----
    let toile = null, ctx = null, parts = [], anneaux = [], enCours = false;
    function preparerToile() {
        if (toile) return;
        toile = document.createElement('canvas');
        toile.className = 'fx-toile';
        toile.setAttribute('aria-hidden', 'true');
        document.body.appendChild(toile);
        ctx = toile.getContext('2d');
        const taille = () => {
            const r = window.devicePixelRatio || 1;
            toile.width = innerWidth * r; toile.height = innerHeight * r;
            ctx.setTransform(r, 0, 0, r, 0, 0);
        };
        taille();
        window.addEventListener('resize', taille);
    }
    function boucle() {
        ctx.clearRect(0, 0, innerWidth, innerHeight);
        anneaux = anneaux.filter(a => (a.vie -= 1) > 0);
        for (const a of anneaux) {
            const p = 1 - a.vie / a.max;
            ctx.globalAlpha = (1 - p) * 0.8;
            ctx.strokeStyle = '#ab9fff';
            ctx.lineWidth = 3 * (1 - p) + 0.5;
            ctx.beginPath(); ctx.arc(a.x, a.y, a.r * (1 - Math.pow(1 - p, 3)), 0, Math.PI * 2); ctx.stroke();
        }
        parts = parts.filter(p => (p.vie -= 1) > 0 && p.y < innerHeight + 40);
        for (const p of parts) {
            p.vx *= p.frein; p.vy = p.vy * p.frein + p.g; p.x += p.vx + Math.sin(p.vie / 9 + p.phase) * p.ondule; p.y += p.vy; p.rot += p.vr;
            ctx.globalAlpha = Math.min(1, p.vie / 25);
            ctx.fillStyle = p.c;
            ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot);
            if (p.forme === 'rect') ctx.fillRect(-p.t, -p.t / 2.5, p.t * 2, p.t / 1.25);
            else { ctx.beginPath(); ctx.arc(0, 0, p.t / 1.6, 0, Math.PI * 2); ctx.fill(); }
            ctx.restore();
        }
        ctx.globalAlpha = 1;
        if (parts.length || anneaux.length) requestAnimationFrame(boucle);
        else { enCours = false; ctx.clearRect(0, 0, innerWidth, innerHeight); }
    }
    function lancer() { if (!enCours) { enCours = true; requestAnimationFrame(boucle); } }

    // Explosion de particules violettes à un point de l'écran (x, y en pixels de la fenêtre)
    window.xhqEclat = function (x, y, nombre = 46) {
        if (calme) return;
        preparerToile();
        anneaux.push({ x, y, r: 90, vie: 34, max: 34 });
        for (let i = 0; i < nombre; i++) {
            const a = Math.random() * Math.PI * 2, v = 2.5 + Math.random() * 7.5;
            parts.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 2, g: 0.16, frein: 0.95, ondule: 0, phase: 0,
                rot: Math.random() * 6, vr: (Math.random() - 0.5) * 0.4, t: 2.5 + Math.random() * 3.5,
                c: VIOLETS[i % VIOLETS.length], forme: i % 3 ? 'rond' : 'rect', vie: 55 + Math.random() * 35 });
        }
        lancer();
    };
    window.xhqEclatSur = function (el, nombre) {
        if (!el) return;
        const r = el.getBoundingClientRect();
        xhqEclat(r.left + r.width / 2, r.top + r.height / 2, nombre);
    };

    // Pluie de confettis sur tout l'écran (victoire, champion…)
    window.xhqConfettis = function (nombre = 150) {
        if (calme) return;
        preparerToile();
        for (let i = 0; i < nombre; i++) {
            parts.push({ x: Math.random() * innerWidth, y: -20 - Math.random() * innerHeight * 0.6,
                vx: (Math.random() - 0.5) * 2, vy: 2 + Math.random() * 3, g: 0.03, frein: 0.995, ondule: 0.8 + Math.random(), phase: Math.random() * 6,
                rot: Math.random() * 6, vr: (Math.random() - 0.5) * 0.25, t: 3 + Math.random() * 4,
                c: VIOLETS[i % VIOLETS.length], forme: 'rect', vie: 260 + Math.random() * 120 });
        }
        lancer();
    };

    // Un objet « vole » d'un élément à un autre (copie visuelle), puis la cible pulse
    window.xhqVoler = function (depuis, vers) {
        if (calme || !depuis || !vers || !depuis.animate) return;
        const a = depuis.getBoundingClientRect(), b = vers.getBoundingClientRect();
        if (!a.width || !b.width) return;
        const copie = depuis.cloneNode(true);
        copie.removeAttribute('id');
        Object.assign(copie.style, { position: 'fixed', left: a.left + 'px', top: a.top + 'px', width: a.width + 'px', height: a.height + 'px',
            margin: 0, zIndex: 9998, pointerEvents: 'none' });
        copie.classList.add('fx-vol');
        document.body.appendChild(copie);
        const dx = b.left + b.width / 2 - (a.left + a.width / 2), dy = b.top + b.height / 2 - (a.top + a.height / 2);
        copie.animate([
            { transform: 'translate(0, 0) scale(1)', opacity: 1 },
            { transform: `translate(${dx * 0.45}px, ${dy * 0.45 - 90}px) scale(1.15) rotate(-6deg)`, opacity: 1, offset: 0.45 },
            { transform: `translate(${dx}px, ${dy}px) scale(0.25)`, opacity: 0.2 }
        ], { duration: 820, easing: 'cubic-bezier(.5,0,.3,1)' }).onfinish = () => {
            copie.remove();
            vers.classList.remove('fx-pouls'); void vers.offsetWidth; vers.classList.add('fx-pouls');
            xhqEclatSur(vers, 22);
        };
    };

    if (calme) return;

    document.addEventListener('DOMContentLoaded', () => {
        // ---- Barre de lecture en haut de la page ----
        const barre = document.createElement('div');
        barre.className = 'fx-progression';
        barre.setAttribute('aria-hidden', 'true');
        document.body.appendChild(barre);
        let attente = false;
        const majBarre = () => {
            attente = false;
            const max = document.documentElement.scrollHeight - innerHeight;
            barre.style.transform = `scaleX(${max > 40 ? Math.min(1, scrollY / max) : 0})`;
        };
        window.addEventListener('scroll', () => { if (!attente) { attente = true; requestAnimationFrame(majBarre); } }, { passive: true });
        majBarre();

        // ---- Onde violette au clic sur les boutons ----
        const BOUTONS = '.btn, .item-btn, .tab, .filtre, .xhq-nav-btn, .tache, .dropdown-item, .equip-btn';
        document.addEventListener('pointerdown', e => {
            const b = e.target.closest(BOUTONS);
            if (!b || b.disabled) return;
            const r = b.getBoundingClientRect();
            if (getComputedStyle(b).position === 'static') b.classList.add('fx-relatif');
            b.classList.add('fx-hote');
            const onde = document.createElement('span');
            const t = Math.max(r.width, r.height) * 2.2;
            onde.className = 'fx-onde';
            Object.assign(onde.style, { width: t + 'px', height: t + 'px', left: (e.clientX - r.left - t / 2) + 'px', top: (e.clientY - r.top - t / 2) + 'px' });
            b.appendChild(onde);
            setTimeout(() => onde.remove(), 650);
        });

        // ---- Bulle qui glisse sous les liens de la barre du haut ----
        const capsule = document.getElementById('nav-capsule');
        if (capsule && souris && capsule.querySelector('.fx-bulle')) {
            const bulle = capsule.querySelector('.fx-bulle');
            const placer = (lien, instant) => {
                if (!lien || capsule.offsetParent === null || getComputedStyle(capsule).flexDirection === 'column') { bulle.style.opacity = 0; return; }
                if (instant) bulle.style.transition = 'none';
                bulle.style.opacity = 1;
                bulle.style.width = lien.offsetWidth + 'px';
                bulle.style.height = lien.offsetHeight + 'px';
                bulle.style.transform = `translate(${lien.offsetLeft}px, ${lien.offsetTop}px)`;
                if (instant) { void bulle.offsetWidth; bulle.style.transition = ''; }
            };
            const actif = () => capsule.querySelector('.xhq-nav-btn.active');
            capsule.classList.add('fx-capsule');
            placer(actif(), true);
            capsule.querySelectorAll('.xhq-nav-btn').forEach(l => l.addEventListener('mouseenter', () => { capsule.classList.add('survol'); placer(l); }));
            capsule.addEventListener('mouseleave', () => { capsule.classList.remove('survol'); const a = actif(); if (a) placer(a); else bulle.style.opacity = 0; });
            window.addEventListener('resize', () => placer(actif(), true));
            if (document.fonts) document.fonts.ready.then(() => placer(actif(), true));
        }

        if (!souris) return;

        // ---- Halo qui suit la souris dans toute la page ----
        const halo = document.createElement('div');
        halo.className = 'fx-curseur';
        halo.setAttribute('aria-hidden', 'true');
        document.body.appendChild(halo);
        let hx = innerWidth / 2, hy = innerHeight / 3, cx = hx, cy = hy, haloActif = false;
        const suivre = () => {
            cx += (hx - cx) * 0.12; cy += (hy - cy) * 0.12;
            halo.style.transform = `translate(${cx - 300}px, ${cy - 300}px)`;
            if (Math.abs(hx - cx) + Math.abs(hy - cy) > 0.5) requestAnimationFrame(suivre); else haloActif = false;
        };

        // ---- Lueur sur le bord des cartes + inclinaison 3D ----
        const LUEUR = '.item-card, .team-card, .carte-tournoi, .stat, .tache, .look, .collection, .bm, .carte-match, .vedette, .profil-card, .a-faire, .ma-banniere';
        const TILT = '.item-card, .team-card, .carte-tournoi, .look, .vedette';
        let carte = null, inclinee = null, ev = null, prevu = false;
        const quitter = () => {
            if (carte) carte.classList.remove('fx-on');
            if (inclinee) { inclinee.style.transform = ''; inclinee.style.transition = ''; }
            carte = inclinee = null;
        };
        const appliquer = () => {
            prevu = false;
            const e = ev;
            const c = e.target.closest ? e.target.closest(LUEUR) : null;
            if (c !== carte) {
                quitter();
                carte = c;
                if (c) {
                    if (!c.querySelector(':scope > .fx-lueur')) {
                        if (getComputedStyle(c).position === 'static') c.classList.add('fx-relatif');
                        const l = document.createElement('span'); l.className = 'fx-lueur'; l.setAttribute('aria-hidden', 'true'); c.appendChild(l);
                    }
                    c.classList.add('fx-on');
                    if (c.matches(TILT) && !c.closest('.edition, form') && !c.querySelector('input, textarea, select')) inclinee = c;
                }
            }
            if (!carte) return;
            const r = carte.getBoundingClientRect();
            const x = e.clientX - r.left, y = e.clientY - r.top;
            carte.style.setProperty('--mx', x + 'px');
            carte.style.setProperty('--my', y + 'px');
            if (inclinee && r.width < 700) {
                const px = x / r.width - 0.5, py = y / r.height - 0.5;
                inclinee.style.transition = 'transform 0.12s ease-out, border-color 0.25s, box-shadow 0.25s';
                inclinee.style.transform = `perspective(900px) translateY(-4px) rotateX(${(-py * 7).toFixed(2)}deg) rotateY(${(px * 8).toFixed(2)}deg)`;
            }
        };
        document.addEventListener('pointermove', e => {
            if (e.pointerType !== 'mouse') return;
            ev = e;
            hx = e.clientX; hy = e.clientY;
            if (!haloActif) { haloActif = true; halo.classList.add('vu'); requestAnimationFrame(suivre); }
            if (!prevu) { prevu = true; requestAnimationFrame(appliquer); }
        }, { passive: true });
        document.addEventListener('mouseleave', () => { quitter(); halo.classList.remove('vu'); });
    });
})();
