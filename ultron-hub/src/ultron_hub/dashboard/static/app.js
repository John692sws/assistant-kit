"use strict";
// Fetches /api/*, writes with textContent (a chantier's text is data, never HTML). Actions (todos #131, #132, #251): every
// form comes from GET /api/catalogue (form()), every sheet from POST /api/action/preparer (Quoi / Sur quoi / Effet / Risque),
// then POST /api/action/confirmer. No action is known here by name: the server's catalogue decides.
var REFRESH_MS = 30000;
var MSG_HORS_LIGNE = "Serveur injoignable : Tailscale actif ?";

// Terminal glyphs (todo #281, in place of the line icons of #269): [glyph, interface emojis it replaces]. 🔀 ❓ ✅ 📎 stay emojis
// (the actions to spot); any other interface emoji at the head of a label becomes a terminal character (el()). Emojis inside
// data (user text, CLI messages, todo titles) are left alone.
var GLYPHES = {
  check: ["✓", "🎉🏁"], x: ["✗", "❌✖"], alert: ["⚠︎", "⚠🚨⛔"], help: ["?", "🤷"], inbox: ["↓", "📥"], branch: ["⎇", "🌿"],
  bot: ["●", "🤖🧠"], refresh: ["↻", "🔄🔁"], wait: ["✻", "⏳⏱"], stop: ["■", "⏹"], play: ["▸", "▶🚀"], pulse: ["∿", "🩺"],
  pencil: ["✎", "✏✍📝"], history: ["◷", "🕓"], tool: ["⚙︎", "🧰🔧⚙"], shield: ["◈", "🛡🛟"], lock: ["#", "🔐"],
  bell: ["◎", "🔔"], belloff: ["○", "🔕"], save: ["⤓", "💾"], compass: ["⊙", "🧭🎯"], eye: ["◐", "🔭🔬"], search: ["/", "🔎"],
  chat: [">", "💬"], user: ["@", "👤👥"], file: ["≡", "📄📋🗒🧾"], book: ["▤", "📚"], leaf: ["~", "🌱"], flame: ["▲", "🔥"],
  tag: ["§", "🏷"], cal: ["▦", "📅"], bulb: ["*", "💡"], arrow: ["→", "➡➤"], plus: ["+", "➕"], minus: ["−", "➖"],
  undo: ["↶", "↩"], up: ["↑", "⬆"], down: ["↓", "⬇"], enter: ["↵", "↵"], layers: ["◇", "🧩"], archive: ["▭", "🗄"],
  trash: ["⌫", "🗑"], battery: ["▮", "🔋"], film: ["◫", "🎬🎨"], chart: ["▥", "📊🔢"], dot: ["●", "🔴🟢🟠🟡"], ring: ["○", "⚪⬜"]
};
var TONS = { "🟢": "vert", "🔴": "rouge", "❌": "rouge", "⛔": "rouge", "🚨": "rouge", "⚠": "ambre", "🟠": "ambre", "⏳": "ambre",
             "🟡": "pale", "⚪": "pale", "⬜": "pale" };
