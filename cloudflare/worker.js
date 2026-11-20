// XHQ League : affiche le site sur xhqleague.fr (le site reste hébergé gratuitement chez PythonAnywhere).
// À coller dans Cloudflare : Workers → ton Worker → Modifier le code. Variable secrète à créer : CLE
// (la clé donnée par « python3.10 app.py domaine » dans la console PythonAnywhere).
const ORIGINE = 'https://xhqtounament.pythonanywhere.com';
const DOMAINE = 'xhqleague.fr';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    // www.xhqleague.fr → xhqleague.fr ; discord.xhqleague.fr → la page /discord
    if (url.hostname === 'www.' + DOMAINE) return Response.redirect(`https://${DOMAINE}${url.pathname}${url.search}`, 301);
    if (url.hostname === 'discord.' + DOMAINE) return Response.redirect(`https://${DOMAINE}/discord`, 301);
    if (url.protocol === 'http:') return Response.redirect(`https://${DOMAINE}${url.pathname}${url.search}`, 301);

    const entetes = new Headers(request.headers);
    entetes.set('X-XHQ-Proxy', env.CLE || '');
    entetes.set('X-XHQ-IP', request.headers.get('CF-Connecting-IP') || '');
    const reponse = await fetch(ORIGINE + url.pathname + url.search, {
      method: request.method,
      headers: entetes,
      body: ['GET', 'HEAD'].includes(request.method) ? undefined : await request.arrayBuffer(),
      redirect: 'manual',
    });

    // Les redirections du site gardent l'adresse xhqleague.fr
    const sortie = new Response(reponse.body, reponse);
    const lieu = sortie.headers.get('Location');
    if (lieu && lieu.startsWith(ORIGINE)) sortie.headers.set('Location', `https://${DOMAINE}` + lieu.slice(ORIGINE.length));
    return sortie;
  },
};
