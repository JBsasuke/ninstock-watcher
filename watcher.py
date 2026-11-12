#!/usr/bin/env python3
"""
Vigilante de NinStock.

Revisa si los productos de products.json están disponibles para comprar y,
cuando uno pasa a "disponible", manda una notificación al teléfono
(Firebase -> app NinStock, y opcionalmente ntfy).

Se ejecuta solo en GitHub Actions cada 5 minutos (ver .github/workflows/vigilante.yml).
Prueba local:  python watcher.py            (revisa todo)
               python watcher.py --test     (manda una notificación de prueba)
"""
import datetime
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
FEED_FILE = BASE / "feed.json"   # lo que lee la app (ofertas, vigilados, noticias)
FCM_TOPIC = "ninstock-alerts"

AVAILABLE, SOLD_OUT, UNKNOWN = "disponible", "agotado", "desconocido"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Textos que indican si hay o no hay existencia, por tienda.
# Cada producto puede cambiarlos con "in_stock_text" / "out_of_stock_text".
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


# ------------------------------------------------------------------ revisiones

def check_bestbuy(p):
    """Usa la API oficial de Best Buy (fiable desde GitHub)."""
    key = os.environ.get("BESTBUY_API_KEY", "").strip()
    if not key:
        return UNKNOWN, p.get("url"), "falta BESTBUY_API_KEY"
    show = "sku,name,salePrice,onlineAvailability,orderable,url"
    if p.get("sku"):
        url = f"https://api.bestbuy.com/v1/products/{p['sku']}.json?show={show}&apiKey={quote(key)}"
    else:
        terms = "&".join("search=" + quote(w) for w in p["search"].split())
        url = (f"https://api.bestbuy.com/v1/products({terms})?format=json&pageSize=5"
               f"&show={show}&apiKey={quote(key)}")
    r = requests.get(url, timeout=25)
    if r.status_code in (401, 403):
        return UNKNOWN, p.get("url"), f"Best Buy rechazó la clave o el límite (HTTP {r.status_code})"
    if r.status_code == 404:
        return UNKNOWN, p.get("url"), "SKU no encontrado"
    r.raise_for_status()
    data = r.json()
    items = data.get("products", []) if "products" in data else [data]
    if not items:
        return UNKNOWN, p.get("url"), "Best Buy todavía no lo tiene en su catálogo"
    ok = [i for i in items if i.get("onlineAvailability") or i.get("orderable") in ("Available", "PreOrder")]
    best = ok[0] if ok else items[0]
    detail = f"{best.get('name')} · ${best.get('salePrice')} · {best.get('orderable')}"
    return (AVAILABLE if ok else SOLD_OUT), best.get("url") or p.get("url"), detail


def check_page(p):
    """Abre la página del producto y busca señales de existencia."""
    store = p["store"].lower()
    try:
        r = requests.get(p["url"], headers=HEADERS, timeout=25)
    except requests.RequestException as e:
        return UNKNOWN, p["url"], f"sin conexión ({e.__class__.__name__})"
    if r.status_code in (403, 429, 503):
        return UNKNOWN, p["url"], f"la tienda bloqueó la consulta (HTTP {r.status_code})"
    if r.status_code == 404:
        return UNKNOWN, p["url"], "la página no existe (revisa el link)"
    if r.status_code >= 400:
        return UNKNOWN, p["url"], f"HTTP {r.status_code}"
    text = r.text.lower()
    if any(h in text for h in BLOCK_HINTS):
        return UNKNOWN, p["url"], "la tienda pidió captcha"

    # 1) Datos estructurados (schema.org) que casi todas las tiendas incluyen
    found = {m.lower() for m in SCHEMA_RE.findall(r.text)}
    if found & {"instock", "preorder", "limitedavailability"}:
        return AVAILABLE, p["url"], "schema.org: " + ", ".join(sorted(found))
    if found & {"outofstock", "soldout", "discontinued", "backorder"}:
        return SOLD_OUT, p["url"], "schema.org: " + ", ".join(sorted(found))

    # 2) Textos visibles en la página
    rules = STORE_TEXT.get(store, DEFAULT_TEXT)
    outs = [s.lower() for s in p.get("out_of_stock_text", rules["out"])]
    ins = [s.lower() for s in p.get("in_stock_text", rules["in"])]
    hit = next((o for o in outs if o in text), None)
    if hit:
        return SOLD_OUT, p["url"], f'dice "{hit}"'
    hit = next((i for i in ins if i in text), None)
    if hit:
        return AVAILABLE, p["url"], f'dice "{hit}"'
    return UNKNOWN, p["url"], "no se encontró ninguna señal en la página"