var EMOJIS = {};
Object.keys(GLYPHES).forEach(function (k) { Array.from(GLYPHES[k][1]).forEach(function (e) { EMOJIS[e] = k; }); });
function icone(nom, ton) {
  var g = document.createElement("b");
  g.className = "g" + (ton ? " " + ton : "");
  g.setAttribute("aria-hidden", "true");
  g.textContent = GLYPHES[nom][0];
  return g;
}
// "🩺 santé" -> ["pulse", "", "santé"]; null when the label does not start with an interface emoji.
function deco(t) {
  var c = typeof t === "string" ? Array.from(t)[0] : null;
  if (!c || !EMOJIS[c]) return null;
  var reste = t.slice(c.length).replace(/^️/, "").replace(/^ /, "");
  return [EMOJIS[c], TONS[c] || "", reste];
}
function sansEmoji(t) { var d = deco(t); return d ? d[2] : t; }
// Interface labels: buttons, titles, summaries, spans (kv, chips) and plain/err/row/evt lines.
function decorable(tag, cls) { return /^(button|strong|h1|h2|summary|span)$/.test(tag) || (tag === "div" && /^(err|row|evt|toast.*)?$/.test(cls || "")); }
function el(tag, cls, text) {
  var n = document.createElement(tag), d = decorable(tag, cls) ? deco(text) : null;
  if (cls) n.className = cls;
  if (d) {
    n.appendChild(icone(d[0], d[1]));
    n.appendChild(document.createTextNode(d[2]));
    if (d[1] === "rouge" && tag === "span") n.classList.add("err");
  } else if (text !== undefined && text !== null) n.textContent = text;
  return n;
}
document.querySelectorAll("i[data-i]").forEach(function (i) { i.replaceWith(icone(i.dataset.i)); });
function body(id) { return document.querySelector("#" + id + " .body"); }
function fill(id, nodes) { var b = body(id); b.replaceChildren.apply(b, nodes); }
function pct(v) { return v === null || v === undefined ? "?" : (Math.round(v * 10) / 10) + " %"; }
function level(v) { return v >= 80 ? "bad" : v >= 50 ? "warn" : ""; }
function bar(v) { var b = el("div", "bar"), i = el("i", level(v)); i.style.transform = "scaleX(" + Math.min(100, Math.max(0, v || 0)) / 100 + ")"; b.appendChild(i); return b; }
function kv(k, v) { var r = el("div", "kv"); r.appendChild(el("span", null, k)); r.appendChild(el("span", null, v)); return r; }
function failed(b) { return b && b.ok === false; }
function hhmm(iso) {
  if (!iso) return "?";
  var d = new Date(iso);
  return isNaN(d) ? "?" : d.toLocaleString("fr-FR", { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

function select(name, options, value) {
  var s = el("select");
  s.setAttribute("aria-label", name);
  options.forEach(function (o) { var opt = el("option", null, sansEmoji(o[1])); opt.value = o[0]; s.appendChild(opt); });
  if (value) s.value = value;
  return s;
}
// Comptes Claude (todo #263): the reading `comptes` (`compte etat`), at most once a minute; buttons = form() of the catalogue.
var COMPTES_MS = 60000, comptesLu = 0;
function loadComptes(force) {
  if (!force && Date.now() - comptesLu < COMPTES_MS) return;
  comptesLu = Date.now();
  lire("comptes").then(function (d) { return lire("budget").then(function (b) { return [d, b]; }, function () { return [d, null]; }); })
    .then(function (r) { showComptes(r[0], r[1]); }).catch(function (e) { comptesLu = 0; fill("comptes", [el("div", "err", "Indisponible (" + (e.message || e) + ")")]); });
}
// todo #339: the address of each Claude account (a / b) read by `comptes`; display only, the actions keep the letters.
var ADRESSES = {};
function nomCompte(slot) { return ADRESSES[slot] || "Compte " + String(slot || "").toUpperCase(); }
function boutonCompte(id, ctx, label) {  // the catalogue labels carry the letter: shown as the address when it is known
  var m = /^compte-(?:connexion|enregistrer)-([ab])$/.exec(id);
  if (m && ADRESSES[m[1]]) return label.replace(/\b[AB]\b/, ADRESSES[m[1]]);
  if (id === "compte-basculer" && ctx && ADRESSES[ctx.vers]) return label.replace("…", " vers " + ADRESSES[ctx.vers] + "…");
  return label;
}
function surCompte(data, text) {  // the sheet's « compte x » shown as the address when known (display only, journal keeps the letter)
  var m = /^compte-(?:connexion|enregistrer)-([ab])$/.exec(data.action || ""), slot = m ? m[1] : data.action === "compte-basculer" ? data.vers : data.compte;
  return slot && ADRESSES[slot] ? String(text).replace(/\b(?:le )?compte [abAB]\b/, ADRESSES[slot]) : text;
}
function conseilNodes(b) {  // todo #412: only the one-line advice of `compte budget`; doses are shown per account
  return b && b.conseil ? [el("div", "muted", "👉 " + b.conseil)] : [];
}
function showComptes(d, b) {
  var n = [];
  ADRESSES = {};
  d.comptes.forEach(function (c) { if (c.adresse) ADRESSES[c.nom] = c.adresse; });
  aToiSources.comptes = d.a_reconnecter || [];  // todo #466: an account to reconnect is a decision (À toi) and an alert (band)
  dessinerAToi(); if (etatLu.sante) showEtat(etatLu.sante, etatLu.quota);
  var doses = {};  // todo #411: the daily dose per account, as served by `compte budget`
  ((b && b.comptes) || []).forEach(function (c) { if (c.dose !== null && c.dose !== undefined) doses[c.nom] = c.dose; });
  if (d.alerte) n.push(el("div", "err", "⚠️ " + d.alerte));
  conseilNodes(b).forEach(function (x) { n.push(x); });
  if (!d.actif) n.push(el("div", "muted", "Compte en cours inconnu."));  // todo #413: otherwise « · actif » on its line says it
  d.comptes.forEach(function (c) {
    var r = el("div", "row");
    r.appendChild(el("strong", null, nomCompte(c.nom) + (c.actif ? " · actif" : "")));
    if (!c.enregistre) r.appendChild(el("div", "muted", "Pas encore enregistré."));
    else if (!c.quota) r.appendChild(el("div", "muted", c.quota_attente ? "⏳ " + (c.quota_erreur || "?")  // todo #330: expired token, not a failure
                                                                          : "Quota illisible : " + (c.quota_erreur || "?")));
    else [["Session 5 h", c.quota.session], ["Semaine", c.quota.week]].forEach(function (p) {
      r.appendChild(kv(p[0], p[1] ? pct(p[1].percent) : "?"));
      if (p[1]) r.appendChild(bar(p[1].percent));
    });
    if (doses[c.nom] !== undefined) r.appendChild(el("div", "muted", "dose du jour : " + doses[c.nom] + " points"));
    if (c.enregistre_a) r.appendChild(el("div", "muted", "enregistré " + jour(c.enregistre_a)));
    n.push(r);
  });
  (d.a_reconnecter || []).forEach(function (x) {  // todo #336: a failure = a check, the 🔑 button right under it
    var r = el("div", "row");
    r.appendChild(el("div", "err", "🔑 " + nomCompte(x.nom) + " à reconnecter : " + x.raison));
    r.appendChild(form("compte-connexion-" + x.nom));
    n.push(r);
  });
  if (d.autre) n.push(form("compte-basculer", { vers: d.autre }));
  n.push(plie("🔑 Se reconnecter à un compte", [form("compte-connexion-a"), form("compte-connexion-b"), form("compte-connexion-abandonner")]));
  n.push(plie("💾 Mémoriser la connexion en cours", [form("compte-enregistrer-a"), form("compte-enregistrer-b")]));
  fill("comptes", n);
}
// 🔑 Se reconnecter (todo #336): the result sheet of `compte connexion demarrer` holds the link, then the code field.
var LIEN_CONNEXION = /^https:\/\/(claude\.ai|claude\.com|console\.anthropic\.com)\//;  // same prefixes as compte/connexion.LINK_PREFIXES
function feuilleConnexion(d) {
  var nom = d.adresse || ADRESSES[d.compte] || "compte " + String(d.compte || "").toUpperCase();
  var n = [el("strong", null, "🔑 Connexion au " + (nom.indexOf("@") < 0 ? nom : "compte " + nom))];
  if (!LIEN_CONNEXION.test(d.lien || "")) return n.concat([el("div", "err", "Lien de connexion inattendu : refusé.")]);
  n.push(el("div", null, "1. Ouvre la page, vérifie que tu es sur le bon compte claude.ai (" + nom + "), puis autorise."));
  var a = el("a", "pj-lien", "🌐 Ouvrir la page de connexion (" + d.lien.split("/")[2] + ")");
  a.href = d.lien; a.target = "_blank"; a.rel = "noopener noreferrer";
  var r = el("div", "row");
  r.appendChild(a);
  n.push(r, el("div", null, "2. Copie le code affiché et colle-le ici :"), form("compte-connexion-code", { compte: d.compte }),
         el("div", "muted", "Le lien et le code ne sont gardés nulle part."), form("compte-connexion-abandonner"));
  return n;
}
// Drafts survive the 30 s refresh (the blocks are rebuilt): key -> text typed so far.
var brouillons = {};
function button(label, data, cls) {
  var b = el("button", cls || null, label);
  b.type = "button";
  b.addEventListener("click", function () { prepare(typeof data === "function" ? data() : data); });
  return b;
}
function textarea(key, placeholder, max) {
  var t = el("textarea");
  t.maxLength = max; t.rows = 2; t.placeholder = placeholder; t.setAttribute("aria-label", placeholder);
  t.value = brouillons[key] || "";
  t.addEventListener("input", function () { brouillons[key] = t.value; });
  return t;
}
// The catalogue (GET /api/catalogue): id -> {bouton, type, danger, champs}. Loaded once, again if it failed.
var CATALOGUE = null;
function chargerCatalogue() {
  if (CATALOGUE) return Promise.resolve();
  return fetch("/api/catalogue", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) {
    var c = {};
    d.actions.forEach(function (a) { c[a.id] = a; });
    CATALOGUE = c;
  }).catch(function () {});
}
// One form for every action: `ctx` gives the row's values (fields of type "contexte"), `listes` the options of a field
// declared with a `liste` (e.g. the subjects that can be launched). Same button, same sheet, whatever the action.
// `niveau` (todo #466): the button's level, "confirm" (filled, the one main action of a screen), "go" (outline, default) or
// "texte" (text only); a catalogue `danger` action stays red (destruction only).
function form(id, ctx, listes, niveau) {
  var a = CATALOGUE && CATALOGUE[id], box = el("div", "form");
  if (!a) return box;
  ctx = ctx || {};
  var key = id + ":" + JSON.stringify(ctx) + ":", inputs = {};
  a.champs.forEach(function (c) {
    if (c.type === "contexte") return;
    var k = key + c.nom, n;
    if (c.type === "choix") {
      var opts = c.liste ? ((listes && listes[c.liste]) || []).map(function (x) { return [x, x]; }) : c.choix;
      if (c.liste && !c.obligatoire) opts = [["", "Main (sans chantier)"]].concat(opts);  // todo #297: optional = none by default
      n = select(c.libelle, opts, brouillons[k] || c.defaut);
      n.addEventListener("change", function () { brouillons[k] = n.value; });
    } else {
      n = textarea(k, c.libelle.replace(/\{(\w+)\}/g, function (m, v) { return ctx[v] === undefined ? m : ctx[v]; }), c.max);
      if (c.ligne) n.rows = 1;
    }
    inputs[c.nom] = n;
    box.appendChild(n);
  });
  box.appendChild(button(boutonCompte(id, ctx, a.bouton), function () {
    var data = Object.assign({ action: id }, ctx), drafts = [];
    Object.keys(inputs).forEach(function (n) { data[n] = inputs[n].value; if (inputs[n].tagName === "TEXTAREA") drafts.push(key + n); });
    data.brouillon = drafts;
    return data;
  }, a.danger ? (niveau === "texte" ? "texte danger" : "danger") : niveau || "go"));
  return box;
}
// One badge (todos #270, #466): the number of cards of À toi; nothing else gets a badge or a pip.
function setBadge(n, tab, quoi) {
  var b = document.querySelector('#tabs [data-tab="' + (tab || "briefing") + '"] .badge');
  b.textContent = n > 99 ? "99+" : n; b.hidden = !n;
  b.setAttribute("aria-label", n + " " + (quoi || "décision(s) en attente"));
}
// Toasts: short result of an action, above the dock. A result shown while the sheet is open waits for it to close.
var toastEnAttente = null;
function toast(msg, bad, fixe) {  // fixe (todo #379): an error that stays until tapped
  var box = document.getElementById("toasts"), t = el("div", "toast" + (bad ? " bad" : ""), msg);
  while (box.children.length >= 2) box.removeChild(box.firstChild);
  box.appendChild(t);
  requestAnimationFrame(function () { requestAnimationFrame(function () { t.classList.add("in"); }); });
  var ferme = function () { t.classList.remove("in"); setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 300); };
  if (fixe) t.addEventListener("click", ferme); else setTimeout(ferme, 3500);
}
// -- À toi (todo #466): ONE queue of decisions, one card each, most urgent first. Sources, all read from the server: the À toi
// items of /api/state (merges, delivered todos, questions; « Approuver » = the action of catalogue.DECISIONS through prepare),
// sessions waiting for an answer (`sessions`), accounts to reconnect (`comptes`), profile facts (`profil`), proposed habits
// (`habitudes`), the Sunday reports (`rapports`). What changes on a branch comes from `etat-fusions` (last commit, refusals).
var decisions = [], aToiSources = { habitudes: [], routines: [], routinesActives: [], rapports: null, profil: [], comptes: [], fusions: null, bilanSoir: null, derniereFusion: null, planning: null };
function showAToi(b) {
  decisions = failed(b) ? [] : b.items;
  aToiErreur = failed(b) ? b.erreur : null;
  dessinerAToi();
}
var aToiErreur = null;
var RANG = { question: 3, fusionner: 4, apres_fusion: 5, livree: 6 };
function rangDecision(it) { return it.question && it.bloquante ? 0 : it.question ? 3 : RANG[it.kind] || 5; }
function nomChantier(sujet) {
  var c = etatChantiers && !failed(etatChantiers) ? etatChantiers.chantiers.filter(function (x) { return x.sujet === sujet; })[0] : null;
  return sujet ? (c && c.titre) || sujet : "Main";
}
// A card: a small head (kind · where, the reference in grey), the title in plain text, its own details, then the buttons.
function carte(tete, ref, titre, details, actions) {
  var r = el("div", "row dec carte"), h = el("div", "dec-tete");
  h.appendChild(el("span", null, tete));
  if (ref) h.appendChild(el("span", "mono pale", ref));
  r.appendChild(h);
  r.appendChild(el("p", "dec-titre", titre));
  (details || []).forEach(function (d) { if (d) r.appendChild(d); });
  var acts = el("div", "acts");
  (actions || []).forEach(function (a) { if (a) acts.appendChild(a); });
  if (acts.childNodes.length) r.appendChild(acts);
  return r;
}
// What a branch changes (todo #466): its last commit and size from `etat-fusions`, the refusal reasons (with the files), or
// « intégrée seule » for an automatic merge waiting for ✅. Nothing found: the catalogue's sentence for the kind.
function changeDe(it) {
  var f = aToiSources.fusions, n = [];
  if (f && it.todo) {
    f.en_cours.concat(f.attente).filter(function (t) { return t.todo_id === it.todo; }).forEach(function (t) {
      t.branches.forEach(function (b) {
        if (b.commit && b.commit.subject) n.push(el("div", "muted", "Dernier commit : " + b.commit.subject));
        n.push(el("div", "muted", b.ahead + " commit" + (b.ahead > 1 ? "s" : "") + " à intégrer" + (b.commit ? " · " + jour(b.commit.date) : "")));
      });
      (t.raisons || []).forEach(function (r) {
        n.push(el("div", "err", "Refus : " + r.reason));
        if (r.files.length) n.push(el("div", "pre mono", r.files.join("\n")));
      });
    });
    f.auto.filter(function (a) { return a.todo_id === it.todo && a.a_valider && !a.annulee; }).forEach(function (a) {
      n.push(el("div", "muted", "Intégrée seule le " + jour(new Date(a.at * 1000).toISOString()) + " : il reste ton ✅."));
    });
  }
  if (!n.length && it.pourquoi) n.push(el("div", "muted", it.pourquoi));
  return n;
}
var TETES = { fusionner: "🔀 à intégrer", livree: "✅ livrée", question: "❓ question", apres_fusion: "🧰 après fusion" };
function decision(it, primaire) {
  var cls = primaire ? "confirm" : "go", acts = [], details = changeDe(it);
  if (it.approuver && it.todo) acts.push(button("Approuver…", { action: it.approuver, id: String(it.todo) }, cls));
  acts.push(pushButton("Voir", function () { voirDecision(it); }, "texte"));
  return carte((TETES[it.kind] || "à voir") + " · " + nomChantier(it.chantier) + (it.bloquante ? " · bloquante" : ""), it.ref, it.texte, details, acts);
}
function ouvrirQuestion(it) {
  chargerCatalogue().then(function () { showOverlay([el("strong", null, "Ma question"), el("p", null, it.texte)], null, null); });
}
function voirDecision(it) {
  if (it.todo) return it.kind === "apres_fusion" ? openApres(it.todo) : openTodo(it.todo);
  if (it.chantier) return openFiche(it.chantier);
}
function carteSession(x, cls) {
  return carte("⚠ session · " + nomChantier(x.chantier), heure(new Date(x.cree * 1000).toISOString()), "La session attend ta réponse.",
               [x.derniere_phrase ? el("div", "muted ligne-coupee", x.derniere_phrase) : null],
               [pushButton("Répondre", function () { ouvrirSession(x.nom, null); }, cls)]);
}
function carteCompte(x, cls) {
  return carte("🔑 compte", null, nomCompte(x.nom) + " est à reconnecter", [el("div", "muted", x.raison)], [form("compte-connexion-" + x.nom, null, null, cls)]);
}
function carteProfil(p, cls) {
  var ctx = { id: String(p.id) };
  return carte("👤 profil · proposé par " + p.source, p.date ? jour(p.date) : null, "« " + p.texte + " »",
               [p.invisible_retire > 0 ? el("div", "err", "⚠️ " + p.invisible_retire + " caractère(s) invisible(s) retiré(s) : texte peut-être piégé.") : null],
               [form("profil-accepter", ctx, null, cls), form("profil-rejeter", ctx, null, "texte")]);
}
function carteHabitude(h, cls) {
  var ok = h.complet && !h.trop_long;
  return carte("💡 nouvelle habitude", null, ({ patch: "Ajout à « ", remplace: "Remplacement de « " }[h.mode] || "Skill « ") + h.nom + " »",
               [el("div", "muted", h.resume), plie("Lire le texte", [el("pre", "terminal habitude-texte", h.texte)]),
                !h.complet ? el("div", "muted", "✏️ Brouillon à compléter (« A COMPLÉTER ») : pas de ✅ tant qu'il reste des trous.")
                : h.trop_long ? el("div", "muted", "Texte trop long pour être relu sur la feuille : raccourcis le brouillon.") : null],
               [ok ? form("habitude", { dossier: h.dossier, empreinte: h.empreinte }, null, cls) : null, form("habitude-jeter", { dossier: h.dossier }, null, "texte")]);
}
// todo #503: 🔁 Routine proposée, right under the habits: the sentence, cadence, goal, end commands and cap, ✅ signed on its fingerprint
function carteRoutine(r, cls) {
  var d = function (t) { return el("div", "muted", t); };
  return carte("🔁 routine proposée", r.chantier, "« " + r.phrase + " »",
               [d("Cadence : " + r.cadence + (r.prochain ? " · prochaine : " + jour(r.prochain) + " " + heure(r.prochain) : "")), d("But : " + r.but),
                d("Fin quand : " + r.critere_fin.join(" ; ")), d("Plafond : " + r.plafond + " point(s) de quota par lancement")],
               [form("routine", { nom: r.nom, empreinte: r.empreinte }, null, cls), form("routine-refuser", { nom: r.nom }, null, "texte")]);
}
// todo #539: 🔁 routine active, with 🛑 Retirer (unsigned: it only removes work)
function carteRoutineActive(r, cls) {
  var d = function (t) { return el("div", "muted", t); };
  return carte("🔁 routine active", r.chantier, "« " + r.phrase + " »",
               [d("Cadence : " + r.cadence + (r.prochain ? " · prochaine : " + jour(r.prochain) + " " + heure(r.prochain) : "")), d("But : " + r.but)],
               [form("routine-retirer", { nom: r.nom }, null, "texte")]);
}
// todo #417: cap review, proposed tidy-up and health on three lines, then ONE signed ✅ (catalogue rapports-lot) naming what it does
function carteRapports(d, cls) {
  var todos = d.actions.filter(function (a) { return a.type === "todo"; }).length, menage = d.menage.empreinte && !d.menage.bloquee;
  var pannes = d.sante.alertes.length + d.sante.pannes.length;
  var dated = function (date, resume) { return (date ? jour(date) + " · " : "") + resume; };
  var lignes = el("div", "lignes");
  [ligne("🔭", "emoji", "Revue de cap", dated(d.cap.date, d.cap.resume), todos ? todos + " à créer" : "à jour", todos ? "ambre" : "pale", ouvrirCap),
   ligne("🧹", "emoji", "Ménage proposé", dated(d.menage.date, d.menage.resume), d.menage.n ? String(d.menage.n) : "rien",
         d.menage.n ? (d.menage.bloquee ? "rouge" : "ambre") : "pale", d.menage.n ? openMenagePropose : function () { toast("Aucun ménage proposé."); }),
   ligne("🩺", "emoji", "Santé", dated(d.sante.date, d.sante.resume), pannes ? String(pannes) : "ok", pannes ? "rouge" : "vert", openSante)
  ].forEach(function (l) { lignes.appendChild(l); });
  var quoi = [todos ? todos + " todo" + (todos > 1 ? "s" : "") + " de cap" : "", menage ? "ménage (" + d.menage.n + ")" : ""].filter(Boolean).join(" + ");
  return carte("📋 rapports du dimanche", d.cap.date ? jour(d.cap.date) : null, "Appliquer : " + quoi,
               [lignes, d.sante.pannes.length ? el("div", "muted", "Les pannes se règlent dans Santé.") : null],
               [form("rapports-lot", { empreinte: d.empreinte }, null, cls)]);
}
// todo #530: 🌙 Bilan du soir, what was merged or closed alone today (catalogue bilan-soir, read only).
// todo #531: ↩️ per line not yet undone: a closure = `rouvrir` (todo reopen); a merge = `annuler-fusion` (branch revert-last, signed),
// only if it is the LAST automatic merge (`fusion-auto`: cible = <todo>-<sha12>), else the reason is shown. Both go through form().
// todo #534: an automatic account switch (sorte bascule) = `compte-basculer` back to `retour` (signed, the server re-checks).
function carteBilanSoir(d, derniere) {
  var lignes = el("div", "lignes");
  d.lignes.forEach(function (x) {
    var cible = x.sha ? x.todo_id + "-" + x.sha : null, bloc = el("div", "lignebloc"), motif = null, f = null;
    if (!x.annulee) {
      if (x.sorte === "bascule") f = form("compte-basculer", { vers: x.retour });  // todo #534: the existing signed switch
      else if (x.sorte !== "fusion") f = form("rouvrir", { id: String(x.todo_id) });
      else if (cible && derniere && derniere.cible === cible && !derniere.annulee) f = form("annuler-fusion", { fusion: cible });
      else motif = !cible ? "↩️ absent : fusion sans empreinte" : derniere && derniere.cible === cible ? "↩️ absent : déjà annulée" : "↩️ absent : ce n'est plus la dernière fusion automatique";
    }
    bloc.appendChild(ligne(x.sorte === "fusion" ? "🔀" : x.sorte === "bascule" ? "🔁" : "✅", "emoji", (x.todo_id ? "#" + x.todo_id + " " : "") + x.titre,
                           x.heure + (x.sha ? " · " + x.sha : ""), x.annulee ? "annulée" : x.sorte, x.annulee ? "pale" : "vert", function () {}));
    if (motif) bloc.appendChild(el("div", "muted", motif));
    if (f) bloc.appendChild(f);
    lignes.appendChild(bloc);
  });
  (d.remises || []).forEach(function (x) {  // todo #534: one line per weekly reset of an account (planned, real, reserve)
    lignes.appendChild(ligne("📅", "emoji", x.texte, x.heure, x.compte.toUpperCase(), "pale", function () {}));
  });
  return carte("🌙 bilan du soir", d.date, d.gestes_evites + " gestes évités, " + d.faits + " faits", [lignes], []);
}
// todo #534: 📅 Planning du jour (catalogue planning, read only): the parts of each account as the server computed them, the
// optional box of the day (planning-poser, until midnight Paris time, empty = defaults) and the pause of the automatic switch.
var SOURCES_RESERVE = { "case": "ta case du jour", "appris": "apprise", "défaut": "par défaut" };
function cartePlanning(d) {
  var n = d.comptes.map(function (c) {
    var t = c.texte ? c.texte + (c.auto_epuisee ? " · part auto dépensée" : "") : c.nom.toUpperCase() + " : pas encore de relevé";
    return el("div", c.auto_epuisee ? "ambre" : null, t);
  });
  n.push(el("div", "muted", "Réserve : " + d.comptes.map(function (c) {
    return c.nom.toUpperCase() + " " + c.reserve_pct + " % (" + (SOURCES_RESERVE[c.source] || c.source) + ")";
  }).join(", ") + (d.case ? " · case valable jusqu'à minuit" : " · case vide : valeurs par défaut")));
  n.push(form("planning-poser", null, null, "go"));
  return carte("📅 planning du jour", d.jour, d.pause ? "Bascule auto en pause" : "Bascule auto à " + d.seuil + " % de la session 5 h",
               n, [form(d.pause ? "planning-reprendre" : "planning-pause", null, null, "texte")]);
}
function dessinerAToi() {
  var cartes = [];  // [rank, builder(cls)]
  decisions.forEach(function (it) { cartes.push([rangDecision(it), function (c) { return decision(it, c === "confirm"); }]); });
  aToiSources.comptes.forEach(function (x) { cartes.push([1, function (c) { return carteCompte(x, c); }]); });
  sessionsLues.filter(function (x) { return x.attend; }).forEach(function (x) { cartes.push([2, function (c) { return carteSession(x, c); }]); });
  aToiSources.profil.forEach(function (p) { cartes.push([7, function (c) { return carteProfil(p, c); }]); });
  aToiSources.habitudes.forEach(function (h) { cartes.push([8, function (c) { return carteHabitude(h, c); }]); });
  aToiSources.routines.forEach(function (r) { cartes.push([8.5, function (c) { return carteRoutine(r, c); }]); });
  aToiSources.routinesActives.forEach(function (r) { cartes.push([8.6, function (c) { return carteRoutineActive(r, c); }]); });
  var rap = aToiSources.rapports;
  if (rap && rap.actions.length && rap.empreinte) cartes.push([9, function (c) { return carteRapports(rap, c); }]);
  var bs = aToiSources.bilanSoir;
  var carteBilan = bs && (bs.lignes.length || (bs.remises || []).length) ? carteBilanSoir(bs, aToiSources.derniereFusion) : null;
  cartes.sort(function (a, b) { return a[0] - b[0]; });
  setBadge(cartes.length);
  var n = [], fus = decisions.filter(function (it) { return it.kind === "fusionner"; }).length;
  if (aToiErreur) n.push(el("div", "err", "Je n'arrive pas à lire ce qui t'attend (" + aToiErreur + ")."));
  if (fus > 1) n.push(pushButton("🔀 Tout intégrer d'un coup (" + fus + ")…", function () { openLot([]); }, "go"));
  // one filled button per screen: the main action of the first card only
  cartes.forEach(function (c, i) { n.push(c[1](i === 0 ? "confirm" : "go")); });
  if (aToiSources.planning) n.push(cartePlanning(aToiSources.planning));  // todo #534: optional box, not counted in the badge
  if (carteBilan) n.push(carteBilan);  // informative, not a decision: after the cards, not counted in the badge
  if (!cartes.length && !aToiErreur) n.push(el("div", "vide", "Rien n'attend ton avis. Tout est à jour."));
  fill("atoi", n);
}
// À toi's other sources, each read at most once a minute (todo #466); a failed reading = no card (the page decides nothing)
var ATOI_MS = 60000, aToiLu = {};
function loadAToi(force) {
  [["profil", function (d) { aToiSources.profil = d.pending || []; }],
   ["etat-fusions", function (d) { aToiSources.fusions = d; }],
   ["bilan-soir", function (d) { aToiSources.bilanSoir = d; }],
   ["fusion-auto", function (d) { aToiSources.derniereFusion = d; }],
   ["planning", function (d) { aToiSources.planning = d; }]].forEach(function (p) {
    if (!force && Date.now() - (aToiLu[p[0]] || 0) < ATOI_MS) return;
    aToiLu[p[0]] = Date.now();
    lire(p[0], {}).then(function (d) { p[1](d); dessinerAToi(); }).catch(function () { aToiLu[p[0]] = 0; });
  });
}
// One line of a list: glyph (or emoji), label, a detail line, a value on the right; the whole line is the button.
function ligne(glyphe, ton, titre, detail, valeur, valeurTon, fn) {
  var b = el("button", "ligne"), g = el("b", "g " + (ton || "")), v = el("span", "val mono " + (valeurTon || ""), valeur);
  b.type = "button";
  g.textContent = glyphe; g.setAttribute("aria-hidden", "true");
  b.appendChild(g); b.appendChild(el("span", "nom", titre)); b.appendChild(v);
  if (detail) { var d = el("span", "det"); [].concat(detail).forEach(function (x) { d.appendChild(typeof x === "string" ? document.createTextNode(x) : x); }); b.appendChild(d); }
  b.addEventListener("click", fn);
  return b;
}
// Bandeau état (top of En cours): the health block and the quota of /api/state, sessions and agents, and (todo #466) an account to
// reconnect, in red: an alert goes up to the band. A tap opens the detailed health.
var etatLu = { sante: null, quota: null };
function showEtat(sante, quota) {
  etatLu = { sante: sante, quota: quota };
  var b = document.getElementById("etat"), s = el("span"), ok = !failed(sante), ton, mot;
  if (!ok) { ton = "pale"; mot = "santé ?"; }
  else if (sante.unites_en_echec.length) { ton = "rouge"; mot = sante.unites_en_echec.length + " en échec"; }
  else { ton = "vert"; mot = "serveur ok"; }
  var g = el("b", "g " + ton, "●");
  s.appendChild(g); s.appendChild(el("span", ton, mot));
  [["5h", failed(quota) ? null : quota.session], ["sem", failed(quota) ? null : quota.semaine]].forEach(function (p) {
    var v = p[1] && p[1].percent !== null && p[1].percent !== undefined ? Math.round(p[1].percent) : null;
    s.appendChild(el("span", "muted", " · " + p[0] + " "));
    s.appendChild(el("span", "mono " + (v === null ? "pale" : { bad: "rouge", warn: "ambre", "": "vert" }[level(v)]), v === null ? "?" : v + "%"));
  });
  // todo #400: open ultron sessions and their known sub-agents, counted apart (todo #322) ; nothing while not read yet
  if (agentsLus !== null) {
    s.appendChild(el("span", "muted", " · "));
    s.appendChild(el("span", agentsLus.sessions ? "vert" : "pale", agentsLus.sessions + " session" + (agentsLus.sessions > 1 ? "s" : "")));
    if (agentsLus.agents) {
      s.appendChild(el("span", "muted", " · "));
      s.appendChild(el("span", "vert", agentsLus.agents + " agent" + (agentsLus.agents > 1 ? "s" : "")));
    }
  }
  if (aToiSources.comptes.length) {
    s.appendChild(el("span", "muted", " · "));
    s.appendChild(el("span", "rouge", "🔑 " + aToiSources.comptes.length + " compte" + (aToiSources.comptes.length > 1 ? "s" : "") + " à reconnecter"));
  }
  b.replaceChildren(s);
}
var agentsLus = null;
function compterAgents() {
  agentsLus = { sessions: sessionsLues.length, agents: sessionsLues.reduce(function (n, x) { return n + (x.sous_agents || 0); }, 0) };
  if (etatLu.sante) showEtat(etatLu.sante, etatLu.quota);  // state not read yet: showEtat will draw the count itself
}
document.getElementById("etat").addEventListener("click", function () { chargerCatalogue().then(openSante); });
// ▓░ progress in text (monospace): 10 cells.
function barre(fait, total) {
  var k = total ? Math.round(10 * fait / total) : 0;
  return el("span", "barre", "▓".repeat(k) + "░".repeat(10 - k));
}
// Chantiers (Moi, todo #466): x/y todos (done / done + open), and its state: actif (an ultron-<chantier> session or a squad), attente
// (items for you), en pause; listed in that order. Sessions come from the `sessions` reading (loadSessions), joined by the chantier's name.
var etatChantiers = null, sessionsLues = [], chantiersLus = [];  // chantiersLus: todo #297, options of « Nouvelle session »
function showChantiers(b) {
  if (b) etatChantiers = b;
  b = etatChantiers;
  if (!b) return;
  if (failed(b)) return fill("chantiers", [el("div", "err", "Indisponible (" + b.erreur + ")")]);
  if (!b.chantiers.length) return fill("chantiers", [el("div", "muted", "Aucun chantier ouvert.")]);
  var actifs = sessionsLues.map(function (x) { return x.chantier; }), vifs = [], attente = [], pause = [];
  b.chantiers.forEach(function (c) {
    var actif = actifs.indexOf(c.sujet) >= 0, total = (c.faites || 0) + (c.ouvertes || 0);
    var etat = actif ? ["vert", "actif"] : c.a_toi ? ["ambre", "attente · " + c.a_toi + " à toi"] : ["pale", "en pause"];
    var l = ligne("●", etat[0], c.titre || c.sujet, [barre(c.faites || 0, total), " " + etat[1]], (c.faites || 0) + "/" + total, "",
                  function () { openFiche(c.sujet); });
    (actif ? vifs : c.a_toi ? attente : pause).push(l);
  });
  // all chantiers visible, no fold: actifs, then en attente, then en pause
  fill("chantiers", vifs.concat(attente, pause));
}
// Sessions ultron and ultron-<chantier> (`session liste`, at most once a minute): age, sub-agents if known, ✻ works, ⚠ waits.
var SESSIONS_MS = 60000, sessionsLu = 0;
function loadSessions(force) {
  if (!force && Date.now() - sessionsLu < SESSIONS_MS) return;
  sessionsLu = Date.now();
  chargerCatalogue().then(function () { return lire("sessions"); }).then(function (d) {
    sessionsLues = d.sessions; chantiersLus = d.chantiers || []; showSessions(); showChantiers(); dessinerDemander(); dessinerAToi();
  }).catch(function (e) { sessionsLu = 0; fill("sessions", [el("div", "err", "Indisponible (" + (e.message || e) + ")")]); dessinerDemander(); });
}
function age(cree) {
  var m = Math.max(0, Math.round((Date.now() / 1000 - cree) / 60));
  return m < 60 ? m + " min" : m < 1440 ? Math.floor(m / 60) + " h " + (m % 60 < 10 ? "0" : "") + m % 60 : Math.floor(m / 1440) + " j";
}
// En cours (todo #466, the Session screen extended): one line per session, its state first (« attend ta réponse » in amber), then
// the chantier in plain words, the opening time in grey; a tap opens the Pilote.
function showSessions() {
  var n = sessionsLues.slice().reverse().map(function (x) {
    var g = x.attend ? ["⚠︎", "ambre"] : x.travaille ? ["✻", "vert vit"] : ["●", "pale"];
    var etat = x.attend ? "attend ta réponse" : x.travaille ? "travaille" : x.ouverte ? "ouverte" : "au repos";
    var det = [nomChantier(x.chantier)];
    if (x.sous_agents) det.push(" · " + x.sous_agents + " sous-agent" + (x.sous_agents > 1 ? "s" : ""));
    var l = ligne(g[0], g[1], etat, det, heure(new Date(x.cree * 1000).toISOString()), "pale", function () { ouvrirSession(x.nom, null); });
    if (x.attend) l.querySelector(".nom").classList.add("ambre");
    return l;
  });
  if (!n.length) n.push(el("div", "muted", "Aucune session ouverte."));
  n.push(pushButton("Gérer les sessions (fermer, relancer)", ouvrirSessions, "texte"));
  fill("sessions", n);
  compterAgents();
}
// « Demander à Ultron » (todo #466): ONE field and ONE button; the selector above says where it goes: an open session (written at
// once, `session-ecrire`, no sheet, then the Pilote opens) or a new session on Main or a chantier (`session-nouvelle`, signed sheet).
// Both are catalogue entries; the page only builds their values.
var CIBLES = { s: "session-ecrire", n: "session-nouvelle" }, CHAMP_TEXTE = { s: "texte", n: "consigne" };
function dessinerDemander() {
  var b = body("demander");
  if (!CATALOGUE || b.contains(document.activeElement)) return;  // never rebuilt under the fingers
  var opts = sessionsLues.slice().reverse().map(function (x) {
    return ["s:" + x.nom, (x.attend ? "⚠ " : "") + "Session " + nomChantier(x.chantier) + " · " + heure(new Date(x.cree * 1000).toISOString())];
  });
  opts.push(["n:", "Nouvelle session · Main"]);
  chantiersLus.forEach(function (c) { opts.push(["n:" + c, "Nouvelle session · " + c]); });
  var attend = sessionsLues.filter(function (x) { return x.attend; })[0], valeurs = opts.map(function (o) { return o[0]; });
  var choix = valeurs.indexOf(brouillons["demander:cible"]) >= 0 ? brouillons["demander:cible"] : attend ? "s:" + attend.nom : valeurs[0];
  var sel = select("Où envoyer", opts, choix), texte = textarea("demander:texte", "Demander à Ultron…", 4000);
  texte.rows = 3;
  var envoyer = pushButton("", function () {
    var v = sel.value, k = v.slice(0, 1), arg = v.slice(2), mot = texte.value.trim(), a = CATALOGUE[CIBLES[k]];
    if (k !== "n" && !mot) return toast("Écris d'abord ta demande.");
    var data = { action: CIBLES[k] };
    data[CHAMP_TEXTE[k]] = texte.value;
    if (k === "s") return direct(Object.assign(data, { nom: arg }), function () {
      texte.value = ""; delete brouillons["demander:texte"]; toast("✍️ Envoyé à la session"); ouvrirSession(arg, null);
    });
    if (k === "n") data.chantier = arg;
    if (a) prepare(Object.assign(data, { brouillon: ["demander:texte"] }));
  }, "confirm");
  function maj() {
    var k = sel.value.slice(0, 1), a = CATALOGUE[CIBLES[k]], c = a && a.champs.filter(function (x) { return x.nom === CHAMP_TEXTE[k]; })[0];
    brouillons["demander:cible"] = sel.value;
    if (c) texte.maxLength = c.max;
    envoyer.textContent = k === "s" ? "Envoyer" : "Ouvrir la session…";
  }
  sel.addEventListener("change", maj);
  maj();
  fill("demander", [sel, texte, envoyer]);
}
// « Fait aujourd'hui »: the catalogue reading `faits-du-jour` (todos done today), at most once a minute, the last 3.
var FAITS_MS = 60000, faitsLu = 0;
function loadFaits(force) {
  if (!force && Date.now() - faitsLu < FAITS_MS) return;
  faitsLu = Date.now();
  lire("faits-du-jour").then(function (d) {
    if (!d.tasks.length) return fill("fait", [el("div", "muted", "Rien de terminé pour l'instant aujourd'hui.")]);
    fill("fait", d.tasks.slice(0, 3).map(function (t) {
      var r = el("div", "fait-l");
      r.appendChild(icone("check", "vert")); r.appendChild(el("span", "t", t.title)); r.appendChild(el("span", "muted mono", heure(t.done_at)));
      return r;
    }));
  }).catch(function (e) { faitsLu = 0; fill("fait", [el("div", "err", "Indisponible (" + (e.message || e) + ")")]); });
}
function showSante(b) {
  if (failed(b)) return fill("sante", [el("div", "err", "Indisponible (" + b.erreur + ")")]);
  var n = [kv("Disque", pct(b.disque_pct)), bar(b.disque_pct), kv("Mémoire", pct(b.ram_pct)), bar(b.ram_pct),
           kv("Charge (1 min)", b.charge + " / " + b.cpus + " cœurs"), kv("hermes-gateway", b.hermes_gateway || "?")];
  if (b.sauvegarde_age_h !== null && b.sauvegarde_age_h !== undefined) n.push(kv("Dernière sauvegarde", "il y a " + b.sauvegarde_age_h + " h"));
  if (b.unites_en_echec.length) n.push(el("div", "err", "En échec : " + b.unites_en_echec.join(", ")));
  n.push(listButton("🩺 Santé détaillée", openSante));
  fill("sante", n);
}

var current = null, pending = null;
// todo #466: three tabs (briefing = À toi, travail = En cours, fil) and Moi behind the top-right icon; the ids stay those of #270
var TABS =["briefing", "travail", "fil", "moi"];
var ANCIENS = { accueil: "briefing", questions: "briefing", todos: "moi", escouades: "briefing", equipes: "briefing", encours: "travail", atoi: "briefing" };  // links before todos #270, #458, #466
function selectTab(tab) {
  tab = ANCIENS[tab] || tab;
  if (TABS.indexOf(tab) < 0) tab = "briefing";
  TABS.forEach(function (t) { document.getElementById("tab-" + t).hidden = t !== tab; });
  // todo #466: « Toutes les tâches » and the chantiers sit in Moi
  if (tab === "moi") chargerCatalogue().then(function () { if (CATALOGUE) { loadTodos(); loadComptes(true); } });
  if (tab === "travail") chargerCatalogue().then(function () { loadSessions(true); loadConseil(); });  // todos #405, #364
  if (tab === "fil" && CATALOGUE) loadFil();
  document.querySelectorAll("#tabs button").forEach(function (b) { b.setAttribute("aria-selected", String(b.dataset.tab === tab)); });
  document.getElementById("ouvrir-moi").setAttribute("aria-pressed", String(tab === "moi"));
  document.getElementById("page").scrollTop = 0;
}
document.querySelectorAll("#tabs button").forEach(function (b) { b.addEventListener("click", function () { selectTab(b.dataset.tab); }); });
document.getElementById("ouvrir-moi").addEventListener("click", function () {
  selectTab(document.getElementById("tab-moi").hidden ? "moi" : "briefing");  // a second tap goes back to À toi
});
function setOpen(id, on) {
  document.getElementById(id).classList.toggle("open", on);
  document.getElementById("scrim").classList.toggle("open", on && id === "confirm");
}
// Session refused (dashboard restarted, or 12 h passed): renew it with GET /, then redo the reading or the preparation
// once; never a confirmation (todo #267: the caller prepares again and the user confirms again).
var MSG_RECONNECTE = "Dashboard redémarré, reconnecté";
function post(path, data, renouvele) {
  return fetch(path, { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json" },
                       body: JSON.stringify(data) }).then(function (r) { return r.json(); }).then(function (j) {
    if (j.session !== "renouveler" || renouvele) return j;
    return fetch("/", { cache: "no-store" }).then(function () {
      toast("🔄 " + MSG_RECONNECTE);
      if (path === "/api/action/confirmer") return Object.assign(j, { reconnecte: true });
      return post(path, data, true);
    });
  });
}
function showConfirm(nodes) {
  current = "confirm";
  fill("confirm", nodes);
  setOpen("confirm", true);
}
var aRecharger = false;
function closeConfirm() {
  menageStop();
  setOpen("confirm", false);
  if (toastEnAttente) { toast(toastEnAttente[0], toastEnAttente[1]); toastEnAttente = null; }
  suiviEnCours = null;  // the job goes on in the background; only the polling stops
  current = document.getElementById("fiche").classList.contains("open") ? "fiche" : null;
  aToiLu.planning = 0;  // todo #534: the box or the pause just changed shows at once (a file-only reading)
  if (current === null) refresh();
  else if (aRecharger && ficheEcran) ficheEcran();
  aRecharger = false;
}
function cancel() {
  if (pending) post("/api/action/confirmer", { jeton: pending, confirme: false }).catch(function () {});
  pending = null;
  closeConfirm();
}
document.getElementById("scrim").addEventListener("click", cancel);
function backButton() {
  var b = el("button", null, "« Retour");
  b.type = "button";
  b.addEventListener("click", cancel);
  return b;
}
function offline() {
  toastEnAttente = ["❌ " + MSG_HORS_LIGNE, true];
  showConfirm([el("div", "err", "❌ " + MSG_HORS_LIGNE), backButton()]);
}
// A background action (todo #267: merges): its steps come from the `suivi` reading, polled every 2 s; the page only draws them.
var suiviEnCours = null, ICONES_ETAPE = { fait: "✅", en_cours: "⏳", a_venir: "⬜", refus: "❌" };
function etapesFond(list) {
  var box = el("div", "etapes"), faits = list.filter(function (e) { return e.etat === "fait"; }).length;
  box.appendChild(bar(100 * faits / Math.max(1, list.length)));
  list.forEach(function (e) {
    var r = el("div", "etape " + e.etat);
    r.appendChild(el("span", null, (ICONES_ETAPE[e.etat] || "•") + " " + e.nom));
    if (e.detail) r.appendChild(el("span", "muted", e.detail));
    box.appendChild(r);
  });
  return box;
}
function suivre(id, data) {
  var coupe = false;
  suiviEnCours = id;
  function tour() {
    if (suiviEnCours !== id) return;
    lire("suivi", { suivi: id }).then(function (d) {
      if (suiviEnCours !== id) return;
      if (d.fini) { suiviEnCours = null; return showResult(d.resultat || { ok: false, erreur: "résultat illisible" }, data, d.etapes); }
      var n = [el("strong", null, "⏳ En cours (" + d.depuis_s + " s)")];
      if (coupe) n.push(el("div", "muted", MSG_RECONNECTE));
      n.push(etapesFond(d.etapes), el("div", "muted", "Tu peux fermer : l'action continue sur le serveur."), backButton());
      showConfirm(n);
      setTimeout(tour, 2000);
    }).catch(function (e) {  // the dashboard restarts (a merge can do it): keep asking; a refusal ends the polling
      if (suiviEnCours !== id) return;
      if (e && e.name === "Error") { suiviEnCours = null; return showResult({ ok: false, erreur: e.message }, data); }
      coupe = true;
      showConfirm([el("strong", null, "⏳ En cours"), el("div", "muted", "Le dashboard redémarre, reconnexion…"), backButton()]);
      setTimeout(tour, 2000);
    });
  }
  tour();
}
function showResult(r, data, faites) {
  if (r.suivi) return suivre(r.suivi, data);
  var n = [el("div", r.ok ? null : "err", r.ok ? "✅ " + sansEmoji(r.resume || "fait") : "❌ " + (r.erreur || r.resume || "échec"))];
  if (faites) n.unshift(etapesFond(faites));
  if (r.texte) n.push(el("div", "pre", r.texte));
  n = n.concat(suiteAnalyser(r, data));  // 📥 Analyser (todo #248): duration, next step
  if (r.ok && r.donnees && RENDUS[data.action]) n.push.apply(n, RENDUS[data.action](r.donnees));
  if (r.apres_fusion && data.id) n.push(listButton("🧰 Actions après fusion (" + r.apres_fusion + ")", function () { aRecharger = false; closeConfirm(); openApres(data.id); }));
  if (r.ok && r.id && SUITES[data.action]) n.push(SUITES[data.action](r));
  if (menage.total && data.action === menage.action) {  // 🧹 Ménage: next todo of the selection, or stop at the first failure
    if (r.ok && menage.file.length) n.push(pushButton("Suivante (" + menage.file.length + " restante(s))", menageSuivante, "go"));
    else { menageStop(); if (r.ok) menage = { actif: false, ids: {}, file: [], total: 0, action: "" }; }
  }
  if (r.ok) { aRecharger = true; comptesLu = 0; rapportsLu = 0; habitudesLu = 0; routinesLu = 0; aToiLu = {}; }
  if (r.ok && (data.action === "session-fermer" || data.action === "session-nouvelle")) sessionsLu = 0;
  if (r.ok && data.action === "session-fermer") ficheEcran = ouvrirSessions;  // the closed session can no longer be reloaded
  toastEnAttente = [r.ok ? "✅ " + sansEmoji(r.resume || "fait") : "❌ " + (r.erreur || r.resume || "échec"), !r.ok];
  if (r.ok) (data.brouillon || []).forEach(function (k) { delete brouillons[k]; });
  n.push(backButton());
  showConfirm(n);
}
// The one confirmation sheet: everything it says (Quoi / Sur quoi / Effet / Risque, button, wait message) comes from the server.
function prepare(data, note) {
  var payload = Object.assign({}, data), a = CATALOGUE && CATALOGUE[data.action];
  delete payload.brouillon;  // kept on the phone only
  if (a && (a.type === "lecture" || a.ouvert)) {
    post("/api/action/lire", payload).then(function (r) { showResult(r, data); }).catch(offline);
    return;
  }
  post("/api/action/preparer", payload).then(function (d) {
    if (!d.ok) return showConfirm([el("div", "err", d.erreur || "refusé"), backButton()]);
    pending = d.jeton;
    var haut = (deco(d.risque) || [])[1] === "rouge";  // no green button under a red risk (todo #269)
    var ok = el("button", d.danger || haut ? "confirm danger" : "confirm", d.confirmer);
    ok.type = "button";
    ok.addEventListener("click", function () {
      ok.disabled = true;
      var jeton = pending;
      pending = null;
      if (d.attente) showConfirm([el("div", "muted", d.attente)]);
      post("/api/action/confirmer", { jeton: jeton, confirme: true }).then(function (r) {
        if (r.reconnecte) return prepare(data, "🔄 " + MSG_RECONNECTE + " : relis et confirme à nouveau.");
        showResult(r, data);
      }).catch(offline);
    });
    var liste = d.detail_brut ? [el("pre", "terminal habitude-texte", (d.detail || []).join("\n"))]  // todo #429: the exact text signed
      : (d.detail || []).map(function (l) { return el("div", "row", l); });  // todo #417: every action signed, listed
    showConfirm([note ? el("div", "muted", note) : el("span"), kv("Quoi", d.quoi), kv("Sur quoi", surCompte(data, d.sur_quoi))].concat(liste,
                [kv("Effet", d.effet), kv("Risque", d.risque), el("div", "muted", "Valable " + d.expire_s + " s."), ok, backButton()]));
  }).catch(offline);
}
// Chantier card (todo #466): where it stands, the todos (a tap opens the card; closing is a conscious act there, no ✅ in the
// list), the questions with their answer, then « + Ajouter une todo » folded and « Archiver » as a text button at the very bottom.
function openFiche(sujet) {
  current = sujet; ficheEcran = function () { openFiche(sujet); }; ficheRetour = null;
  fetch("/api/chantier/" + encodeURIComponent(sujet), { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) {
    var nodes = [];
    if (!d.ok) nodes.push(el("div", "err", "Indisponible (" + d.erreur + ")"));
    else {
      nodes.push(el("strong", null, d.titre || d.sujet));
      nodes.push(el("p", null, d.ou_en_est));
      if (d.reprise.length) nodes.push(plie("Reprise (" + d.reprise.length + ")", d.reprise.map(function (l) { return el("div", "muted", l); })));
      if (d.todos.length) nodes.push(el("h2", null, "Todos (" + d.todos.length + ")"));
      d.todos.forEach(function (t) {
        nodes.push(listButton(t.titre, function () { openTodo(t.id, function () { openFiche(sujet); }); }));
      });
      d.questions.forEach(function (q) {
        var r = el("div", "row", "❓ " + q.texte);
        nodes.push(r);
      });
      nodes.push(plie("+ Ajouter une todo", [form("todo", { chantier: d.sujet && d.sujet !== "-" ? d.sujet : "" })]));
      if (d.sujet && d.sujet !== "-") nodes.push(form("chantier-archiver", { sujet: d.sujet }, null, "texte"));
    }
    fill("fiche", nodes);
    document.getElementById("fiche").scrollTop = 0;
    setOpen("fiche", true);
  }).catch(function () { toast("❌ " + MSG_HORS_LIGNE, true); });
}
document.getElementById("retour").addEventListener("click", function () {
  if (ficheRetour) { var f = ficheRetour; ficheRetour = null; return f(); }
  current = null; ficheEcran = null;
  document.body.classList.remove("pilote");
  setOpen("fiche", false);
  refresh();
});

// -- Todos (todo #242): readings of the catalogue (POST /api/action/lire), the same commands as Telegram's /todo ----------
// Every value is the CLI's (whitelisted by the server); pages of 10 like Telegram. No rule here: the buttons are form().
var PAGE = 10, ICONES = { high: "🔴", normal: "🟠", low: "🟡" }, STATUTS = { todo: "à faire", done: "terminée", cancelled: "annulée" };
var vue = { filtre: "open", categorie: "", page: 0 }, categories = [], ficheEcran = null, ficheRetour = null;
function lire(id, ctx) {
  return post("/api/action/lire", Object.assign({ action: id }, ctx || {})).then(function (r) {
    if (!r.ok || !r.donnees) throw new Error(r.erreur || r.resume || "lecture impossible");
    return r.donnees;
  });
}
function listButton(label, fn) {
  var b = el("button", "list", label);
  b.type = "button";
  b.addEventListener("click", fn);
  return b;
}
function chip(label, on, fn) { var b = el("button", "chip" + (on ? " on" : ""), label); b.type = "button"; b.addEventListener("click", fn); return b; }
function jour(iso) { return iso ? new Date(iso.length === 10 ? iso + "T12:00:00" : iso).toLocaleDateString("fr-FR", { day: "numeric", month: "short" }) : ""; }
function ligneTodo(t, choix) {
  var b = listButton("", function () { if (choix) choix(); else openTodo(t.id); }), txt = el("span");
  txt.appendChild(el("div", null, (choix ? (menage.ids[t.id] ? "☑ " : "☐ ") : "") + (ICONES[t.priority] || "⚪") + " #" + t.id + " " + t.title));
  var info = [t.overdue ? "en retard" : t.today ? "aujourd'hui" : t.due_date ? "échéance " + jour(t.due_date) : "",
              t.badges || "", t.category || "", t.status !== "todo" ? STATUTS[t.status] || t.status : ""].filter(Boolean);
  if (info.length) txt.appendChild(el("div", "muted", info.join(" · ")));
  b.appendChild(txt);
  return b;
}
// 🧹 Ménage (todo #243): pick several todos, then ✖ Abandonner runs the catalogue's signed `abandon` once per todo: one sheet each,
// so each proof names its own number. Nothing here knows the command; « Retour » on a sheet stops the rest.
var menage = { actif: false, ids: {}, file: [], total: 0, action: "" };
function menageAction() { return Object.keys(CATALOGUE || {}).filter(function (k) { return CATALOGUE[k].groupe; })[0] || ""; }
function menageIds() { return Object.keys(menage.ids).filter(function (k) { return menage.ids[k]; }); }
function menageStop() { menage.file = []; menage.total = 0; }
function menageSuivante() {
  var id = menage.file.shift();
  if (!id) return;
  prepare({ action: menage.action, id: id }, "🧹 Ménage : todo " + (menage.total - menage.file.length) + " sur " + menage.total);
}
function ligneMenage(t, compteur) {
  var b = ligneTodo(t, function () {
    menage.ids[t.id] = !menage.ids[t.id];
    var d = b.firstChild.firstChild;
    d.textContent = (menage.ids[t.id] ? "☑ " : "☐ ") + d.textContent.slice(2);
    compteur.textContent = "✖ Abandonner la sélection (" + menageIds().length + ")";
    compteur.disabled = !menageIds().length;
  });
  return b;
}
function menageBarre() {
  var box = el("div", "row"), go = pushButton("", function () {
    menage.file = menageIds(); menage.total = menage.file.length; menage.action = menageAction();
    menage.ids = {};
    menageSuivante();
  }, "danger");
  go.textContent = "✖ Abandonner la sélection (" + menageIds().length + ")";
  go.disabled = !menageIds().length;
  box.appendChild(go);
  box.appendChild(pushButton("« Quitter le ménage", function () { menage = { actif: false, ids: {}, file: [], total: 0, action: "" }; loadTodos(); }));
  return { box: box, go: go };
}
function openRanger() {
  var recharger = openRanger;
  lire("ranger-proposition").then(function (d) {
    var n = [el("strong", null, "🗂 Ranger les todos dans les chantiers")];
    if (!d.lines.length) n.push(el("div", "muted", "Rien à ranger : chaque todo est déjà dans son chantier."));
    d.lines.forEach(function (l) {
      var row = el("div", "row");
      row.appendChild(el("div", null, "#" + l.id + " " + l.titre));
      row.appendChild(el("div", "muted", (l.de || "aucun chantier") + " → " + (l.vers || "aucun chantier")));
      n.push(row);
    });
    if (d.lines.length && d.empreinte) {
      n.push(el("div", "muted", "Proposition " + d.empreinte + " : si une todo change avant ta confirmation, rien n'est modifié."));
      n.push(form("ranger-appliquer", { empreinte: d.empreinte }));
    }
    showOverlay(n, recharger, null);
  }).catch(function (e) { overlayErreur(e, recharger, null); });
}
// 🧹 Ménage proposé (todo #362): `menage proposer`, applied whole by ONE signed ✅ on its fingerprint (the CLI checks it again)
function openMenagePropose() {
  lire("menage-proposition").then(function (d) {
    var n = [el("strong", null, "🧹 Ménage proposé (" + d.n + ")")];
    if (d.par) n.push(el("div", "muted", "Préparé par " + d.par));
    d.cancel.map(function (c) { return ["✖ #" + c.id + " " + c.titre, c.refus || c.raison, c.refus]; })
      .concat(d.archiver.map(function (a) { return ["🗄 Archiver le chantier " + a.sujet, a.refus, a.refus]; }))
      .concat((d.corbeille || []).map(function (c) { return ["🗑 Corbeille : skill " + c.nom, c.refus || c.raison, c.refus]; }))
      .concat((d.dossiers || []).map(function (c) { return ["📁 Corbeille (var/.trash) : " + c.chemin, c.refus || c.raison, c.refus]; })).forEach(function (l) {
        var row = el("div", "row");
        row.appendChild(el("div", null, l[0]));
        if (l[1]) row.appendChild(el("div", l[2] ? "err" : "muted", l[1]));
        n.push(row);
      });
    if (d.bloquee) n.push(el("div", "err", "Proposition à refaire : " + d.bloquee));
    else if (d.empreinte) n.push(el("div", "muted", "Abandon = annulée, notes gardées, ↩️ Rouvrir possible. Arrêt au premier refus."), form("menage-lot", { empreinte: d.empreinte }));
    showOverlay(n, openMenagePropose, null);
  }).catch(function (e) { overlayErreur(e, openMenagePropose, null); });
}
function loadTodos() {
  var FILTRES = (CATALOGUE.todos.champs[0].choix || []);
  lire("todos", { filtre: vue.filtre, categorie: vue.categorie }).then(function (d) {
    categories = d.categories || [];
    var nodes = [], chips = el("div", "chips");
    FILTRES.forEach(function (f) { chips.appendChild(chip(f[1], vue.filtre === f[0] && !vue.categorie, function () { vue = { filtre: f[0], categorie: "", page: 0 }; loadTodos(); })); });
    categories.forEach(function (c) { chips.appendChild(chip("🏷 " + c, vue.categorie === c, function () { vue = { filtre: "open", categorie: c, page: 0 }; loadTodos(); })); });
    nodes.push(el("div", "muted", d.open + " ouverte(s) · " + d.overdue + " en retard · " + d.today + " aujourd'hui · " + d.high +
                  " prioritaire(s)" + (d.branches ? " · " + d.branches + " branche(s)" : "")));
    nodes.push(chips);
    var propose = el("div");
    nodes.push(propose);
    lire("menage-proposition").then(function (m) { if (m.n) propose.appendChild(listButton("🧹 Ménage proposé (" + m.n + ")", openMenagePropose)); }, function () {});
    var pages = Math.max(1, Math.ceil(d.tasks.length / PAGE));
    vue.page = Math.min(vue.page, pages - 1);
    var chunk = d.tasks.slice(vue.page * PAGE, (vue.page + 1) * PAGE);
    if (!chunk.length) nodes.push(el("div", "muted", "Rien ici."));
    var barre = menage.actif ? menageBarre() : null;
    if (barre) nodes.push(barre.box);
    chunk.forEach(function (t) { nodes.push(barre && t.status === "todo" ? ligneMenage(t, barre.go) : ligneTodo(t)); });
    if (pages > 1) {
      var nav = el("div", "nav");
      if (vue.page > 0) nav.appendChild(pushButton("‹ Préc.", function () { vue.page--; loadTodos(); }));
      nav.appendChild(el("span", "muted", "Page " + (vue.page + 1) + "/" + pages));
      if (vue.page < pages - 1) nav.appendChild(pushButton("Suiv. ›", function () { vue.page++; loadTodos(); }));
      nodes.push(nav);
    }
    if (vue.filtre === "open" && !vue.categorie && d.fond) nodes.push(listButton("🌱 En fond (" + d.fond + ")", function () { vue = { filtre: "fond", categorie: "", page: 0 }; loadTodos(); }));
    nodes.push(listButton("🧹 Ménage des todos", function () { menage = { actif: !menage.actif, ids: {}, file: [], total: 0, action: "" }; loadTodos(); }));
    nodes.push(listButton("🗂 Ranger dans les chantiers", openRanger));
    nodes.push(plie("➕ Ajouter une todo", [form("todo", { chantier: "" }, { categories: categories })]));
    nodes.push(listButton("🌿 État des fusions", openEtat));
    fill("toutes", nodes);
  }).catch(function (e) { fill("toutes", [el("div", "err", "Indisponible (" + (e.message || e) + ")")]); });
}
function showOverlay(nodes, recharger, retour) {
  ficheEcran = recharger; ficheRetour = retour || null;
  current = "fiche";
  var f = document.getElementById("fiche");
  f.classList.remove("plein", "clavier"); f.style.height = f.style.top = f.style.bottom = "";
  document.body.classList.remove("pilote");
  fill("fiche", nodes);
  f.scrollTop = 0;
  setOpen("fiche", true);
}
function overlayErreur(e, recharger, retour) { showOverlay([el("div", "err", "Indisponible (" + (e.message || e) + ")")], recharger, retour); }
function plie(titre, nodes) {
  var d = el("details"), s = el("summary", null, titre);
  d.appendChild(s);
  nodes.forEach(function (n) { d.appendChild(n); });
  return d;
}
function libelle(action, champ, code) {
  var a = CATALOGUE && CATALOGUE[action], c = a && a.champs.filter(function (x) { return x.nom === champ; })[0];
  var o = c && c.choix.filter(function (x) { return x[0] === code; })[0];
  return o ? o[1] : code;
}
// Todo card (todo #466): the decision first (title, in plain words, notes, then the buttons: ONE filled), the branch's change, then
// the metadata and history folded, « Modifier » folded, Abandonner (red) and the after-merge actions last.
function openTodo(id, retour) {
  var ctx = { id: String(id) }, recharger = function () { openTodo(id, retour); };
  var cats = categories.length ? Promise.resolve() : lire("todos", { filtre: "urgent" }).then(function (d) { categories = d.categories || []; }, function () {});
  cats.then(function () { return lire("todo-fiche", ctx); }).then(function (t) {
    var n = [el("strong", null, (ICONES[t.priority] || "⚪") + " " + t.title), el("div", "mono pale", "#" + t.id + (t.chantier ? " · " + t.chantier : "")),
             el("p", null, t.clair ? t.clair : "En clair : — (pas encore rédigé)")];
    if (t.notes) n.push(el("div", "pre notes", t.notes));
    var branche = t.branch && t.branch.ahead ? t.branch : null;
    if (t.status !== "todo") n.push(form("rouvrir", ctx, null, "confirm"));
    else {
      if (branche) n.push(form("merge", ctx, null, "confirm"), form("done", ctx));
      else n.push(form("done", ctx, null, "confirm"));
      var annuler = el("div");  // ↩️ only on the todo of the last automatic merge (todo #254), still in main
      n.push(annuler);
      lire("fusion-auto", {}).then(function (f) {
        if (f.cible && !f.annulee && f.fusion && String(f.fusion.todo_id) === String(id)) annuler.appendChild(form("annuler-fusion", { fusion: f.cible }));
      }).catch(function () {});
    }
    if (branche) n.push(listButton("🌿 Ce qui change : " + branche.branch + " (" + branche.ahead + " commit(s))", function () { openCommits(id, recharger); }));
    var due = t.due_date ? jour(t.due_date) : "aucune";
    if (t.status === "todo" && t.due_date && t.due_date < new Date().toISOString().slice(0, 10)) due += " · en retard";
    var meta = [kv("Statut", STATUTS[t.status] || t.status), kv("Priorité", libelle("priorite", "priorite", t.priority)), kv("Catégorie", t.category || "—"),
                kv("Échéance", due), kv("Nature", t.nature === "fond" ? "🌱 fond" : "🔥 en tête")];
    if (t.chantier) meta.push(kv("Chantier", t.chantier));
    if (t.created_at) meta.push(kv("Créée le", jour(t.created_at) + " (" + (t.source || "?") + ")"));
    if (t.done_at) meta.push(kv("Terminée le", jour(t.done_at)));
    n.push(plie("Détails (statut, échéance)", meta));
    var hist = (t.history || []).slice(-5).map(function (h) {
      var d = h.details || {}, quoi = h.action === "status" ? (STATUTS[d.from] || d.from) + " → " + (STATUTS[d.to] || d.to)
        : h.action === "edit" ? "modifiée : " + (d.cles || []).join(", ") : h.action;
      return el("div", "muted", "• " + jour(h.at) + " · " + quoi + (d.by ? " (" + d.by + ")" : ""));
    });
    if (hist.length) n.push(plie("🕓 Historique", hist));
    if (t.status === "todo") {
      n.push(plie("✏️ Modifier", [form("priorite", ctx), form("echeance", ctx), form("echeance-date", ctx),
        form("categorie", ctx, { categories: categories.length ? categories : [t.category] }), form("titre", ctx),
        form("notes", ctx), form("clair", ctx), form(t.nature === "fond" ? "tete" : "fond", ctx)]));
      n.push(form("abandon", ctx));
    }
    n.push(pushButton("🧰 Actions après fusion", function () { openApres(id, recharger); }, "texte"));
    showOverlay(n, recharger, retour);
  }).catch(function (e) { overlayErreur(e, recharger, retour); });
}
function openCommits(id, retour) {
  lire("commits", { id: String(id) }).then(function (b) {
    var n = [el("strong", null, "🌿 " + (b.branch || (b.branches || []).join(", ") || "aucune branche")),
             el("div", "muted", b.ahead + " commit(s) hors main")];
    (b.commits || []).forEach(function (c) { n.push(el("div", "row", c)); });
    showOverlay(n, function () { openCommits(id, retour); }, retour);
  }).catch(function (e) { overlayErreur(e, null, retour); });
}
// 🧰 After a merge: the exact text of each action (its commands), then ✅ Exécuter (signed on todo-<id>:<n>) or ✖ Refuser.
function openApres(id, retour) {
  var recharger = function () { openApres(id, retour); };
  lire("apres", { id: String(id) }).then(function (d) {
    var n = [el("strong", null, "🧰 Actions après la fusion de #" + id)];
    if (!d.requests.length) n.push(el("div", "muted", "Aucune action prévue après la fusion."));
    d.requests.forEach(function (r) {
      var row = el("div", "row");
      row.appendChild(el("div", null, "#" + r.n + " · " + r.line + " (" + r.status + ")"));
      row.appendChild(el("div", "pre", r.text));
      if (r.status === "pending" || r.status === "interrupted") {
        row.appendChild(form("apres-run", { id: String(id), n: String(r.n) }));
      }
      n.push(row);
    });
    showOverlay(n, recharger, retour);
  }).catch(function (e) { overlayErreur(e, recharger, retour); });
}
// 🔀 Tout fusionner: the lot of `branch lot`, its order and EVERY command of its after-merge actions (the signature on the
// fingerprint covers them); ➖ Retirer / ➕ Remettre read the lot again without those todos.
function openLot(sans) {
  var recharger = function () { openLot(sans); };
  lire("lot", { sans: sans.join(",") }).then(function (d) {
    var n = [el("strong", null, "🔀 Tout fusionner")];
    sans.forEach(function (t) { n.push(pushButton("➕ Remettre #" + t, function () { openLot(sans.filter(function (x) { return x !== t; })); })); });
    if (!d.lot.length || !d.empreinte) n.push(el("div", "muted", "Rien à fusionner : aucune tâche prête."));
    // todo #401: the real order, and for each todo its branch, commits and files touched (since it left main)
    if (d.lot.length) n.push(el("h2", null, "Sera fusionné, dans cet ordre (" + d.lot.length + ")"));
    d.lot.forEach(function (t, i) {
      var row = el("div", "row");
      row.appendChild(el("div", null, (i + 1) + ". #" + t.todo_id + " " + t.title));
      t.repos.forEach(function (r) {
        row.appendChild(el("div", "muted", "🌿 " + (r.label || r.branch) + " · " + r.ahead + " commit(s) · " + r.files + " fichier(s) touché(s)"));
      });
      if (t.apres.length) row.appendChild(el("div", "muted", "empilée : passe après " + t.apres.map(function (p) { return "#" + p; }).join(", ") +
        " (ses commits et fichiers comptent aussi les siens)"));
      if (t.actions_error) row.appendChild(el("div", "err", "⚠️ déclaration après fusion illisible, rien ne sera lancé : " + t.actions_error));
      t.actions.forEach(function (a) {
        var quoi = "🧰 action " + a.n + " : " + a.action + " " + (a.args || []).join(" ");
        if (a.plan === "lancée") {
          row.appendChild(el("div", null, quoi + " (" + (a.reason || "raison non précisée") + ")"));
          row.appendChild(el("div", "pre", (a.command || []).map(function (c) { return "• " + c; }).join("\n")));
        } else row.appendChild(el("div", "muted", sansEmoji(quoi) + (a.plan === "redémarrage du lot" ? " → fondue dans le redémarrage unique" : " : jamais lancée (" + (a.error || "invalide") + ")")));
      });
      row.appendChild(pushButton("➖ Retirer #" + t.todo_id, function () { openLot(sans.concat([t.todo_id])); }));
      n.push(row);
    });
    if (d.ecartees.length) {  // shown, not folded: what will NOT be merged, and why (todo #401)
      n.push(el("h2", null, "Ne sera pas fusionné (" + d.ecartees.length + ")"));
      d.ecartees.forEach(function (e) { n.push(el("div", "muted", "#" + e.todo_id + " " + e.title + " : " + e.raison)); });
    }
    if (d.lot.length && d.empreinte) {
      n.push(el("h2", null, "En cas d'échec"));
      ["Chaque tâche, dans l'ordre : fusion d'essai et tests, puis fusion dans main et tâche terminée.",
       "Tests rouges ou trop longs : la tâche reste ouverte, main n'a rien d'elle, les suivantes continuent et elle est retentée à la fin ; celles empilées sur elle attendent.",
       "Tout autre refus (conflit, branche qui bouge, fusion déjà en cours) arrête le lot : ce qui est déjà fusionné le reste, les suivantes restent ouvertes.",
       "Une branche qui bouge avant la confirmation : refus, rien n'est fusionné.",
       "Actions après fusion : lancées seulement si tout le lot est fusionné.",
       "À la fin : compte rendu fusionnées / restées ouvertes et pourquoi, aussi dans le journal."
      ].forEach(function (l) { n.push(el("div", "muted", "• " + l)); });
      n.push(form("fusion-lot", { ids: d.ids.join(","), empreinte: d.empreinte }));
    }
    showOverlay(n, recharger, null);
  }).catch(function (e) { overlayErreur(e, recharger, null); });
}

// 🌿 État des fusions (todo #255): `branch etat`, read only. Branches in progress, branches waiting for the user's 🔀 (and the
// CLI's own reasons), automatic merges of the orchestrator (todo still waiting for ✅), and ↩️ on the last one.
function brancheEtat(t, b) {
  var row = el("div", "row");
  row.appendChild(el("div", null, "#" + t.todo_id + " " + (t.title || "(todo introuvable)")));
  row.appendChild(el("div", "muted", b.label + " · " + b.ahead + " commit(s)" + (b.commit ? " · " + b.commit.sha + " · " + jour(b.commit.date) : "")));
  if (b.commit && b.commit.subject) row.appendChild(el("div", "muted", b.commit.subject));
  var chip = el("span", "chip" + (!b.worktree || b.worktree.propre ? " ok" : ""),
    b.worktree ? (b.worktree.propre ? "worktree propre" : "⚠️ worktree pas propre") : "pas de worktree");
  row.appendChild(chip);
  return row;
}
function openEtat() {
  lire("etat-fusions", {}).then(function (d) {
    var n = [el("strong", null, "🌿 État des fusions")];
    n.push(el("h2", null, "En cours (" + d.en_cours.length + ")"));
    if (!d.en_cours.length) n.push(el("div", "muted", "Aucune branche que l'orchestrateur puisse fusionner seul."));
    d.en_cours.forEach(function (t) { t.branches.forEach(function (b) { n.push(brancheEtat(t, b)); }); });
    n.push(el("h2", null, "En attente de ta fusion (" + d.attente.length + ")"));
    if (!d.attente.length) n.push(el("div", "muted", "Rien n'attend ta fusion."));
    d.attente.forEach(function (t) {
      t.branches.forEach(function (b) { n.push(brancheEtat(t, b)); });
      t.raisons.forEach(function (r) {
        n.push(el("div", "err", "⛔ " + r.reason));
        if (r.files.length) n.push(el("div", "pre", r.files.join("\n")));
      });
    });
    n.push(el("h2", null, "Fusionnées seules (" + d.auto.length + ")"));
    if (!d.auto.length) n.push(el("div", "muted", "Aucune fusion automatique pour l'instant."));
    d.auto.forEach(function (f) {
      var row = el("div", "row");
      row.appendChild(el("div", null, "#" + f.todo_id + " " + (f.title || "(todo introuvable)")));
      row.appendChild(el("div", "muted", jour(new Date(f.at * 1000).toISOString()) + " · " + f.sha));
      row.appendChild(el("span", "chip" + (!f.annulee && f.a_valider ? " ok" : ""),
        f.annulee ? "↩️ annulée" : f.a_valider ? "attend ta validation" : "plus à valider"));
      n.push(row);
    });
    if (d.annulation.cible && !d.annulation.annulee) n.push(form("annuler-fusion", { fusion: d.annulation.cible }));
    var a = d.autorisation;  // todo #495: the signed advance authorisation
    if (a) n.push(el("h2", null, "Autorisation d'avance"));
    if (a && a.valide) {
      n.push(el("div", null, "✅ Jusqu'au " + a.jusqu_au + " inclus, l'orchestrateur fusionne seul tout sauf la liste de sécurité."));
      n.push(el("div", "muted", "Donnée par " + a.by + " · liste de sécurité " + a.perimetre));
      n.push(form("autorisation-retirer", {}));
    } else if (a) {
      n.push(el("div", "muted", "Aucune : l'orchestrateur ne fusionne seul que hors fichiers sensibles."));
      n.push(el("div", "muted", a.fins.map(function (f) { return f.jours + " j → jusqu'au " + f.jusqu_au; }).join(" · ")));
      n.push(form("autorisation", { perimetre: a.perimetre }));
    }
    showOverlay(n, openEtat, null);
  }).catch(function (e) { overlayErreur(e, openEtat, null); });
}

// What a result sheet offers next, from the result's id (see showResult).
var SUITES = {
  // todo #297: after a new session, offer to close the old one (the session the user was in, else the list to pick one)
  "session-nouvelle": function (r) {
    var box = el("div", "row"), ancienne = sessionOrigine;
    box.appendChild(listButton("💬 Ouvrir la nouvelle session", function () { aRecharger = false; closeConfirm(); ouvrirSession(r.id, function () { ouvrirSessions(); }); }));
    if (ancienne && ancienne !== r.id) box.appendChild(listButton("✖ Fermer l'ancienne session (" + ancienne + ")…", function () { fermerSession(ancienne); }));
    else box.appendChild(listButton("✖ Fermer une ancienne session…", function () { aRecharger = false; closeConfirm(); ouvrirSessions(); }));
    return box;
  }
};
// -- Santé détaillée (todo #247): the readings of Telegram's /sante (health reflexes, guard, battery), the signed button of a
// known breakdown (form "reflexe" on the exact "<réflexe>:<empreinte>" of the diagnosis shown) and the full analysis.
var NIVEAUX = { ok: "🟢 OK", warn: "🟠 À surveiller", critical: "🔴 Critique" };
var RENDUS = { "sante-analyse": function (d) {  // the report of `run sante-serveur`, shown in the result sheet
  var n = [el("strong", null, "🩺 Santé serveur — " + (NIVEAUX[d.level] || "⚪ ?"))];
  if (d.summary) n.push(el("p", null, d.summary));
  (d.points || []).forEach(function (p) { n.push(el("div", "row", (NIVEAUX[p.level] || "⚪").split(" ")[0] + " " + p.label + " — " + p.detail)); });
  if ((d.recommendations || []).length) n.push(el("h2", null, "À faire"), el("div", "pre", d.recommendations.map(function (r) { return "• " + r; }).join("\n")));
  return n;
} };
RENDUS["compte-connexion-a"] = RENDUS["compte-connexion-b"] = feuilleConnexion;  // todo #336
function lireOuErreur(id) { return lire(id).then(function (d) { return { d: d }; }, function (e) { return { e: e.message || String(e) }; }); }
function openSante() {
  Promise.all([lireOuErreur("sante-garde"), lireOuErreur("sante-reflexes"), lireOuErreur("sante-batterie")]).then(function (r) {
    var garde = r[0], pannes = r[1], bat = r[2], n = [el("strong", null, "🩺 Santé détaillée")];
    var diags = pannes.d ? pannes.d.diagnostics || [] : [];
    if (garde.e) n.push(el("div", "err", "ultron-guard : vérification impossible (" + garde.e + ")"));
    else if (garde.d.ok) n.push(el("div", "row", "🛡 ultron-guard chargé : les actions de Hermes sont encadrées."));
    else n.push(el("div", "err", "🚨 ultron-guard n'est PAS chargé (" + (garde.d.problem || "?") + ") : les actions de Hermes ne sont plus encadrées."));
    n.push(el("h2", null, "Pannes connues"));
    if (pannes.e) n.push(el("div", "err", "Indisponible (" + pannes.e + ")"));
    else if (!diags.length) n.push(el("div", "muted", "Aucune panne connue en cours."));
    diags.forEach(function (x) {
      var row = el("div", "row");
      row.appendChild(el("div", null, "🩺 " + x.diagnostic));
      if (x.detail) row.appendChild(el("div", "muted", x.detail));
      if (x.bouton && x.bouton.cible) {
        row.appendChild(el("div", "muted", "Proposé : " + (x.bouton.label || "agir") + " · " + x.bouton.cible +
          ". Juste avant d'agir, ultron-user revérifie chaque condition ; sinon rien n'est fait."));
        row.appendChild(form("reflexe", { panne: x.bouton.cible }));
      } else if (x.action) row.appendChild(el("div", "muted", x.action));
      n.push(row);
    });
    n.push(el("h2", null, "Batterie"), bat.e ? el("div", "err", "Batterie illisible (" + bat.e + ")") : el("div", "pre", bat.d.text || "?"));
    n.push(el("h2", null, "Analyse complète"), el("div", "muted", "Collecte des métriques puis Claude (~15 s)."), form("sante-analyse", {}));
    showOverlay(n, openSante, null);
  });
}

var chargement = false, derniere = "";
function maintenant() { return new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }); }
function showErreur(msg) {
  var e = document.getElementById("erreur");
  e.textContent = msg || ""; e.hidden = !msg;
  document.getElementById("maj").textContent = msg ? "hors ligne" + (derniere ? " · dernière MAJ " + derniere : "") : "mis à jour " + derniere;
}
function refresh(manuel) {
  if (chargement) return Promise.resolve();
  chargement = true;
  return chargerCatalogue().then(function () { return fetch("/api/state", { cache: "no-store" }); }).then(function (r) {
    if (r.status === 403) throw new Error("Accès refusé : cet appareil n'est pas autorisé.");
    if (!r.ok) throw new Error("Serveur en erreur (" + r.status + ").");
    return r.json();
  }).then(function (s) {
    showChantiers(s.chantiers); showAToi(s.a_toi); showSante(s.sante);
    showEtat(s.sante, s.quota);
    if (!document.getElementById("tab-moi").hidden) chargerCatalogue().then(function () { if (CATALOGUE) loadTodos(); });
    loadFil(); loadComptes(manuel); loadRapports(manuel); loadHabitudes(manuel); loadRoutines(manuel); loadFaits(manuel); loadSessions(manuel); loadAToi(manuel);
    derniere = maintenant();
    showErreur("");
    if (manuel) toast("À jour · " + derniere);
  }).catch(function (e) {
    var msg = e instanceof TypeError ? MSG_HORS_LIGNE : e.message;
    showErreur(msg);
    if (manuel) toast("❌ " + msg, true);
  }).then(function () { chargement = false; });
}
function actif() { return !document.hidden && current === null; }
// Pull to refresh: only from the very top of the page, with a finger drag; the indicator moves by transform/opacity only.
(function () {
  var page = document.getElementById("page"), ptr = document.getElementById("ptr"), y0 = null, dy = 0, SEUIL = 70;
  function draw(d, on) { ptr.style.transform = "translateY(" + d + "px) rotate(" + (d * 3) + "deg)"; ptr.style.opacity = on ? Math.min(1, d / SEUIL) : 0; }
  page.addEventListener("touchstart", function (e) { y0 = page.scrollTop <= 0 && current === null && e.touches.length === 1 ? e.touches[0].clientY : null; dy = 0; }, { passive: true });
  page.addEventListener("touchmove", function (e) {
    if (y0 === null) return;
    dy = Math.max(0, Math.min(110, (e.touches[0].clientY - y0) * 0.5));
    if (page.scrollTop > 0) { y0 = null; draw(0, false); return; }
    draw(dy, dy > 0);
  }, { passive: true });
  function fin() {
    if (y0 === null) return;
    var go = dy >= SEUIL * 0.8;
    y0 = null; dy = 0;
    ptr.classList.add("lache"); draw(go ? 40 : 0, go);
    if (go) refresh(true).then(function () { draw(0, false); }); 
    setTimeout(function () { ptr.classList.remove("lache"); }, 300);
  }
  page.addEventListener("touchend", fin, { passive: true });
  page.addEventListener("touchcancel", fin, { passive: true });
})();
// Web Push (todo #240): the service worker shows notifications; subscribing needs a tap (iOS asks permission then),
// the installed app (iOS 16.4+) and HTTPS. The server only keeps the subscription of an allowed device.
var push = { reg: null, cle: null };
function b64ToBytes(s) {
  var raw = atob((s + "===".slice((s.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/")), out = new Uint8Array(raw.length);
  for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}
function showNotifs(etat, msg) {
  var card = document.getElementById("notifs");
  card.hidden = false;
  var nodes = [el("div", "muted", msg)];
  if (etat === "off") nodes.push(pushButton("🔔 Activer les notifications", abonner, "go"));
  if (etat === "on") nodes.push(pushButton("🔕 Couper les notifications", desabonner));
  fill("notifs", nodes);
}
function pushButton(label, fn, cls) { var b = el("button", cls || null, label); b.type = "button"; b.addEventListener("click", fn); return b; }
function abonner() {
  if (!push.reg || !push.cle) return showNotifs("off", "Notifications indisponibles pour le moment.");
  var demande = typeof Notification !== "undefined" ? Notification.requestPermission() : Promise.resolve("granted");
  demande.then(function (p) {
    if (p !== "granted") throw new Error("permission refusée");
    return push.reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(push.cle) });
  }).then(function (sub) {
    return post("/api/push/abonner", { abonnement: sub.toJSON() });
  }).then(function (r) {
    if (!r.ok) throw new Error(r.erreur || "abonnement refusé");
    showNotifs("on", "Activées sur cet appareil."); toast("🔔 Notifications activées");
  }).catch(function (e) {
    var refus = typeof Notification !== "undefined" && Notification.permission === "denied";
    showNotifs(refus ? "denied" : "off", refus ? "Refusées : Réglages > Notifications > Ultron pour les autoriser." : "❌ " + (e.message || e));
  });
}
function desabonner() {
  push.reg.pushManager.getSubscription().then(function (sub) { return sub ? sub.unsubscribe() : true; }).then(function () {
    return post("/api/push/desabonner", {});
  }).then(function () { showNotifs("off", "Coupées sur cet appareil."); }).catch(function () { toast("❌ " + MSG_HORS_LIGNE, true); });
}
function initPush() {
  if (!("serviceWorker" in navigator)) return;
  navigator.serviceWorker.addEventListener("message", function (e) { if (e.data && e.data.onglet) selectTab(e.data.onglet); });
  navigator.serviceWorker.register("/sw.js").then(function (reg) {
    push.reg = reg;
    if (!("PushManager" in window)) return showNotifs("na", "Ajoute l'app à l'écran d'accueil (Partager > Sur l'écran d'accueil) pour recevoir les notifications.");
    return fetch("/api/push", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (info) {
      if (!info.ok || !info.https) return;
      push.cle = info.cle;
      return reg.pushManager.getSubscription().then(function (sub) {
        if (!sub) return showNotifs("off", "Reçois ici les messages d'Ultron, en plus de Telegram.");
        showNotifs("on", "Activées sur cet appareil.");
        if (!info.abonne) post("/api/push/abonner", { abonnement: sub.toJSON() }).catch(function () {});  // server forgot it
      });
    });
  }).catch(function () {});
}
// -- 📥 Analyser (todo #248): one screen, every button is form() of the catalogue; nothing here knows a command ------------
var analyse = { video: null, fichier: null, session: null, son: null };
function dureeTexte(s) { var m = Math.floor(s / 60), r = Math.round(s % 60); return m + " min " + (r < 10 ? "0" : "") + r + " s"; }
function suiteAnalyser(r, data) {
  var out = [], a = data && data.action;
  if (r.ok && r.duree !== undefined) out.push(el("div", null, r.duree ? "Vidéo de " + dureeTexte(r.duree) + ", analyse Opus de quelques minutes." : "Durée inconnue."));
  if (!r.ok || !a) return out;
  if (a === "savoir-verifier" && r.empreinte) {  // the fiche shown above, then 📚 Ranger signed on what was shown (todo #249)
    out.push(form("savoir-ranger", { source: data.source, id: data.id, n: data.n, empreinte: r.empreinte }));
    return out;
  }
  if (a === "joindre") {  // todo #293: from the Pilote, the path and the summary also go into the session (signed, journaled)
    analyse.fichier = null;
    if (data.session && r.donnees && r.donnees.pour_session)
      direct({ action: "session-ecrire", nom: data.session, texte: r.donnees.pour_session }, function () { toast("✍️ Écrit dans la session " + data.session); });
    out.push(el("div", "muted", data.session ? "Chemin et résumé écrits dans la session ; le résultat est aussi dans le Fil." : "Le résultat est aussi dans le Fil."));
    return out;
  } else if ((a === "video-analyse" || a === "video-tout") && r.donnees && r.donnees.actions && r.donnees.id)  // todo #441: batch
    analyse.propositions = { source: "video", id: analyse.video = r.donnees.id, items: r.donnees.actions, reponse: r.donnees.reponse || "" };
  else if (a === "video-collecte" && r.id) analyse.video = r.id;
  else return out;
  var next = el("button", "go", "➡️ Étape suivante");
  next.type = "button";
  next.addEventListener("click", function () { cancel(); ouvrirAnalyser(); });
  out.push(next);
  return out;
}
// todo #466: ONE link field and a choice of what to do with it (analyse the video by default, its duration, collect it, or present
// a page); each choice is the catalogue entry of the same name, its sheet unchanged.
var LIEN_ACTIONS = ["video-tout", "video-duree", "video-collecte"];
function lienAnalyser(depuis) {
  var box = el("div", "form"), dispo = LIEN_ACTIONS.filter(function (id) { return CATALOGUE && CATALOGUE[id]; });
  if (!dispo.length) return box;
  var champ = function (id, nom) { return CATALOGUE[id].champs.filter(function (c) { return c.nom === nom; })[0]; };
  var lien = textarea("analyser:lien", champ(dispo[0], "lien").libelle, champ(dispo[0], "lien").max), phrase = textarea("analyser:phrase", "Ta phrase (facultatif)", 500);
  lien.rows = 1;
  var sel = select("Que faire du lien", dispo.map(function (id) { return [id, sansEmoji(CATALOGUE[id].bouton).replace(/…$/, "")]; }),
                   dispo.indexOf(brouillons["analyser:action"]) >= 0 ? brouillons["analyser:action"] : dispo[0]);
  var go = pushButton("", function () {
    var id = sel.value, data = { action: id, lien: lien.value, brouillon: ["analyser:lien", "analyser:phrase"] };
    if (champ(id, "phrase")) data.phrase = phrase.value;
    if (champ(id, "session") && depuis.session) data.session = depuis.session;
    prepare(data);
  }, "confirm");
  function maj() {
    var id = sel.value, p = champ(id, "phrase");
    brouillons["analyser:action"] = id;
    phrase.hidden = !p; if (p) phrase.maxLength = p.max;
    go.textContent = sansEmoji(CATALOGUE[id].bouton);
  }
  sel.addEventListener("change", maj);
  maj();
  [lien, sel, phrase, go].forEach(function (n) { box.appendChild(n); });
  return box;
}
function titreAnalyser(t) { return el("h2", null, t); }
// 📎 joindre (todo #293): the file is STORED by POST /api/fichier (nothing run), then analysed by the catalogue's `joindre`
// (sheet + confirmation). The live voice note is a MediaRecorder recording sent the same way.
var vocal = { rec: null, debut: 0, timer: null, session: null, analyses: 0, boutons: [] }, envoiFichier = el("div", "muted");
function envoyerFichier(blob, nom, estVocal, renouvele, session) {  // estVocal: only the 🎙 recorder (todo #365), then no sheet (todo #379)
  var entetes = { "Content-Type": "application/octet-stream", "X-Nom-Fichier": encodeURIComponent(nom) };
  if (estVocal) entetes["X-Source"] = "vocal";
  envoiFichier.className = "muted";
  envoiFichier.textContent = "⏳ Envoi de " + nom + " (" + Math.max(1, Math.round(blob.size / 1024)) + " Ko)…";
  fetch("/api/fichier", { method: "POST", cache: "no-store", body: blob,
                          headers: entetes })
    .then(function (r) { return r.json(); }).then(function (j) {
      if (j.session === "renouveler" && !renouvele) return fetch("/", { cache: "no-store" }).then(function () { envoyerFichier(blob, nom, estVocal, true, session); });
      if (!j.ok) return erreurEnvoi("❌ " + (j.erreur || "refusé"), estVocal);
      envoiFichier.textContent = "";
      if (estVocal) return analyserVocal(j.fichier, session);
      analyse.fichier = j;
      if (j.genre === "audio" || j.genre === "video") analyse.son = j.fichier;  // todo #437: kept for « 🎬 Monter ce fichier audio… »
      dessinerAnalyser();
      prepare(Object.assign({ action: "joindre", fichier: j.fichier }, analyse.session ? { session: analyse.session } : {}));
    }).catch(function () { erreurEnvoi("❌ " + MSG_HORS_LIGNE, estVocal); });
}
function erreurEnvoi(msg, vocal) {  // the Analyser line, and for the recorder also a toast that stays (the Pilote has no such line)
  envoiFichier.className = "err"; envoiFichier.textContent = msg;
  if (vocal) toast(msg, true, true);
}
// todo #379: the recorder's voice is analysed WITHOUT a sheet (`joindre-vocal`: the CLI refuses anything not marked vocal), then
// written into the session, as suiteAnalyser does for a file.
function analyserVocal(fichier, session) {
  vocal.analyses++; majVocal();
  var fin = function () { vocal.analyses--; majVocal(); };
  post("/api/action/lire", Object.assign({ action: "joindre-vocal", fichier: fichier }, session ? { session: session } : {})).then(function (r) {
    fin();
    if (!r.ok) return toast("❌ Vocal non analysé : " + (r.erreur || r.resume || "refusé") + " (fichier " + fichier + ")", true, true);
    var ps = r.donnees && r.donnees.pour_session;
    if (!session) return toast("🎙 Vocal analysé, résultat dans le Fil");
    if (!ps) return toast("❌ Vocal analysé mais rien à écrire dans la session (voir le Fil)", true, true);
    direct({ action: "session-ecrire", nom: session, texte: ps }, function () { toast("🎙 Vocal écrit dans la session " + session); }, true);
  }).catch(function () { fin(); toast("❌ " + MSG_HORS_LIGNE + " (vocal reçu : " + fichier + ", voir le Fil)", true, true); });
}
function minutes(ms) { var s = Math.floor(ms / 1000); return Math.floor(s / 60) + ":" + (s % 60 < 10 ? "0" : "") + s % 60; }
function paintVocal(b) {
  var long = b.id === "vocal-bouton", rec = vocal.rec;
  b.textContent = rec ? "⏹ " + minutes(Date.now() - vocal.debut) + (long ? " Arrêter et envoyer" : "") : long ? "🎙 Enregistrer un vocal" : vocal.analyses ? "⏳" : "🎙";
  b.classList.toggle("danger", !!rec); b.classList.toggle("go", long && !rec); b.classList.toggle("en-cours", !!rec);
}
function majVocal() {
  if (vocal.rec && veilleVoulue && !veille && !veilleDemande && document.visibilityState === "visible" && Date.now() - veilleEssai > 5000) prendreVeille();
  vocal.boutons = vocal.boutons.filter(function (b) { return b.isConnected; });
  vocal.boutons.forEach(paintVocal);
}
function basculerVocal(session) {
  if (vocal.rec) return vocal.rec.stop();
  prendreVeille();  // todo #332: inside the tap, before any wait (mic prompt, catalogue)
  demarrerVocal(session);
}
function boutonVocal(b) { vocal.boutons.push(b); paintVocal(b); return b; }
function micro() { return !!(navigator.mediaDevices && window.MediaRecorder); }
// todo #398: the device's own dictation first (Safari iOS = Apple's), written LIVE into the session's input, sent when it stops.
// Any failure (API absent, refused, network, no result) falls back to the recording above, never a lost voice. Unknown: an installed
// iOS web app (standalone) may not allow it, hence the fallback and the toast naming the path used.
var SR = window.SpeechRecognition || window.webkitSpeechRecognition, MAUVAIS_DICTEE = /^(not-allowed|service-not-allowed|audio-capture|network)$/;
function demarrerVocal(session) {
  var saisie = session && document.getElementById("pilote-saisie");
  if (!SR || vocal.classique || !saisie) { toast("🎙 transcription serveur"); return demarrerEnregistrement(session); }
  var r = new SR(), base = saisie.value ? saisie.value.replace(/\s*$/, " ") : "", recu = false, arret = false, panne = false, fin = null;
  r.lang = "fr-FR"; r.interimResults = true; r.continuous = true;
  r.onresult = function (e) {
    var t = ""; recu = true; clearTimeout(fin);
    for (var i = 0; i < e.results.length; i++) t += e.results[i][0].transcript;
    saisie.value = base + t; brouillons["pilote:" + session] = saisie.value;
  };
  r.onerror = function (e) { if (MAUVAIS_DICTEE.test(e.error)) panne = true; };
  r.onend = function () {
    clearTimeout(fin); clearInterval(vocal.timer); vocal.rec = null; majVocal();
    if (arret || recu) relacherVeille();  // else the classic recording below keeps the lock taken at the tap
    if (!panne && (recu || arret)) {
      if (recu) return envoyerDictee(saisie);
      vocal.classique = true; return toast("🎙 rien entendu : prochain 🎙 = enregistrement classique");
    }
    vocal.classique = true;  // the next 🎙 records the classic way
    toast("⚠️ Dictée indisponible" + (recu ? ", texte gardé" : "") + " : enregistrement classique" + (arret || recu ? " au prochain 🎙" : ""), true, true);
    if (!arret && !recu) demarrerEnregistrement(session);
  };
  try { r.start(); } catch (e) { vocal.classique = true; return demarrerEnregistrement(session); }
  vocal.rec = { stop: function () { arret = true; r.stop(); } }; vocal.debut = Date.now(); vocal.session = session;
  prendreVeille();  // todo #332: the dictation too keeps the screen awake
  vocal.timer = setInterval(majVocal, 1000);
  fin = setTimeout(function () { panne = true; r.abort(); }, 8000);  // nothing heard in 8 s: assume the dictation does not work here
  toast("🎙 dictée de l'appareil"); majVocal();
}
// stopping the dictation sends the line at once, like the classic recording (no second tap on the input)
function envoyerDictee(saisie) {
  var f = saisie.form;
  if (!f || !saisie.value.trim()) return toast("🎙 rien entendu");
  if (f.requestSubmit) f.requestSubmit(); else f.dispatchEvent(new Event("submit", { cancelable: true }));
  toast("🎙 dictée envoyée");
}
// todo #332: keep the screen awake while recording (iOS cuts the mic on sleep); no wakeLock = nothing breaks
// Panne du 30/10: asked after the mic prompt or the catalogue, out of the tap, and refusals were swallowed. Now: asked AT the
// tap (prendreVeille in the click), taken again when iOS drops it while visible (release, then every 5 s), a refusal said once.
var veille = null, veilleVoulue = false, veilleDemande = false, veilleDit = false, veilleEssai = 0;
function prendreVeille() {
  veilleVoulue = true;
  if (veille || veilleDemande) return;
  if (!navigator.wakeLock) return signalerVeille("non pris en charge");
  veilleDemande = true; veilleEssai = Date.now();
  try {
    navigator.wakeLock.request("screen").then(function (l) {
      veilleDemande = false;
      if (!veilleVoulue) return l.release();
      veille = l; l.addEventListener("release", function () {
        if (veille !== l) return;
        veille = null;
        if (veilleVoulue && document.visibilityState === "visible") prendreVeille();  // iOS can drop it while still on screen
      });
    }, function (e) { veilleDemande = false; signalerVeille(e && e.name || "refus"); });
  } catch (e) { veilleDemande = false; signalerVeille(e && e.name || "erreur"); }
}
function signalerVeille(raison) {  // once per recording, the toast stays until tapped
  if (veilleDit || !veilleVoulue) return;
  veilleDit = true;
  toast("⚠️ Écran non gardé allumé (" + raison + ") : touche l'écran de temps en temps pendant le vocal", true, true);
}
function relacherVeille() {
  var l = veille; veilleVoulue = false; veilleDit = false; veille = null;
  try { if (l) l.release().catch(function () {}); } catch (e) {}
}
document.addEventListener("visibilitychange", function () {  // the browser drops the lock when hidden: take it again
  if (document.visibilityState === "visible" && vocal.rec && veilleVoulue && !veille) prendreVeille();
});
function demarrerEnregistrement(session) {
  navigator.mediaDevices.getUserMedia({ audio: true }).then(function (flux) {
    var morceaux = [], rec = new MediaRecorder(flux), demande = false, coupe = false;
    function alerteCoupure() {  // todo #332: a stop nobody asked for (sleep): warn, the recorded part is still sent
      if (demande || coupe) return;
      coupe = true; toast("⚠️ Vocal coupé par la veille à " + minutes(Date.now() - vocal.debut), true, true);
    }
    flux.getTracks().forEach(function (t) { t.addEventListener("ended", alerteCoupure); });
    rec.addEventListener("dataavailable", function (e) { if (e.data && e.data.size) morceaux.push(e.data); });
    rec.addEventListener("stop", function () {
      alerteCoupure(); relacherVeille();
      flux.getTracks().forEach(function (t) { t.stop(); });
      clearInterval(vocal.timer); vocal.rec = null; majVocal();
      var type = rec.mimeType || "audio/mp4", ext = /mp4|aac/.test(type) ? "m4a" : /ogg/.test(type) ? "ogg" : "weba";
      dessinerAnalyser();
      if (morceaux.length) envoyerFichier(new Blob(morceaux, { type: type }), "vocal-" + heure(new Date().toISOString()).replace(":", "h") + "." + ext, true, false, vocal.session);
    });
    vocal.rec = { stop: function () { demande = true; if (rec.state !== "inactive") rec.stop(); } };
    vocal.debut = Date.now(); vocal.session = session || null;
    vocal.timer = setInterval(function () {
      if (Date.now() - vocal.debut >= 20 * 60000) return vocal.rec && vocal.rec.stop();  // the analysis takes 20 minutes at most
      majVocal();
    }, 1000);
    rec.addEventListener("error", function () { relacherVeille(); });
    prendreVeille(); rec.start(1000);
    majVocal();
  }).catch(function () { relacherVeille(); erreurEnvoi("❌ Micro refusé ou indisponible.", true); });
}
function sectionFichier() {
  var out = [titreAnalyser("Fichier")], input = el("input");
  input.type = "file"; input.hidden = true;
  input.accept = "image/*,video/*,audio/*,application/pdf,.pdf,.txt,.md,.html";
  input.addEventListener("change", function () { var f = input.files && input.files[0]; if (f) envoyerFichier(f, f.name, false); input.value = ""; });
  out.push(input, pushButton("📎 Photo, vidéo, audio ou PDF", function () { input.click(); }, "go"));  // the link's button is the filled one
  if (micro()) {
    var b = pushButton("", function () { basculerVocal(analyse.session); });
    b.id = "vocal-bouton";
    out.push(boutonVocal(b));
  } else out.push(el("div", "muted", "Vocal en direct : micro indisponible dans ce navigateur."));
  out.push(envoiFichier);
  if (analyse.fichier) {
    var f = analyse.fichier;
    out.push(el("div", "muted", "Reçu : " + f.fichier + " (" + Math.max(1, Math.round(f.taille / 1024)) + " Ko)"));
    out.push(form("joindre", Object.assign({ fichier: f.fichier }, analyse.session ? { session: analyse.session } : {})));
    if (analyse.son === f.fichier) out.push(form("montage-audio", { fichier: f.fichier }));  // todo #437: audio or video just received
  }
  out.push(el("div", "muted", analyse.session ? "Ouvert depuis la session " + analyse.session + " : le chemin et le résumé y seront écrits." :
                                                 "Le résultat arrive aussi dans le Fil, avec le fichier."));
  return out;
}
function dessinerAnalyser() {
  var nodes = [el("h1", null, "📥 Analyser")].concat(sectionFichier());
  var depuis = analyse.session ? { session: analyse.session } : {};  // todo #440: the video knows where it is sent from
  nodes.push(titreAnalyser("Lien"), lienAnalyser(depuis));
  if (analyse.video) {
    nodes.push(el("div", "muted", "Vidéo prête : " + analyse.video));
    nodes.push(form("video-analyse", { id: analyse.video }));
  }
  nodes = nodes.concat(propositions("video"));
  nodes.push(titreAnalyser("Montage"));
  nodes.push(form("montage-script"));  // todo #437: from a script or a received audio/video to the MP4, one gesture
  if (analyse.son) nodes.push(el("div", "muted", "Dernier audio ou vidéo reçu : " + analyse.son), form("montage-audio", { fichier: analyse.son }));
  else nodes.push(el("div", "muted", "Pour monter un audio ou une vidéo : d'abord 📎 le joindre (section Fichier)."));
  nodes.push(el("div", "muted", "Ajouter des photos à un montage : depuis Telegram."));
  fill("analyse", nodes);
}
// 💡 The proposals of the last reply (todo #249): a fiche is 🔎 checked (shown, nothing filed) then 📚 filed, signed.
var ICONES_PROPOSITION = { fiche: "📚", todo: "➕", recherche: "🔎", profil: "👤" };
function propositions(source) {
  var p = analyse.propositions;
  if (!p || p.source !== source) return [];
  var out = [titreAnalyser("Propositions")];
  if (p.reponse) out.push(el("div", "muted", p.reponse));
  if (!p.items.length) out.push(el("div", "muted", "Aucune proposition."));
  p.items.forEach(function (it) {
    var r = el("div", "row");
    r.appendChild(el("div", null, (ICONES_PROPOSITION[it.type] || "•") + " " + it.n + ". " + it.titre));
    if (it.resume) r.appendChild(el("div", "muted", it.resume));
    if (it.type === "fiche") r.appendChild(form("savoir-verifier", { source: p.source, id: p.id, n: String(it.n) }));
    out.push(r);
  });
  if (p.source === "video" && p.id) out.push(rangerLot(p.id));
  return out;
}
// todo #477: ✅ Tout ranger = every proposal still pending (catalogue video-lot-voir), then ONE signed video-lot on its fingerprint
function rangerLot(id) {
  var box = el("div", "row", "⏳ propositions en attente…");
  lire("video-lot-voir", { id: id }).then(function (d) {
    var n = (d.actions || []).length;
    box.textContent = "";
    if (!d.empreinte || !n) return box.appendChild(el("div", "muted", "📥 Rien d'autre à ranger pour cette vidéo."));
    box.appendChild(el("div", null, "📥 " + n + " proposition(s) en attente : fiches, todos, recherches, fait"));
    box.appendChild(form("video-lot", { id: id, empreinte: d.empreinte }));
  }).catch(function (e) { box.textContent = ""; box.appendChild(el("div", "err", "📥 Lot illisible : " + e.message)); });
  return box;
}
function ouvrirAnalyser(session) {
  analyse.session = typeof session === "string" ? session : null;  // 📎 of the Pilote: its session; Fil and Moi: none
  dessinerAnalyser();
  current = "analyse";
  document.getElementById("analyse").scrollTop = 0;
  setOpen("analyse", true);
}
document.getElementById("fil-vu").addEventListener("click", function () {  // same sheet as the catalogue's notifs-vu (todo #355)
  chargerCatalogue().then(function () { prepare({ action: "notifs-vu", brouillon: [] }); });
});
["fil-joindre"].forEach(function (id) {  // Moi, and 📎 joindre of Travail (todo #293, #407, #410)
  document.getElementById(id).addEventListener("click", function () { chargerCatalogue().then(function () { ouvrirAnalyser(null); }); });
});
document.getElementById("analyse-retour").addEventListener("click", function () {  // 📎 of the Pilote: back to the session
  current = document.getElementById("fiche").classList.contains("open") ? "fiche" : null;
  setOpen("analyse", false);
  if (current === null) refresh();
});
// -- ⋯ Plus (todo #249): profil, focus, revue de cap, anciennes conversations, as Telegram's ⋯ Plus. Readings and forms of the
// catalogue only: what is shown is the CLI's data (whitelisted by the server), never interpreted here.
function ouvrirProfil() {
  var recharger = function () { ouvrirProfil(); };
  lire("profil").then(function (d) {
    var n = [el("strong", null, "👤 Profil"), el("div", "muted", d.pending.length ? d.pending.length + " fait(s) proposé(s) attendent ton avis." : "Aucun fait proposé en attente.")];
    d.pending.forEach(function (p) {
      var r = el("div", "row"), ctx = { id: String(p.id) };
      r.appendChild(el("div", null, "#" + p.id + " « " + p.texte + " »"));
      r.appendChild(el("div", "muted", "proposé par " + p.source + (p.date ? " · " + jour(p.date) : "")));
      if (p.invisible_retire > 0) r.appendChild(el("div", "err", "⚠️ " + p.invisible_retire + " caractère(s) invisible(s) retiré(s) : texte peut-être piégé."));
      r.appendChild(form("profil-accepter", ctx));
      r.appendChild(form("profil-rejeter", ctx));
      n.push(r);
    });
    n.push(plie("📄 Profil actuel", [el("div", "pre", d.profil || "—")]));
    showOverlay(n, recharger, null);
  }).catch(function (e) { overlayErreur(e, recharger, null); });
}
function ouvrirFocus() {
  var recharger = function () { ouvrirFocus(); };
  lire("focus").then(function (d) {
    var n = [el("strong", null, "🧭 Focus de la semaine"), el("p", null, d.line || "")];
    if (d.focus && d.focus.par) n.push(el("div", "muted", "choisi par " + d.focus.par + (d.focus.depuis ? " · " + jour(d.focus.depuis) : "")));
    var codes = (CATALOGUE["focus-choisir"].champs[0].choix || []).map(function (c) { return c[0]; }), actuel = d.focus ? d.focus.domaines : [];
    var code = [actuel.join("+"), actuel.slice().reverse().join("+")].filter(function (c) { return codes.indexOf(c) >= 0; })[0];
    var k = "focus-choisir:{}:domaines";
    if (code && !brouillons[k]) brouillons[k] = code;
    n.push(el("div", "muted", "1 ou 2 domaines. Un chantier est dans le focus si son domaine l'est."), form("focus-choisir"));
    showOverlay(n, recharger, null);
  }).catch(function (e) { overlayErreur(e, recharger, null); });
}
function ouvrirCap() {
  var recharger = function () { ouvrirCap(); };
  lire("cap").then(function (d) {
    if (!d.exists) return showOverlay([el("strong", null, "🔭 Revue de cap"), el("div", "muted", "Pas encore de revue de cap.")], recharger, null);
    var n = [el("strong", null, "🔭 Revue de cap du " + jour(d.date)), el("div", "pre", d.text)];
    if (d.corrections.length) n.push(el("h2", null, "Corrections proposées"));
    d.corrections.forEach(function (c) {
      var r = el("div", "row");
      r.appendChild(el("div", null, c.titre + (c.chantier ? " (" + c.chantier + ")" : "")));
      if (c.preuve) r.appendChild(el("div", "muted", c.preuve));
      if (c.notes) r.appendChild(el("div", "muted", c.notes));
      n.push(r);
    });
    if (d.corrections.length) n.push(el("div", "muted", "En faire des todos : « Todo… » dans la fiche du chantier (onglet Todos)."));
    showOverlay(n, recharger, null);
  }).catch(function (e) { overlayErreur(e, recharger, null); });
}
// -- 📋 Rapports du dimanche (todo #417): reading `rapports` (`rapports dimanche`), three lines, then ONE signed ✅ (form
// "rapports-lot" on the fingerprint of the exact actions; the sheet lists them all). Reflexes stay on their own button (Santé).
var RAPPORTS_MS = 60000, rapportsLu = 0;
function loadRapports(force) {
  if (!force && Date.now() - rapportsLu < RAPPORTS_MS) return;
  rapportsLu = Date.now();
  chargerCatalogue().then(function () { return lire("rapports"); }).then(showRapports)
    .catch(function () { rapportsLu = 0; });  // unreadable = no card
}
function showRapports(d) { aToiSources.rapports = d; dessinerAToi(); }  // todo #466: a card of À toi (carteRapports)
// -- 💡 Nouvelle habitude proposée (todo #429): reading `habitudes` (`habitudes liste`), one card per pending skill draft with
// its FULL text, ✅ = form "habitude" signed on "<dossier>:<empreinte>" (the sheet shows the text again), ❌ = "habitude-jeter".
var HABITUDES_MS = 60000, habitudesLu = 0;
function loadHabitudes(force) {
  if (!force && Date.now() - habitudesLu < HABITUDES_MS) return;
  habitudesLu = Date.now();
  chargerCatalogue().then(function () { return lire("habitudes"); }).then(showHabitudes)
    .catch(function () { habitudesLu = 0; showHabitudes({ habitudes: [] }); });  // nothing to propose = no card
}
function showHabitudes(d) { aToiSources.habitudes = d.habitudes || []; dessinerAToi(); }  // todo #466: cards of À toi (carteHabitude)
// -- 🔁 Routine proposée (todo #503): reading `routines` (`routine liste`), one card per proposed routine (none = no card),
// ✅ = form "routine" signed on "<nom>:<empreinte>" (the sheet lists it again), ❌ = "routine-refuser".
var routinesLu = 0;
function loadRoutines(force) {
  if (!force && Date.now() - routinesLu < HABITUDES_MS) return;
  routinesLu = Date.now();
  chargerCatalogue().then(function () { return lire("routines"); }).then(showRoutines)
    .catch(function () { routinesLu = 0; showRoutines({}); });
}
function showRoutines(d) { aToiSources.routines = d.proposees || []; aToiSources.routinesActives = d.actives || []; dessinerAToi(); }
function fill_(node, nodes) { node.replaceChildren.apply(node, nodes); }

// -- Sessions ultron (todo #264, Pilote #283): thread from the journal or raw tmux screen; all from the catalogue ----------
// `sessions` / `session` / `session-ecran` / `usage` / `lot` are readings (POST /api/action/lire); managing sessions (todo #266):
// `session-nouvelle` and `session-relancer`. Writing and keys (todos #265, #283): `session-ecrire`, `session-choisir`, « ouvert ».
var SESSION_AVANCEMENT_MS = 30000;
function heure(iso) { var d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) : ""; }
// todo #297: the session the Pilote shows (null in the list): « Nouvelle session » then offers to close it.
var sessionOrigine = null;
// ✖ Fermer: first the reading `session-fermeture` (sub-agents at work, uncommitted todo worktrees), because a sheet's words are fixed;
// the sheet itself (`session-fermer`, signed) comes from the catalogue. The server refuses again while sub-agents work.
function fermerSession(nom) {
  chargerCatalogue().then(function () { return lire("session-fermeture", { nom: nom }); }).then(function (d) {
    if (d.sous_agents) {
      return showConfirm([el("div", "err", "✖ Fermeture refusée : " + d.sous_agents + " sous-agent" + (d.sous_agents > 1 ? "s travaillent" : " travaille") +
                                           " encore dans " + nom + ". Attends leur fin."), backButton()]);
    }
    var note = (d.travail_non_commite.length ? "⚠️ Travail non commité dans : " + d.travail_non_commite.join(", ") + " (rien n'y est touché). " : "") +
               (d.sous_agents === null ? "Sous-agents : état illisible, la fermeture se fait quand même. " : "");
    prepare({ action: "session-fermer", nom: nom }, note || null);
  }).catch(function (e) { showConfirm([el("div", "err", "Indisponible (" + (e.message || e) + ")"), backButton()]); });
}
function boutonFermer(nom) {
  var b = el("button", "danger", "✖ Fermer…");
  b.type = "button"; b.setAttribute("aria-label", "Fermer la session " + nom);
  b.addEventListener("click", function () { fermerSession(nom); });
  return b;
}
// One row per session (open, ✖ Fermer) then ➕ Nouvelle session; shared by the Travail tab and the Moi > Sessions screen (todo #405).
// Detail of a session (todo #406): context gauge, one line per running sub-agent, last sentence (all from the `sessions` reading).
function detailSession(s) {
  var n = [];
  if (s.contexte !== null && s.contexte !== undefined) n.push(el("div", "muted" + (s.contexte >= 60 ? " ambre" : ""), "contexte " + s.contexte + " %"));
  (s.agents || []).forEach(function (a) {
    var t = Date.parse(a.depuis);
    n.push(el("div", "muted", a.nom + (a.modele ? " · " + a.modele : "") + (isNaN(t) ? "" : " · depuis " + age(t / 1000))));
  });
  if (s.derniere_phrase) {
    var f = el("div", "muted", s.derniere_phrase);
    f.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
    n.push(f);
  }
  return n;
}
function lignesSessions(d, recharger) {
  var n = [];
  if (!d.sessions.length) n.push(el("div", "muted", "Aucune session ouverte."));
  d.sessions.slice().reverse().forEach(function (s) {
    var r = el("div", "row");
    r.appendChild(listButton(s.nom + " · " + (s.chantier ? "chantier " + s.chantier : "Main") + (s.ouverte ? " · ouverte" : ""), function () { ouvrirSession(s.nom, recharger); }));
    detailSession(s).forEach(function (x) { r.appendChild(x); });  // detail right under the name, ✖ Fermer last (todo #418)
    r.appendChild(boutonFermer(s.nom));
    n.push(r);
  });
  n.push(form("session-nouvelle", {}, { chantiers: d.chantiers || [] }));
  return n;
}
// Advice card (todo #364): `conseil` is a reading (plain rules, no AI); its button only aims « Demander à Ultron » at a new session there.
function showConseil(d) {
  var qui = d.chantier || "Main";
  fill("travail-conseil", [el("strong", null, "Lance " + qui), el("div", "muted", d.raison),
    listButton("➕ Préparer la session " + qui, function () {
      brouillons["demander:cible"] = "n:" + (d.chantier || "");
      dessinerDemander();
      var f = document.getElementById("demander"); if (f && f.scrollIntoView) f.scrollIntoView({ block: "start" });
    })]);
}
function loadConseil() {
  chargerCatalogue().then(function () { return lire("conseil"); }).then(showConseil)
    .catch(function (e) { fill("travail-conseil", [el("div", "err", "Conseil indisponible (" + (e.message || e) + ")")]); });
}
function ouvrirSessions() {
  var recharger = function () { ouvrirSessions(); };
  sessionOrigine = null;
  lire("sessions").then(function (d) {
    chantiersLus = d.chantiers || [];
    var n = [el("strong", null, "💬 Sessions ultron"), el("div", "muted", "Tu pilotes la session comme au terminal : chaque envoi part tout de suite et reste tracé.")];
    showOverlay(n.concat(lignesSessions(d, recharger)), recharger, null);
  }).catch(function (e) { overlayErreur(e, recharger, null); });
}
// Notification history (todo #276): `notifs` is a reading, `notifs-vu` a simple action (the sheet reloads this screen).
// Journal by day (todo #346): sticky day headers, the time alone on each line, filters decided by the server (`genre_fil`).
// todo #466: four filters (Questions = `genre` question or feu-vert, Fichiers, Alertes, Tout), they fit on 375 px
var FIL_FILTRES = [["question", "Questions"], ["fichier", "Fichiers"], ["alerte", "Alertes"], ["tout", "Tout"]];
var filFiltre = "tout";
function dansFiltre(x, f) {
  if (f === "question") return x.genre === "question" || x.genre === "feu-vert";
  if (f === "fichier") return x.pj.length > 0 || x.genre_fil === "fichier";
  if (f === "alerte") return x.genre_fil === "alerte" || x.genre === "alerte";
  return true;
}
function jourCle(iso) { var d = new Date(iso); return isNaN(d) ? "" : d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate(); }
function jourNom(iso) {
  var d = new Date(iso), j = new Date(), h = new Date();
  h.setDate(h.getDate() - 1);
  var court = d.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "short" });
  if (jourCle(iso) === jourCle(j)) return "Aujourd'hui · " + court;
  if (jourCle(iso) === jourCle(h)) return "Hier · " + court;
  return court;
}
// Reader (todo #373): every attachment opens in a full-screen reader inside the page (never a new tab: an installed iOS app
// would leave the page with no way back). Browser back = close (pushState/popstate). Text is rendered with createElement and
// textContent only (no raw HTML); a .html page runs in a sandboxed iframe WITHOUT allow-same-origin (opaque origin, no cookie, no API).
var LECTEUR_MAX = 200000;
var lecteur = null;
function fermerLecteur(depuisPopstate) {
  if (!lecteur) return;
  lecteur.remove();
  lecteur = null;
  if (!depuisPopstate && history.state && history.state.lecteur) { ignorePop++; history.back(); }
}
// Screens and history (todo #296): ONE history entry {ecran:1} for the whole stack of open overlays (fiche, analyse), pushed when the
// first one opens and consumed when the last one closes, whatever closed it (button, edge swipe, browser back). ignorePop counts the
// history.back() we call ourselves, so their popstate closes nothing. The reader keeps its own entry on top ({lecteur:1}).
var ignorePop = 0, ecranHist = false;
function ecranHaut() {
  if (lecteur) return { el: lecteur, fermer: function () { fermerLecteur(false); } };
  var a = document.getElementById("analyse"), f = document.getElementById("fiche");
  if (a.classList.contains("open")) return { el: a, fermer: function () { document.getElementById("analyse-retour").click(); } };
  if (f.classList.contains("open")) return { el: f, fermer: function () { document.getElementById("retour").click(); } };
  return null;
}
function histSync() {
  var ouvert = !!(document.getElementById("fiche").classList.contains("open") || document.getElementById("analyse").classList.contains("open"));
  if (ouvert && !ecranHist) {
    ecranHist = true;
    try { history.pushState({ ecran: 1 }, ""); } catch (e) { ecranHist = false; }
  } else if (!ouvert && ecranHist) {
    ecranHist = false;
    if (history.state && history.state.ecran) { ignorePop++; history.back(); }
  }
}
["fiche", "analyse"].forEach(function (id) {
  new MutationObserver(histSync).observe(document.getElementById(id), { attributes: true, attributeFilter: ["class"] });
});
window.addEventListener("popstate", function () {
  if (ignorePop > 0) { ignorePop--; return; }
  if (lecteur) { fermerLecteur(true); return; }
  var haut = ecranHaut();
  if (!haut) return;
  ecranHist = false;  // the browser consumed our entry: close one level, then re-push if a screen is still open (fiche > fiche)
  haut.fermer();
  histSync();
});
// Edge swipe back (todo #296): the installed iOS app has no native one. A single finger starting within 24 px of the left edge and
// moving mostly sideways drags the top screen (translateX); past a third of the width or faster than 0.5 px/ms it runs the very same
// close as the button (click on #retour / #analyse-retour, or the reader's Fermer). A vertical start abandons the gesture.
(function () {
  var EDGE = 24, g = null;
  function reset(e) { e.style.transition = "transform .2s var(--ease)"; e.style.transform = ""; setTimeout(function () { e.style.transition = ""; }, 220); }
  document.addEventListener("touchstart", function (ev) {
    g = null;
    var haut = ecranHaut(), t = ev.touches[0];
    if (!haut || ev.touches.length !== 1 || t.clientX > EDGE || current === "confirm" || document.getElementById("confirm").classList.contains("open")) return;
    for (var n = ev.target; n && n !== document.body; n = n.parentNode) if (n.scrollLeft > 0) return;  // horizontally scrolled content keeps its touch
    g = { haut: haut, x: t.clientX, y: t.clientY, t: Date.now(), dx: 0, v: 0, h: false, px: t.clientX, pt: Date.now() };
  }, { passive: true });
  document.addEventListener("touchmove", function (ev) {
    if (!g) return;
    var t = ev.touches[0], dx = t.clientX - g.x, dy = t.clientY - g.y, now = Date.now();
    if (!g.h) {
      if (Math.abs(dy) > 8 && Math.abs(dy) > Math.abs(dx)) { g = null; return; }  // vertical first: scroll, pull-to-refresh, terminal
      if (dx < 8) return;
      g.h = true; g.haut.el.style.transition = "none";
    }
    if (ev.cancelable) ev.preventDefault();
    if (now > g.pt) g.v = (t.clientX - g.px) / (now - g.pt);
    g.px = t.clientX; g.pt = now; g.dx = Math.max(0, dx);
    g.haut.el.style.transform = "translateX(" + g.dx + "px)";
  }, { passive: false });
  function fin(ev) {
    var s = g; g = null;
    if (!s || !s.h) return;
    var e = s.haut.el, w = window.innerWidth || 1;
    if (ev.type === "touchcancel" || !(s.dx > w / 3 || s.v > 0.5)) { reset(e); return; }
    e.style.transition = "transform .18s var(--ease)"; e.style.transform = "translateX(100%)";
    setTimeout(function () {
      e.style.transition = "none";
      s.haut.fermer();
      if (e.parentNode && e.classList.contains("open")) {  // still open (back to the previous level): slide the new content in
        e.style.transform = "translateX(-20%)"; void e.offsetWidth; reset(e);
      } else setTimeout(function () { e.style.transition = ""; e.style.transform = ""; }, 320);
    }, 190);
  }
  document.addEventListener("touchend", fin, { passive: true });
  document.addEventListener("touchcancel", fin, { passive: true });
})();
function inlineMd(parent, t) {
  t.split(/(`[^`]+`|\*\*[^*]+\*\*)/).forEach(function (m) {
    if (!m) return;
    if (m[0] === "`" && m.length > 2) parent.appendChild(el("code", null, m.slice(1, -1)));
    else if (m.slice(0, 2) === "**" && m.length > 4) parent.appendChild(el("strong", null, m.slice(2, -2)));
    else parent.appendChild(document.createTextNode(m));
  });
}
function rendreMd(texte) {
  var racine = el("div", "md"), lignes = texte.split("\n"), i = 0, liste = null, para = null, m;
  function fin() { liste = null; para = null; }
  while (i < lignes.length) {
    var l = lignes[i++];
    if (/^```/.test(l)) {
      var code = [];
      while (i < lignes.length && !/^```/.test(lignes[i])) code.push(lignes[i++]);
      i++; fin();
      racine.appendChild(el("pre", "code", code.join("\n")));
    } else if ((m = /^(#{1,6})\s+(.*)$/.exec(l))) {
      fin();
      var h = el("div", "md-h"); inlineMd(h, m[2]); racine.appendChild(h);
    } else if ((m = /^\s*(?:[-*]|\d+\.)\s+(.*)$/.exec(l))) {
      if (!liste) { liste = el("ul"); racine.appendChild(liste); }
      var li = el("li"); inlineMd(li, m[1]); liste.appendChild(li);
    } else if (!l.trim()) { fin(); }
    else {
      if (!para) { para = el("p"); racine.appendChild(para); } else para.appendChild(document.createTextNode(" "));
      inlineMd(para, l);
    }
  }
  return racine;
}
function ouvrirLecteur(p) {
  fermerLecteur(true);
  var r = el("div", "lecteur"), haut = el("div", "lecteur-haut"), zone = el("div", "lecteur-zone");
  r.setAttribute("role", "dialog"); r.setAttribute("aria-label", p.orig);
  var fermer = el("button", "lecteur-btn", "✖ Fermer"); fermer.type = "button";
  fermer.addEventListener("click", function () { fermerLecteur(false); });
  haut.appendChild(fermer);
  haut.appendChild(el("span", "lecteur-nom", p.orig));
  var secours = el("a", "lecteur-btn", "Ouvrir le fichier"); secours.href = p.url;  // same tab: a fallback link, never a new tab
  var copier = null, brut = "";
  if (p.genre === "texte") {
    copier = el("button", "lecteur-btn", "Copier"); copier.type = "button";
    copier.addEventListener("click", function () {
      var ok = function () { copier.textContent = "Copié"; };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(brut).then(ok, function () {});
    });
    haut.appendChild(copier);
  }
  haut.appendChild(secours);
  r.appendChild(haut); r.appendChild(zone);
  if (p.genre === "image") {
    var img = el("img", "lecteur-img"); img.src = p.url; img.alt = p.orig; zone.appendChild(img);
  } else if (p.genre === "audio") {
    var au = el("audio"); au.controls = true; au.preload = "metadata"; au.src = p.url; zone.appendChild(au);
  } else if (p.genre === "html") {
    var fr = el("iframe", "lecteur-frame"); fr.setAttribute("sandbox", "allow-scripts"); fr.title = p.orig; fr.src = p.url;
    zone.appendChild(fr);
  } else if (p.genre === "texte") {
    zone.appendChild(el("div", "plain", "Chargement…"));
    fetch(p.url, { credentials: "same-origin" }).then(function (rep) { return rep.ok ? rep.text() : Promise.reject(); }).then(function (t) {
      var coupe = t.length > LECTEUR_MAX;
      brut = coupe ? t.slice(0, LECTEUR_MAX) : t;
      zone.textContent = "";
      zone.appendChild(/\.md$/i.test(p.orig) ? rendreMd(brut) : el("pre", "code", brut));
      if (coupe) zone.appendChild(el("div", "plain", "… texte coupé (" + LECTEUR_MAX + " caractères), « Ouvrir le fichier » pour le reste."));
    }).catch(function () { zone.textContent = ""; zone.appendChild(el("div", "plain", "Lecture impossible.")); });
  } else {
    zone.appendChild(el("div", "plain", "Ce fichier se télécharge."));
  }
  document.body.appendChild(r);
  lecteur = r;
  try { history.pushState({ lecteur: 1 }, ""); } catch (e) { /* no history: the Fermer button still works */ }
}
// The fil (todo #289): newest first, unread in bold with a dot, an entry unfolds to its whole text, each attachment opens in the reader.
function pjFil(p) {
  var b;
  if (p.genre === "image") {
    b = el("button", "pj"); b.type = "button";
    var img = el("img"); img.src = p.url; img.alt = p.orig; img.loading = "lazy";
    b.appendChild(img);
  } else {
    // todo #350: unfolded, a non-image file is a card « name · size · open » (the whole card is the 44 px target)
    b = el("button", "pj-carte pj-lien"); b.type = "button";
    b.appendChild(el("span", "pj-nom", (p.genre === "audio" ? "🎧 " : "📎 ") + p.orig));
    b.appendChild(el("span", "pj-meta", (p.taille ? tailleFil(p.taille) + " · " : "") + "ouvrir"));
  }
  b.addEventListener("click", function () { ouvrirLecteur(p); });
  return b;
}
// todo #350: human size of an attachment (the server sends bytes; 0 or missing = no size shown)
function tailleFil(o) {
  if (o < 1024) return o + " o";
  if (o < 1048576) return Math.round(o / 1024) + " Ko";
  return (o / 1048576).toFixed(1).replace(".", ",") + " Mo";
}
// todo #350: what a CLOSED line shows of its attachments: the first image as a thumbnail, otherwise a chip with the first file name
function pjResume(x) {
  var i = x.pj.find(function (p) { return p.genre === "image"; }), n = [];
  if (i) { var im = el("img", "fil-vignette"); im.src = i.url; im.alt = ""; im.loading = "lazy"; n.push(im); }
  var f = x.pj.find(function (p) { return p.genre !== "image"; });
  if (f) n.push(el("span", "fil-puce", (f.genre === "audio" ? "🎧 " : "📎 ") + f.orig));
  if (x.pj.length > 1) n.push(el("span", "g", "+" + (x.pj.length - 1)));
  return n;
}
// Opening an unread entry marks it read at once (todo #301): bold and badge now, `notifs-lire` (no sheet) in the background.
var filDonnees = null;
function liseeFil(x, d, s) {  // d: the entry element, s: the element holding its dot
  x.lue = true;
  d.classList.remove("nonlue");
  var p = s.querySelector(".dot");
  if (p) p.remove();
  filDonnees.non_lues = Math.max(0, filDonnees.non_lues - 1);  // todo #466: no badge on the Fil any more (one badge: À toi)
  filSig = JSON.stringify(filDonnees);  // the next refresh finds nothing new: the entries stay unfolded
  direct({ action: "notifs-lire", at: x.at });
}
// Todo #341: https URLs of a fil entry become links (createElement + textContent only); http and other schemes stay text.
var URL_FIL = /https:\/\/[^\s<>"'`]+/g;
function texteLiens(texte) {
  var d = el("div", "pre"), i = 0, m;
  URL_FIL.lastIndex = 0;
  while ((m = URL_FIL.exec(texte))) {
    var u = m[0].replace(/[.,;:!?)\]]+$/, "");  // closing punctuation stays outside the link
    if (u.length <= 8) continue;  // the bare scheme alone
    if (m.index > i) d.appendChild(document.createTextNode(texte.slice(i, m.index)));
    var a = document.createElement("a");
    a.className = "lien-fil"; a.textContent = u; a.href = u; a.target = "_blank"; a.rel = "noopener noreferrer";
    d.appendChild(a);
    i = m.index + u.length;
    URL_FIL.lastIndex = i;
  }
  if (i < texte.length) d.appendChild(document.createTextNode(texte.slice(i)));
  return d;
}
// todo #466: the link to a todo or a session sits INSIDE the message, only when the entry carries a target
function lienCible(x) {
  var c = x.cible;
  if (!c) return null;
  return pushButton("→ " + c.libelle, function () { if (c.genre === "todo") openTodo(c.ref); else ouvrirSession(c.ref, null); }, "texte");
}
function titreFil(x) {
  var t = el("span", "titre-fil");
  t.appendChild(el("time", "mono pale", heure(x.at)));
  t.appendChild(document.createTextNode(" " + (x.origine === "toi" ? "↑ Toi · " + x.titre.split(" : ")[0].replace(/ reçu[e]?$/, "") : x.titre)));  // todo #353: data field, not the text
  return t;
}
// todo #351: technical lines (sha, rc=, branch, test counts, paths) split by the server (`details`), shown in a CLOSED drawer
function detailsTech(x) {
  if (!x.details || !x.details.length) return null;
  var d = el("details", "tech");
  d.appendChild(el("summary", null, "Détails techniques"));
  d.appendChild(el("pre", "code", x.details.join("\n")));
  return d;
}
function ligneFil(x) {
  var d = el("details", "fil" + (x.lue ? "" : " nonlue") + (x.origine === "toi" ? " toi" : "")), s = el("summary");
  if (!x.lue) s.appendChild(el("i", "dot"));
  s.appendChild(titreFil(x));
  pjResume(x).forEach(function (n) { s.appendChild(n); });  // todo #350
  d.appendChild(s);
  if (!x.lue) d.addEventListener("toggle", function () { if (d.open && !x.lue) liseeFil(x, d, s); });
  var texte = x.complet || x.corps;
  if (texte) d.appendChild(texteLiens(texte));
  x.pj.forEach(function (p) { d.appendChild(pjFil(p)); });
  var l = lienCible(x);
  if (l) d.appendChild(l);
  var t = detailsTech(x);  // todo #351: jargon lines in a closed drawer at the bottom
  if (t) d.appendChild(t);
  return d;
}
// Todo #409: an unread entry as a card (title, two lines of preview, thumbnail). Todo #466: a tap ALWAYS opens the message (text,
// attachments, then the link to its target if it has one) and marks it read; the card stays in place until the next redraw.
function carteFil(x) {
  var k = el("div", "notif nonlue" + (x.origine === "toi" ? " toi" : "")), h = el("button", "notif-tete"), t = el("div", "notif-titre");
  h.type = "button"; h.setAttribute("aria-expanded", "false");
  t.appendChild(el("i", "dot"));
  t.appendChild(titreFil(x));
  var col = el("div", "notif-col");
  col.appendChild(t);
  if (x.corps) col.appendChild(el("div", "notif-apercu", x.corps));
  h.appendChild(col);
  var p0 = x.pj[0];
  if (p0 && p0.genre === "image") {
    var im = el("img", "notif-vignette"); im.src = p0.url; im.alt = ""; im.loading = "lazy";
    h.appendChild(im);
  }
  k.appendChild(h);
  var plus = el("div", "notif-plus"), ouvert = false;
  plus.hidden = true;
  var texte = x.complet || x.corps;
  if (texte) plus.appendChild(texteLiens(texte));
  x.pj.forEach(function (p) { plus.appendChild(pjFil(p)); });
  var l = lienCible(x);
  if (l) plus.appendChild(l);
  var t = detailsTech(x);  // todo #351
  if (t) plus.appendChild(t);
  k.appendChild(plus);
  h.addEventListener("click", function () {
    if (!x.lue) liseeFil(x, k, k);
    ouvert = !ouvert; plus.hidden = !ouvert; h.classList.toggle("ouvert", ouvert); h.setAttribute("aria-expanded", String(ouvert));
  });
  return k;
}
// todo #466: repeated entries (merges, « résolu », handovers…) folded: 3 or more in a row of the same family = one line « N× … ».
// The family = the first two words of the title without digits nor signs (a pure display grouping; every entry stays inside).
function familleFil(x) {
  return String(x.titre || "").toLowerCase().split(/\s+/).map(function (w) { return w.replace(/[^a-zà-ÿ'-]/g, ""); })
    .filter(function (w) { return w && !/\d/.test(w); }).slice(0, 2).join(" ") || x.at;  // no word: never grouped
}
function groupeFil(lot) {
  var d = el("details", "fil groupe"), s = el("summary"), t = el("span", "titre-fil");
  t.appendChild(el("time", "mono pale", heure(lot[lot.length - 1].at) + "–" + heure(lot[0].at)));
  t.appendChild(document.createTextNode(" " + lot.length + "× " + lot[0].titre));
  s.appendChild(t); d.appendChild(s);
  lot.forEach(function (x) { d.appendChild(ligneFil(x)); });
  return d;
}
// todo #347: several messages of one day aimed at the same todo = ONE line (title of the newest + « · N étapes »); a tap unfolds the steps.
function todoFondue(lot) {
  var d = el("details", "fil groupe fondue"), s = el("summary"), t = el("span", "titre-fil");
  t.appendChild(el("time", "mono pale", heure(lot[0].at)));
  t.appendChild(document.createTextNode(" " + (lot[0].origine === "toi" ? "↑ Toi · " + lot[0].titre.split(" : ")[0].replace(/ reçu[e]?$/, "") : lot[0].titre)));
  s.appendChild(t); s.appendChild(el("span", "g", "· " + lot.length + " étapes")); d.appendChild(s);
  lot.forEach(function (x) { d.appendChild(ligneFil(x)); });
  return d;
}
// Tab « fil » (todo #292): read at every refresh, drawn only while the tab is shown.
var filSig = "";
function dessinerFil(d, n) {
  var nouv = d.notifs.filter(function (x) { return x.lue === false; }), vieux = d.notifs.filter(function (x) { return x.lue !== false; });
  document.getElementById("fil-nouveau").hidden = !nouv.length;  // todo #409: no unread, no « Nouveau » card
  fill("fil-nouveau", nouv.map(carteFil));
  if (vieux.length) {
    var f = el("div", "chips");
    FIL_FILTRES.forEach(function (p) {
      var b = el("button", "chip" + (filFiltre === p[0] ? " on" : ""), p[1]);
      b.type = "button"; b.setAttribute("aria-pressed", filFiltre === p[0] ? "true" : "false");
      b.addEventListener("click", function () { filFiltre = p[0]; dessinerFil(d, []); });
      f.appendChild(b);
    });
    n.push(f);
    var vues = vieux.filter(function (x) { return dansFiltre(x, filFiltre); }), lot = [];
    var vider = function () {
      if (lot.length >= 3) n.push(groupeFil(lot)); else lot.forEach(function (x) { n.push(ligneFil(x)); });
      lot = [];
    };
    if (!vues.length) n.push(el("div", "muted", "Rien dans ce filtre."));
    // todo #347: the filter applies first, then per (local day, todo) the messages fold into one line (key `todo` from the server)
    var parJour = [], jours = {};
    vues.forEach(function (x) {
      var j = jourCle(x.at);
      if (!jours[j]) { jours[j] = { nom: jourNom(x.at), items: [], todos: {} }; parJour.push(jours[j]); }
      var J = jours[j];
      if (!x.todo) { J.items.push({ x: x }); return; }
      if (!J.todos[x.todo]) { J.todos[x.todo] = []; J.items.push({ etapes: J.todos[x.todo] }); }
      J.todos[x.todo].push(x);
    });
    parJour.forEach(function (J) {
      n.push(el("div", "jour", J.nom));
      J.items.forEach(function (it) {
        if (it.etapes && it.etapes.length > 1) { vider(); n.push(todoFondue(it.etapes)); return; }
        var x = it.x || it.etapes[0];
        if (!x.todo && lot.length && familleFil(lot[0]) !== familleFil(x)) vider();
        if (x.todo) { vider(); n.push(ligneFil(x)); } else lot.push(x);
      });
      vider();
    });
    vider();
  } else if (!nouv.length) n.push(el("div", "muted", "Rien pour le moment."));
  else n.push(el("div", "muted", "Rien de plus ancien."));
  fill("fil-liste", n);
}
function loadFil() {
  return lire("notifs").then(function (d) {
    var sig = JSON.stringify(d);
    if (sig === filSig) return;  // unchanged: keep the entries the user unfolded
    filSig = sig; filDonnees = d;
    document.getElementById("fil-vu").hidden = !d.non_lues;  // todo #355: the icon in the Nouveau header
    dessinerFil(d, []);
  }).catch(function (e) { filSig = ""; fill("fil-liste", [el("div", "err", "Indisponible (" + (e.message || e) + ")")]); });
}
// todo #423: text read aloud: no code (fences, indent >= 4), links, `inline code`, hashes, paths; what is removed leaves no « Voir et »
function texteParle(texte) {
  var feu = false, X = "\u0001";
  return String(texte).split("\n").filter(function (l) {
    if (/^\s*```/.test(l)) { feu = !feu; return false; }
    return !feu && (!/^\s{4,}/.test(l) || /^\s+[-•*] /.test(l));
  }).map(function (l) {
    var puce = /^\s*[-•*] /.test(l);
    l = l.replace(/^\s*[-•*] /, "").replace(/https?:\/\/\S+/g, X).replace(/`[^`]*`/g, X).replace(/\b[0-9a-f]{7,40}\b/g, X)
      .replace(/\S*\/\S*/g, X).replace(/[│─╭╮╰╯┃*_#]/g, " ");
    l = l.replace(new RegExp(X + "(?:\\s*(?:,|et|ou)\\s*" + X + ")+", "g"), X)
      .replace(new RegExp("\\b(?:voir|cf\\.?|dans|sur|via|le commit|commit|la branche)\\s+" + X, "gi"), X).replace(new RegExp("\\(\\s*" + X + "\\s*\\)", "g"), X).replace(new RegExp(X, "g"), " ")
      .replace(/\s+([.,;:!?])/g, "$1").replace(/\s+/g, " ").trim();
    return l && puce && !/[.!?:;]$/.test(l) ? l + "." : l;
  }).filter(Boolean).join(" ");
}
function avecModele(texte, modele) { return texte + (modele && texte.toLowerCase().indexOf(modele.toLowerCase()) < 0 ? " (" + modele + ")" : ""); }
function bulle(e) {
  if (e.k === "outil") {
    var d = el("details", "outil"), s = el("summary"), t = el("span", null, e.outil + (e.texte ? " · " + e.texte : ""));
    s.appendChild(t); d.appendChild(s); d.appendChild(el("div", "pre", e.texte || e.outil));
    return d;
  }
  if (e.k === "agent_lance") return el("div", "evt", "🚀 sous-agent lancé : " + avecModele(e.texte, e.modele));
  if (e.k === "agent_fini") return el("div", "evt", (e.etat && e.etat !== "fini" ? "⚠️ " : "🏁 ") + "sous-agent " + (e.etat && e.etat !== "fini" ? e.etat : "fini") + " : " + e.texte);
  var b = el("div", "bulle " + (e.k === "utilisateur" ? "moi" : "lui"), e.texte), h = e.k === "utilisateur" ? heure(e.ts) : "";
  if (h) b.appendChild(el("time", null, h));
  return b;
}
function avancementNodes(usage, lot, agents, finis) {
  var n = [];
  if (usage.e) n.push(el("div", "err", "Quota indisponible (" + usage.e + ")"));
  else [["Session 5 h", usage.d.session], ["Semaine", usage.d.week]].forEach(function (p) {
    n.push(kv(p[0], p[1] ? pct(p[1].percent) : "indisponible"));
    if (p[1]) n.push(bar(p[1].percent));
  });
  n.push(kv("Sous-agents en cours", String(agents.length) + (finis ? " · " + finis + " finis" : "")));
  agents.forEach(function (a) { n.push(el("div", "muted", avecModele(a.description, a.model) + (a.depuis ? " · depuis " + heure(a.depuis) : ""))); });
  if (lot.e) n.push(el("div", "err", "Branches indisponibles (" + lot.e + ")"));
  else {
    n.push(kv("Branches todo-* prêtes", String(lot.d.lot.length)));
    lot.d.lot.forEach(function (t) { n.push(el("div", "muted", "#" + t.todo_id + " " + t.title)); });
  }
  return n;
}
// -- Le Pilote (todo #283): one fixed line (name · age · sub-agents · context), the thread or the raw tmux screen, quick replies
// only when the session asks (menu read on the screen, or a question in the journal: the server says which), then the prompt
// `ultron@<chantier>:~$` and the keys. Writing and keys are `ouvert` entries of the catalogue: no sheet, signed and journaled by
// the server (length, never the text). Every value shown is the server's.
var PILOTE_FIL_MS = 3000, PILOTE_TERMINAL_MS = 1000, PILOTE_BARRE_MS = 10000;
function direct(data, apres, fixe) {
  return post("/api/action/lire", data).then(function (r) {
    if (!r.ok) toast("❌ " + (r.erreur || r.resume || "refusé"), true, fixe);
    else if (apres) apres(r);
  }).catch(function () { toast("❌ " + MSG_HORS_LIGNE, true, fixe); });
}
function piloteResume(d, node) {
  var g = el("b", "g " + (d.attend ? "ambre" : d.travaille ? "vert vit" : "pale"));
  g.textContent = d.attend ? "⚠︎" : d.travaille ? "✻" : "●"; g.setAttribute("aria-hidden", "true");
  var parts = [], n = (d.sous_agents || []).length, court = (d.chantier ? d.chantier + " " : "") + (d.session.match(/[0-9]{2}h[0-9]{2}(-[0-9]+)?$/) || [d.session])[0];
  if (d.cree) parts.push(age(d.cree).replace(/ /g, ""));
  parts.push(n + " agent" + (n > 1 ? "s" : ""));
  if (d.contexte !== null && d.contexte !== undefined) parts.push(d.contexte + "%");
  node.replaceChildren(g, el("span", "nom", court), el("span", "met", parts.join(" · ")));
}
// todo #293: one thin line on top (‹ · state · fil|term), the thread takes the rest, then only when the session asks the quick
// replies on one line, and ONE line at the bottom: 📎 then the input (the keyboard's « send » key sends; empty = the Enter key).
function ouvrirSession(nom, retour) {
  sessionOrigine = nom;
  var recharger = function () { ouvrirSession(nom, retour); }, fiche = document.getElementById("fiche");
  var vue = "fil", dernier = null, cache = { usage: { e: "…" }, lot: { e: "…" } }, enCours = false, avLu = 0, barreLu = 0, premier = true, envoi = false;
  var haut = el("div", "pilote-haut"), chevron = el("button", "chevron", "‹"), etatBtn = el("button", "pilote-etat");
  var detail = el("div", "avancement pilote-detail"), vues = el("div", "vues"), fil = el("div", "fil");
  var term = el("pre", "terminal"), etat = el("div", "err"), bas = el("div", "pilote-bas"), rapides = el("div", "rapides");
  var props = el("div", "chips props"), touche_ = false, texteTerm = null;
  // terminal text is frozen while a finger is down (or text is selected) and while the reader is scrolled up (todo #418)
  term.addEventListener("touchstart", function () { touche_ = true; }, { passive: true });
  ["touchend", "touchcancel"].forEach(function (t) { term.addEventListener(t, function () { touche_ = false; }, { passive: true }); });
  function selection() { var sel = window.getSelection && window.getSelection(); return !!(sel && !sel.isCollapsed && term.contains(sel.anchorNode)); }
  chevron.type = "button"; chevron.setAttribute("aria-label", "Retour");
  chevron.addEventListener("click", function () { document.getElementById("retour").click(); });
  etatBtn.type = "button"; etatBtn.setAttribute("aria-expanded", "false"); etatBtn.setAttribute("aria-label", "État de la session " + nom);
  etatBtn.appendChild(el("span", "nom", nom));
  etatBtn.addEventListener("click", function () {
    detail.hidden = !detail.hidden; etatBtn.setAttribute("aria-expanded", String(!detail.hidden));
    if (!detail.hidden) { avLu = 0; maj(); }
  });
  haut.appendChild(chevron); haut.appendChild(etatBtn); haut.appendChild(vues);
  detail.hidden = true; etat.hidden = true;
  term.hidden = true; term.setAttribute("aria-label", "Terminal de la session");
  var ecrire = CATALOGUE && CATALOGUE["session-ecrire"];
  var champ = ecrire && ecrire.champs.filter(function (c) { return c.nom === "texte"; })[0];
  // the bottom line: 📎 joindre (opens 📥 Analyser for this session) then the input; no send button (todo #293)
  var ligneInvite = el("form", "invite"), saisie = el("input"), joindre = el("button", "joindre", "📎");
  joindre.type = "button"; joindre.setAttribute("aria-label", "📎 joindre un fichier");
  joindre.addEventListener("click", function () { chargerCatalogue().then(function () { ouvrirAnalyser(nom); }); });
  saisie.type = "text"; saisie.id = "pilote-saisie"; saisie.maxLength = champ ? champ.max : 4000;
  saisie.autocomplete = "off"; saisie.setAttribute("autocapitalize", "off"); saisie.setAttribute("enterkeyhint", "send");
  saisie.setAttribute("aria-label", "Écrire à la session"); saisie.placeholder = "ultron@…:~$";
  saisie.value = brouillons["pilote:" + nom] || "";
  ligneInvite.appendChild(joindre);
  if (micro()) {  // todo #379: 🎙 then ⏹ = the voice goes to this session (no sheet)
    var mic = el("button", "joindre");
    mic.type = "button"; mic.setAttribute("aria-label", "🎙 enregistrer un vocal pour cette session");
    mic.addEventListener("click", function () {
      if (!vocal.rec) prendreVeille();  // todo #332: the lock is asked inside the tap, the catalogue may take a while
      chargerCatalogue().then(function () { basculerVocal(nom); }, function () { if (!vocal.rec) relacherVeille(); });
    });
    ligneInvite.appendChild(boutonVocal(mic));
  }
  ligneInvite.appendChild(saisie);
  // todo #419: terminal view only: 3 shortcuts (session-choisir keys)
  var raccourcis = el("div", "raccourcis");
  [["ctrl-b", "Arrière-plan"], ["ctrl-x-s", "Envoyer maintenant"], ["echap", "Échap"]].forEach(function (r) {
    var b = el("button", "raccourci", r[1]);
    b.type = "button"; b.addEventListener("click", function () { touche(r[0]); });
    raccourcis.appendChild(b);
  });
  // todo #423/#439: 🔊 Lire sits on the LAST « reponse » bubble of the Fil (Main sessions only), full text of `session lire` (the terminal
  // view shows 24 lines only). The server speaks it with the montage's Piper voice (action voix-lire -> a wav of /api/pj/, played by an
  // <audio>); if that fails the browser's speechSynthesis takes over. The <audio> is unlocked inside the click (iOS needs the gesture);
  // second tap = stop.
  var parle = false, boutonLire = null, lecteur = null, rafale = 0;
  var SILENCE = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
  function lireFin() { parle = false; rafale++; if (boutonLire) boutonLire.textContent = "🔊 Lire"; }
  function lireStop() {
    if (lecteur) { lecteur.onended = lecteur.onerror = null; try { lecteur.pause(); } catch (e) { /* none */ } lecteur = null; }
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    lireFin();
  }
  function lireNavigateur(t) {
    var ss = window.speechSynthesis;
    if (!ss) { lireFin(); return; }
    var u = new SpeechSynthesisUtterance(t), v = (ss.getVoices() || []).filter(function (x) { return /^fr[-_]FR/i.test(x.lang); })[0];
    u.lang = "fr-FR"; if (v) u.voice = v;
    u.onend = u.onerror = lireFin;
    ss.cancel(); ss.speak(u);
  }
  function lireBouton(texte) {
    var b = el("button", "chip lire", parle ? "⏹ Stop" : "🔊 Lire");
    b.type = "button"; boutonLire = b;
    b.addEventListener("click", function () {
      if (parle) { lireStop(); return; }
      var t = texteParle(texte);
      if (!t) { b.textContent = "Rien à lire"; setTimeout(function () { b.textContent = "🔊 Lire"; }, 1500); return; }
      if (t.length > 2500) { var c = t.slice(0, 2500), p = Math.max(c.lastIndexOf(". "), c.lastIndexOf("\n")); t = (p > 1200 ? c.slice(0, p + 1) : c).trim(); }
      var mon = ++rafale, a = new Audio();
      a.src = SILENCE; var essai = a.play(); if (essai && essai.catch) essai.catch(function () { /* locked: the fallback speaks */ });
      lecteur = a; parle = true; b.textContent = "⏳ Voix…";
      var repli = function () { if (mon !== rafale || !parle) return; lecteur = null; b.textContent = "⏹ Stop"; lireNavigateur(t); };
      post("/api/action/lire", { action: "voix-lire", texte: t }).then(function (r) {
        if (mon !== rafale || !parle) return;
        var d = r && r.ok && r.donnees;
        if (!d || !/^\/api\/pj\/[A-Za-z0-9._-]+$/.test(d.url || "")) return repli();
        a.onended = lireFin; a.onerror = repli;
        a.src = d.url; b.textContent = "⏹ Stop";
        var p = a.play(); if (p && p.catch) p.catch(repli);
      }).catch(repli);
    });
    return b;
  }
  function dessinerFilBulles(d) {
    var dern = -1;
    d.entrees.forEach(function (e, i) { if (e.k === "reponse") dern = i; });
    boutonLire = null;
    fill_(fil, d.entrees.map(function (e, i) {
      var x = bulle(e);
      if (i === dern && !d.chantier) x.appendChild(lireBouton(e.texte));  // Main sessions only
      return x;
    }));
  }
  function bientot() { setTimeout(maj, 400); }
  function touche(code) { direct({ action: "session-choisir", nom: nom, touche: code }, bientot); }
  // the keyboard's send key (or Enter) submits the form; an empty line sends the Enter key, as the old ↵ button did
  ligneInvite.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var texte = saisie.value;
    if (!texte.trim()) return touche("entree");
    if (envoi) return;
    envoi = true;
    direct({ action: "session-ecrire", nom: nom, texte: texte }, function () {
      saisie.value = ""; delete brouillons["pilote:" + nom]; completer(); bientot();
    }).then(function () { envoi = false; });
  });
  // completion of the /commands (the catalogue's suggestions): shown while the line is « /… » without a space
  function completer() {
    var v = saisie.value, liste = (champ && champ.suggestions) || [];
    var ok = v.charAt(0) === "/" && v.indexOf(" ") < 0 ? liste.filter(function (c) { return c.indexOf(v) === 0 && c !== v; }) : [];
    props.replaceChildren.apply(props, ok.map(function (c) {
      return chip(c, false, function () { saisie.value = c; brouillons["pilote:" + nom] = c; completer(); saisie.focus(); });
    }));
    props.hidden = !ok.length;
  }
  saisie.addEventListener("input", function () { brouillons["pilote:" + nom] = saisie.value; completer(); });
  function dessinerVues() {
    vues.replaceChildren(chip("fil", vue === "fil", function () { basculer("fil"); }), chip("term", vue === "terminal", function () { basculer("terminal"); }));
    fil.hidden = vue !== "fil"; term.hidden = vue !== "terminal";
    ligneInvite.hidden = vue !== "fil"; raccourcis.hidden = vue !== "terminal"; if (vue !== "fil") props.hidden = true; else completer();
  }
  function basculer(v) { vue = v; premier = true; texteTerm = null; dessinerVues(); maj(); }
  // quick replies, only when the session asks: the menu read on its screen (keys 1-9), else a question (catalogue's texts)
  function dessinerRapides(d) {
    var n = [];
    if (d.menu && d.menu.length) d.menu.slice(0, 3).forEach(function (o) {
      n.push(chip(o.touche + ". " + o.libelle, false, function () { touche(o.touche); }));
    });
    else if (d.attend && champ) champ.rapides.forEach(function (x) {
      n.push(chip(x[1], false, function () { direct({ action: "session-ecrire", nom: nom, texte: x[0] }, bientot); }));
    });
    rapides.replaceChildren.apply(rapides, n);
    rapides.hidden = !n.length;
  }
  function dessinerBarre() {
    if (!dernier) return;
    var n = [kv("Session", dernier.session), kv("Chantier", dernier.chantier || "Main")];
    if (dernier.contexte !== null && dernier.contexte !== undefined) { n.push(kv("Contexte utilisé", dernier.contexte + " %")); n.push(bar(dernier.contexte)); }
    if (dernier.tronque) n.push(kv("Échanges affichés", dernier.entrees.length + " sur " + dernier.total));
    n = n.concat(avancementNodes(cache.usage, cache.lot, dernier.sous_agents || [], dernier.sous_agents_finis));
    n.push(form("session-relancer", { nom: nom }));
    n.push(boutonFermer(nom));
    n.push(form("session-nouvelle", {}, { chantiers: chantiersLus }));
    piloteResume(dernier, etatBtn);
    detail.replaceChildren.apply(detail, n);
    saisie.placeholder = "ultron@" + (dernier.chantier || "main") + ":~$";
  }
  function vivant() { return ficheEcran === recharger; }
  function maj() {
    if (!vivant() || enCours) return;
    enCours = true;
    var av = !detail.hidden && Date.now() - avLu > SESSION_AVANCEMENT_MS, lireBarre = vue === "fil" || !dernier || Date.now() - barreLu > PILOTE_BARRE_MS;
    if (av) avLu = Date.now();
    Promise.all([lireBarre ? lire("session", vue === "fil" ? { nom: nom } : { nom: nom, n: "1" }) : null,
                 vue === "terminal" ? lire("session-ecran", { nom: nom }) : null,
                 av ? lireOuErreur("usage") : null, av ? lireOuErreur("lot") : null]).then(function (r) {
      if (!vivant()) return;
      var scroller = vue === "fil" ? fil : term, enBas = premier || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < (vue === "fil" ? 120 : 40);
      if (r[0]) { barreLu = Date.now(); dernier = r[0]; if (vue === "fil") dessinerFilBulles(r[0]); }
      if (r[2]) cache.usage = r[2];
      if (r[3]) cache.lot = r[3];
      if (r[1]) {
        var texte = r[1].lignes.join("\n");
        if (texte !== texteTerm && (premier || (enBas && !touche_ && !selection()))) { term.textContent = texte; texteTerm = texte; }
        if (touche_) enBas = false;  // never move the view under a finger
        dessinerRapides(r[1]);
      } else if (r[0]) dessinerRapides(r[0]);
      dessinerBarre();
      etat.hidden = true;  // no refresh-time line any more (todo #293): only an error shows
      if (enBas) scroller.scrollTop = scroller.scrollHeight;
      premier = false;
    }).catch(function (e) { etat.textContent = "Indisponible (" + (e.message || e) + ")"; etat.hidden = false; }).then(function () { enCours = false; });
  }
  rapides.hidden = true; props.hidden = true;
  bas.appendChild(rapides); bas.appendChild(props); bas.appendChild(raccourcis); bas.appendChild(ligneInvite);
  dessinerVues();
  showOverlay([haut, detail, fil, term, etat, bas], recharger, retour);
  // full screen (todos #292, #293): no « Retour » bar (the ‹ of the top line), no tab bar under it; the sheet follows the visible
  // height (iPhone keyboard) and, keyboard open, drops the bottom safe area (the keyboard covers the home indicator)
  fiche.classList.add("plein");
  document.body.classList.add("pilote");
  var vv = window.visualViewport;
  function suivreClavier() {
    if (!vivant()) { fiche.style.height = ""; fiche.style.top = ""; fiche.classList.remove("clavier"); if (vv) { vv.removeEventListener("resize", suivreClavier); vv.removeEventListener("scroll", suivreClavier); } return; }
    if (!vv) return;
    var ouvert = vv.height < window.innerHeight - 120;
    fiche.classList.toggle("clavier", ouvert);
    if (ouvert) { fiche.style.height = vv.height + "px"; fiche.style.top = vv.offsetTop + "px"; fiche.style.bottom = "auto"; }
    else { fiche.style.height = fiche.style.top = fiche.style.bottom = ""; }
  }
  if (vv) { vv.addEventListener("resize", suivreClavier); vv.addEventListener("scroll", suivreClavier); suivreClavier(); }
  completer();
  maj();
  var tic = 0, timer = setInterval(function () {
    if (!vivant()) { if (parle) lireStop(); return clearInterval(timer); }
    tic += PILOTE_TERMINAL_MS;
    if (document.hidden || current !== "fiche") return;
    if (vue === "terminal" || tic % PILOTE_FIL_MS === 0) maj();
  }, PILOTE_TERMINAL_MS);
}
[["plus-profil", ouvrirProfil], ["plus-focus", ouvrirFocus], ["plus-cap", ouvrirCap]].forEach(function (p) {
  document.getElementById(p[0]).addEventListener("click", function () { chargerCatalogue().then(p[1]); });
});
var depart = (location.hash || "").slice(1);
if (depart) selectTab(depart);
initPush();
refresh();
setInterval(function () { if (actif()) refresh(); }, REFRESH_MS);
document.addEventListener("visibilitychange", function () { if (actif()) refresh(); });
window.addEventListener("pageshow", function (e) { if (e.persisted && actif()) refresh(); });
