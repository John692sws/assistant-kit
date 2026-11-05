"use strict";
// Minimal service worker (todo #240): shows pushed notifications, a click opens the right tab, the app shell is cached
// (network first, cache only when offline). /api/* is never cached.
var CACHE = "ultron-shell-v10";  // todo #542: annulation des fusions du 05/11 (542, 543, 546), retour à l'état de 56bd002
var SHELL = ["/", "/style.css", "/app.js", "/manifest.webmanifest", "/icon-192.png", "/apple-touch-icon.png"];
var TABS = ["briefing", "travail", "equipes", "fil", "moi"];
var ANCIENS = { accueil: "briefing", questions: "briefing", todos: "travail", escouades: "equipes" };  // before todo #270
function onglet(t) { t = ANCIENS[t] || t; return TABS.indexOf(t) >= 0 ? t : "briefing"; }

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function () { return self.skipWaiting(); }));
});
self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});
self.addEventListener("fetch", function (e) {
  var url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin || SHELL.indexOf(url.pathname) < 0) return;
  e.respondWith(fetch(e.request).then(function (r) {
    if (r.ok) { var copy = r.clone(); caches.open(CACHE).then(function (c) { c.put(url.pathname, copy); }); }
    return r;
  }).catch(function () { return caches.match(url.pathname); }));
});
self.addEventListener("push", function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {}; }
  var tab = onglet(d.onglet);
  var opts = { body: d.body || "", icon: "/icon-192.png", badge: "/icon-192.png", data: { onglet: tab } };
  if (d.tag) opts.tag = d.tag;
  e.waitUntil(self.registration.showNotification(d.title || "Ultron", opts));
});
self.addEventListener("notificationclick", function (e) {
  var tab = onglet(e.notification.data && e.notification.data.onglet);
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    if (list.length) { list[0].postMessage({ onglet: tab }); return list[0].focus(); }
    return self.clients.openWindow("/#" + tab);
  }));
});
