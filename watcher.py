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
HISTORY_DIR = BASE / "history"      # historial propio: un archivo por juego con el precio más bajo de cada día
HISTORY_DAYS = 400
GENERAL_TOPIC = "ninstock-alerts"   # coleccionistas: disponibles y ediciones nuevas
DEALS_TOPIC = "ninstock-deals"      # ofertas buenas (30 % o más)
GOOD_DEAL_PCT = 30
DROP_PCT = 10                       # bajada mínima para avisar de un juego guardado
TZ = "America/New_York"

AVAILABLE, SOLD_OUT, UNKNOWN = "disponible", "agotado", "desconocido"
STORE_NAMES = {"nintendo": "Nintendo", "amazon": "Amazon", "walmart": "Walmart",
               "target": "Target", "bestbuy": "Best Buy", "gamestop": "GameStop",
               "squareenix": "Square Enix Store", "capcom": "Capcom Store"}

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
    "squareenix": {"in": ["add to cart", "pre-order"], "out": ["sold out", "out of stock"]},
    "capcom": {"in": ["add to cart", "pre-order"], "out": ["sold out", "out of stock"]},
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
        r = requests.get(url, headers=HEADERS, timeout=(6, 15))
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

    # 1) Lo que se VE en la página manda (sin scripts: ahí suele haber datos de otros productos)
    visible = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", r.text, flags=re.S | re.I)
    visible = " ".join(re.sub(r"<[^>]+>", " ", visible).split()).lower()
    rules = STORE_TEXT.get(code, DEFAULT_TEXT)
    outs = [s.lower() for s in st.get("out_of_stock_text", rules["out"])]
    ins = [s.lower() for s in st.get("in_stock_text", rules["in"])]
    hit = next((o for o in outs if o in visible), None)
    if hit:
        return SOLD_OUT, url, f'la página dice "{hit}"', image
    hit = next((i for i in ins if i in visible), None)
    if hit:
        return AVAILABLE, url, f'la página dice "{hit}"', image

    # 2) Si la página se arma con JavaScript, usar los datos del producto principal (schema.org)
    found = [s.lower() for s in SCHEMA_RE.findall(r.text)]
    if found:
        main = found[0]   # el primero suele ser el producto de la página
        if main in ("outofstock", "soldout", "discontinued", "backorder") or any(
                f in ("outofstock", "soldout") for f in found):
            return SOLD_OUT, url, "schema.org: " + main, image
        if main in ("instock", "preorder", "limitedavailability"):
            return AVAILABLE, url, "schema.org: " + main, image
    hit = next((o for o in outs if o in text), None)
    if hit:
        return SOLD_OUT, url, f'dice "{hit}"', image
    return UNKNOWN, url, "no se encontró ninguna señal en la página", image


def check(st):
    try:
        if st["store"].lower() == "bestbuy" and st.get("sku") and os.environ.get("BESTBUY_API_KEY"):
            return check_bestbuy_api(st)
        return check_page(st)
    except Exception as e:
        return UNKNOWN, st.get("url"), f"error: {e}", None


# ------------------------------------------------------------------ avisos

def send_fcm(title, body, url, topics, urgent=False):
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
        "data": {"title": title, "body": body, "url": url or "", "urgent": "1" if urgent else "0"},
        "android": {"priority": "HIGH", "ttl": "600s" if urgent else "3600s"},
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


def notify(title, body, url, topics=(GENERAL_TOPIC,), urgent=False):
    """urgent=True: coleccionista disponible -> canal de máxima prioridad en el teléfono."""
    sent = []
    try:
        if send_fcm(title, body, url, list(topics), urgent):
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


