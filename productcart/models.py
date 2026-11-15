"""Спільна модель даних: акційна пропозиція мережі і профіль покупця."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Offer:
    """Одна акційна пропозиція в одній мережі (і, якщо відомо, в конкретному магазині)."""
    chain: str                      # "silpo", "varus", ... (ключ мережі)
    product_id: str
    name: str
    price: float                    # ціна зараз, ₴ за одиницю продажу (упаковку чи кг для вагового)
    old_price: float | None = None  # ціна без акції
    brand: str | None = None
    category: str | None = None
    pack_qty: float | None = None   # 900 для «900 г»
    pack_unit: str | None = None    # "g" | "ml" | "pcs"
    weighted: bool = False          # ваговий товар: ціна за кг
    store_id: str | None = None
    store_name: str | None = None
    valid_to: str | None = None     # YYYY-MM-DD, якщо мережа віддає
    promo: str | None = None        # назва акції: «Цінотижики», «2 за ціною 1»…
    url: str | None = None
    ean: str | None = None
    image: str | None = None
    source: str = ""                # звідки дані: "silpo-mcp", "feed:varus", "demo"
    also_in: int = 0                # у скількох ще магазинах поруч є ця ж акція (заповнює merge_nearby)
    tags: set[str] = field(default_factory=set)  # заповнює catalog.enrich()

    @property
    def discount_pct(self) -> float:
        if not self.old_price or self.old_price <= self.price:
            return 0.0
        return round((1 - self.price / self.old_price) * 100, 1)

    @property
    def saving(self) -> float:
        return round(max(0.0, (self.old_price or self.price) - self.price), 2)

    def unit_price(self) -> tuple[float, str] | None:
        """Ціна за кг / л / шт — щоб порівнювати різні упаковки."""
        if self.weighted:
            return self.price, "кг"
        if not self.pack_qty or not self.pack_unit:
            return None
        if self.pack_unit == "g":
            return round(self.price / self.pack_qty * 1000, 2), "кг"
        if self.pack_unit == "ml":
            return round(self.price / self.pack_qty * 1000, 2), "л"
        return round(self.price / self.pack_qty, 2), "шт"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tags"] = sorted(self.tags)
        d["discount_pct"] = self.discount_pct
        return d


# Обмеження в харчуванні → теги товару, які вони виключають (див. catalog.TAG_RULES).
DIETS = {
    "no_gluten": {"gluten"},
    "no_lactose": {"lactose"},
    "vegetarian": {"meat", "fish"},
    "vegan": {"meat", "fish", "dairy", "eggs", "honey"},
    "no_pork": {"pork"},
    "no_alcohol": {"alcohol"},
    "no_sugar": {"sweets"},
}
DIET_LABELS = {
    "no_gluten": "без глютену", "no_lactose": "без лактози", "vegetarian": "вегетаріанське",
    "vegan": "веганське", "no_pork": "без свинини", "no_alcohol": "без алкоголю", "no_sugar": "без солодкого",
}


@dataclass
class Profile:
    """Вподобання покупця. Заповнює сам покупець (анкета), частину можна підтягнути з MCP Сільпо."""
    name: str = "Мій профіль"
    household: int = 1
    diets: list[str] = field(default_factory=list)          # ключі з DIETS
    likes: list[str] = field(default_factory=list)          # теги або категорії: "dairy", "fish", "coffee"…
    dislikes: list[str] = field(default_factory=list)       # теги, які не показувати взагалі
    regulars: list[str] = field(default_factory=list)       # що купую щотижня: «молоко», «яйця», «кава зернова»
    brands_like: list[str] = field(default_factory=list)
    brands_avoid: list[str] = field(default_factory=list)
    chains: list[str] = field(default_factory=list)         # порожньо = усі мережі
    stores: dict[str, str] = field(default_factory=dict)    # мережа → id магазину поруч
    city: str | None = None
    budget_week: float | None = None                        # ₴ на тиждень для списку
    min_discount: float = 10.0                              # %, слабші знижки не показуємо
    max_items: int = 20
    # Місце для списку «магазини поруч»: округлено до ~100 м, зберігається лише за згодою.
    lat: float | None = None
    lon: float | None = None
    radius_km: float = 1.0
    location_at: str | None = None
    # Меню вечерь з акцій
    dinners: int = 5
    max_minutes: int = 60
    menu_budget: float | None = None
    menu: list[str] = field(default_factory=list)          # поточне меню (id рецептів) після замін
    menu_skip: list[str] = field(default_factory=list)     # нещодавно замінені страви — не пропонувати знову одразу
    pantry_buy: list[str] = field(default_factory=list)    # запаси, які вдома закінчились — додати в кошик
    dish_likes: list[str] = field(default_factory=list)    # id рецептів, які людина позначила «смачно»
    dish_dislikes: list[str] = field(default_factory=list) # «не моє» — у меню не пропонуємо

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        p = cls(**known)
        unknown = sorted(set(p.diets) - set(DIETS))
        if unknown:
            raise ValueError(f"Невідомі обмеження в харчуванні: {', '.join(unknown)}. Доступні: {', '.join(DIETS)}")
        return p

    def to_dict(self) -> dict:
        return asdict(self)

    def excluded_tags(self) -> set[str]:
        out = set(self.dislikes)
        for d in self.diets:
            out |= DIETS[d]
        return out
