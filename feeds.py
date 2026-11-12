"""
Fuentes públicas que el vigilante junta en feed.json para que la app las lea
sin ninguna clave:
  - Ofertas de Nintendo Switch / Switch 2 de todas las tiendas (RSS de Slickdeals)
  - Noticias de Nintendo (RSS de Nintendo Life)
"""
import datetime
import email.utils
import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET

import requests

HEADERS = {"User-Agent": "NinStockWatcher/1.0 (+https://github.com)"}

DEAL_FEEDS = [
    "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
    "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1",
    "https://slickdeals.net/newsearch.php?searchin=first&forumchoice%5B%5D=9&rss=1",
    "https://slickdeals.net/newsearch.php?searchin=first&forumchoice%5B%5D=9&rss=1&q=nintendo+switch",
]
NEWS_FEEDS = [
    ("Nintendo Life", "https://www.nintendolife.com/feeds/latest"),
]

KEEP_DAYS = 7
MAX_DEALS = 80
MAX_NEWS = 30

IS_SWITCH = re.compile(r"nintendo switch|switch\s*2\b|\(switch\)|\bfor switch\b|[-–]\s*switch\b|\bNSW\b", re.I)
IS_DIGITAL = re.compile(r"\beshop\b|\bdigital\b|\bdownload\b|gift card|switch online|\be-?code\b", re.I)
IS_HARDWARE = re.compile(r"console|bundle|controller|joy-?con|amiibo|\bcase\b|\bdock\b|headset|microsd|charging|grip", re.I)
STORES = ["Best Buy", "GameStop", "Walmart", "Target", "Amazon", "Nintendo", "Costco", "Sam's Club",
          "Woot", "eBay", "Kohl's", "Newegg", "Meijer", "Macy's", "BJ's", "Dell", "Lowe's", "Home Depot"]
PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")
IMG_RE = re.compile(r"(https?://[^\s\"'<>]+?\.(?:jpe?g|png|webp|gif|thumb)(?:\?[^\s\"'<>]*)?)", re.I)
IMG_TAG_RE = re.compile(r"<img[^>]+src=[\"'](https?://[^\"']+)[\"']", re.I)
MEDIA_RE = re.compile(r"(?:url|href)=[\"'](https?://[^\"']+)[\"'][^>]*(?:image|medium=[\"']image)", re.I)
OG_IMAGE_RE = re.compile(r"<meta[^>]+property=[\"']og:image[\"'][^>]+content=[\"'](https?://[^\"']+)[\"']"
                         r"|<meta[^>]+content=[\"'](https?://[^\"']+)[\"'][^>]+property=[\"']og:image[\"']", re.I)
MAX_PAGE_LOOKUPS = 12   # fotos que se buscan por corrida en la página de la oferta
BAD_IMG = re.compile(r"avatar|logo|icon|pixel|spacer|emoji|badge|\.gif", re.I)


def _find_image(raw):
    """Busca la foto del producto dentro del item del RSS (img, media, o URL de imagen)."""
    text = html.unescape(raw)
    for rx in (IMG_TAG_RE, MEDIA_RE, IMG_RE):
        for url in rx.findall(text):
            if not BAD_IMG.search(url):
                return url
    return None


def _image_from_page(url):
    """Si el RSS no trae foto, la saca de la página de la oferta (og:image)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        m = OG_IMAGE_RE.search(r.text)
        if not m:
            return None
        img = html.unescape(m.group(1) or m.group(2))
        return None if BAD_IMG.search(img) else img
    except requests.RequestException:
        return None


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
                if not it["link"] or not IS_SWITCH.search(t) or IS_DIGITAL.search(t):
                    continue
                price, old = _prices(t)
                did = hashlib.md5(it["link"].encode()).hexdigest()[:12]
                image = it["image"] or (deals.get(did) or {}).get("image")
                deals[did] = {
                    "id": did,
                    "title": t,
                    "price": price,
                    "old_price": old,
                    "store": _store(t),
                    "kind": "hardware" if IS_HARDWARE.search(t) else "game",
                    "url": it["link"],
                    "image": image,
                    "posted": it["date"].isoformat(timespec="seconds"),
                }
            ok += 1
        except Exception as e:
            log(f"  fuente de ofertas falló ({e.__class__.__name__}): {url[:70]}")
    cutoff = _now() - datetime.timedelta(days=KEEP_DAYS)
    fresh = [d for d in deals.values()
             if datetime.datetime.fromisoformat(d["posted"]) >= cutoff]
    fresh.sort(key=lambda d: d["posted"], reverse=True)
    fresh = fresh[:MAX_DEALS]

    # Fotos que faltan: buscarlas en la página de cada oferta (pocas por corrida)
    lookups = 0
    for d in fresh:
        if d.get("image") or d.get("no_image") or lookups >= MAX_PAGE_LOOKUPS:
            continue
        lookups += 1
        img = _image_from_page(d["url"])
        if img:
            d["image"] = img
        else:
            d["no_image"] = True   # no volver a intentarlo en cada corrida
        time.sleep(1)
    with_img = sum(1 for d in fresh if d.get("image"))
    log(f"ofertas: {len(fresh)}, con foto: {with_img} (fuentes que respondieron: {ok}/{len(DEAL_FEEDS)})")
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
