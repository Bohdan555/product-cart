/* Кошик з акцій — спільні мікровзаємодії: підказки, «набігання» чисел, політ у кошик, лайк, вібрація.
   Працює разом із Turbo: усе вішається через делегування подій або на turbo:load. */
(function () {
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

  // легка вібрація там, де телефон її підтримує (iOS Safari тихо ігнорує)
  window.haptic = function (ms) { try { if (navigator.vibrate) navigator.vibrate(ms || 12); } catch (e) {} };

  // коротка підказка внизу екрана
  var tt;
  window.toast = function (msg) {
    var t = document.getElementById('uiToast');
    if (!t) { t = document.createElement('div'); t.id = 'uiToast'; t.className = 'ui-toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
    t.textContent = msg; t.classList.add('on'); clearTimeout(tt);
    tt = setTimeout(function () { t.classList.remove('on'); }, 1900);
  };

  // число «набігає» від 0 (або від попереднього значення) до цільового
  function fmt(n, dec) { return n.toLocaleString('uk-UA', { minimumFractionDigits: dec, maximumFractionDigits: dec }).replace(/ /g, ' '); }
  window.countUp = function (el) {
    var to = parseFloat(el.dataset.count); if (isNaN(to)) return;
    var dec = +(el.dataset.dec || 0), from = parseFloat(el.dataset.from || '0'), prefix = el.dataset.prefix || '';
    if (reduce) { el.textContent = prefix + fmt(to, dec); return; }
    var t0 = performance.now(), dur = 650;
    (function step(t) {
      var k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      el.textContent = prefix + fmt(from + (to - from) * e, dec);
      if (k < 1) requestAnimationFrame(step);
    })(t0);
  };
  function countAll(root) { (root || document).querySelectorAll('[data-count]').forEach(window.countUp); }
  window.uiRefresh = countAll;
  document.addEventListener('turbo:load', function () { countAll(); });
  if (document.readyState !== 'loading') countAll(); else document.addEventListener('DOMContentLoaded', function () { countAll(); });

  // «пульс» елемента (значок кошика, сердечко)
  window.bump = function (el) { if (!el) return; el.classList.remove('ui-bump'); void el.offsetWidth; el.classList.add('ui-bump'); };

  // товар «летить» до значка кошика
  window.flyToCart = function (from) {
    var target = Array.prototype.filter.call(document.querySelectorAll('.hcart, .tabs a[href="/silpo/cart"] .tico'),
      function (x) { return x.offsetParent !== null; })[0];
    if (!from || !target || reduce) { window.bump(target); return; }
    var a = from.getBoundingClientRect(), b = target.getBoundingClientRect();
    var dot = document.createElement('div'); dot.className = 'ui-fly'; dot.textContent = '+1';
    dot.style.left = (a.left + a.width / 2 - 16) + 'px'; dot.style.top = (a.top + a.height / 2 - 16) + 'px';
    document.body.appendChild(dot);
    requestAnimationFrame(function () {
      dot.style.transform = 'translate(' + (b.left + b.width / 2 - a.left - a.width / 2) + 'px,' + (b.top + b.height / 2 - a.top - a.height / 2) + 'px) scale(.5)';
      dot.style.opacity = '.2';
    });
    setTimeout(function () { dot.remove(); window.bump(target); }, 620);
  };

  // маленьке свято (коли все зі списку куплено)
  window.celebrate = function () {
    if (reduce) return;
    var box = document.createElement('div'); box.className = 'ui-confetti';
    for (var i = 0; i < 28; i++) {
      var s = document.createElement('i');
      s.style.left = Math.random() * 100 + '%'; s.style.animationDelay = Math.random() * 0.25 + 's';
      s.style.background = ['#ffd84d', '#1f7a4d', '#4cc38a', '#ff8a65', '#7c9cff'][i % 5];
      s.style.transform = 'rotate(' + Math.random() * 360 + 'deg)';
      box.appendChild(s);
    }
    document.body.appendChild(box); setTimeout(function () { box.remove(); }, 1800);
  };

  // Фото для розпізнавання: зменшуємо до 1568 px (швидше й дешевше). Якщо телефон не вміє декодувати
  // (рідкісні формати, старі вбудовані браузери) — віддаємо оригінал, аби фото все одно пішло.
  window.shrinkPhoto = function (file) {
    return new Promise(function (ok) {
      var done = false, url = null;
      function give(x) { if (done) return; done = true; if (url) { try { URL.revokeObjectURL(url); } catch (e) {} } ok(x || file); }
      try { url = URL.createObjectURL(file); } catch (e) { return give(file); }
      var img = new Image();
      img.onload = function () {
        try {
          var k = Math.min(1, 1568 / Math.max(img.naturalWidth || img.width, img.naturalHeight || img.height));
          var c = document.createElement('canvas');
          c.width = Math.max(1, Math.round((img.naturalWidth || img.width) * k)); c.height = Math.max(1, Math.round((img.naturalHeight || img.height) * k));
          c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
          if (c.toBlob) c.toBlob(function (b) { give(b); }, 'image/jpeg', 0.85); else give(file);
        } catch (e) { give(file); }
      };
      img.onerror = function () { give(file); };
      setTimeout(function () { give(file); }, 10000);
      img.src = url;
    });
  };
})();

/* ---------- PWA: встановлення на екран і push-сповіщення ---------- */
(function () {
  var bip = null;   // Android/Chrome: подія «можна встановити» — показуємо свою кнопку
  window.addEventListener('beforeinstallprompt', function (e) { e.preventDefault(); bip = e; render(); });
  window.addEventListener('appinstalled', function () { bip = null; report('pwa_installed'); render(); });
  var ua = navigator.userAgent, ios = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  var standalone = function () { return window.isStandalone || (window.matchMedia && matchMedia('(display-mode: standalone)').matches); };
  var csrf = function () { var m = document.querySelector('meta[name=csrf]'); return m ? m.content : ''; };
  function report(ev, extra) {
    try { var fd = new FormData(); fd.append('event', ev); fd.append('extra', extra || '');
      if (navigator.sendBeacon) navigator.sendBeacon('/client-log', fd); } catch (e) {}
  }
  function hidden(k) { try { return !!localStorage.getItem(k); } catch (e) { return false; } }

  // Картка «Додати на екран»: Android — кнопка, iPhone — інструкція, уже встановлено — ховаємо
  function render() {
    document.querySelectorAll('[data-install]').forEach(function (box) {
      var mode = standalone() ? 'done' : bip ? 'prompt' : ios ? 'ios' : 'none';
      if (box.dataset.dismiss && hidden(box.dataset.dismiss)) mode = 'none';
      box.dataset.mode = mode;
      box.hidden = mode === 'none' || (mode === 'done' && box.dataset.install !== 'always');
    });
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-install-go]');
    if (b && bip) { bip.prompt(); bip.userChoice.then(function (c) { report('pwa_prompt', c.outcome); bip = null; render(); }); }
    var x = e.target.closest('[data-install-x]');
    if (x) { var box = x.closest('[data-install]'); try { localStorage.setItem(box.dataset.dismiss, '1'); } catch (err) {} box.hidden = true; }
  });

  // Push: ввімкнути / вимкнути на цьому пристрої
  function pushSupported() { return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window; }
  function b64(s) { s = s.replace(/-/g, '+').replace(/_/g, '/'); while (s.length % 4) s += '=';
    var raw = atob(s), out = new Uint8Array(raw.length); for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i); return out; }
  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF': csrf(), 'X-PWA': standalone() ? '1' : '0' },
      body: JSON.stringify(body) }).then(function (r) { return r.json(); });
  }
  function current() {
    if (!pushSupported()) return Promise.resolve(null);
    return navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); });
  }
  window.pushOn = function () {
    if (!pushSupported()) return Promise.reject(new Error(ios && !standalone() ? 'ios-not-installed' : 'unsupported'));
    return Notification.requestPermission().then(function (perm) {
      if (perm !== 'granted') throw new Error(perm === 'denied' ? 'denied' : 'dismissed');
      return Promise.all([navigator.serviceWorker.ready, fetch('/push/key').then(function (r) { return r.json(); })]);
    }).then(function (x) {
      return x[0].pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(x[1].key) });
    }).then(function (sub) { return post('/push/subscribe', sub.toJSON()); })
      .then(function (d) { if (!d.ok) throw new Error(d.error || 'server'); report('push_on', standalone() ? 'pwa' : 'browser'); return d; });
  };
  window.pushOff = function () {
    return current().then(function (sub) {
      if (!sub) return;
      return post('/push/unsubscribe', { endpoint: sub.endpoint }).then(function () { return sub.unsubscribe(); });
    });
  };
  function paint(box) {
    var t = box.querySelector('[data-push-toggle]'), msg = box.querySelector('[data-push-msg]');
    if (!pushSupported()) {
      box.dataset.state = ios && !standalone() ? 'ios' : 'unsupported';
      if (t) t.disabled = true; return;
    }
    if (Notification.permission === 'denied') { box.dataset.state = 'denied'; if (t) { t.checked = false; t.disabled = true; } return; }
    current().then(function (sub) { box.dataset.state = sub ? 'on' : 'off'; if (t) { t.checked = !!sub; t.disabled = false; } });
  }
  function bindPush() {
    document.querySelectorAll('[data-push]').forEach(function (box) {
      paint(box);
      var t = box.querySelector('[data-push-toggle]'); if (!t || t.dataset.bound) return; t.dataset.bound = '1';
      t.addEventListener('change', function () {
        t.disabled = true;
        (t.checked ? window.pushOn() : window.pushOff()).then(function () {
          if (window.toast) toast(t.checked ? 'Сповіщення ввімкнено 🔔' : 'Сповіщення вимкнено');
          if (window.haptic) haptic(12);
        }).catch(function (e) {
          t.checked = false;
          var why = e && e.message;
          if (window.toast) toast(why === 'denied' ? 'Сповіщення заборонені в налаштуваннях' : why === 'ios-not-installed'
            ? 'Спершу додайте Кошик на початковий екран' : 'Не вдалося ввімкнути сповіщення');
          report('push_fail', why || '');
        }).then(function () { paint(box); });
      });
    });
  }
  function init() { render(); bindPush(); }
  document.addEventListener('turbo:load', init);
  if (document.readyState !== 'loading') init(); else document.addEventListener('DOMContentLoaded', init);
})();