def find_image(prod):
    """Foto del producto: "image" fija, o la og:image de "image_page" (p. ej. Deku Deals)."""
    if prod.get("image"):
        return prod["image"]
    for cand in prod.get("image_candidates", []):   # se comprueba que la foto existe
        try:
            r = requests.get(cand, headers=HEADERS, timeout=20, stream=True)
            ok = r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image")
            r.close()
            if ok:
                return cand
        except requests.RequestException:
            pass
    for page in prod.get("image_pages", []):
        try:
            r = requests.get(page, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                m = feeds.OG_IMAGE_RE.search(r.text)
                if m:
                    return html.unescape(m.group(1) or m.group(2))
        except requests.RequestException:
            pass
    return None


def active_stores(prod):
    return [s for s in prod.get("stores", []) if s.get("enabled", True) and s.get("url", "").startswith("http")]


RUN_START = time.monotonic()
EXTRA_PASSES_UNTIL_S = 210   # vueltas extra hasta ~3,5 min desde que arrancó la corrida
PASS_GAP_S = 45


def check_products(catalog, state, images):
    """Revisa cada tienda de los productos de Colección. Devuelve True si algo cambió."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    changed = False
    for prod in catalog:
        if not prod.get("image") and prod["id"] not in images and (prod.get("image_pages") or prod.get("image_candidates")):
            img = find_image(prod)
            if img:
                images[prod["id"]] = img
                changed = True
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
                           link, topics=(GENERAL_TOPIC, f"p-{prod['id']}"), urgent=True)
                if status != prev:
                    state[key] = {"status": status, "since": now}
                    changed = True
            time.sleep(2)
    return changed


def refresh_collection(catalog, state):
    """Después de una vuelta extra: poner en feed.json el estado nuevo de cada tienda de Colección."""
    feed = load_json(FEED_FILE, {})
    if not feed.get("collection"):
        return
    by_id = {p["id"]: p for p in catalog}
    for item in feed["collection"]:
        p = by_id.get(item["id"])
        if not p:
            continue
        names = {store_name(s["store"]): s["store"].lower() for s in active_stores(p)}
        for st in item.get("stores", []):
            key = names.get(st["store"])
            if key and f"{p['id']}:{key}" in state:
                st["status"] = state[f"{p['id']}:{key}"].get("status", st["status"])
                st["since"] = state[f"{p['id']}:{key}"].get("since", st.get("since"))
    feed["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    FEED_FILE.write_text(json.dumps(feed, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    if "--test" in sys.argv:
        notify("NinStock: prueba", "Si ves esto, las alertas funcionan.", "https://www.nintendo.com/us/store/")
        return

    catalog = [p for p in load_json(PRODUCTS_FILE, []) if p.get("enabled", True)]
    state = load_json(STATE_FILE, {})
    images = state.setdefault("_images", {})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    changed = False

    changed = check_products(catalog, state, images) or changed
    if write_feed(catalog, state):
        changed = True
    # Vueltas extra: GitHub solo arranca cada 5 min (a veces más), así que mientras dure la
    # corrida se vuelven a revisar los coleccionistas para avisar lo antes posible.
    passes = 0
    while time.monotonic() - RUN_START < EXTRA_PASSES_UNTIL_S and passes < 3:
        time.sleep(PASS_GAP_S)
        passes += 1
        log(f"vuelta extra {passes} a los coleccionistas")
        if check_products(catalog, state, images):
            changed = True
            refresh_collection(catalog, state)
    if changed:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log("estado actualizado")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def pct_off(price, old):
    return round((1 - price / old) * 100) if price and old and old > price else 0


def record_history(points, today):
    """Guarda el precio más bajo del día de cada juego en history/<id>.json."""
    HISTORY_DIR.mkdir(exist_ok=True)
    written = 0
    for hid, price in points:
        if not hid or not price:
            continue
        f = HISTORY_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '', hid)}.json"
        data = load_json(f, [])
        if data and data[-1][0] == today:
            if price >= data[-1][1]:
                continue
            data[-1][1] = price
        else:
            data.append([today, price])
        f.write_text(json.dumps(data[-HISTORY_DAYS:]), encoding="utf-8")
        written += 1
    if written:
        log(f"historial de precios: {written} juegos actualizados")


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
    first_run = not old_deals or old.get("version", 0) < 4   # cambió el formato: no avisar de todo otra vez
    state_changed = False

    raw_old = old.get("raw_slick") or [d for d in old_deals if d.get("source") not in ("dekudeals",) and "offers" not in d]
    slick, _ = feeds.fetch_deals(raw_old, log)
    cards, deku_checked, archive = feeds.fetch_deku(old.get("releases", []), old.get("deku_checked"), log,
                                                    old.get("deku_archive", []))
    deals = feeds.merge_deals(slick + feeds.deku_deals(cards + archive))
    log(f"ofertas en la app (sin repetidos): {len(deals)}")

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
        seen_raw = {d["id"] for d in raw_old}
        new_collectors = [d for d in slick if d.get("collector") and d["id"] not in seen_raw]
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

    points = [(d["id"], d.get("price")) for d in deals]
    points += [(c["id"], (c.get("offers") or [{}])[0].get("price") or c.get("price")) for c in cards + archive]
    record_history(points, today)

    # 5) Productos de Colección que se siguen por Deku Deals (todas las tiendas, sin links a mano)
    deku_stores = {}
    images = state.setdefault("_images", {})
    for p in catalog:
        rows = None
        if p.get("deku_page"):
            rows, img = feeds.fetch_deku_rows(p["deku_page"])
            if img and not p.get("image") and p["id"] not in images:
                images[p["id"]] = img
                state_changed = True
        elif p.get("match"):
            card = next((c for c in cards if re.search(p["match"], c["title"], re.I)), None)
            if card:
                rows = [{"store": o["store"], "price": o["price"], "url": o["url"]} for o in card.get("offers") or []]
                if card.get("image") and p["id"] not in images and not p.get("image"):
                    images[p["id"]] = card["image"]
                    state_changed = True
                if card.get("release") and not p.get("note"):
                    p["note"] = "Sale: " + card["release"]
        if rows is None:
            continue
        found = []
        for r in rows:
            key = f"{p['id']}:{r['store'].lower()}"
            status = AVAILABLE if r.get("price") else SOLD_OUT
            prev = state.get(key, {}).get("status")
            if status == AVAILABLE and prev != AVAILABLE and r.get("url"):
                notify(f"¡Disponible! {p['name']}",
                       f"Ya se puede comprar en {r['store']} por ${r['price']:.2f}. Toca para abrir la tienda.",
                       r["url"], topics=(GENERAL_TOPIC, f"p-{p['id']}"), urgent=True)
            if status != prev:
                state[key] = {"status": status, "since": now_iso()}
                state_changed = True
            found.append({"store": r["store"], "url": r.get("url") or "", "price": r.get("price"),
                          "status": status, "since": state.get(key, {}).get("since")})
        deku_stores[p["id"]] = found

    feed = {
        "version": 4,
        "raw_slick": slick,
        "deku_checked": deku_checked,
        "deals": deals,
        "releases": cards,
        "deku_archive": archive,
        "collection": [
            {
                "id": p["id"],
                "name": p["name"],
                "kind": p.get("kind", ""),
                "image": p.get("image") or images.get(p["id"]),
                "note": p.get("note"),
                "stores": deku_stores.get(p["id"], []) + [
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
