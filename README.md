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

Деплой: Vercel — `docs/deploy-vercel.md`, Azure — `docs/deploy-azure.md`. Джерела й правила — у `docs/data-sources.md`, формат фіду для мереж — у `docs/feed-format.md`.

## Сайт
Реєстрація → профіль (анкета) → «Підключити Сільпо» (вхід на сторінці «Сільпо») → вибір магазину → «Мій список». Поки «Сільпо» не підключено, список будується з демо-акцій з чіткою позначкою.
```bash
.venv/bin/pip install -e '.[web,dev]'
APP_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))") \
DB_PATH=data/web.db BASE_URL=http://127.0.0.1:8000 \
.venv/bin/uvicorn productcart.web.app:app --port 8000
```
- `APP_SECRET` — ключ шифрування токенів «Сільпо» і обов'язковий. Якщо його змінити, усім доведеться підключити «Сільпо» знову.
- `BASE_URL` — публічна адреса сайту; з неї формується адреса повернення після входу в «Сільпо» (`/auth/silpo/callback`). Застосунок реєструється в «Сільпо» один раз для кожної адреси повернення і зберігає client_id у БД.
- Паролі — scrypt; сесії й CSRF — у БД; токени «Сільпо» — зашифровані (Fernet). Обмеження в харчуванні зберігаються лише з окремої згоди (дані про здоров'я).
