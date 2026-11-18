"""Двоколірні іконки сайту: темний контур + зсунута насичена пляма (CSS-змінна --ic). У шаблонах: {{ ic("pan", "big") }}."""
from markupsafe import Markup

ICONS = {
    "pan": '<circle cx="10" cy="13" r="7" class="f"/><circle cx="10" cy="13" r="7"/><path d="M17 13h5"/><path d="M8 10.5c1-.8 2.4-.8 3.4 0"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15" rx="4" class="f"/><rect x="3.5" y="5" width="17" height="15" rx="4"/><path d="M8 3v4M16 3v4M3.5 10h17"/><path d="M8 14h2M14 14h2M8 17h2"/>',
    "tag": '<path d="M3.5 12.2V5a1.5 1.5 0 0 1 1.5-1.5h7.2l8.3 8.3a1.5 1.5 0 0 1 0 2.1l-7.1 7.1a1.5 1.5 0 0 1-2.1 0z" class="f"/><path d="M3.5 12.2V5a1.5 1.5 0 0 1 1.5-1.5h7.2l8.3 8.3a1.5 1.5 0 0 1 0 2.1l-7.1 7.1a1.5 1.5 0 0 1-2.1 0z"/><circle cx="8.3" cy="8.3" r="1.5"/>',
    "user": '<circle cx="12" cy="8.5" r="4" class="f"/><circle cx="12" cy="8.5" r="4"/><path d="M4.5 20c1.2-3.6 4-5.5 7.5-5.5s6.3 1.9 7.5 5.5"/>',
    "family": '<circle cx="8.5" cy="8" r="3.2" class="f"/><circle cx="8.5" cy="8" r="3.2"/><circle cx="16.5" cy="10" r="2.5"/><path d="M3 19.5c.8-3.2 3-4.9 5.5-4.9s4.7 1.7 5.5 4.9M14.5 15.2c.6-.3 1.3-.4 2-.4 2 0 3.6 1.3 4.2 3.9"/>',
    "clock": '<circle cx="12" cy="13" r="8" class="f"/><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2M9.5 2.5h5"/>',
    "taste": '<path d="M12 20s-7-4.3-8.6-8.6C2.2 8.2 4.1 5 7.4 5c1.9 0 3.4 1 4.6 2.8C13.2 6 14.7 5 16.6 5c3.3 0 5.2 3.2 4 6.4C19 15.7 12 20 12 20z" class="f"/><path d="M12 20s-7-4.3-8.6-8.6C2.2 8.2 4.1 5 7.4 5c1.9 0 3.4 1 4.6 2.8C13.2 6 14.7 5 16.6 5c3.3 0 5.2 3.2 4 6.4C19 15.7 12 20 12 20z"/>',
    "book": '<path d="M5 4.5h11a3 3 0 0 1 3 3V20H8a3 3 0 0 1-3-3z" class="f"/><path d="M5 4.5h11a3 3 0 0 1 3 3V20H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h11M9 8.5h6"/>',
    "store": '<path d="M4 10v9.5h16V10" class="f"/><path d="M4 10v9.5h16V10"/><path d="M3 10l1.8-5.5h14.4L21 10c0 1.4-1.2 2.5-2.6 2.5S15.8 11.4 15.8 10c0 1.4-1.2 2.5-2.6 2.5h-.4C11.4 12.5 10.2 11.4 10.2 10c0 1.4-1.2 2.5-2.6 2.5S5 11.4 5 10"/><path d="M10 19.5v-4h4v4"/>',
    "plane": '<path d="M21 3.5L2.8 10.6l6.7 2.6 2.6 6.7z" class="f"/><path d="M21 3.5L2.8 10.6l6.7 2.6 2.6 6.7zM9.5 13.2L21 3.5"/>',
    "cart": '<path d="M6 6h15l-1.8 8.2a2 2 0 0 1-2 1.6H9.6a2 2 0 0 1-2-1.6z" class="f"/><path d="M3 3.5h2.2L7.6 14.2a2 2 0 0 0 2 1.6h7.6a2 2 0 0 0 2-1.6L21 6H5.8"/><circle cx="10" cy="20" r="1.4"/><circle cx="17" cy="20" r="1.4"/>',
    "brain": '<path d="M12 5.5C10.8 3.6 7 3.8 6.6 6.5 4 6.8 3.2 9.6 4.6 11.3 3 13 4 16 6.4 16.2 6.9 19 10.6 19.5 12 17.5c1.4 2 5.1 1.5 5.6-1.3 2.4-.2 3.4-3.2 1.8-4.9 1.4-1.7.6-4.5-2-4.8C17 3.8 13.2 3.6 12 5.5z" class="f"/><path d="M12 5.5C10.8 3.6 7 3.8 6.6 6.5 4 6.8 3.2 9.6 4.6 11.3 3 13 4 16 6.4 16.2 6.9 19 10.6 19.5 12 17.5c1.4 2 5.1 1.5 5.6-1.3 2.4-.2 3.4-3.2 1.8-4.9 1.4-1.7.6-4.5-2-4.8C17 3.8 13.2 3.6 12 5.5zM12 5.5v12"/>',
    "chef": '<path d="M7 13.5V19h10v-5.5" class="f"/><path d="M7 13.5C4.5 13.3 3 11.4 3.4 9.3 3.8 7 6.2 5.9 8.2 6.7 8.9 4.6 10.3 3.5 12 3.5s3.1 1.1 3.8 3.2c2-.8 4.4.3 4.8 2.6.4 2.1-1.1 4-3.6 4.2V19H7z"/><path d="M7 16.2h10"/>',
    "fridge": '<rect x="5.5" y="2.8" width="13" height="18.4" rx="3" class="f"/><rect x="5.5" y="2.8" width="13" height="18.4" rx="3"/><path d="M5.5 9.5h13M9 5.6v1.6M9 12.2v2.6"/>',
    "leaf": '<path d="M5 19C4 11 9 5 19.5 4.5 20 15 14 20 6 19z" class="f"/><path d="M5 19C4 11 9 5 19.5 4.5 20 15 14 20 6 19zM5 19c3-4.5 6-7.5 10-10"/>',
    "out": '<path d="M14 4.5H7.5a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2H14"/><path d="M11 12h9.5M17 8.5l3.5 3.5-3.5 3.5"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "camera": '<rect x="3" y="7" width="18" height="13" rx="3.5" class="f"/><path d="M3 10.5A3.5 3.5 0 0 1 6.5 7h1.8l1.5-2.5h4.4L15.7 7h1.8A3.5 3.5 0 0 1 21 10.5v6A3.5 3.5 0 0 1 17.5 20h-11A3.5 3.5 0 0 1 3 16.5z"/><circle cx="12" cy="13.3" r="3.4"/>',
    "image": '<rect x="3.5" y="4.5" width="17" height="15" rx="3.5" class="f"/><rect x="3.5" y="4.5" width="17" height="15" rx="3.5"/><circle cx="9" cy="9.5" r="1.6"/><path d="M4 17l4.8-4.8a1.5 1.5 0 0 1 2.1 0L16 17.3M14 15.2l1.7-1.7a1.5 1.5 0 0 1 2.1 0l2.5 2.5"/>',
    "bell": '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z" class="f"/><path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15zM10 21h4"/>',
    "bolt": '<path d="M13.5 2.5L5 13.5h6l-1 8 8.5-11h-6z" class="f"/><path d="M13.5 2.5L5 13.5h6l-1 8 8.5-11h-6z"/>',
    "wallet": '<rect x="3" y="6.5" width="18" height="13.5" rx="3.5" class="f"/><path d="M17 6.5V5a1.5 1.5 0 0 0-1.9-1.4L5.6 6.2A3.5 3.5 0 0 0 3 9.6v6.9A3.5 3.5 0 0 0 6.5 20h11a3.5 3.5 0 0 0 3.5-3.5v-6.5A3.5 3.5 0 0 0 17.5 6.5H6"/><path d="M16.5 13.5h.01"/>',
    "sun": '<circle cx="12" cy="12" r="4.5" class="f"/><circle cx="12" cy="12" r="4.5"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    # кухонні прилади
    "stove": '<rect x="3" y="5" width="18" height="14" rx="3" class="f"/><rect x="3" y="5" width="18" height="14" rx="3"/><circle cx="8.5" cy="10.5" r="2.4"/><circle cx="15.5" cy="10.5" r="2.4"/><path d="M7 16h.01M12 16h.01M17 16h.01"/>',
    "oven": '<rect x="7" y="11" width="10" height="6" rx="1.5" class="f"/><rect x="3.5" y="3.5" width="17" height="17" rx="3"/><path d="M3.5 8.5h17M7.5 6h.01M11 6h.01"/><rect x="7" y="11" width="10" height="6" rx="1.5"/>',
    "multicooker": '<path d="M4 11h16v5a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z" class="f"/><path d="M4 11h16v5a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4zM4 11c0-2.8 3.6-5 8-5s8 2.2 8 5M10.5 3.5h3M12 3.5V6M10 15h4"/>',
    "airfryer": '<rect x="5" y="3" width="14" height="18" rx="5" class="f"/><rect x="5" y="3" width="14" height="18" rx="5"/><path d="M5 13.5h14M10 17h4"/><circle cx="12" cy="8.2" r="2.2"/>',
    "microwave": '<rect x="2.5" y="5" width="19" height="14" rx="3" class="f"/><rect x="2.5" y="5" width="19" height="14" rx="3"/><rect x="5.5" y="8" width="9" height="8" rx="1.5"/><path d="M18 9h.01M18 12h.01M17 16h2"/>',
    "blender": '<path d="M7 3.5h10l-1.6 10H8.6z" class="f"/><path d="M7 3.5h10l-1.6 10H8.6zM10 9.5l2 1.5 2-1.5"/><rect x="6.5" y="15" width="11" height="6" rx="2"/><path d="M12 13.5V15M11 18h2"/>',
    "lock": '<rect x="5" y="10.5" width="14" height="10" rx="3" class="f"/><rect x="5" y="10.5" width="14" height="10" rx="3"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
}


def ic(name: str, cls: str = "") -> Markup:
    return Markup(f'<svg class="ico {cls}" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>')
