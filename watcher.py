#!/usr/bin/env python3
"""
Vigilante de NinStock.

Cada 5 minutos (GitHub Actions):
  1. Revisa los productos de coleccionista de products.json en cada tienda.
     Cuando uno pasa a "disponible" avisa a:
       - todos los que tienen los avisos generales activados (tema ninstock-alerts)
       - los que pusieron ese producto en su lista (tema p-<id del producto>)
  2. Publica feed.json (ofertas, colección y noticias) que lee la app.

Prueba local:  python watcher.py            (revisa todo)
               python watcher.py --test     (manda una notificación de prueba)
"""
import datetime
import html
import json
import os
import pathlib
import re
import sys
import time
from urllib.parse import quote

import requests

import feeds

BASE = pathlib.Path(__file__).resolve().parent
PRODUCTS_FILE = BASE / "products.json"
STATE_FILE = BASE / "state.json"
FEED_FILE = BASE / "feed.json"
GENERAL_TOPIC = "ninstock-alerts"   # coleccionistas: disponibles y ediciones nuevas
DEALS_TOPIC = "ninstock-deals"      # ofertas buenas (30 % o más)
GOOD_DEAL_PCT = 30
DROP_PCT = 10                       # bajada mínima para avisar de un juego guardado
TZ = "America/New_York"

