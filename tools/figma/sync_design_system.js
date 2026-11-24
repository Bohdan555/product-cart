// Синхронізація дизайн-системи Figma з кодом (static/style.css :root). Запускати ОДНИМ викликом use_figma
// у файлі VPQQbUGx4d4mkoVdstqCWc (Starter: мало викликів на добу). Скрипт ідемпотентний: можна повторити.
// Що робить: 1) кольори змінних «Кольори» за назвою → нова палітра (світла тема; на Starter 1 режим);
// 2) радіуси й розміри в «Розміри» → 6/12/16/20/28 і шкала шрифтів; 3) текстові стилі → SF Pro (або Inter)
// і найближчий розмір шкали 13/14/16/18/24/32; 4) компоненти на «Дизайн-система»: картки й рядки — білі (#fffdf9),
// мінімальна висота кнопок/чипів 44. Повертає, що змінено і що не розпізнано (для ручної правки).
const C = { bg: '#f6f3ee', card: '#fffdf9', ink: '#151413', muted: '#6b665e', line: '#e6e0d6', accent: '#151413',
  'accent-ink': '#f6f3ee', soft: '#ece6dc', chip: '#ece7df', sticker: '#c9a45c', 'sticker-ink': '#151413',
  bad: '#b3261e', 'bad-soft': '#f6e1dc', brand: '#1d3b2f', heart: '#b4485f', pastel: '#eee8df', spot: '#c9a45c' };
// ключові слова в назві змінної → токен (перший збіг)
const MAP = [[/accent.?ink|on.?accent|button.?text/i, 'accent-ink'], [/sticker.?ink/i, 'sticker-ink'], [/bad.?soft|error.?bg/i, 'bad-soft'],
  [/bad|error|danger/i, 'bad'], [/sticker|gold|promo|sale|discount/i, 'sticker'], [/brand|green|header/i, 'brand'],
  [/heart|like/i, 'heart'], [/accent|primary|button/i, 'accent'], [/muted|secondary|subtle/i, 'muted'],
  [/line|border|divider|stroke/i, 'line'], [/chip/i, 'chip'], [/soft/i, 'soft'], [/card|surface|white/i, 'card'],
  [/^(s|spot)[-/ ]|насич|spot/i, 'spot'], [/^p[-/ ]|pastel|tomato|citrus|basil|plum|berry|sky|пастел/i, 'pastel'],
  [/ink|text|fg|foreground/i, 'ink'], [/bg|background|фон|page/i, 'bg']];
const RADII = { xs: 6, sm: 12, md: 16, lg: 20, xl: 28 };
const SCALE = [13, 14, 16, 18, 24, 32];
const rgb = h => ({ r: parseInt(h.slice(1, 3), 16) / 255, g: parseInt(h.slice(3, 5), 16) / 255, b: parseInt(h.slice(5, 7), 16) / 255 });
const out = { colors: [], floats: [], unmatched: [], textStyles: [], components: [] };
for (const col of await figma.variables.getLocalVariableCollectionsAsync()) {
  for (const id of col.variableIds) {
    const v = await figma.variables.getVariableByIdAsync(id);
    for (const m of col.modes) {
      const cur = v.valuesByMode[m.modeId];
      if (cur && typeof cur === 'object' && cur.type === 'VARIABLE_ALIAS') continue;     // аліаси не чіпаємо
      if (v.resolvedType === 'COLOR') {
        const hit = MAP.find(([re]) => re.test(v.name));
        if (!hit) { out.unmatched.push(v.name); continue; }
        v.setValueForMode(m.modeId, rgb(C[hit[1]])); out.colors.push(v.name + '→' + hit[1]);
      } else if (v.resolvedType === 'FLOAT') {
        const r = v.name.match(/(?:radius|радіус|r)[-/ ]?(xs|sm|md|lg|xl)\b/i);
        if (r) { v.setValueForMode(m.modeId, RADII[r[1].toLowerCase()]); out.floats.push(v.name + '=' + RADII[r[1].toLowerCase()]); continue; }
        if (/font|text|шрифт|size/i.test(v.name) && typeof cur === 'number' && cur >= 9 && cur <= 60) {
          const s = SCALE.reduce((a, b) => Math.abs(b - cur) < Math.abs(a - cur) ? b : a);
          v.setValueForMode(m.modeId, s); out.floats.push(v.name + '=' + s);
        }
      }
    }
  }
}
const fonts = await figma.listAvailableFontsAsync();
const fam = fonts.some(f => f.fontName.family === 'SF Pro Text') ? 'SF Pro Text' : 'Inter';
const styles = new Set(fonts.filter(f => f.fontName.family === fam).map(f => f.fontName.style));
const pick = w => ['Bold', 'Semibold', 'Semi Bold', 'Medium', 'Regular'].find(s => styles.has(s) && (w >= 700 ? /Bold/.test(s) : w >= 600 ? /Semi/.test(s) : w >= 500 ? s === 'Medium' : s === 'Regular')) || 'Regular';
for (const s of await figma.getLocalTextStylesAsync()) {
  const w = /black|heavy|extra ?bold|bold/i.test(s.fontName.style) ? 700 : /semi/i.test(s.fontName.style) ? 600 : /medium/i.test(s.fontName.style) ? 500 : 400;
  const fn = { family: fam, style: pick(w) };
  await figma.loadFontAsync(fn);
  s.fontName = fn;
  s.fontSize = SCALE.reduce((a, b) => Math.abs(b - s.fontSize) < Math.abs(a - s.fontSize) ? b : a);
  out.textStyles.push(s.name + ': ' + fn.style + ' ' + s.fontSize);
}
const page = figma.root.children.find(p => p.name === 'Дизайн-система');
await figma.setCurrentPageAsync(page);
const WHITE = [{ type: 'SOLID', color: rgb(C.card) }];
for (const n of page.findAllWithCriteria({ types: ['COMPONENT'] })) {
  const name = (n.parent && n.parent.type === 'COMPONENT_SET' ? n.parent.name : n.name);
  if (/Картка|Рядок|Плитка/i.test(name) && n.fills.length && !n.boundVariables?.fills) { n.fills = WHITE; out.components.push(n.id + ' fill'); }
  if (/Кнопка|Чип|Вкладка/i.test(name) && n.height < 44 && n.layoutMode !== 'NONE') { n.minHeight = 44; out.components.push(n.id + ' min44'); }
}
out.font = fam; out.pageId = page.id;
return out;
