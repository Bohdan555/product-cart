# Логи

Кожен рядок — JSON (`productcart/web/logs.py`). Рівень задає `LOG_LEVEL` (за замовчуванням `INFO`).

## Поля

| поле | що це |
|---|---|
| `t`, `lvl`, `log`, `msg` | час (UTC), рівень, модуль, подія |
| `rid` | id запиту — той самий, що в заголовку відповіді `X-Request-ID` і на сторінці помилки |
| `uid` | id користувача (число, не email) |
| `exc` | траса помилки |

## Події

| `msg` | коли | рівень |
|---|---|---|
| `req` | кожен запит, крім `/static/`: `method`, `path`, `st`, `ms`, `db`, `db_ms`, `silpo`, `silpo_ms` | INFO; повільніше 800 мс — WARNING; 5xx — ERROR |
| `silpo.call` | виклик інструмента «Сільпо»: помилка або довше 3 с | WARNING (решта — DEBUG) |
| `ev.*` | доменні події з аналітики: `ev.signup`, `ev.login`, `ev.silpo_connected`, `ev.silpo_cart_built`, `ev.tg_linked`… | INFO (`*_viewed` — DEBUG) |
| `auth.login_failed`, `auth.google_failed` | невдалий вхід | WARNING |
| `silpo.connect_failed`, `silpo.refresh_failed` | не вдалося підключити «Сільпо» або оновити токен | WARNING |
| `tg.cron` | підсумок щоденної розсилки: `users`, `deals`, `menus`, `errors` | INFO / WARNING |
| `Помилка: …` | непередбачений виняток; людина бачить сторінку з кодом запиту | ERROR |

## Чого в логах немає

Токени («Сільпо», Google, Telegram), паролі, cookies, email, координати, адреси, вміст кошиків та історія покупок.
Перед виведенням кожен рядок ще й проходить маскування (`logs.redact`). Сирі відповіді «Сільпо» пишуться лише на рівні
`DEBUG`, який на хостингу вимкнений. `httpx`/`mcp` приглушені до WARNING: на INFO вони пишуть повні URL.

## Як читати

```bash
vercel logs --since 1h
```

Знайти запит, про який повідомив користувач: код зі сторінки помилки = `rid`.
