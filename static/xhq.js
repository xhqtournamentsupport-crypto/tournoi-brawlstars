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
