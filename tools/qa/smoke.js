// QA-прохід «Кошика з акцій»: сторінки × світла/темна тема на iPhone 13.
// Збирає: помилки JS і консолі, відповіді ≥ 400, биті зображення, горизонтальний скрол,
// зони дотику < 44 px, текст < 13 px, биті внутрішні посилання. Знімки — у out/.
// Запуск: node tools/qa/smoke.js [BASE_URL] [OUT_DIR] [--login]
//   --login: зареєструвати тимчасового користувача (ЛИШЕ для локального сервера, не для продакшну).
const { chromium, devices } = require('playwright');
const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2).filter(a => !a.startsWith('--'));
const BASE = (args[0] || 'http://127.0.0.1:8765').replace(/\/$/, '');
const OUT = args[1] || 'qa-out';
const LOGIN = process.argv.includes('--login');
const PROD = !/127\.0\.0\.1|localhost/.test(BASE);
fs.mkdirSync(OUT, { recursive: true });

const GUEST = ['/', '/today', '/today/dishes?have=eggs,cheese,pepper,onion&t=30&n=2&meal=dinner',
  '/today/dish/omelette?have=eggs,cheese,pepper,onion&t=30&n=2', '/menu/setup?step=3', '/menu', '/menu?view=buy',
  '/list', '/me', '/register', '/privacy', '/photos', '/offline'];
const USER = ['/start', '/fridge', '/me', '/profile', '/dishes', '/notify', '/silpo', '/cook'];
const IGNORE_URL = /google-analytics|doubleclick|favicon\.ico$/;

async function audit(page) {
  return page.evaluate(() => {
    const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const label = e => (e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\s+/).slice(0, 2).join('.') : '')
      + ' «' + (e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 30) + '»');
    const doc = document.documentElement;
    const overflowX = doc.scrollWidth > doc.clientWidth + 1;
    const wide = overflowX ? [...document.querySelectorAll('body *')].filter(e => vis(e) && e.getBoundingClientRect().right > doc.clientWidth + 1
      && !e.closest('.hm-row,.td-car,.td-pool,.cr,[style*="overflow"]') && getComputedStyle(e.parentElement).overflowX === 'visible').slice(0, 5).map(label) : [];
    const brokenImg = [...document.images].filter(i => vis(i) && i.complete && i.naturalWidth === 0).map(i => i.src.slice(0, 90));
    const smallTap = [...document.querySelectorAll('a,button,label,[role=button],input[type=checkbox],select')]
      .filter(e => vis(e) && !e.closest('footer') && e.getBoundingClientRect().height < 44 && e.getBoundingClientRect().height > 8)
      // не рахуємо: логотип, посилання всередині тексту (WCAG 2.5.8 їх не вимагає), чекбокс у великому рядку-підписі
      .filter(e => !e.matches('a.logo') && !(e.tagName === 'A' && getComputedStyle(e).display === 'inline' && e.closest('p,li,small,span.hint'))
        && !(e.tagName === 'INPUT' && e.closest('label') && e.closest('label').getBoundingClientRect().height >= 44))
      .map(label);
    const smallText = [...document.querySelectorAll('body *')].filter(e => vis(e) && [...e.childNodes].some(t => t.nodeType === 3 && t.textContent.trim())
      && parseFloat(getComputedStyle(e).fontSize) < 13 && !e.closest('footer')).map(label);
    const links = [...new Set([...document.querySelectorAll('a[href^="/"]')].map(a => a.getAttribute('href')).filter(h => !h.startsWith('//')))];
    return { overflowX, wide, brokenImg, smallTap: smallTap.slice(0, 8), smallTapN: smallTap.length, smallText: smallText.slice(0, 5), smallTextN: smallText.length, links };
  });
}

(async () => {
  const browser = await chromium.launch();
  const report = { base: BASE, at: new Date().toISOString(), pages: [], badLinks: [] };
  const allLinks = new Set();
  for (const scheme of ['light', 'dark']) {
    const ctx = await browser.newContext({ ...devices['iPhone 13'], deviceScaleFactor: 1, colorScheme: scheme });
    const page = await ctx.newPage();
    let errs = [], bad = [];
    page.on('pageerror', e => errs.push('JS: ' + String(e.message).slice(0, 160)));
    page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 160)); });
    page.on('response', r => { if (r.status() >= 400 && !IGNORE_URL.test(r.url())) bad.push(r.status() + ' ' + r.url().replace(BASE, '').slice(0, 100)); });
    let list = GUEST;
    if (LOGIN && !PROD) {
      await page.goto(BASE + '/register');
      await page.fill('input[name=email]', `qa${Date.now()}@example.com`); await page.fill('input[name=password]', 'password123');
      await page.check('input[name=agree]'); await Promise.all([page.waitForNavigation(), page.click('button[type=submit]')]);
      list = [...GUEST, ...USER];
    }
    for (const p of list) {
      errs = []; bad = [];
      const t0 = Date.now(); let status = 0;
      try { const r = await page.goto(BASE + p, { waitUntil: 'networkidle', timeout: 30000 }); status = r ? r.status() : 0; }
      catch (e) { errs.push('навігація: ' + String(e.message).slice(0, 120)); }
      await page.waitForTimeout(500);
      const ms = Date.now() - t0;
      const a = await audit(page).catch(e => ({ error: String(e) }));
      (a.links || []).forEach(l => allLinks.add(l.split('#')[0]));
      delete a.links;
      const shot = path.join(OUT, `${scheme}-${p.replace(/[^a-z0-9]+/gi, '_').replace(/^_|_$/g, '') || 'home'}.png`);
      await page.screenshot({ path: shot, fullPage: true }).catch(() => {});
      report.pages.push({ scheme, path: p, status, ms, errors: [...new Set(errs)], badResponses: [...new Set(bad)], ...a, shot });
    }
    await ctx.close();
  }
  // внутрішні посилання (лише GET, без форм)
  const ctx = await browser.newContext();
  for (const l of [...allLinks].filter(l => !/logout|auth\/|\/go\?|cron|admin/.test(l)).slice(0, 80)) {
    try { const r = await ctx.request.get(BASE + l, { maxRedirects: 5, timeout: 20000 }); if (r.status() >= 400) report.badLinks.push(r.status() + ' ' + l); }
    catch (e) { report.badLinks.push('ERR ' + l); }
  }
  await browser.close();
  const issues = report.pages.filter(p => p.status >= 400 || p.errors.length || p.badResponses.length || p.overflowX || (p.brokenImg || []).length);
  report.summary = { pages: report.pages.length, withIssues: issues.length, badLinks: report.badLinks.length,
    slow: report.pages.filter(p => p.ms > 3000).map(p => `${p.path} ${p.ms}ms`),
    smallTapTotal: report.pages.reduce((s, p) => s + (p.smallTapN || 0), 0), smallTextTotal: report.pages.reduce((s, p) => s + (p.smallTextN || 0), 0) };
  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 1));
  // короткий підсумок у консоль (щоб не читати весь JSON)
  console.log(JSON.stringify(report.summary));
  for (const p of issues) console.log(`! [${p.scheme}] ${p.path} st=${p.status}` + (p.errors.length ? ' errors=' + JSON.stringify(p.errors) : '') + (p.badResponses.length ? ' bad=' + JSON.stringify(p.badResponses) : '')
    + (p.overflowX ? ' overflowX=' + JSON.stringify(p.wide) : '') + ((p.brokenImg || []).length ? ' img=' + JSON.stringify(p.brokenImg) : ''));
  for (const l of report.badLinks) console.log('! link ' + l);
})();
