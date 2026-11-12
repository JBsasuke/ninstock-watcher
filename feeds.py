"""
Fuentes públicas que el vigilante junta en feed.json para que la app las lea
sin ninguna clave:
  - Ofertas de Nintendo Switch / Switch 2 de todas las tiendas (RSS de Slickdeals)
  - Noticias oficiales de Nintendo (canal de YouTube de Nintendo of America)
"""
import datetime
import email.utils
import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, unquote, urlparse

import requests

HEADERS = {"User-Agent": "NinStockWatcher/1.0 (+https://github.com)"}

DEAL_FEEDS = [
    "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
    "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1",
    "https://slickdeals.net/newsearch.php?searchin=first&forumchoice%5B%5D=9&rss=1",
    "https://slickdeals.net/newsearch.php?searchin=first&forumchoice%5B%5D=9&rss=1&q=nintendo+switch",
]
# Solo fuentes oficiales de Nintendo
NEWS_FEEDS = [
    ("Nintendo", "https://www.youtube.com/feeds/videos.xml?channel_id=UCGIY_O-8vW4rfX98KlMkvRg"),
]

KEEP_DAYS = 7
MAX_DEALS = 80
MAX_NEWS = 30

IS_SWITCH = re.compile(r"nintendo switch|switch\s*2\b|\(switch\)|\bfor switch\b|[-–]\s*switch\b|\bNSW\b", re.I)
IS_DIGITAL = re.compile(r"\beshop\b|\bdigital\b|\bdownload\b|gift card|switch online|\be-?code\b", re.I)
IS_AMIIBO = re.compile(r"\bamiibo\b", re.I)
IS_COLLECTOR = re.compile(r"collector'?s?|limited edition|special edition|anniversary edition|premium edition|"
                          r"steelbook|\bamiibo\b|signature edition|launch edition", re.I)
IS_HARDWARE = re.compile(r"console|bundle|controller|joy-?con|amiibo|\bcase\b|\bdock\b|headset|microsd|charging|grip", re.I)
STORES = ["Best Buy", "GameStop", "Walmart", "Target", "Amazon", "Nintendo", "Costco", "Sam's Club",
          "Woot", "eBay", "Kohl's", "Newegg", "Meijer", "Macy's", "BJ's", "Dell", "Lowe's", "Home Depot"]
PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")
IMG_RE = re.compile(r"(https?://[^\s\"'<>]+?\.(?:jpe?g|png|webp|gif|thumb)(?:\?[^\s\"'<>]*)?)", re.I)
IMG_TAG_RE = re.compile(r"<img[^>]+src=[\"'](https?://[^\"']+)[\"']", re.I)
MEDIA_RE = re.compile(r"(?:url|href)=[\"'](https?://[^\"']+)[\"'][^>]*(?:image|medium=[\"']image)", re.I)
OG_IMAGE_RE = re.compile(r"<meta[^>]+property=[\"']og:image[\"'][^>]+content=[\"'](https?://[^\"']+)[\"']"
                         r"|<meta[^>]+content=[\"'](https?://[^\"']+)[\"'][^>]+property=[\"']og:image[\"']", re.I)
MAX_PAGE_LOOKUPS = 15   # páginas de oferta que se abren por corrida (foto + link de la tienda)
RETAILER_RE = re.compile(r"https?://(?:www\.)?(walmart\.com|target\.com|amazon\.com|bestbuy\.com|gamestop\.com|"
                         r"nintendo\.com|costco\.com|samsclub\.com|woot\.com|ebay\.com|newegg\.com|meijer\.com|"
                         r"kohls\.com|macys\.com|bjs\.com)/[^\s\"'<>]+", re.I)
U2_RE = re.compile(r"[?&](?:amp;)?u2=([^&\"'\s<>]+)", re.I)
BAD_IMG = re.compile(r"avatar|logo|icon|pixel|spacer|emoji|badge|\.gif", re.I)


def _find_image(raw):
    """Busca la foto del producto dentro del item del RSS (img, media, o URL de imagen)."""
    text = html.unescape(raw)
    for rx in (IMG_TAG_RE, MEDIA_RE, IMG_RE):
        for url in rx.findall(text):
            if not BAD_IMG.search(url):
                return url
    return None


def _clean_store_url(u):
    """Quita parámetros de seguimiento comunes del link de la tienda."""
    try:
        parsed = urlparse(u)
        if "amazon." in parsed.netloc:
            m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", parsed.path)
            if m:
                return f"https://www.amazon.com/dp/{m.group(1)}"
        return u
    except Exception:
        return u