/* 👍 / 👎 рецепту: одразу перемикаємо, запит у фоні; повторне натискання знімає голос */
document.addEventListener('click', function (e) {
  var b = e.target.closest('.rvote .rv-b'); if (!b) return;
  e.preventDefault();
  var box = b.closest('.rvote'), prev = box.dataset.my, v = b.dataset.v, next = prev === v ? '' : v;
  var n = box.querySelector('.rv-n'), cnt = parseInt(n.textContent || '0', 10) || 0;
  function paint(my, likes) {
    box.dataset.my = my;
    box.querySelectorAll('.rv-b').forEach(function (x) { x.setAttribute('aria-pressed', x.dataset.v === my); });
    n.textContent = likes > 0 ? likes : '';
  }
  var guess = cnt + (next === 'up' ? 1 : 0) - (prev === 'up' ? 1 : 0);
  paint(next, guess); if (window.bump) bump(b); if (window.haptic) haptic(next ? 15 : 8);
  var fd = new FormData(); fd.append('id', box.dataset.vote); fd.append('vote', next || 'none');
  if (box.dataset.csrf) fd.append('csrf', box.dataset.csrf);
  fetch('/recipe/vote', { method: 'POST', body: fd, credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
    if (d.error) throw new Error(d.error);
    paint(d.vote, d.likes);
    if (window.toast) window.toast(d.vote === 'up' ? 'Фух, рецепт видихнув 😮‍💨' : d.vote === 'down' ? 'Смаки різні — підберемо інше 💛' : 'Добре, поки без оцінки 🤷';
  }).catch(function () { paint(prev, cnt); if (window.toast) window.toast('Не вдалося зберегти'); });
});
