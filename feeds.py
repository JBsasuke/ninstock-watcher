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
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

HEADERS = {"User-Agent": "NinStockWatcher/1.0 (+https://github.com)"}

DEAL_FEEDS = [
    "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
    "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1",
    "https://slickdeals.net/newsearch.php?searchin=first&forumchoice%5B%5D=9&rss=1",
    "https://slickdeals.net/newsearch.php?searchin=first&forumchoice%5B%5D=9&rss=1&q=nintendo+switch",
]
# Solo fuentes oficiales de Nintendo
# (fuente, url, idioma). Solo canales oficiales de Nintendo; la app traduce al otro idioma.
NEWS_FEEDS = [
    ("Nintendo", "https://www.youtube.com/feeds/videos.xml?channel_id=UCGIY_O-8vW4rfX98KlMkvRg", "en"),
]
ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
YT = "{http://www.youtube.com/xml/schemas/2015}"

KEEP_DAYS = 7
START = time.monotonic()
LINKS_BUDGET_S = 180      # máximo 3 min por corrida buscando links de ofertas
DEKU_BUDGET_S = 150       # máximo 2,5 min por corrida abriendo páginas de juegos
DEBUG_DIR = None  # carpeta para copias de páginas (la pone watcher.py)
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
MAX_PAGE_LOOKUPS = 30   # páginas de oferta que se abren por corrida (foto + link de la tienda)
RETAILER_DOMAINS = r"(walmart\.com|target\.com|amazon\.com|bestbuy\.com|gamestop\.com|nintendo\.com|costco\.com|" \
                   r"samsclub\.com|woot\.com|ebay\.com|newegg\.com|meijer\.com|kohls\.com|macys\.com|bjs\.com)"
RETAILER_RE = re.compile(r"https?://(?:www\.)?" + RETAILER_DOMAINS + r"/[^\s\"'<>]+", re.I)
# Links reales (en atributos href o dentro de JSON con \/), nunca el texto recortado que se ve en pantalla
HREF_RETAILER_RE = re.compile(r"(?:href|data-href|data-url)=[\"'](https?://(?:www\.)?" + RETAILER_DOMAINS + r"/[^\"']+)[\"']", re.I)
JSON_RETAILER_RE = re.compile(r"https?:\\/\\/(?:www\.)?" + RETAILER_DOMAINS + r"\\/[^\"'\s<>]+", re.I)
DOMAIN_STORE = {"walmart.com": "Walmart", "target.com": "Target", "amazon.com": "Amazon", "bestbuy.com": "Best Buy",
                "gamestop.com": "GameStop", "nintendo.com": "Nintendo", "costco.com": "Costco",
                "samsclub.com": "Sam's Club", "woot.com": "Woot", "ebay.com": "eBay", "newegg.com": "Newegg",
                "meijer.com": "Meijer", "kohls.com": "Kohl's", "macys.com": "Macy's", "bjs.com": "BJ's"}
LINK_VERSION = 3
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


CLICK_RE = re.compile(r"<a\b[^>]*href=[\"'](https://slickdeals\.net/click\?[^\"']+)[\"'][^>]*>", re.I)
WRAP_PARAMS = ("u", "url", "murl", "u2", "u1", "dest", "destination", "dl", "RD_PARM1", "target", "link", "redirect")


BBY_CLICK_RE = re.compile(r"https?://api\.bestbuy\.com/click/[^/]*/(\d{7})", re.I)


def unwrap_url(u, depth=0):
    """Saca el link de la tienda que viene envuelto dentro de un link de afiliado/redirección."""
    if not u or depth > 4:
        return u
    m = BBY_CLICK_RE.match(u)
    if m:
        return f"https://www.bestbuy.com/site/{m.group(1)}.p?skuId={m.group(1)}"
    try:
        if RETAILER_RE.match(u) and "goto." not in urlparse(u).netloc:
            return u
        q = parse_qs(urlparse(u).query)
        for key in WRAP_PARAMS:
            for val in q.get(key, []):
                val = unquote(val)
                if val.startswith("http"):
                    inner = unwrap_url(val, depth + 1)
                    if inner and RETAILER_RE.match(inner):
                        return inner
    except Exception:
        pass
    return u


