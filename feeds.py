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