def check(p):
    try:
        if p["store"].lower() == "bestbuy" and (p.get("search") or p.get("sku")):
            return check_bestbuy(p)
        return check_page(p)
    except Exception as e:  # nunca romper toda la corrida por un producto
        return UNKNOWN, p.get("url"), f"error: {e}"


# ------------------------------------------------------------------ avisos

def send_fcm(title, body, url):
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
        "message": {
            "topic": FCM_TOPIC,
            "data": {"title": title, "body": body, "url": url or ""},
            "android": {"priority": "HIGH", "ttl": "3600s"},
        }
    }
    r = requests.post(
        f"https://fcm.googleapis.com/v1/projects/{info['project_id']}/messages:send",
        headers={"Authorization": f"Bearer {creds.token}"}, json=message, timeout=20)
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
    r = requests.post("https://ntfy.sh/", json=payload, timeout=20)
    return r.status_code < 300


def notify(title, body, url):
    sent = []
    for name, fn in (("Firebase", send_fcm), ("ntfy", send_ntfy)):
        try:
            if fn(title, body, url):
                sent.append(name)
        except Exception as e:
            log(f"  error enviando por {name}: {e}")
    log(f"  aviso enviado por: {', '.join(sent) or 'ninguno (configura los secretos)'}")


# ------------------------------------------------------------------ principal

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    if "--test" in sys.argv:
        notify("NinStock: prueba", "Si ves esto, las alertas funcionan.", "https://www.nintendo.com/us/store/")
        return

    products = [p for p in load_json(PRODUCTS_FILE, []) if p.get("enabled", True)]
    state = load_json(STATE_FILE, {})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    changed = False

    for p in products:
        pid = p["id"]
        status, link, detail = check(p)
        prev = state.get(pid, {}).get("status")
        log(f"{p['name']} [{p['store']}]: {status} — {detail}")

        if status == UNKNOWN:
            continue  # no cambiamos nada si no pudimos saber
        if status == AVAILABLE and prev != AVAILABLE:
            notify(f"¡Disponible! {p['name']}",
                   f"Ya se puede comprar en {p.get('store_name', p['store'].title())}. Toca para abrir la tienda.",
                   link)
        if status != prev:
            state[pid] = {"status": status, "since": now}
            changed = True
        time.sleep(2)  # no abusar de las tiendas

    if changed:
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log("estado actualizado")

    write_feed(products, state)


def write_feed(products, state):
    """Escribe feed.json solo si su contenido cambió (para no hacer commits de más)."""
    old = load_json(FEED_FILE, {})
    deals, _ = feeds.fetch_deals(old.get("deals", []), log)

    # Aviso de ediciones de coleccionista NUEVAS (no en la primera corrida, para no llenar el teléfono)
    seen = {d["id"] for d in old.get("deals", [])}
    if seen:
        new_collectors = [d for d in deals if d.get("collector") and d["id"] not in seen]
        for d in new_collectors[:3]:
            price = f" · ${d['price']:.2f}" if d.get("price") else ""
            notify("Nueva edición de coleccionista", f"{d['title']}{price} ({d['store']})", d["url"])
    feed = {
        "version": 1,
        "deals": deals,
        "watch": [
            {
                "id": p["id"],
                "name": p["name"],
                "store": p.get("store_name", p["store"].title()),
                "url": p.get("url") or "",
                "status": state.get(p["id"], {}).get("status", UNKNOWN),
                "since": state.get(p["id"], {}).get("since"),
            }
            for p in products
        ],
        "news": feeds.fetch_news(old.get("news", []), log),
    }
    if {k: v for k, v in old.items() if k != "updated"} == feed:
        log("feed.json sin cambios")
        return
    feed["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    FEED_FILE.write_text(json.dumps(feed, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    log("feed.json actualizado")


if __name__ == "__main__":
    main()
