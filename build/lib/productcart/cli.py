"""CLI: python -m productcart <команда>

  list      скласти список з акцій під профіль
  profile   створити профіль (анкета) або показати його
  silpo     вхід у «Сільпо», пошук магазину, вивантаження акцій
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from . import matcher, planner, render
from .models import DIET_LABELS, DIETS, Offer, Profile
from .sources import demo, feed

OUT = Path("out")


def load_profile(path: str) -> Profile:
    return Profile.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def load_offers(spec: str) -> list[Offer]:
    """demo | feed:шлях.csv|json | silpo (з кешу out/silpo-offers.json або живий запит)"""
    if spec == "demo":
        return demo.load()
    if spec.startswith("feed:"):
        return feed.load(spec[5:])
    if spec == "silpo":
        cache = OUT / "silpo-offers.json"
        if not cache.exists():
            raise SystemExit("Немає вивантаження акцій «Сільпо». Спершу: productcart silpo offers --store <id>")
        return feed.load(cache)
    raise SystemExit(f"Невідоме джерело: {spec}")


def cmd_list(a):
    profile = load_profile(a.profile)
    offers = [o for spec in a.source for o in load_offers(spec)]
    matches, rejected = matcher.rank(offers, profile)
    sl = planner.build(matches, profile)
    if a.format == "json":
        print(render.to_json(sl))
    else:
        print(render.text(sl, markdown=a.format == "md"))
    if a.verbose:
        print(f"\nАкцій усього: {len(offers)}, підійшло: {len(matches)}, відсіяно: {len(rejected)}", file=sys.stderr)
        for r in rejected[:30]:
            print(f"  ✗ {r.offer.name}: {r.reason}", file=sys.stderr)


def _ask(prompt: str, default: str = "") -> str:
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def _split(v: str) -> list[str]:
    return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]


def cmd_profile(a):
    path = Path(a.path)
    if a.show:
        print(json.dumps(load_profile(a.path).to_dict(), ensure_ascii=False, indent=2))
        return
    print("Анкета профілю. Порожня відповідь = значення в дужках.")
    diets_help = ", ".join(f"{k} ({v})" for k, v in DIET_LABELS.items())
    p = Profile(
        name=_ask("Назва профілю", "Мій профіль"),
        household=int(_ask("Скільки людей у сім'ї", "1")),
        diets=[d for d in _split(_ask(f"Обмеження через кому — {diets_help}")) if d in DIETS],
        regulars=_split(_ask("Що купуєте щотижня (через кому)", "молоко, яйця, хліб")),
        likes=_split(_ask("Що любите (теги: dairy, fish, meat, coffee, tea, fruits, vegetables, sweets, grains)")),
        dislikes=_split(_ask("Чого не показувати (теги)", "alcohol")),
        brands_like=_split(_ask("Улюблені бренди")),
        brands_avoid=_split(_ask("Бренди, які не купуєте")),
        city=_ask("Місто", "Київ"),
        budget_week=float(b) if (b := _ask("Бюджет на тиждень, ₴ (порожньо — без обмеження)")) else None,
        min_discount=float(_ask("Мінімальна знижка, %", "10")),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(p.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Збережено: {path}")


def cmd_silpo(a):
    from .silpo_mcp import FileTokenStorage, silpo_session
    from .sources import silpo

    storage = FileTokenStorage()

    async def run():
        if a.action == "logout":
            storage.forget()
            print("Вихід виконано, токени видалено.")
            return
        async with silpo_session(storage, interactive=a.action == "login") as call:
            if a.action == "login":
                tools = await call.session.list_tools()  # type: ignore[attr-defined]
                print(f"Вхід виконано. Доступно інструментів: {len(tools.tools)}")
            elif a.action == "stores":
                for b in (await silpo.list_branches(call, a.city, a.q))[:50]:
                    print(f"{b['id']}\t{b['title']}")
            elif a.action == "hints":
                print(json.dumps(await silpo.profile_hints(call), ensure_ascii=False, indent=2))
            elif a.action == "offers":
                if not a.store:
                    raise SystemExit("Вкажіть магазин: --store <id> (див. productcart silpo stores --city Київ)")
                branches = await silpo.list_branches(call)
                branch = next((b for b in branches if b["id"] == a.store), {"id": a.store, "title": None})
                offers = await silpo.fetch_offers(call, branch)
                OUT.mkdir(exist_ok=True)
                path = OUT / "silpo-offers.json"
                path.write_text(json.dumps({"offers": [o.to_dict() for o in offers]}, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"Акцій: {len(offers)} → {path}")

    asyncio.run(run())


def main(argv=None):
    p = argparse.ArgumentParser(prog="productcart", description="Персональний список покупок з акцій мереж")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("list", help="скласти список під профіль")
    l.add_argument("--profile", default="profiles/example.json")
    l.add_argument("--source", action="append", default=None, help="demo | feed:шлях | silpo (можна кілька)")
    l.add_argument("--format", choices=["text", "md", "json"], default="text")

    pr = sub.add_parser("profile", help="анкета профілю")
    pr.add_argument("path", nargs="?", default="profiles/me.json")
    pr.add_argument("--show", action="store_true")

    s = sub.add_parser("silpo", help="MCP «Сільпо»")
    s.add_argument("action", choices=["login", "logout", "stores", "hints", "offers"])
    s.add_argument("--city")
    s.add_argument("--q", help="частина адреси")
    s.add_argument("--store", help="id магазину для offers")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if a.cmd == "list":
        a.source = a.source or ["demo"]
        cmd_list(a)
    elif a.cmd == "profile":
        cmd_profile(a)
    elif a.cmd == "silpo":
        cmd_silpo(a)


if __name__ == "__main__":
    main()
