/* Кошик з акцій — service worker.
   - статика (стилі, скрипти, шрифти, іконки): з кешу, бо адреси мають версію;
   - сторінки: спершу мережа, копію відкладаємо — без інтернету покажемо останню збережену (список покупок, рецепти);
   - push: показуємо сповіщення, тап відкриває потрібну сторінку.
   Нічого, що ходить у «Сільпо», платить чи змінює дані, не кешуємо. */
var VERSION = '__VERSION__';
var STATIC = 'static-' + VERSION, PAGES = 'pages-v2';
var PRECACHE = ['/offline', '/static/style.css?v=__CSS_V__', '/static/ui.js?v=__JS_V__', '/static/vendor/turbo-8.0.12.js',
                '/static/icons/icon-192.png?v=__ICON_V__'];
var NO_CACHE = /^\/(silpo|auth|go|cron|tg|push|client-log|logout|login|register|admin|offers|fridge\/(scan|badge)|photos)/;
var MAX_PAGES = 40;
var FAST = /^\/(|me|pantry|menu|buy|cook|today|list|profile|notify)$/;   // екрани, що відкриваються миттєво з копії
function strip(t) { return t.replace(/<meta name="csrf"[^>]*>/, '').replace(/data-rid="[^"]*"/g, ''); }
function tell(id, path) {
  self.clients.get(id).then(function (cl) {
    if (cl) cl.postMessage({ type: 'fresh', path: path });
    else self.clients.matchAll({ type: 'window' }).then(function (list) { list.forEach(function (w) { w.postMessage({ type: 'fresh', path: path }); }); });
  });
}


self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(STATIC).then(function (c) { return c.addAll(PRECACHE); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return (k.indexOf('static-') === 0 && k !== STATIC) || k === 'pages-v1'; })
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
  // Відкриття застосунку (повне завантаження головних екранів): одразу збережена копія, свіжа — у фоні.
  // Якщо свіжа інша — кажемо сторінці тихо оновитися (Turbo). Переходи всередині (Turbo) — як і раніше, з мережі.
  // лише «відкриття застосунку» (без referrer): після збереження форми чи переходу — завжди свіже з мережі
  if (req.mode === 'navigate' && FAST.test(url.pathname) && !url.search && !req.referrer) {
    var key = url.pathname, client = e.resultingClientId || e.clientId;
    e.respondWith(caches.open(PAGES).then(function (c) {
      return c.match(key).then(function (hit) {
        var old = hit && hit.clone();      // клон до того, як hit піде сторінці (тіло читається лише раз)
        var fresh = fetch(req).then(function (res) {
          if (res.ok && res.type === 'basic' && !res.redirected) {
            var copy = res.clone();
            if (old) Promise.all([old.text(), res.clone().text()]).then(function (t) {
              if (strip(t[0]) !== strip(t[1])) setTimeout(function () { tell(client, key); }, 300);
            });
            c.put(key, copy).then(function () { trim(c); });
          }
          return res;
        });
        if (!hit) return fresh.catch(function () { return caches.match('/offline'); });
        e.waitUntil(fresh.catch(function () {}));
        return hit;
      });
    }));
    return;
  }
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
  // число на іконці апки (скільки продуктів скоро зіпсується) — там, де браузер це вміє
  if (typeof d.badge === 'number' && self.navigator && self.navigator.setAppBadge) {
    try { (d.badge ? self.navigator.setAppBadge(d.badge) : self.navigator.clearAppBadge()).catch(function () {}); } catch (err) {}
  }
  e.waitUntil(self.registration.showNotification(d.title || 'Кошик з акцій', {
    body: d.body || '', icon: '/static/icons/icon-192.png?v=__ICON_V__', badge: '/static/icons/favicon-64.png',
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