def _deal_page(url):
    """Abre la página de la oferta y saca la foto (og:image) y el link directo de la tienda."""
    image = store_url = None
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None, None
        page = r.text
        m = OG_IMAGE_RE.search(page)
        if m:
            img = html.unescape(m.group(1) or m.group(2))
            image = None if BAD_IMG.search(img) else img
        for raw in U2_RE.findall(page):
            cand = unquote(unquote(html.unescape(raw)))
            if RETAILER_RE.match(cand):
                store_url = _clean_store_url(cand)
                break
        if not store_url:
            m = RETAILER_RE.search(html.unescape(page))
            if m:
                store_url = _clean_store_url(m.group(0))
    except requests.RequestException:
        pass
    return image, store_url


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_date(text):
    try:
        return email.utils.parsedate_to_datetime(text).astimezone(datetime.timezone.utc)
    except Exception:
        return _now()


def _items(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    for item in root.iter("item"):
        raw = ET.tostring(item, encoding="unicode")
        yield {
            "title": html.unescape((item.findtext("title") or "").strip()),
            "link": (item.findtext("link") or "").strip(),
            "date": _parse_date(item.findtext("pubDate") or ""),
            "image": _find_image(raw),
        }


def _store(title):
    low = title.lower()
    found = [(low.rfind(s.lower()), s) for s in STORES if s.lower() in low]
    return max(found)[1] if found else "Slickdeals"


def _prices(title):
    values = []
    for v in PRICE_RE.findall(title):
        try:
            values.append(float(v.replace(",", "")))
        except ValueError:
            pass
    if not values:
        return None, None
    price = values[0]
    higher = [v for v in values[1:] if v > price]
    return price, (max(higher) if higher else None)


def fetch_deals(previous, log):
    """Junta ofertas nuevas con las de antes y quita las de más de 7 días."""
    deals = {d["id"]: d for d in previous}
    ok = 0
    for url in DEAL_FEEDS:
        try:
            for it in _items(url):
                t = it["title"]
                if not it["link"] or IS_DIGITAL.search(t) or not (IS_SWITCH.search(t) or IS_AMIIBO.search(t)):
                    continue
                price, old = _prices(t)
                did = hashlib.md5(it["link"].encode()).hexdigest()[:12]
                prev = deals.get(did) or {}
                image = it["image"] or prev.get("image")
                deals[did] = {
                    "id": did,
                    "title": t,
                    "price": price,
                    "old_price": old,
                    "store": _store(t),
                    "kind": "hardware" if IS_HARDWARE.search(t) else "game",
                    "collector": bool(IS_COLLECTOR.search(t)),
                    "url": it["link"],
                    "image": image,
                    "posted": it["date"].isoformat(timespec="seconds"),
                }
                for k in ("store_url", "page_checked"):
                    if k in prev:
                        deals[did][k] = prev[k]
            ok += 1
        except Exception as e:
            log(f"  fuente de ofertas falló ({e.__class__.__name__}): {url[:70]}")
    cutoff = _now() - datetime.timedelta(days=KEEP_DAYS)
    fresh = [d for d in deals.values()
             if datetime.datetime.fromisoformat(d["posted"]) >= cutoff]
    fresh.sort(key=lambda d: d["posted"], reverse=True)
    fresh = fresh[:MAX_DEALS]

    # Abrir la página de cada oferta nueva: foto (si falta) y link directo a la tienda
    lookups = 0
    for d in fresh:
        if d.get("page_checked") or lookups >= MAX_PAGE_LOOKUPS:
            continue
        lookups += 1
        img, store_url = _deal_page(d["url"])
        if img and not d.get("image"):
            d["image"] = img
        if store_url:
            d["store_url"] = store_url
        d["page_checked"] = True
        time.sleep(1)
    with_img = sum(1 for d in fresh if d.get("image"))
    with_link = sum(1 for d in fresh if d.get("store_url"))
    log(f"ofertas: {len(fresh)}, con foto: {with_img}, con link directo: {with_link} "
        f"(fuentes que respondieron: {ok}/{len(DEAL_FEEDS)})")
    return fresh, ok > 0


def fetch_news(previous, log):
    news = []
    for source, url in NEWS_FEEDS:
        try:
            for it in _items(url):
                if not it["link"]:
                    continue
                news.append({
                    "id": hashlib.md5(it["link"].encode()).hexdigest()[:12],
                    "title": it["title"],
                    "source": source,
                    "url": it["link"],
                    "image": it["image"],
                    "posted": it["date"].isoformat(timespec="seconds"),
                })
        except Exception as e:
            log(f"  fuente de noticias falló ({e.__class__.__name__}): {source}")
    if not news:
        return previous
    news.sort(key=lambda n: n["posted"], reverse=True)
    log(f"noticias: {len(news[:MAX_NEWS])}")
    return news[:MAX_NEWS]


# ------------------------------------------------------------------ Deku Deals
# Juegos físicos de Switch 2 con descuento. Se revisa como mucho una vez por hora
# y el botón de la app manda a la página del juego en Deku Deals (ahí están las tiendas).

DEKU_PAGES = [
    "https://www.dekudeals.com/switch-2-physical-games",
    "https://www.dekudeals.com/switch-2-physical-games?page=2",
]
DEKU_EVERY_MIN = 60
DEKU_ITEM_RE = re.compile(r'href="(/items/[a-z0-9\-]+)"', re.I)
DEKU_IMG_RE = re.compile(r'<img[^>]+(?:data-src|src)="(https://[^"]+)"[^>]*?(?:alt="([^"]*)")?', re.I)
DEKU_STRIKE_RE = re.compile(r"<(?:s|del)\b[^>]*>\s*\$\s?([\d,]+(?:\.\d{2})?)", re.I)
DEKU_STRONG_RE = re.compile(r"<strong[^>]*>\s*\$\s?([\d,]+(?:\.\d{2})?)\s*</strong>", re.I)
TAG_RE = re.compile(r"<[^>]+>")
DEBUG_DIR = None  # lo pone watcher.py


def _num(v):
    try:
        return float(v.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _parse_deku(page):
    """Corta la página en un bloque por juego y saca título, foto y precios."""
    found = []
    marks = [(m.start(), m.group(1)) for m in DEKU_ITEM_RE.finditer(page)]
    done = set()
    for idx, (pos, path) in enumerate(marks):
        if path in done:
            continue
        done.add(path)
        end = next((p for p, q in marks[idx + 1:] if q != path), len(page))
        start = page.rfind("<div", max(0, pos - 1500), pos)
        block = page[start if start >= 0 else pos:end]
        strike = DEKU_STRIKE_RE.search(block)
        strong = DEKU_STRONG_RE.search(block)
        if not (strike and strong):
            continue   # solo los que tienen descuento
        price, old = _num(strong.group(1)), _num(strike.group(1))
        if not price or not old or old <= price:
            continue
        img, title = None, ""
        for tag in re.findall(r"<img\b[^>]*>", block, re.I):
            src = re.search(r'(?:data-src|src)="(https://[^"]+)"', tag, re.I)
            if src and ("deku" in src.group(1) or "cdn" in src.group(1)):
                img = html.unescape(src.group(1))
                alt = re.search(r'alt="([^"]+)"', tag, re.I)
                title = html.unescape(alt.group(1)).strip() if alt else ""
                break
        if not title:
            for m in re.finditer(r'href="' + re.escape(path) + r'"[^>]*>(.*?)</a>', block, re.S | re.I):
                text = html.unescape(" ".join(TAG_RE.sub(" ", m.group(1)).split()))
                if text:
                    title = text
                    break
        if title:
            found.append({"path": path, "title": title, "price": price, "old_price": old, "image": img})
    return found


DEKU_STORES = ["Nintendo eShop", "My Nintendo Store", "Nintendo", "Amazon", "Walmart", "Target", "Best Buy",
               "GameStop", "Costco", "Sam's Club", "Woot", "eBay", "Newegg", "Meijer", "Kohl's", "Macy's", "BJ's"]
DEKU_ITEM_PAGES_PER_RUN = 25
DEKU_OFFERS_EVERY_H = 6
PRICE_ANY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")


def _parse_deku_offers(page):
    """Tabla de precios de un juego: tienda, precio y link (sin la eShop, que es digital)."""
    offers = {}
    for row in re.split(r"<tr\b", page, flags=re.I)[1:]:
        row = row.split("</tr>")[0]
        text = html.unescape(" ".join(TAG_RE.sub(" ", row).split()))
        alts = " ".join(re.findall(r'(?:alt|title)="([^"]+)"', row, re.I))
        hay = (text + " " + alts).lower()
        store = next((st for st in DEKU_STORES if st.lower() in hay), None)
        if not store or "eshop" in store.lower():
            continue
        if "digital" in hay and "physical" not in hay:
            continue
        href = re.search(r'href="([^"]+)"', row, re.I)
        price = PRICE_ANY_RE.search(text)
        if not href or not price:
            continue
        url = html.unescape(href.group(1))
        if url.startswith("/"):
            url = "https://www.dekudeals.com" + url
        name = "Nintendo" if store == "My Nintendo Store" else store
        value = _num(price.group(1))
        if value and (name not in offers or value < offers[name]["price"]):
            offers[name] = {"store": name, "price": value, "url": url}
    return sorted(offers.values(), key=lambda o: o["price"])


def _add_deku_offers(deals, log):
    """Abre la página de cada juego (pocas por corrida) para sacar el precio y link de cada tienda."""
    now = _now()
    opened = 0
    for d in deals:
        when = d.get("offers_checked")
        if when:
            try:
                if now - datetime.datetime.fromisoformat(when) < datetime.timedelta(hours=DEKU_OFFERS_EVERY_H):
                    continue
            except ValueError:
                pass
        if opened >= DEKU_ITEM_PAGES_PER_RUN:
            break
        opened += 1
        try:
            r = requests.get(d["url"], headers={**HEADERS, "User-Agent": "Mozilla/5.0 (compatible; NinStockWatcher/1.0)"}, timeout=25)
            if r.status_code == 200:
                offers = _parse_deku_offers(r.text)
                if DEBUG_DIR is not None and (opened == 1 and (not offers or not (DEBUG_DIR / "deku_item.html").exists())):
                    DEBUG_DIR.mkdir(exist_ok=True)
                    (DEBUG_DIR / "deku_item.html").write_text(r.text[:400_000], encoding="utf-8")
                if offers:
                    d["offers"] = offers
                    d["store"] = offers[0]["store"]
                    d["store_url"] = offers[0]["url"]
                    d["price"] = offers[0]["price"]
            d["offers_checked"] = now.isoformat(timespec="seconds")
        except Exception as e:
            log(f"  Deku Deals (página del juego) falló ({e.__class__.__name__})")
        time.sleep(2)
    with_offers = sum(1 for d in deals if d.get("offers"))
    log(f"Deku Deals: {with_offers} juegos con precios por tienda")


def fetch_deku(previous, last_checked, log):
    """Devuelve (ofertas_deku, cuándo_se_revisó). Respeta el intervalo de 1 hora."""
    now = _now()
    if last_checked:
        try:
            if now - datetime.datetime.fromisoformat(last_checked) < datetime.timedelta(minutes=DEKU_EVERY_MIN):
                return previous, last_checked
        except ValueError:
            pass
    prev_by_id = {d["id"]: d for d in previous}
    out = []
    for i, url in enumerate(DEKU_PAGES):
        try:
            r = requests.get(url, headers={**HEADERS, "User-Agent": "Mozilla/5.0 (compatible; NinStockWatcher/1.0)"}, timeout=25)
            if r.status_code != 200:
                log(f"  Deku Deals respondió HTTP {r.status_code}")
                continue
            items = _parse_deku(r.text)
            if i == 0 and DEBUG_DIR is not None and (not items or not (DEBUG_DIR / "deku.html").exists()):
                DEBUG_DIR.mkdir(exist_ok=True)
                (DEBUG_DIR / "deku.html").write_text(r.text[:400_000], encoding="utf-8")
            for it in items:
                did = "deku-" + it["path"].rsplit("/", 1)[-1]
                before = prev_by_id.get(did, {})
                out.append({k: before[k] for k in ("offers", "offers_checked", "store", "store_url") if k in before})
                out[-1].update({
                    "id": did,
                    "title": it["title"] + " (Nintendo Switch 2)",
                    "price": it["price"],
                    "old_price": it["old_price"],
                    "store": before.get("store", "Deku Deals"),
                    "kind": "game",
                    "collector": bool(IS_COLLECTOR.search(it["title"])),
                    "url": "https://www.dekudeals.com" + it["path"],
                    "image": it["image"],
                    "posted": prev_by_id.get(did, {}).get("posted") or now.isoformat(timespec="seconds"),
                    "source": "dekudeals",
                })
            time.sleep(2)
        except Exception as e:
            log(f"  Deku Deals falló ({e.__class__.__name__})")
    if not out and previous:
        log("Deku Deals: sin datos nuevos, se mantienen los anteriores")
        return previous, now.isoformat(timespec="seconds")
    log(f"Deku Deals: {len(out)} juegos físicos con descuento")
    _add_deku_offers(out, log)
    return out, now.isoformat(timespec="seconds")

