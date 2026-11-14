# Product Cart

Персональний список покупок з акцій мереж. Людина один раз заповнює профіль — сім'я, дієта й алергії, що купує щотижня, улюблені й небажані бренди, мережі, бюджет — і щотижня отримує готовий список акційних товарів по магазинах з поясненням «чому вам». Доставку додамо пізніше.

## Як працює
1. **Джерела** (`productcart/sources/`) приносять акції в спільну модель `Offer`: MCP «Сільпо», фіди мереж, демо.
2. **Каталог** (`catalog.py`) розбирає упаковку й теги з назви: молочне, лактоза, глютен, м'ясо, риба, алкоголь…
3. **Підбір** (`matcher.py`) відсіює те, що не можна (дієта, «не показувати», бренди, слабкі знижки), і рахує бал: «купую щотижня», «подобається», улюблений бренд, глибина знижки.
4. **Список** (`planner.py`) закриває пункти «купую щотижня», не повторює однакові товари, тримає бюджет і групує по магазинах.

Числа і бали рахує код, без LLM, тому результат відтворюваний і пояснюваний.

## Швидкий старт
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[silpo,dev]'
.venv/bin/python -m productcart list --source demo            # демо-акції, профіль profiles/example.json
.venv/bin/python -m productcart profile profiles/me.json      # анкета свого профілю
.venv/bin/python -m pytest -q
```

## «Сільпо» (офіційний MCP)
```bash
.venv/bin/python -m productcart silpo login                   # вхід у свій акаунт «Сільпо» в браузері
.venv/bin/python -m productcart silpo stores --city Київ --q Франка
.venv/bin/python -m productcart silpo offers --store <id>     # акції магазину → out/silpo-offers.json
.venv/bin/python -m productcart silpo hints                   # підказки для профілю з «Сільпо»
.venv/bin/python -m productcart list --source silpo --profile profiles/me.json
```
Токени зберігаються в `~/.config/product-cart/silpo.json` (права 0600) і в репозиторій не потрапляють. Якщо браузер на іншій машині, задайте `PRODUCTCART_PASTE=1` і вставте адресу після входу вручну.

Джерела й правила — у `docs/data-sources.md`, формат фіду для мереж — у `docs/feed-format.md`.