AVAILABLE, SOLD_OUT, UNKNOWN = "disponible", "agotado", "desconocido"
STORE_NAMES = {"nintendo": "Nintendo", "amazon": "Amazon", "walmart": "Walmart",
               "target": "Target", "bestbuy": "Best Buy", "gamestop": "GameStop"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
STORE_TEXT = {
    "nintendo": {"in": ["add to cart"], "out": ["out of stock", "sold out", "currently unavailable", "not available"]},
    "gamestop": {"in": ["add to cart", "pre-order now"], "out": ["currently unavailable", "not available", "sold out"]},
    "walmart": {"in": ["add to cart"], "out": ["out of stock", "get in-stock alert"]},
    "target": {"in": ["add to cart", "ship it"], "out": ["out of stock", "sold out"]},
    "amazon": {"in": ["add to cart", "buy now"], "out": ["currently unavailable", "temporarily out of stock"]},
    "bestbuy": {"in": ["add to cart"], "out": ["sold out", "coming soon"]},
}
DEFAULT_TEXT = {"in": ["add to cart"], "out": ["out of stock", "sold out", "unavailable"]}
BLOCK_HINTS = ["captcha", "robot check", "are you a human", "verify you are human", "access denied", "px-captcha"]
SCHEMA_RE = re.compile(r"schema\.org/(InStock|OutOfStock|PreOrder|SoldOut|BackOrder|Discontinued|LimitedAvailability)", re.I)


def log(msg):
    print(f"[{datetime.datetime.now(datetime.timezone.utc):%H:%M:%S}] {msg}", flush=True)


def store_name(code):
    return STORE_NAMES.get(code.lower(), code.title())


# ------------------------------------------------------------------ revisiones

def check_bestbuy_api(st):
    key = os.environ.get("BESTBUY_API_KEY", "").strip()
    show = "sku,name,salePrice,onlineAvailability,orderable,url,image"
    url = f"https://api.bestbuy.com/v1/products/{st['sku']}.json?show={show}&apiKey={quote(key)}"
    r = requests.get(url, timeout=25)
    if r.status_code in (401, 403):
        return UNKNOWN, st.get("url"), f"Best Buy rechazó la clave (HTTP {r.status_code})", None
    r.raise_for_status()
    d = r.json()
    ok = d.get("onlineAvailability") or d.get("orderable") in ("Available", "PreOrder")
    return (AVAILABLE if ok else SOLD_OUT), st.get("url") or d.get("url"), f"API: {d.get('orderable')}", d.get("image")


def check_page(st):
    code = st["store"].lower()
    url = st["url"]
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
    except requests.RequestException as e:
        return UNKNOWN, url, f"sin conexión ({e.__class__.__name__})", None
    if r.status_code in (403, 429, 503):
        return UNKNOWN, url, f"la tienda bloqueó la consulta (HTTP {r.status_code})", None
    if r.status_code >= 400:
        return UNKNOWN, url, f"HTTP {r.status_code} (revisa el link)", None
    text = r.text.lower()
    if any(h in text for h in BLOCK_HINTS):
        return UNKNOWN, url, "la tienda pidió captcha", None

    m = feeds.OG_IMAGE_RE.search(r.text)
    image = html.unescape(m.group(1) or m.group(2)) if m else None

    found = {s.lower() for s in SCHEMA_RE.findall(r.text)}
    if found & {"instock", "preorder", "limitedavailability"}:
        return AVAILABLE, url, "schema.org: " + ", ".join(sorted(found)), image
    if found & {"outofstock", "soldout", "discontinued", "backorder"}:
        return SOLD_OUT, url, "schema.org: " + ", ".join(sorted(found)), image

    rules = STORE_TEXT.get(code, DEFAULT_TEXT)
    outs = [s.lower() for s in st.get("out_of_stock_text", rules["out"])]
    ins = [s.lower() for s in st.get("in_stock_text", rules["in"])]
    hit = next((o for o in outs if o in text), None)
    if hit:
        return SOLD_OUT, url, f'dice "{hit}"', image
    hit = next((i for i in ins if i in text), None)
    if hit:
        return AVAILABLE, url, f'dice "{hit}"', image
    return UNKNOWN, url, "no se encontró ninguna señal en la página", image


def check(st):
    try:
        if st["store"].lower() == "bestbuy" and st.get("sku") and os.environ.get("BESTBUY_API_KEY"):
            return check_bestbuy_api(st)
        return check_page(st)
    except Exception as e:
        return UNKNOWN, st.get("url"), f"error: {e}", None


# ------------------------------------------------------------------ avisos

def send_fcm(title, body, url, topics):
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not raw:
        return False
    from google.oauth2 import service_account
    import google.auth.transport.requests

    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
    creds.refresh(google.auth.transport.requests.Request())
    message = {
        "data": {"title": title, "body": body, "url": url or ""},
        "android": {"priority": "HIGH", "ttl": "3600s"},
    }
    if len(topics) == 1:
        message["topic"] = topics[0]
    else:  # un solo aviso por teléfono aunque esté en varios temas
        message["condition"] = " || ".join(f"'{t}' in topics" for t in topics[:5])
    r = requests.post(
        f"https://fcm.googleapis.com/v1/projects/{info['project_id']}/messages:send",
        headers={"Authorization": f"Bearer {creds.token}"}, json={"message": message}, timeout=20)
    if r.status_code >= 300:
        log(f"  Firebase respondió {r.status_code}: {r.text[:300]}")
        return False
    return True


def send_ntfy(title, body, url):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    payload = {"topic": topic, "title": title, "message": body, "priority": 5, "tags": ["video_game"]}
    if url:
        payload["click"] = url
    return requests.post("https://ntfy.sh/", json=payload, timeout=20).status_code < 300


def notify(title, body, url, topics=(GENERAL_TOPIC,)):
    sent = []
    try:
        if send_fcm(title, body, url, list(topics)):
            sent.append("Firebase")
    except Exception as e:
        log(f"  error enviando por Firebase: {e}")
    try:
        if send_ntfy(title, body, url):
            sent.append("ntfy")
    except Exception as e:
        log(f"  error enviando por ntfy: {e}")
    log(f"  aviso enviado por: {', '.join(sent) or 'ninguno (configura los secretos)'}")


# ------------------------------------------------------------------ principal

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def active_stores(prod):
    return [s for s in prod.get("stores", []) if s.get("enabled", True) and s.get("url", "").startswith("http")]


def main():
    if "--test" in sys.argv:
        notify("NinStock: prueba", "Si ves esto, las alertas funcionan.", "https://www.nintendo.com/us/store/")
        return

    catalog = [p for p in load_json(PRODUCTS_FILE, []) if p.get("enabled", True)]
    state = load_json(STATE_FILE, {})
    images = state.setdefault("_images", {})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    changed = False

    for prod in catalog:
        for st in active_stores(prod):
            key = f"{prod['id']}:{st['store'].lower()}"
            status, link, detail, image = check(st)
            log(f"{prod['name']} [{store_name(st['store'])}]: {status} — {detail}")
            if image and not prod.get("image") and prod["id"] not in images:
                images[prod["id"]] = image
                changed = True
            if status != UNKNOWN:
                prev = state.get(key, {}).get("status")
                if status == AVAILABLE and prev != AVAILABLE:
                    notify(f"¡Disponible! {prod['name']}",
                           f"Ya se puede comprar en {store_name(st['store'])}. Toca para abrir la tienda.",
                           link, topics=(GENERAL_TOPIC, f"p-{prod['id']}"))
                if status != prev:
                    state[key] = {"status": status, "since": now}
                    changed = True
            time.sleep(2)

    if write_feed(catalog, state):
        changed = True
    if changed:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log("estado actualizado")


def pct_off(price, old):
    return round((1 - price / old) * 100) if price and old and old > price else 0


def cheapest_text(c):
    offers = c.get("offers") or []
    if offers:
        return f"desde ${offers[0]['price']:.2f} en {offers[0]['store']}", offers[0]["url"]
    if c.get("price"):
        return f"desde ${c['price']:.2f}", c["url"]
    return "", c["url"]


def write_feed(catalog, state):
    """Escribe feed.json solo si cambió. Devuelve True si cambió el estado (precios/avisos)."""
    old = load_json(FEED_FILE, {})
    feeds.DEBUG_DIR = BASE / "debug"
    old_deals = old.get("deals", [])
    first_run = not old_deals
    state_changed = False

    slick, _ = feeds.fetch_deals([d for d in old_deals if d.get("source") != "dekudeals"], log)
    cards, deku_checked = feeds.fetch_deku(old.get("releases", []), old.get("deku_checked"), log)
    deals = sorted(slick + feeds.deku_deals(cards), key=lambda d: d.get("posted") or "", reverse=True)

    # 1) Ofertas buenas nuevas (30 % o más) -> quien activó "buenas ofertas"
    seen = {d["id"] for d in old_deals}
    if not first_run:
        sent = 0
        for d in deals:
            pct = pct_off(d.get("price"), d.get("old_price"))
            if d["id"] in seen or pct < GOOD_DEAL_PCT or sent >= 3:
                continue
            notify(f"¡Buena oferta! -{pct}%",
                   f"{d['title']}: ${d['price']:.2f} (antes ${d['old_price']:.2f}) en {d['store']}",
                   d.get("store_url") or d["url"], topics=(DEALS_TOPIC,))
            sent += 1
        # 2) Ediciones de coleccionista nuevas (ofertas y anuncios) -> avisos generales
        old_release_ids = {c["id"] for c in old.get("releases", [])}
        new_collectors = [d for d in slick if d.get("collector") and d["id"] not in seen]
        new_collectors += [c for c in cards if c.get("collector") and old_release_ids and c["id"] not in old_release_ids]
        for d in new_collectors[:3]:
            extra = f" · sale {d['release']}" if d.get("release") else ""
            price = f" · ${d['price']:.2f}" if d.get("price") else ""
            notify("Nueva edición de coleccionista", f"{d['title']}{price}{extra}",
                   d.get("store_url") or d["url"])

    # 3) Juegos guardados (tema r-<id>): bajada de precio y día de salida
    prices = state.setdefault("_prices", {})
    released = state.setdefault("_release_notified", [])
    try:
        from zoneinfo import ZoneInfo
        today = datetime.datetime.now(ZoneInfo(TZ)).date().isoformat()
    except Exception:
        today = datetime.date.today().isoformat()
    for c in cards:
        best = (c.get("offers") or [{}])[0].get("price") or c.get("price")
        prev = prices.get(c["id"])
        if best and prev and best <= prev * (1 - DROP_PCT / 100):
            text, link = cheapest_text(c)
            notify(f"Bajó de precio: {c['title']}",
                   f"De ${prev:.2f} a ${best:.2f} (-{pct_off(best, prev)}%) {text}".strip(),
                   link, topics=(f"r-{c['id']}",))
        if best and best != prev:
            prices[c["id"]] = best
            state_changed = True
        if c.get("release_date") == today and c["id"] not in released:
            text, link = cheapest_text(c)
            notify(f"¡Sale hoy! {c['title']}", f"Ya está a la venta {text}".strip(), link, topics=(f"r-{c['id']}",))
            released.append(c["id"])
            state_changed = True
    if len(released) > 300:
        del released[:-300]

    images = state.get("_images", {})
    feed = {
        "version": 3,
        "deku_checked": deku_checked,
        "deals": deals,
        "releases": cards,
        "collection": [
            {
                "id": p["id"],
                "name": p["name"],
                "kind": p.get("kind", ""),
                "image": p.get("image") or images.get(p["id"]),
                "stores": [
                    {
                        "store": store_name(s["store"]),
                        "url": s["url"],
                        "status": state.get(f"{p['id']}:{s['store'].lower()}", {}).get("status", UNKNOWN),
                        "since": state.get(f"{p['id']}:{s['store'].lower()}", {}).get("since"),
                    }
                    for s in active_stores(p)
                ],
            }
            for p in catalog
        ],
        "news": feeds.fetch_news(old.get("news", []), log),
    }
    if {k: v for k, v in old.items() if k != "updated"} == feed:
        log("feed.json sin cambios")
        return state_changed
    feed["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    FEED_FILE.write_text(json.dumps(feed, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    log("feed.json actualizado")
    return state_changed

if __name__ == "__main__":
    main()
