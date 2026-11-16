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
})();
