/* Кошик з акцій — service worker.
   - статика (стилі, скрипти, шрифти, іконки): з кешу, бо адреси мають версію;
   - сторінки: спершу мережа, копію відкладаємо — без інтернету покажемо останню збережену (список покупок, рецепти);
   - push: показуємо сповіщення, тап відкриває потрібну сторінку.
   Нічого, що ходить у «Сільпо», платить чи змінює дані, не кешуємо. */
var VERSION = '__VERSION__';
var STATIC = 'static-' + VERSION, PAGES = 'pages-v1';
var PRECACHE = ['/offline', '/static/style.css?v=__CSS_V__', '/static/ui.js?v=__JS_V__', '/static/vendor/turbo-8.0.12.js',
                '/static/icons/icon-192.png', '/static/fonts/onest-cyrillic-wght-normal.woff2',
                '/static/fonts/unbounded-cyrillic-wght-normal.woff2'];
var NO_CACHE = /^\/(silpo|auth|go|cron|tg|push|client-log|logout|login|register|admin|offers|fridge\/scan|photos)/;
var MAX_PAGES = 40;

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(STATIC).then(function (c) { return c.addAll(PRECACHE); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k.indexOf('static-') === 0 && k !== STATIC; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

function trim(cache) {
  cache.keys().then(function (keys) { if (keys.length > MAX_PAGES) cache.delete(keys[0]).then(function () { trim(cache); }); });
}

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.indexOf('/static/') === 0) {
    e.respondWith(caches.match(req).then(function (hit) {
      return hit || fetch(req).then(function (res) {
        if (res.ok) { var copy = res.clone(); caches.open(STATIC).then(function (c) { c.put(req, copy); }); }
        return res;
      });
    }));
    return;
  }
  var html = req.mode === 'navigate' || (req.headers.get('accept') || '').indexOf('text/html') >= 0;
  if (!html || NO_CACHE.test(url.pathname) || url.searchParams.has('partial')) return;
  e.respondWith(fetch(req).then(function (res) {
    if (res.ok && res.type === 'basic' && !res.redirected) {
      var copy = res.clone();
      caches.open(PAGES).then(function (c) { c.put(url.pathname + url.search, copy).then(function () { trim(c); }); });
    }
    return res;
  }).catch(function () {
    return caches.open(PAGES).then(function (c) {
      return c.match(url.pathname + url.search).then(function (hit) {
        return hit || c.match(url.pathname).then(function (hit2) {
          return hit2 || caches.match('/offline');
        });
      });
    });
  }));
});

// Вийшли з акаунта — прибираємо збережені сторінки
self.addEventListener('message', function (e) {
  if (e.data === 'logout') e.waitUntil(caches.delete(PAGES));
});

self.addEventListener('push', function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'Кошик з акцій', {
    body: d.body || '', icon: '/static/icons/icon-192.png', badge: '/static/icons/favicon-64.png',
    tag: d.tag || undefined, data: { url: d.url || '/' }
  }));
});

self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  var url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if ('focus' in list[i]) { list[i].navigate(url); return list[i].focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