def resolve_click(url):
    """Sigue el botón "Get Deal" de Slickdeals hasta la tienda. None si no se pudo."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=(6, 10), allow_redirects=True, stream=True)
        chain = [h.headers.get("Location", "") for h in r.history] + [r.url]
        body = ""
        if not RETAILER_RE.match(r.url or ""):
            body = r.raw.read(200_000, decode_content=True).decode("utf-8", "ignore")
        r.close()
        for u in reversed(chain):
            u = unwrap_url(u)
            if u and RETAILER_RE.match(u):
                return _clean_store_url(u)
        m = RETAILER_RE.search(body.replace("\\/", "/"))
        if m:
            return _clean_store_url(unwrap_url(m.group(0)))
    except Exception:
        pass
    return None


def _clean_store_url(u):
    """Deja el link en la página del producto (sin reseñas ni seguimiento). None si está recortado."""
    if not u or "..." in u or "\u2026" in u:
        return None
    u = unwrap_url(u)
    try:
        parsed = urlparse(u)
        host, path = parsed.netloc.lower(), parsed.path
        if "amazon." in host:
            m = re.search(r"/(?:dp|gp/product|product-reviews)/([A-Z0-9]{10})", path)
            return f"https://www.amazon.com/dp/{m.group(1)}" if m else u
        if "walmart." in host:
            m = re.search(r"/(?:ip|reviews/product)/(?:[^/]+/)?(\d{5,})", path)
            return f"https://www.walmart.com/ip/{m.group(1)}" if m else u
        if "target." in host:
            m = re.search(r"(A-\d{5,})", path)
            return f"https://www.target.com/p/-/{m.group(1)}" if m else u
        if "bestbuy." in host:
            m = re.search(r"(?:skuId=|/)(\d{7})(?:\.p|$|\?)", u)
            return f"https://www.bestbuy.com/site/{m.group(1)}.p?skuId={m.group(1)}" if m else u.split("#")[0]
        return u.split("#")[0]
    except Exception:
        return u


def _deal_page(url):
    """Abre la página de la oferta: foto, tienda real y link directo (siguiendo "Get Deal")."""
    image = store_url = store = click = None
    try:
        r = requests.get(url, headers=HEADERS, timeout=(6, 12))
        if r.status_code != 200:
            return None, None, None, None
        page = r.text
        m = OG_IMAGE_RE.search(page)
        if m:
            img = html.unescape(m.group(1) or m.group(2))
            image = None if BAD_IMG.search(img) else img
        m = re.search(r"data-storename=[\"']([^\"']+)[\"']", page)
        if m:
            store = html.unescape(m.group(1)).strip()
        cm = CLICK_RE.search(page)
        if cm:
            click = html.unescape(cm.group(1))
            store_url = resolve_click(click)
        if not store_url:
            candidates = [unquote(unquote(html.unescape(raw))) for raw in U2_RE.findall(page)]
            candidates += [html.unescape(m.group(1)) for m in HREF_RETAILER_RE.finditer(page)]
            candidates += [m.group(0).replace("\\/", "/") for m in JSON_RETAILER_RE.finditer(page)]
            for cand in candidates:
                if RETAILER_RE.match(cand):
                    store_url = _clean_store_url(cand)
                    if store_url:
                        break
        if not store_url and not click and DEBUG_DIR is not None and not (DEBUG_DIR / "slickdeals.html").exists():
            DEBUG_DIR.mkdir(exist_ok=True)
            (DEBUG_DIR / "slickdeals.html").write_text(page[:400_000], encoding="utf-8")
    except requests.RequestException:
        pass
    return image, store_url, store, click


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_date(text):
    try:
        return email.utils.parsedate_to_datetime(text).astimezone(datetime.timezone.utc)
    except Exception:
        pass
    try:  # Atom / ISO 8601 (YouTube)
        return datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00")).astimezone(datetime.timezone.utc)
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
    found = []
    for s in STORES:
        if s == "Nintendo":
            # "Nintendo Switch" no es una tienda; solo cuenta "My Nintendo Store" / "Nintendo Store"
            m = list(re.finditer(r"(my )?nintendo (store|\.com)", low))
            if m:
                found.append((m[-1].start(), s))
        else:
            m = list(re.finditer(r"\b" + re.escape(s.lower()) + r"\b", low))
            if m:
                found.append((m[-1].start(), s))
    return max(found)[1] if found else "Slickdeals"


def store_from_url(url):
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        for dom, name in DOMAIN_STORE.items():
            if host == dom or host.endswith("." + dom):
                return name
    except Exception:
        pass
    return None


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
                for k in ("store_url", "page_checked", "link_v", "store"):
                    if k in prev:
                        deals[did][k] = prev[k]
            ok += 1
        except Exception as e:
            log(f"  fuente de ofertas falló ({e.__class__.__name__}): {url[:70]}")
    for d in deals.values():   # links viejos cortados o de reseñas: fuera
        su = d.get("store_url")
        if su and ("..." in su or "/reviews/" in su or "\u2026" in su):
            d.pop("store_url", None)
            d.pop("link_v", None)
    cutoff = _now() - datetime.timedelta(days=KEEP_DAYS)
    fresh = [d for d in deals.values()
             if datetime.datetime.fromisoformat(d["posted"]) >= cutoff]
    fresh.sort(key=lambda d: d["posted"], reverse=True)
    fresh = fresh[:MAX_DEALS]

    # Abrir la página de cada oferta nueva: foto (si falta) y link directo a la tienda
    lookups = 0
    t0 = time.monotonic()
    for d in fresh:
        if (d.get("page_checked") and d.get("link_v") == LINK_VERSION) or lookups >= MAX_PAGE_LOOKUPS:
            continue
        if time.monotonic() - t0 > LINKS_BUDGET_S:
            log("  tiempo de búsqueda de links agotado; sigue en la próxima corrida")
            break
        lookups += 1
        img, store_url, store, click = _deal_page(d["url"])
        if img and not d.get("image"):
            d["image"] = img
        d.pop("store_url", None)
        if store:
            d["store"] = store
        if store_url:
            d["store_url"] = store_url
            d["store"] = store_from_url(store_url) or d["store"]
        elif click:
            d["store_url"] = click   # el botón de Slickdeals te deja directo en la tienda
        d["page_checked"] = True
        d["link_v"] = LINK_VERSION
        time.sleep(1)
    with_img = sum(1 for d in fresh if d.get("image"))
    with_link = sum(1 for d in fresh if d.get("store_url"))
    log(f"ofertas: {len(fresh)}, con foto: {with_img}, con link directo: {with_link} "
        f"(fuentes que respondieron: {ok}/{len(DEAL_FEEDS)})")
    return fresh, ok > 0


def _clean_summary(text):
    """Descripción del video sin links, hashtags ni líneas de ESRB/redes; máx. ~700 letras."""
    keep = []
    for line in (text or "").splitlines():
        line = line.strip()
        low = line.lower()
        if not line or "http" in low or line.startswith("#") or "esrb" in low or low.startswith(("subscribe", "follow", "©")):
            continue
        keep.append(line)
    out = " ".join(keep)
    return (out[:700].rsplit(" ", 1)[0] + "…") if len(out) > 700 else out


def _atom_entries(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    for e in root.iter(ATOM + "entry"):
        link = e.find(ATOM + "link")
        group = e.find(MEDIA + "group")
        thumb = group.find(MEDIA + "thumbnail") if group is not None else None
        vid = e.findtext(YT + "videoId")
        image = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else (thumb.get("url") if thumb is not None else None)
        yield {
            "title": html.unescape((e.findtext(ATOM + "title") or "").strip()),
            "link": link.get("href") if link is not None else "",
            "date": _parse_date(e.findtext(ATOM + "published") or ""),
            "image": image,
            "summary": _clean_summary(group.findtext(MEDIA + "description") if group is not None else ""),
        }


def fetch_news(previous, log):
    news = []
    for source, url, lang in NEWS_FEEDS:
        try:
            entries = list(_atom_entries(url)) if "youtube.com/feeds" in url else list(_items(url))
            for it in entries:
                if not it["link"] or "#shorts" in it["title"].lower():
                    continue
                news.append({
                    "id": hashlib.md5(it["link"].encode()).hexdigest()[:12],
                    "title": it["title"],
                    "summary": it.get("summary") or "",
                    "lang": lang,
                    "source": source,
                    "url": it["link"],
                    "image": it["image"],
                    "posted": it["date"].isoformat(timespec="seconds"),
                })
        except Exception as e:
            log(f"  fuente de noticias falló ({e.__class__.__name__}): {source}")
    if not news:
        # nunca dejar noticias de sitios no oficiales guardadas de antes
        return [n for n in previous or [] if n.get("source") == "Nintendo" and "youtube.com" in n.get("url", "")]
    news.sort(key=lambda n: n["posted"], reverse=True)
    log(f"noticias: {len(news[:MAX_NEWS])}")
    return news[:MAX_NEWS]


# ------------------------------------------------------------------ Deku Deals
# Lista de juegos FÍSICOS de Switch 2 ordenada por fecha de salida: sirve para
# Lanzamientos, ediciones de coleccionista y descuentos. Se revisa como mucho
# una vez por hora y los links pasan por Deku Deals (así reciben su comisión).

DEKU_BASE = "https://www.dekudeals.com/switch-2-physical-games"
DEKU_MAX_PAGES = 8              # hasta llegar a juegos que salieron hace más de 60 días
DEKU_PAST_DAYS = 60
NINTENDO_HINT = re.compile(r"mario|zelda|pok[eé]mon|kirby|metroid|donkey kong|pikmin|splatoon|fire emblem|"
                           r"xenoblade|animal crossing|yoshi|wario|smash bros|switch sports|nintendo labo|luigi|star fox|"
                           r"kid icarus|tomodachi|rhythm heaven|drag x drive|f-zero|chibi-robo|1-2-switch", re.I)
DEKU_EVERY_MIN = 60
DEKU_HEADERS = {**HEADERS, "User-Agent": "Mozilla/5.0 (compatible; NinStockWatcher/1.0)"}
DEKU_CARD_SPLIT = re.compile(r"<div class=['\"]col d-block['\"]>", re.I)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DEKU_DATE_RE = re.compile(r"<div>\s*((?:" + MONTHS + r") \d{1,2}, \d{4}|(?:" + MONTHS + r") \d{4}|"
                          r"(?:Q[1-4]|Spring|Summer|Fall|Winter|Early|Late|Mid)[^<]{0,6}\d{4}|\d{4}|TBA)\s*</div>")
TAG_RE = re.compile(r"<[^>]+>")
PRICE_ANY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")
DEKU_STORES = ["My Nintendo Store", "Nintendo eShop", "Amazon", "Walmart", "Target", "Best Buy", "GameStop",
               "Costco", "Sam's Club", "Woot", "eBay", "Newegg", "Meijer", "Kohl's", "Macy's", "BJ's", "Nintendo"]
DEKU_ITEM_PAGES_PER_RUN = 25
DEKU_OFFERS_EVERY_H = 6


def _num(v):
    try:
        return float(v.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _iso_date(text):
    for fmt in ("%B %d, %Y",):
        try:
            return datetime.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_deku_cards(page):
    """Una tarjeta por juego: título, foto, link, precio, precio anterior y fecha de salida."""
    cards = []
    for c in DEKU_CARD_SPLIT.split(page)[1:]:
        path = re.search(r"href=['\"](/items/[^'\"]+)['\"]", c)
        title = re.search(r"<h6[^>]*>(.*?)</h6>", c, re.S)
        if not path or not title:
            continue
        img = re.search(r"src=['\"](https://cdn\.dekudeals\.com/images/[^'\"]+)['\"]", c)
        strong = re.search(r"<strong[^>]*>\s*\$\s?([\d,]+(?:\.\d{2})?)\s*</strong>", c)
        strike = re.search(r"<s\b[^>]*>\s*\$\s?([\d,]+(?:\.\d{2})?)\s*</s>", c)
        date = DEKU_DATE_RE.search(re.sub(r"<svg.*?</svg>", "", c, flags=re.S))
        price = _num(strong.group(1)) if strong else None
        old = _num(strike.group(1)) if strike else None
        name = html.unescape(" ".join(TAG_RE.sub(" ", title.group(1)).split()))
        cards.append({
            "id": "deku-" + path.group(1).rsplit("/", 1)[-1],
            "title": name,
            "url": "https://www.dekudeals.com" + path.group(1),
            "image": html.unescape(img.group(1)) if img else None,
            "price": price,
            "old_price": old if (old and price and old > price) else None,
            "release": date.group(1) if date else None,
            "release_date": _iso_date(date.group(1)) if date else None,
            "collector": bool(IS_COLLECTOR.search(name)),
        })
    return cards


def parse_deku_offers(page):
    """Tabla de precios: devuelve (ofertas físicas por tienda, precio digital o None)."""
    offers = {}
    digital = None
    main = page.split("Also available at")[0]   # solo la tabla principal de tiendas
    for row in re.split(r"<tr\b", main, flags=re.I)[1:]:
        row = row.split("</tr>")[0]
        text = html.unescape(" ".join(TAG_RE.sub(" ", row).split()))
        attrs = " ".join(re.findall(r"(?:alt|title|aria-label)=['\"]([^'\"]+)['\"]", row, re.I))
        hay = (text + " " + attrs).lower()
        store = next((st for st in DEKU_STORES if st.lower() in hay), None)
        href = re.search(r"href=['\"]([^'\"]+)['\"]", row, re.I)
        price = PRICE_ANY_RE.search(text)
        if not store or not href or not price:
            continue
        if "eshop" in store.lower() or ("digital" in hay and "physical" not in hay):
            value = _num(price.group(1))
            link = _clean_store_url(html.unescape(href.group(1))) or ""
            if value and (digital is None or value < digital["price"]):
                digital = {"price": value, "url": link if RETAILER_RE.match(link) else ""}
            continue
        url = html.unescape(href.group(1))
        if url.startswith("/"):
            url = "https://www.dekudeals.com" + url
        url = _clean_store_url(url) or url
        if not RETAILER_RE.match(url):
            continue   # solo links directos a la tienda
        name = "Nintendo" if "nintendo" in store.lower() else store
        value = _num(price.group(1))
        if value and (name not in offers or value < offers[name]["price"]):
            offers[name] = {"store": name, "price": value, "url": url}
    return sorted(offers.values(), key=lambda o: o["price"]), digital


def clean_offers(offers):
    """Limpia links guardados de antes (afiliados, envoltorios) y quita los que no son de una tienda."""
    out = []
    for o in offers or []:
        url = _clean_store_url(o.get("url", "")) or ""
        if RETAILER_RE.match(url):
            out.append({**o, "url": url})
    return sorted(out, key=lambda o: o["price"])


def _add_offers(cards, log):
    now = _now()
    opened = 0
    t0 = time.monotonic()
    for d in cards:
        if time.monotonic() - t0 > DEKU_BUDGET_S:
            log("  tiempo de páginas de juegos agotado; sigue en la próxima corrida")
            break
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
            r = requests.get(d["url"], headers=DEKU_HEADERS, timeout=(6, 12))
            if r.status_code == 200:
                offers, digital = parse_deku_offers(r.text)
                d["digital"] = digital
                pub = re.search(r"Publisher:\s*(?:<[^>]+>\s*)*([^<\n]+)", r.text)
                if pub:
                    d["publisher"] = html.unescape(pub.group(1)).strip()
                    d["nintendo"] = "nintendo" in d["publisher"].lower()
                if DEBUG_DIR is not None and opened == 1 and (not offers or not (DEBUG_DIR / "deku_item.html").exists()):
                    DEBUG_DIR.mkdir(exist_ok=True)
                    (DEBUG_DIR / "deku_item.html").write_text(r.text[:400_000], encoding="utf-8")
                d["offers"] = offers
            d["offers_checked"] = now.isoformat(timespec="seconds")
        except Exception as e:
            log(f"  Deku Deals (página del juego) falló ({e.__class__.__name__})")
        time.sleep(2)
    log(f"Deku Deals: {sum(1 for d in cards if d.get('offers'))}/{len(cards)} juegos con precio por tienda")


def fetch_deku(previous, last_checked, log):
    """Devuelve (tarjetas, cuándo_se_revisó). Respeta el intervalo de 1 hora."""
    now = _now()
    for c in previous:   # links guardados de antes: limpiar siempre
        c["offers"] = clean_offers(c.get("offers"))
    if last_checked and previous:
        try:
            if now - datetime.datetime.fromisoformat(last_checked) < datetime.timedelta(minutes=DEKU_EVERY_MIN):
                return previous, last_checked
        except ValueError:
            pass
    before = {c["id"]: c for c in previous}
    cards, seen = [], set()
    cutoff = (now - datetime.timedelta(days=DEKU_PAST_DAYS)).date().isoformat()
    for i in range(DEKU_MAX_PAGES):
        url = DEKU_BASE if i == 0 else f"{DEKU_BASE}?page={i + 1}"
        try:
            r = requests.get(url, headers=DEKU_HEADERS, timeout=(6, 15))
            if r.status_code != 200:
                log(f"  Deku Deals respondió HTTP {r.status_code}")
                continue
            page_cards = parse_deku_cards(r.text)
            if i == 0 and DEBUG_DIR is not None and not page_cards:
                DEBUG_DIR.mkdir(exist_ok=True)
                (DEBUG_DIR / "deku.html").write_text(r.text[:400_000], encoding="utf-8")
            for c in page_cards:
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                old = before.get(c["id"], {})
                for k in ("offers", "offers_checked", "first_seen", "publisher", "digital"):
                    if k in old:
                        c[k] = old[k]
                c["offers"] = clean_offers(c.get("offers"))
                c.setdefault("first_seen", now.isoformat(timespec="seconds"))
                c["nintendo"] = bool(re.search(r"nintendo", c.get("publisher", ""), re.I)) or \
                    (not c.get("publisher") and bool(NINTENDO_HINT.search(c["title"])))
                cards.append(c)
            time.sleep(2)
            dated = [c["release_date"] for c in page_cards if c.get("release_date")]
            if not page_cards or (dated and max(dated) < cutoff):
                break
        except Exception as e:
            log(f"  Deku Deals falló ({e.__class__.__name__})")
    if not cards:
        log("Deku Deals: sin datos nuevos, se mantienen los anteriores")
        return previous, now.isoformat(timespec="seconds")
    log(f"Deku Deals: {len(cards)} juegos físicos de Switch 2 "
        f"({sum(1 for c in cards if c['old_price'])} con descuento, {sum(1 for c in cards if c['collector'])} de coleccionista)")
    _add_offers(cards, log)
    return cards, now.isoformat(timespec="seconds")


def deku_deals(cards):
    """Convierte las tarjetas con descuento en ofertas para Inicio."""
    out = []
    for c in cards:
        if not (c.get("price") and c.get("old_price")):
            continue
        offers = c.get("offers") or []
        best = offers[0] if offers else None
        out.append({
            "id": c["id"],
            "title": c["title"] + " (Nintendo Switch 2)",
            "price": best["price"] if best else c["price"],
            "old_price": c["old_price"],
            "store": best["store"] if best else "Deku Deals",
            "store_url": best["url"] if best else None,
            "offers": offers,
            "kind": "game",
            "collector": c["collector"],
            "url": c["url"],
            "image": c.get("image"),
            "posted": c.get("first_seen"),
            "source": "dekudeals",
            "digital": c.get("digital"),
        })
    return out


# ------------------------------------------------------------------ juntar repetidos
# Una sola oferta por juego: se juntan las de Slickdeals y Deku Deals, se queda el
# precio más bajo de cada tienda y se ordenan de más barata a más cara.
# Solo se usan links directos a la tienda (nunca la página de Slickdeals o Deku Deals).

STORE_SEARCH = {
    "Nintendo": ("https://www.nintendo.com/us/search/", "q"),
    "Walmart": ("https://www.walmart.com/search", "q"),
    "Target": ("https://www.target.com/s", "searchTerm"),
    "Amazon": ("https://www.amazon.com/s", "k"),
    "GameStop": ("https://www.gamestop.com/search/", "q"),
    "Best Buy": ("https://www.bestbuy.com/site/searchpage.jsp", "st"),
}


def is_direct(url):
    return bool(url) and (bool(RETAILER_RE.match(url)) or url.startswith("https://slickdeals.net/click"))


def clean_name(title):
    t = PRICE_RE.sub(" ", title)
    t = re.sub(r"\((?:reg\.?|was|list|orig)[^)]*\)", " ", t, flags=re.I)
    t = re.sub(r"\s(?:at|@|from|via)\s+[A-Z].*$", " ", t)
    t = re.sub(r"\+\s*free\s+(?:shipping|s/h|store pickup).*$", " ", t, flags=re.I)
    t = re.sub(r"\s+w/\s+.*$", " ", t, flags=re.I)
    t = re.sub(r"[-–,:;]\s*$", "", " ".join(t.split()))
    return t.strip(" -–,") or title


def game_key(title):
    t = clean_name(title).lower()
    plat = "s2" if re.search(r"switch\s*2|\bns2\b", t) else "s1"
    t = re.sub(r"nintendo switch 2 edition|switch 2 edition", " ns2edition ", t)
    t = re.sub(r"\((?:nintendo )?switch[^)]*\)", " ", t)
    t = re.sub(r"\b(?:nintendo switch 2|nintendo switch|switch 2|switch|nsw|ns2|game-?key card|physical|"
               r"pre-?order|new|sealed)\b", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return plat + ":" + " ".join(t.split())


def _search_url(store, name):
    if store not in STORE_SEARCH:
        return None
    base, param = STORE_SEARCH[store]
    return base + "?" + param + "=" + quote_plus(name)


def merge_deals(deals):
    groups = {}
    for d in deals:
        groups.setdefault(game_key(d["title"]), []).append(d)
    out = []
    for key, items in groups.items():
        offers = {}

        def add(store, price, url):
            if not store or price is None:
                return
            if not is_direct(url):
                url = _search_url(store, clean_name(items[0]["title"]))
            if not url:
                return
            if store not in offers or price < offers[store]["price"]:
                offers[store] = {"store": store, "price": price, "url": url}

        for d in items:
            for o in d.get("offers") or []:
                add(o.get("store"), o.get("price"), o.get("url"))
            if d.get("source") != "dekudeals":
                add(d.get("store"), d.get("price"), d.get("store_url"))
        ranked = sorted(offers.values(), key=lambda o: o["price"])
        if not ranked:
            continue   # sin link a una tienda: no se muestra
        best = ranked[0]
        olds = [d["old_price"] for d in items if d.get("old_price")]
        old = max(olds) if olds and max(olds) > best["price"] else None
        deku = next((d for d in items if d.get("source") == "dekudeals"), None)
        name = clean_name((deku or min(items, key=lambda d: len(d["title"])))["title"])
        out.append({
            "id": hashlib.md5(key.encode()).hexdigest()[:12],
            "title": name,
            "price": best["price"],
            "old_price": old,
            "store": best["store"],
            "store_url": best["url"],
            "url": best["url"],
            "offers": ranked,
            "image": next((d["image"] for d in items if d.get("image")), None),
            "kind": items[0].get("kind", "game"),
            "collector": any(d.get("collector") for d in items),
            "posted": max((d.get("posted") or "") for d in items),
            "digital": next((d["digital"] for d in items if d.get("digital")), None),
        })
    out.sort(key=lambda d: d["posted"], reverse=True)
    return out


# ------------------------------------------------------------------ productos de Colección vía Deku Deals
def parse_deku_rows(page):
    """Todas las tiendas físicas de la tabla principal, con precio o sin él (sin precio = agotado)."""
    rows = {}
    main = page.split("Also available at")[0]
    for row in re.split(r"<tr\b", main, flags=re.I)[1:]:
        row = row.split("</tr>")[0]
        text = html.unescape(" ".join(TAG_RE.sub(" ", row).split()))
        attrs = " ".join(re.findall(r"(?:alt|title|aria-label)=['\"]([^'\"]+)['\"]", row, re.I))
        hay = (text + " " + attrs).lower()
        store = next((st for st in DEKU_STORES if st.lower() in hay), None)
        if not store or "eshop" in store.lower() or ("digital" in hay and "physical" not in hay):
            continue
        href = re.search(r"href=['\"]([^'\"]+)['\"]", row, re.I)
        url = _clean_store_url(html.unescape(href.group(1))) if href else None
        if url and not RETAILER_RE.match(url):
            url = None
        price = PRICE_ANY_RE.search(text)
        name = "Nintendo" if "nintendo" in store.lower() else store
        value = _num(price.group(1)) if price and "unavailable" not in hay else None
        old = rows.get(name)
        if not old or (value is not None and (old["price"] is None or value < old["price"])):
            rows[name] = {"store": name, "price": value, "url": url or (old or {}).get("url")}
    return list(rows.values())


def fetch_deku_rows(url):
    try:
        r = requests.get(url, headers=DEKU_HEADERS, timeout=(6, 15))
        if r.status_code == 200:
            m = OG_IMAGE_RE.search(r.text)
            img = html.unescape(m.group(1) or m.group(2)) if m else None
            return parse_deku_rows(r.text), img
    except requests.RequestException:
        pass
    return None, None

