#!/usr/bin/env python3
"""
Bot Telegram semplificato per brandizzare immagini
Riceve un brand personalizzato, poi sovrappone le immagini inviate su quel brand
Versione: 2025-12-08 v2 - RAM only, fast polling
"""
import os
import time
import logging
import httpx
import asyncio
import tempfile
import re
import random
import glob
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ContextTypes
from telegram import Update

# Thread pool per operazioni sincrone (PIL, I/O) — max 1 per non superare 512MB su Render
# DEPLOY: 2026-06-15
thread_pool = ThreadPoolExecutor(max_workers=1)

# Client HTTP condiviso (evita overhead TLS per ogni richiesta)
http_client = None

def get_http_client():
    """Ottieni client HTTP condiviso con headers browser"""
    global http_client
    if http_client is None:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
            'Accept-Language': 'it-IT,it;q=0.9,en;q=0.8',
            'Referer': 'https://premiumtools.it/',
        }
        http_client = httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers)
    return http_client

# Cache degli sfondi in memoria (path + immagini PIL pre-caricate)
cached_backgrounds = []
cached_background_images = {}  # path -> PIL.Image

# Calculate the background path correctly relative to this script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(SCRIPT_DIR, "assets")
USER_BRANDS_DIR = os.path.join(SCRIPT_DIR, "user_brands")

# Crea la cartella user_brands se non esiste
os.makedirs(USER_BRANDS_DIR, exist_ok=True)

# PROTEZIONE DUPLICATI: Cache degli update_id già processati (in RAM - veloce!)
from collections import deque
processed_updates = set()
processed_updates_queue = deque(maxlen=500)

# PROTEZIONE MESSAGGI: Cache dei message_id già processati
processed_messages = set()
processed_messages_queue = deque(maxlen=500)

# PROTEZIONE ALBUM: Cache dei media_group_id già processati (per album inoltrati)
processed_media_groups = set()
processed_media_groups_queue = deque(maxlen=200)

# ALTERNANZA SFONDI: Indice dell'ultimo sfondo usato (in RAM)
last_background_index = -1

def load_backgrounds_cache():
    """Carica solo i percorsi degli sfondi (non le immagini) per risparmiare RAM"""
    global cached_backgrounds, cached_background_images
    try:
        bg_files = glob.glob(os.path.join(ASSETS_DIR, "*.png")) + glob.glob(os.path.join(ASSETS_DIR, "*.jpg"))
        bg_files = [f for f in bg_files if not f.endswith('test_output.png') and '_inactive' not in os.path.basename(f)]
        cached_backgrounds = bg_files
        cached_background_images = {}  # Non pre-caricare in RAM
        for f in bg_files:
            logger.info(f"  ✅ Trovato sfondo: {os.path.basename(f)}")
        logger.info(f"📁 Sfondi trovati: {len(cached_backgrounds)} (caricati su richiesta)")
    except Exception as e:
        logger.error(f"❌ Errore cache sfondi: {e}")

# File di log dedicato per thread pool (per debug)
try:
    thread_log_file = open("/tmp/telegram_bot_threads.log", "a", buffering=1)
except:
    # Fallback: usa un file nullo se /tmp non è accessibile
    import io
    thread_log_file = io.StringIO()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def thread_log(msg):
    """Log in thread pool che flush subito"""
    import time
    thread_log_file.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    thread_log_file.flush()

# Salva il brand per ogni utente {user_id: Image}
user_brands = {}

def get_user_brand_path(user_id):
    """Ritorna il percorso del file brand salvato per l'utente"""
    return os.path.join(USER_BRANDS_DIR, f"brand_{user_id}.png")

def save_user_brand(user_id, image_bytes):
    """Salva il brand dell'utente su file"""
    try:
        brand_path = get_user_brand_path(user_id)
        with open(brand_path, 'wb') as f:
            f.write(image_bytes)
        logger.info(f"✅ Brand salvato su file per utente {user_id}: {brand_path}")
        return True
    except Exception as e:
        logger.error(f"❌ Errore salvataggio brand: {e}")
        return False

def load_user_brand(user_id):
    """Carica il brand dell'utente dal file se esiste"""
    try:
        brand_path = get_user_brand_path(user_id)
        if os.path.exists(brand_path):
            with open(brand_path, 'rb') as f:
                brand_bytes = f.read()
            logger.info(f"✅ Brand caricato da file per utente {user_id}")
            return brand_bytes
    except Exception as e:
        logger.error(f"❌ Errore caricamento brand: {e}")
    return None

def get_random_background():
    """Carica un background ALTERNATO dalla cache (ogni volta diverso dal precedente)"""
    global cached_backgrounds, last_background_index
    try:
        # Usa la cache, ricarica se vuota
        if not cached_backgrounds:
            load_backgrounds_cache()
        
        if cached_backgrounds:
            # ALTERNANZA: usa il prossimo sfondo nella lista
            last_background_index = (last_background_index + 1) % len(cached_backgrounds)
            chosen = cached_backgrounds[last_background_index]
            logger.info(f"🎨 Background #{last_background_index}: {os.path.basename(chosen)}")
            return chosen
        else:
            logger.warning(f"⚠️ Nessun background in cache")
            return None
    except Exception as e:
        logger.error(f"❌ Errore background: {e}")
        return None

# Fallback path se nessun background trovato
BACKGROUND_PATH = os.path.join(SCRIPT_DIR, "assets/background.png")

# PostApp utilities
def extract_amazon_link(text: str):
    """Estrae il link Amazon dal testo (supporta link diretti e short: amzn.to, amzn.eu, a.co)"""
    if not text:
        return None
    # Link diretti Amazon (amazon.it, amazon.com, ecc.)
    pattern_full = r'https?://(?:www\.)?amazon\.[a-z.]+/[^\s\n]+'
    match = re.search(pattern_full, text)
    if match:
        link = match.group(0)
        logger.info(f"🔗 Link Amazon trovato: {link}")
        return link
    # Link corti Amazon (amzn.to, amzn.eu, amzn.com, a.co)
    pattern_short = r'https?://(?:amzn\.to|amzn\.eu|amzn\.com|a\.co)/[^\s\n]+'
    match = re.search(pattern_short, text)
    if match:
        link = match.group(0)
        logger.info(f"🔗 Link Amazon corto trovato: {link}")
        return link
    return None


def extract_price(text: str):
    """Estrae il prezzo corrente dal testo (cerca 'Lo paghi' prima)"""
    if not text:
        return None
    
    # Prima cerca "Lo paghi X€" - formato PremiumTools
    match = re.search(r'[Ll]o\s+paghi\s+(\d+[.,]\d{2})\s*€', text)
    if match:
        return f"{match.group(1).replace('.', ',')}€"
    
    # Pattern generici per prezzi
    patterns = [
        r'(\d+[.,]\d{2})\s*€',
        r'€\s*(\d+[.,]\d{2})',
        r'(\d+[.,]\d{2})\s*euro',
        r'(\d+)\s*€',
        r'€\s*(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            price = match.group(1)
            price = price.replace('.', ',')
            return f"{price}€"
    
    return None


def extract_discount_percentage(text: str):
    """Estrae la percentuale di sconto dal testo (es: -62%).
    Cerca solo nella riga che contiene '💥' o 'Lo paghi' per evitare
    di prendere percentuali dal nome del prodotto (es: '99% Acqua')."""
    if not text:
        return None
    # Cerca solo nelle righe di sconto, non nel nome del prodotto
    for line in text.split('\n'):
        if '💥' in line or 'paghi' in line.lower() or 'scont' in line.lower():
            match = re.search(r'(-\d+%)', line)
            if match:
                return match.group(1)
    # Fallback: cerca nella prima riga che ha -XX% senza testo dopo (fine riga)
    match = re.search(r'(-\d+%)\s*$', text, re.MULTILINE)
    if match:
        return match.group(1)
    return None


def extract_savings(text: str):
    """Estrae il risparmio dal testo (es: RISPARMI €6,16)"""
    if not text:
        return None
    # Cerca "RISPARMI €X" o "risparmi X€" (case insensitive)
    match = re.search(r'risparmi\w*\s+€\s*(\d+[.,]\d{2})', text, re.IGNORECASE)
    if match:
        return f"{match.group(1).replace('.', ',')}€"
    match = re.search(r'risparmi\w*\s+(\d+[.,]\d{2})\s*€', text, re.IGNORECASE)
    if match:
        return f"{match.group(1).replace('.', ',')}€"
    return None


def calculate_old_price(current_price_str: str, savings_str: str):
    """Calcola il prezzo vecchio sommando prezzo attuale + risparmio"""
    try:
        if not current_price_str or not savings_str:
            return None
        current = float(current_price_str.replace('€', '').replace(',', '.').strip())
        savings = float(savings_str.replace('€', '').replace(',', '.').strip())
        old = current + savings
        old_str = f"{old:.2f}".replace('.', ',')
        return f"{old_str}€"
    except Exception:
        return None


def calculate_percentage(current_price_str: str, old_price_str: str):
    """Calcola la percentuale di sconto da prezzo attuale e vecchio"""
    try:
        if not current_price_str or not old_price_str:
            return None
        current = float(current_price_str.replace('€', '').replace(',', '.').strip())
        old = float(old_price_str.replace('€', '').replace(',', '.').strip())
        if old <= 0 or current >= old:
            return None
        pct = round((old - current) / old * 100)
        return f"-{pct}%"
    except Exception:
        return None


def parse_manual_prices(text: str):
    """
    Estrae i prezzi scritti manualmente dall'utente.
    Formati supportati: '35 20', '35€ 20€', '35,99 20,50', 'prima 35 ora 20', ecc.
    Restituisce dict con price, old_price, percentage, savings — oppure None.
    """
    if not text:
        return None
    try:
        # Trova tutti i numeri (interi o decimali con punto o virgola)
        numbers = re.findall(r'\d+[.,]\d+|\d+', text)
        values = [float(n.replace(',', '.')) for n in numbers]
        if len(values) < 1:
            return None
        if len(values) >= 2:
            old_val  = max(values[0], values[1])
            new_val  = min(values[0], values[1])
            if old_val <= 0 or new_val <= 0 or new_val >= old_val:
                return None
            savings_val = round(old_val - new_val, 2)
            pct = round((old_val - new_val) / old_val * 100)
            return {
                'price':      f"{new_val:.2f}".replace('.', ','),
                'old_price':  f"{old_val:.2f}".replace('.', ','),
                'percentage': f"-{pct}%",
                'savings':    f"{savings_val:.2f}".replace('.', ','),
            }
        # Solo un numero → prezzo senza confronto
        return {
            'price':      f"{values[0]:.2f}".replace('.', ','),
            'old_price':  None,
            'percentage': None,
            'savings':    None,
        }
    except Exception:
        return None


FONT_PATH = os.path.join(SCRIPT_DIR, "fonts/Oswald-Bold.ttf")
FONT_PATH_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Cache font caricati una sola volta all'avvio
def _load_fonts():
    fp = FONT_PATH if os.path.exists(FONT_PATH) else FONT_PATH_FALLBACK
    try:
        return (
            ImageFont.truetype(fp, size=130),
            ImageFont.truetype(fp, size=80),
            ImageFont.truetype(fp, size=65),
        )
    except Exception:
        try:
            return (
                ImageFont.truetype(FONT_PATH_FALLBACK, size=130),
                ImageFont.truetype(FONT_PATH_FALLBACK, size=80),
                ImageFont.truetype(FONT_PATH_FALLBACK, size=65),
            )
        except Exception:
            d = ImageFont.load_default()
            return (d, d, d)

_FONT_BIG, _FONT_SMALL, _FONT_LINE3 = _load_fonts()

def format_price_euro_first(price_str: str) -> str:
    """Converte '22,97€' in '€22,97'"""
    if not price_str:
        return price_str
    # Rimuove € finale e aggiunge davanti
    clean = price_str.replace('€', '').strip()
    return f"€{clean}"

def draw_text_with_shadow(draw, pos, text, font, fill, shadow_color=(0, 0, 0, 255), shadow_offset=3):
    """Disegna testo senza contorno"""
    x, y = pos
    draw.text((x, y), text, font=font, fill=fill)


def draw_price_overlay(image: Image.Image, price: str, savings: str, percentage: str, old_price: str, image_top_y: int = None, text_zone_top: int = None, text_zone_bottom: int = None, text_gap: int = 20) -> Image.Image:
    """
    Disegna il prezzo/sconto nella zona testo (testo centrato verticalmente).
    text_zone_top / text_zone_bottom definiscono la zona dove il testo viene centrato.
    """
    available_variants = []
    if price:
        available_variants.append(0)
    if price and not percentage:
        available_variants.append(2)
    if price and percentage:
        available_variants.append(3)
    if percentage:
        available_variants.append(4)

    if not available_variants:
        return image

    variant = random.choice(available_variants)

    line3 = ""
    if variant == 0:
        pct_clean = percentage.lstrip('-') if percentage else None
        line1 = "Lo paghi"
        line2 = format_price_euro_first(price)
        line3 = f"Sconto {pct_clean}" if pct_clean else ""
    elif variant == 2:
        line1 = ""
        line2 = format_price_euro_first(price)
    elif variant == 3:
        pct_clean = percentage.lstrip('-') if percentage else ""
        line1 = f"Sconto {pct_clean}" if pct_clean else "Offerta"
        line2 = format_price_euro_first(price)
    else:  # variant == 4
        line1 = "Scontato del"
        line2 = percentage.lstrip('-') if percentage else percentage

    logger.info(f"🎨 [Overlay] Variante {variant}: '{line1}' '{line2}' '{line3}'")

    img = image if image.mode == 'RGB' else image.convert("RGB")
    draw = ImageDraw.Draw(img)
    width, height = img.size

    font_big   = _FONT_BIG
    font_small = _FONT_SMALL
    font_line3 = _FONT_LINE3

    # Misura altezze testo
    tmp = draw
    text1_h = (tmp.textbbox((0,0), line1, font=font_small)[3] - tmp.textbbox((0,0), line1, font=font_small)[1]) if line1 else 0
    text2_h = tmp.textbbox((0,0), line2, font=font_big)[3] - tmp.textbbox((0,0), line2, font=font_big)[1]
    text3_h = (tmp.textbbox((0,0), line3, font=font_line3)[3] - tmp.textbbox((0,0), line3, font=font_line3)[1]) if line3 else 0

    gap = 30
    total_h = (text1_h + gap if line1 else 0) + text2_h + (gap + text3_h if line3 else 0)

    center_x = width // 2

    # Posizione verticale
    if text_zone_top is not None and text_zone_bottom is not None:
        zone_h = text_zone_bottom - text_zone_top
        y = text_zone_top + (zone_h - total_h) // 2
    elif image_top_y is not None:
        # Testo posizionato text_gap px sopra il top del prodotto, clamped al margine
        y = max(40, image_top_y - total_h - text_gap)
    else:
        y = 40

    # Nero
    NERO           = (0, 0, 0, 255)
    color_label    = NERO
    color_price    = NERO
    color_discount = NERO

    if line1:
        b = tmp.textbbox((0,0), line1, font=font_small)
        w1 = b[2] - b[0]
        draw_text_with_shadow(draw, (center_x - w1 // 2, y), line1, font_small, color_label)
        y += text1_h + gap

    b2 = tmp.textbbox((0,0), line2, font=font_big)
    w2 = b2[2] - b2[0]
    draw_text_with_shadow(draw, (center_x - w2 // 2, y), line2, font_big, color_price)
    y += text2_h + gap

    if line3:
        b3 = tmp.textbbox((0,0), line3, font=font_line3)
        w3 = b3[2] - b3[0]
        draw_text_with_shadow(draw, (center_x - w3 // 2, y), line3, font_line3, color_discount)

    return img


def draw_coupon_badge(image: Image.Image) -> Image.Image:
    """Aggiunge un badge 'COUPON' rosso in alto a sinistra quando il post ha un coupon."""
    try:
        if image.mode != 'RGB':
            image = image.convert("RGB")
        draw = ImageDraw.Draw(image)
        fp = FONT_PATH if os.path.exists(FONT_PATH) else FONT_PATH_FALLBACK
        try:
            font_big = ImageFont.truetype(fp, size=52)
            font_small = ImageFont.truetype(fp, size=32)
        except Exception:
            font_big = ImageFont.load_default()
            font_small = font_big

        scissor = "✂"
        label = "COUPON"

        # Misure testo
        bb1 = draw.textbbox((0, 0), scissor, font=font_big)
        bb2 = draw.textbbox((0, 0), label, font=font_small)
        w1 = bb1[2] - bb1[0]
        w2 = bb2[2] - bb2[0]
        h1 = bb1[3] - bb1[1]
        h2 = bb2[3] - bb2[1]

        pad_x, pad_y = 28, 18
        badge_w = max(w1, w2) + pad_x * 2
        badge_h = h1 + h2 + pad_y * 3

        # Posizione: angolo in alto a sinistra con margine
        x0, y0 = 40, 40
        x1, y1 = x0 + badge_w, y0 + badge_h

        # Rettangolo rosso con bordi arrotondati simulati
        draw.rectangle([x0, y0, x1, y1], fill=(220, 30, 30))
        # Bordo bianco
        draw.rectangle([x0, y0, x1, y1], outline=(255, 255, 255), width=3)

        # Forbici
        draw.text((x0 + (badge_w - w1) // 2, y0 + pad_y), scissor, font=font_big, fill=(255, 255, 255))
        # Scritta COUPON
        draw.text((x0 + (badge_w - w2) // 2, y0 + pad_y + h1 + 6), label, font=font_small, fill=(255, 255, 255))

        logger.info(f"✅ [Coupon] Badge aggiunto ({x0},{y0})")
        return image
    except Exception as e:
        logger.error(f"❌ [Coupon] Errore badge: {e}")
        return image


def draw_affiliate_label(image: Image.Image, content_bottom: int = 1250, inner_margin: int = 95) -> Image.Image:
    """Aggiunge la scritta 'link affiliato' DENTRO la zona bianca, in basso a destra"""
    try:
        if image.mode != 'RGB':
            image = image.convert("RGB")
        width, height = image.size

        fp = FONT_PATH if os.path.exists(FONT_PATH) else FONT_PATH_FALLBACK
        try:
            font = ImageFont.truetype(fp, size=28)
        except Exception:
            font = ImageFont.load_default()

        label = "link affiliato"
        draw = ImageDraw.Draw(image)

        bbox = draw.textbbox((0, 0), label, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        # Posizione: dentro la zona bianca, angolo basso a destra
        x = width - inner_margin - text_w
        y = content_bottom - text_h - 14

        # Ombra bianca sottile per staccarsi dallo sfondo
        for dx, dy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
            draw.text((x + dx, y + dy), label, font=font, fill=(255, 255, 255))

        # Testo principale: nero
        draw.text((x, y), label, font=font, fill=(0, 0, 0))

        logger.info(f"✅ [Affiliate] 'link affiliato' dentro zona bianca ({x},{y})")
        return image
    except Exception as e:
        logger.error(f"❌ [Affiliate] Errore: {e}")
        return image


def extract_brand_name(text: str, amazon_url: str = None):
    """Estrae il nome del brand dal testo (solo prima riga, pulito)"""
    if not text:
        return None
    
    # Prendi solo la prima riga
    first_line = text.split('\n')[0].strip()
    
    if not first_line:
        return None
    
    # Rimuovi prezzi (es: "29.99€", "€29,99", "29,99 €")
    brand_text = re.sub(r'[\d.,]+\s*[€$]', '', first_line)
    brand_text = re.sub(r'[€$]\s*[\d.,]+', '', brand_text)
    
    # Rimuovi numeri isolati rimasti
    brand_text = re.sub(r'\b\d+[.,]?\d*\b', '', brand_text)
    
    # Pulisci spazi multipli e ai lati
    brand_text = re.sub(r'\s+', ' ', brand_text).strip()
    
    # Se il testo è molto lungo, prendi solo le prime parole (probabilmente il brand)
    words = brand_text.split()
    if len(words) > 5:
        # Prendi le prime 3-4 parole come brand
        brand_text = ' '.join(words[:4])
    
    return brand_text if brand_text else None


# Client PostTap persistente — mantiene i cookie aggiornati tra una chiamata e l'altra
_posttap_client: httpx.AsyncClient | None = None

def _get_posttap_client() -> httpx.AsyncClient:
    """Restituisce il client PostTap persistente (lo crea se non esiste ancora)."""
    global _posttap_client
    if _posttap_client is None or _posttap_client.is_closed:
        cookies = get_posttap_cookies()
        _posttap_client = httpx.AsyncClient(
            timeout=15,
            cookies=cookies,
            follow_redirects=True,
        )
        logger.info(f"🆕 [PostTap] Client creato con {len(cookies)} cookie")
    return _posttap_client

def _save_client_cookies():
    """Salva i cookie aggiornati dal client persistente solo su file locale (NON blocca l'event loop)."""
    global _posttap_client
    if _posttap_client is None:
        return
    try:
        jar = dict(_posttap_client.cookies)
        if not jar:
            return
        cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
        cookies_file = os.path.join(os.path.dirname(__file__), 'posttap_cookies.txt')
        with open(cookies_file, 'w') as f:
            f.write(cookie_str)
        logger.info(f"💾 [PostTap] Cookie salvati su file locale: {list(jar.keys())}")
    except Exception as e:
        logger.warning(f"⚠️ [PostTap] Errore salvataggio cookie client: {e}")

async def _save_client_cookies_async():
    """Salva i cookie su file locale + Gist in background (non blocca l'event loop)."""
    global _posttap_client
    if _posttap_client is None:
        return
    try:
        jar = dict(_posttap_client.cookies)
        if not jar:
            return
        cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
        cookies_file = os.path.join(os.path.dirname(__file__), 'posttap_cookies.txt')
        with open(cookies_file, 'w') as f:
            f.write(cookie_str)
        logger.info(f"💾 [PostTap] Cookie salvati su file locale: {list(jar.keys())}")
        # Gist save in thread separato — non blocca event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, save_cookies_to_gist, cookie_str)
    except Exception as e:
        logger.warning(f"⚠️ [PostTap] Errore salvataggio async cookie: {e}")

# Cache cookies PostTap (legacy — usato solo da get_posttap_cookies)
_posttap_cookies = None

GIST_FILENAME = "posttap_cookies.txt"

def _load_cookies_from_gist() -> str:
    """Scarica i cookie dal GitHub Gist privato (storage persistente)."""
    github_token = os.getenv('GITHUB_TOKEN', '')
    gist_id = os.getenv('GIST_ID', '')
    if not github_token or not gist_id:
        return ''
    try:
        r = httpx.get(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"},
            timeout=10
        )
        if r.status_code == 200:
            content = r.json().get('files', {}).get(GIST_FILENAME, {}).get('content', '')
            if content:
                logger.info("🍪 [Gist] Cookie PostTap caricati da GitHub Gist")
                return content.strip()
    except Exception as e:
        logger.warning(f"⚠️ [Gist] Errore lettura: {e}")
    return ''

def save_cookies_to_gist(cookie_str: str):
    """Salva i cookie aggiornati nel GitHub Gist (sopravvive ai riavvii del container)."""
    github_token = os.getenv('GITHUB_TOKEN', '')
    gist_id = os.getenv('GIST_ID', '')
    if not github_token or not gist_id:
        logger.warning("⚠️ [Gist] GITHUB_TOKEN o GIST_ID non configurati — cookie NON persistenti")
        return
    try:
        r = httpx.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"},
            json={"files": {GIST_FILENAME: {"content": cookie_str}}},
            timeout=10
        )
        if r.status_code == 200:
            logger.info("✅ [Gist] Cookie salvati su GitHub Gist (persistenti)")
        else:
            logger.warning(f"⚠️ [Gist] Errore salvataggio: {r.status_code} {r.text[:100]}")
    except Exception as e:
        logger.warning(f"⚠️ [Gist] Eccezione salvataggio: {e}")

def _init_cookies_from_gist():
    """Chiamato UNA SOLA VOLTA all'avvio: legge Gist SOLO se il file locale è assente/vuoto."""
    cookies_file = os.path.join(os.path.dirname(__file__), 'posttap_cookies.txt')
    # Se il file locale esiste e ha contenuto, usalo (non sovrascrivere con Gist potenzialmente vecchio)
    if os.path.exists(cookies_file):
        try:
            with open(cookies_file, 'r') as f:
                existing = f.read().strip()
            if existing:
                logger.info("🍪 [Avvio] File cookie locale trovato — Gist ignorato")
                return
        except Exception:
            pass
    # Solo se il file è assente o vuoto, prova il Gist
    cookies_str = _load_cookies_from_gist()
    if not cookies_str:
        return
    try:
        with open(cookies_file, 'w') as f:
            f.write(cookies_str)
        logger.info("✅ [Avvio] Cookie Gist salvati su file locale (file era assente)")
    except Exception as e:
        logger.warning(f"⚠️ [Avvio] Impossibile salvare cookie su file: {e}")

def get_posttap_cookies():
    """Legge i cookie dal file locale o env var. Restituisce dict."""
    cookies_str = get_posttap_cookie_string()
    cookies = {}
    if cookies_str:
        for cookie_pair in cookies_str.split(';'):
            cookie_pair = cookie_pair.strip()
            if '=' in cookie_pair:
                key, value = cookie_pair.split('=', 1)
                cookies[key.strip()] = value.strip()
    return cookies

def get_posttap_cookie_string() -> str:
    """Legge i cookie dal file locale o env var. Restituisce stringa grezza (per Cookie header)."""
    # Priorità 1: file locale
    cookies_file = os.path.join(os.path.dirname(__file__), 'posttap_cookies.txt')
    if os.path.exists(cookies_file):
        try:
            with open(cookies_file, 'r') as f:
                cookies_str = f.read().strip()
            if cookies_str:
                logger.info("🍪 Cookie PostTap caricati da file locale")
                return cookies_str
        except Exception as e:
            logger.warning(f"⚠️ Errore lettura file cookie: {e}")
    # Priorità 2: variabile d'ambiente
    env_cookies = os.getenv('POSTTAP_COOKIES', '').strip()
    if env_cookies:
        logger.info("🍪 Cookie PostTap caricati da variabile d'ambiente")
    return env_cookies

async def create_posttap_shortlink(url: str, name: str = "link"):
    """Trasforma un URL Amazon in shortlink con PostTap.
    Chiama PostTap direttamente con i cookie dal file posttap_cookies.txt.
    Nessuna dipendenza da Replit (il bot gira in autonomia su Render)."""
    try:
        # ── CHIAMATA DIRETTA a PostTap (nessuna dipendenza da Replit) ──────
        # PostTap NON blocca gli IP dei datacenter (testato: funziona da cloud).
        # Il proxy Replit è stato rimosso perché Replit verrà disattivato.
        cookie_str = get_posttap_cookie_string()
        if not cookie_str:
            logger.warning("⚠️ Nessun cookie PostTap configurato")
            return url

        clean_url = url
        if 'tag=' not in url.lower():
            clean_url = url.split('?')[0] if '?' in url else url
            if 'ref=' in url:
                clean_url = re.sub(r'/ref=[^/?]+', '', clean_url)

        logger.info(f"🔗 [PostTap] Shortlink diretto per: {clean_url}")

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            payload = {"name": name, "url": clean_url, "tags": []}
            response = await client.post(
                'https://creators.posttap.com/api/create-shortlink',
                json=payload,
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json, text/plain, */*',
                    'Origin': 'https://creators.posttap.com',
                    'Referer': 'https://creators.posttap.com/dashboard',
                    'Cookie': cookie_str,
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                }
            )

            logger.info(f"📡 [PostTap] Status: {response.status_code}")

            if response.status_code in [200, 201]:
                data = response.json()
                obj = data.get('object', {})
                shortlink = (obj.get('shortlink') or obj.get('shortLink') or obj.get('short_url')
                             or data.get('shortlink') or data.get('shortLink'))
                if shortlink:
                    if not shortlink.startswith('http'):
                        shortlink = f"https://{shortlink}"
                    logger.info(f"✅ [PostTap] Shortlink creato: {shortlink}")
                    return shortlink
                logger.warning(f"⚠️ [PostTap] Shortlink non in risposta: {data}")
            else:
                logger.error(f"❌ [PostTap] Errore {response.status_code}: {response.text[:200]}")

        return url
    except Exception as e:
        logger.error(f"❌ [PostTap] Eccezione: {e}")
        return url

async def cmd_rinnova_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /rinnovalink — rinnova i cookie PostTap via Telegram"""
    msg = update.effective_message
    chat_id = msg.chat_id

    if _renewal_state["phase"] in ("starting", "waiting_otp"):
        await msg.reply_text("⏳ Rinnovo già in corso. Se aspetti il codice 2FA, mandamelo direttamente.")
        return

    # Reset stato
    _renewal_state["phase"] = "idle"
    _renewal_state["otp_event"].clear()
    _renewal_state["otp_value"] = None
    _renewal_state["final_cookies"] = None
    _renewal_state["error"] = None
    _renewal_state["chat_id"] = chat_id

    email = os.environ.get("POSTTAP_EMAIL", "")
    password = os.environ.get("POSTTAP_PASSWORD", "")
    if not email or not password:
        await msg.reply_text("❌ Email o password PostTap non configurati. Contattami.")
        return

    status_msg = await msg.reply_text("🔄 Avvio login PostTap... attendi circa 30 secondi.")
    _renewal_state["phase"] = "starting"

    # Avvia login in thread separato
    t = threading.Thread(target=_renewal_thread, daemon=True)
    t.start()

    # Aggiorna il messaggio di stato mentre il login procede
    asyncio.create_task(_poll_renewal_state(context.bot, chat_id, status_msg.message_id))
    logger.info(f"🔑 [Rinnovo] Avviato da chat {chat_id}")


async def _poll_renewal_state(bot, chat_id: int, status_msg_id: int):
    """Controlla periodicamente lo stato del rinnovo e informa l'utente via Telegram"""
    import telegram
    otp_msg_sent = False
    for _ in range(300):  # max 15 minuti (copre attesa OTP)
        await asyncio.sleep(3)
        ph = _renewal_state["phase"]

        if ph == "waiting_otp":
            if not otp_msg_sent:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg_id,
                        text="📱 Amazon ha richiesto il codice 2FA.\nMandami il codice a 6 cifre:"
                    )
                    otp_msg_sent = True
                except Exception:
                    pass
            continue  # aspetta finché il login finisce

        elif ph == "done":
            # Testa subito se i cookie funzionano davvero
            test_result = "⏳ test in corso..."
            try:
                test_url = "https://www.amazon.it/dp/B0CX6FWGYS"
                test_link = await create_posttap_shortlink(test_url, name="test-rinnovo")
                if test_link and test_link != test_url:
                    test_result = f"✅ Link testato: {test_link}"
                else:
                    test_result = "⚠️ Cookie salvati ma il test del link non ha funzionato — riprova /rinnovalink"
            except Exception as te:
                test_result = f"⚠️ Errore nel test: {te}"

            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=f"✅ Login riuscito! Cookie rinnovati e salvati.\n\n{test_result}\n\nI prossimi post useranno il link affiliato automaticamente."
                )
            except Exception:
                pass
            logger.info("✅ [Rinnovo] Completato con successo")
            return

        elif ph == "error":
            err = _renewal_state.get("error", "Errore sconosciuto")
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=f"❌ Errore rinnovo: {err}\n\nRiprova con /rinnovalink"
                )
            except Exception:
                pass
            logger.error(f"❌ [Rinnovo] Fallito: {err}")
            return


async def cmd_test_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /testlink — testa PostTap e mostra cosa succede"""
    msg = update.effective_message
    await msg.reply_text("🔍 Test PostTap in corso...")
    try:
        cookies = get_posttap_cookies()
        cookie_keys = list(cookies.keys())
        await msg.reply_text(f"🍪 Cookie trovati: {cookie_keys}")
        
        result = await create_posttap_shortlink("https://www.amazon.it/dp/B0TEST123", name="test")
        if "amzlink" in result or "posttap" in result:
            await msg.reply_text(f"✅ PostTap funziona!\n{result}")
        else:
            await msg.reply_text(f"❌ PostTap ha restituito l'URL originale\nCookies: {cookie_keys}")
    except Exception as e:
        await msg.reply_text(f"❌ Errore: {e}")

async def cmd_set_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /setcookie <stringa_cookie> — salva manualmente i cookie PostTap"""
    msg = update.effective_message
    args = context.args

    if not args:
        await msg.reply_text(
            "📋 <b>Come rinnovare i cookie:</b>\n\n"
            "1. Apri PostApp nel browser\n"
            "2. Fai login\n"
            "3. Premi F12 → Application → Cookies\n"
            "4. Copia la stringa dei cookie\n"
            "5. Inviami: <code>/setcookie tuaStringaCookie</code>",
            parse_mode="HTML"
        )
        return

    cookie_str = " ".join(args).strip()
    if not cookie_str:
        await msg.reply_text("❌ Cookie vuoti. Riprova con /setcookie seguita dalla stringa.")
        return

    try:
        cookies_file = os.path.join(os.path.dirname(__file__), "posttap_cookies.txt")
        with open(cookies_file, "w") as f:
            f.write(cookie_str)
        global _posttap_cookies, _posttap_client
        _posttap_cookies = None  # reset cache legacy
        # Reset client persistente — forza rilettura cookie da file
        if _posttap_client and not _posttap_client.is_closed:
            await _posttap_client.aclose()
        _posttap_client = None
        # Salva anche su Gist (in background)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, save_cookies_to_gist, cookie_str)
        logger.info(f"✅ [Cookie] Salvati manualmente + client resettato: {cookie_str[:60]}...")
        await msg.reply_text("✅ Cookie salvati! Prova subito un link.")
    except Exception as e:
        logger.error(f"❌ [Cookie] Errore salvataggio: {e}")
        await msg.reply_text(f"❌ Errore nel salvataggio: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    await update.message.reply_text(
        "👋 Ciao! Sono un bot che brandizza le tue immagini.\n\n"
        "🎨 <b>Come usarmi:</b>\n"
        "1️⃣ Invia il tuo <b>brand/sfondo</b> con il comando /brand\n"
        "2️⃣ Invia le <b>immagini</b> che vuoi incollarci sopra\n"
        "3️⃣ Ricevi le immagini brandizzate! ✨\n\n"
        "Oppure invia solo immagini per usare il brand di default.",
        parse_mode="HTML"
    )
    logger.info(f"Start comando ricevuto da {update.effective_user.username}")

async def set_brand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /brand - imposta il brand personalizzato"""
    user_id = update.effective_user.id
    await update.message.reply_text(
        "📸 Ok! Adesso inviami l'immagine del brand/sfondo che vuoi usare.\n\n"
        "Successivamente, quando invierai immagini normali, le incollerò su questo brand! 🎨"
    )
    # Imposta lo stato per il prossimo messaggio
    context.user_data['waiting_for_brand'] = True
    logger.info(f"Utente {user_id} in attesa di inviare il brand")


# Funzione per creare un video da un'immagine
def create_video_from_image(image_pil, duration_sec=1):
    """Crea un file MP4 da un'immagine PIL usando ffmpeg - 720p per risparmiare RAM"""
    import subprocess, gc
    tmp_img = None
    tmp_video = None
    try:
        # Ridimensiona a 720x1280 per risparmiare RAM (formato verticale Stories)
        img_resized = image_pil.resize((720, 1280), Image.Resampling.LANCZOS)

        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as f:
            tmp_img = f.name
        img_resized.save(tmp_img, 'JPEG', quality=85)
        del img_resized
        gc.collect()

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
            tmp_video = f.name

        result = subprocess.run([
            'ffmpeg', '-y',
            '-loop', '1',
            '-framerate', '25',
            '-i', tmp_img,
            '-c:v', 'libx264',
            '-t', str(duration_sec),
            '-pix_fmt', 'yuv420p',
            '-preset', 'ultrafast',
            '-tune', 'stillimage',
            '-crf', '28',
            '-r', '25',
            tmp_video
        ], capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            logger.error(f"❌ ffmpeg error: {result.stderr[-300:]}")
            return None

        logger.info(f"✅ Video creato con ffmpeg: {tmp_video}")
        return tmp_video
    except Exception as e:
        logger.error(f"❌ Errore creazione video: {e}")
        return None
    finally:
        if tmp_img and os.path.exists(tmp_img):
            os.unlink(tmp_img)

async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce le immagini ricevute - semplice e affidabile"""
    global processed_updates, processed_updates_queue
    global processed_messages, processed_messages_queue
    global processed_media_groups, processed_media_groups_queue
    
    msg = update.message
    if not msg:
        return
    
    # CRITICAL: Ignora messaggi dal bot stesso (evita duplicati!)
    if msg.from_user and msg.from_user.is_bot:
        return
    
    chat_id = msg.chat_id
    message_id = msg.message_id
    update_id = update.update_id
    
    # PROTEZIONE MESSAGGI: Skip se message_id già processato
    msg_key = (chat_id, message_id)
    if msg_key in processed_messages:
        logger.info(f"⚠️ [#{update_id}] SKIP - Messaggio già processato: {message_id}")
        return
    
    # Aggiungi a cache messaggi
    if len(processed_messages_queue) >= 500:
        old_key = processed_messages_queue.popleft()
        processed_messages.discard(old_key)
    processed_messages.add(msg_key)
    processed_messages_queue.append(msg_key)
    
    # PROTEZIONE ALBUM: Se è parte di un album, processa solo la prima foto
    media_group_id = msg.media_group_id
    
    if media_group_id:
        group_key = (chat_id, media_group_id)
        if group_key in processed_media_groups:
            logger.info(f"⚠️ [#{update_id}] SKIP - Album già processato: {media_group_id}")
            return
        
        # Aggiungi a cache
        if len(processed_media_groups_queue) >= 200:
            old_key = processed_media_groups_queue.popleft()
            processed_media_groups.discard(old_key)
        processed_media_groups.add(group_key)
        processed_media_groups_queue.append(group_key)
    
    # PROTEZIONE UPDATE: Skip se update_id già processato
    if update_id in processed_updates:
        logger.info(f"⚠️ [#{update_id}] SKIP - Update già processato")
        return
    
    if len(processed_updates_queue) >= 500:
        old_id = processed_updates_queue.popleft()
        processed_updates.discard(old_id)
    processed_updates.add(update_id)
    processed_updates_queue.append(update_id)
    
    user_id = update.effective_user.id
    logger.info(f"📥 [#{update_id}] MESSAGGIO - User: {user_id}, chat: {chat_id}, msg_id: {message_id}")
    
    # Tenta di ottenere l'immagine
    offer_bytes = None
    # Prendi il testo da qualsiasi fonte possibile (testo o caption)
    offer_text = msg.text or msg.caption or ""

    # --- INTERCETTA OTP PER RINNOVO COOKIE ---
    if (
        _renewal_state["phase"] == "waiting_otp"
        and msg.text
        and not msg.photo
        and _renewal_state.get("chat_id") == chat_id
    ):
        otp_code = msg.text.strip()
        if otp_code.isdigit() and 4 <= len(otp_code) <= 8:
            _renewal_state["otp_value"] = otp_code
            _renewal_state["otp_event"].set()
            await msg.reply_text("✅ Codice inviato! Attendi il completamento del login...")
            logger.info(f"📱 [Rinnovo] OTP ricevuto: {otp_code}")
            return

    # Se il messaggio è un inoltrato, a volte il testo è nel messaggio originale
    if not offer_text and msg.forward_origin:
        logger.info("🔍 Messaggio inoltrato con origin, testo potrebbe essere nascosto")
    
    # PULIZIA TESTO: Rimuovi messaggi di servizio PremiumTools se presenti
    if "inoltrato da" in offer_text.lower() or "premiumtools" in offer_text.lower():
        logger.info("🧹 Pulizia testo inoltrato...")
    
    logger.info(f"📝 Testo ricevuto: '{offer_text[:100]}...'")

    try:
        # 1. Controlla se c'è una foto diretta
        if msg.photo:
            logger.info("✅ Foto diretta trovata")
            photo = msg.photo[-1]
            file = await photo.get_file()
            offer_bytes = await file.download_as_bytearray()
        
        # 2. Se no, controlla se c'è un'immagine nel link preview
        elif msg.link_preview_options and msg.link_preview_options.url:
            url = msg.link_preview_options.url
            logger.info(f"✅ Link preview: {url[:60]}...")
            
            # Download con client HTTP condiviso (più veloce!)
            try:
                client = get_http_client()
                resp = await client.get(url)
                if resp.status_code == 200:
                    offer_bytes = resp.content
                    logger.info(f"✅ Scaricato: {len(offer_bytes)} bytes")
            except Exception as e:
                logger.error(f"❌ Errore download: {e}")
    except Exception as e:
        logger.error(f"❌ Errore handler: {e}", exc_info=True)
    
    # 3. Se non c'è niente, rifiuta
    if not offer_bytes:
        logger.info(f"⚠️ Nessuna immagine trovata")
        return
    
    logger.info(f"✅ Ho l'immagine! Inizio elaborazione...")
    try:
        # Determino se è una foto diretta (senza testo e senza link preview)
        has_link_preview = msg.link_preview_options and msg.link_preview_options.url
        manual_prices = parse_manual_prices(offer_text) if offer_text else None
        is_manual_product = msg.photo and offer_text and manual_prices is not None
        is_direct_photo = msg.photo and not offer_text and not has_link_preview

        logger.info(f"🔍 is_direct_photo={is_direct_photo}, has_link_preview={has_link_preview}, is_manual_product={is_manual_product}")

        # Se è una FOTO DIRETTA (senza testo) → salva come background
        if is_direct_photo and not has_link_preview and not is_manual_product:
            logger.info("📸 Foto diretta ricevuta → Salvo come background automaticamente")
            
            # Resetta il flag /brand in ogni caso
            context.user_data['waiting_for_brand'] = False
            
            status = await msg.reply_text("⏳ Sto salvando la tua immagine come background...")
            
            # Salva il brand su file (persistente)
            loop = asyncio.get_event_loop()
            save_success = await loop.run_in_executor(thread_pool, lambda: save_user_brand(user_id, offer_bytes))
            
            if save_success:
                logger.info(f"✅ Background salvato per utente {user_id}")
                
                await msg.reply_photo(
                    photo=BytesIO(offer_bytes),
                    caption="✅ Background salvato! Adesso inviami le immagini da incollare sopra 🎨"
                )
            else:
                await msg.reply_text("❌ Errore nel salvataggio. Riprova!")
            
            await status.delete()
            return
        
        # Se c'è un link preview o testo → SEMPRE elabora l'immagine (mai salvare come background)
        # Resetta il flag /brand per evitare confusione
        context.user_data['waiting_for_brand'] = False
        
        # Altrimenti, sovrapponi l'immagine sul brand
        status = await msg.reply_text("⏳ Sto elaborando la tua immagine...")
        
        # Carica immagine in thread pool
        loop = asyncio.get_event_loop()
        _ob = offer_bytes
        offer_img = await loop.run_in_executor(thread_pool, lambda: Image.open(BytesIO(_ob)))
        del _ob  # libera offer_bytes dalla RAM subito (può essere svariati MB)
        import gc as _gc_main; _gc_main.collect()
        
        logger.info(f"Immagine ricevuta: {offer_img.size[0]}x{offer_img.size[1]}")
        
        # Decido quale sfondo usare — carica sempre dal disco (non tenere in RAM)
        background = None
        user_brand_bytes = await loop.run_in_executor(thread_pool, lambda: load_user_brand(user_id))
        if user_brand_bytes:
            try:
                background = Image.open(BytesIO(user_brand_bytes))
                user_brand_bytes = None  # libera subito
                logger.info(f"🎨 Usando brand personalizzato per utente {user_id}")
            except Exception as e:
                logger.warning(f"⚠️ Errore apertura brand: {e}")
                background = None
        if background is None:
            background = Image.new('RGB', (1080, 1920), color=(255, 255, 255))
            logger.info("🤍 Sfondo bianco puro creato")
        
        # Sfondo già in RGB e già ridimensionato a 1080x1920 dalla cache
        TARGET_SIZE = (1080, 1920)
        if background.mode != 'RGB' or background.size != TARGET_SIZE:
            background = background.convert('RGB').resize(TARGET_SIZE, Image.Resampling.LANCZOS)
        
        logger.info(f"🔍 Background size: {background.size}")
        bg_width, bg_height = background.size
        
        margin = 50
        # Sfondo bianco — usa quasi tutta l'altezza
        CONTENT_TOP    = 80
        CONTENT_BOTTOM = 1820
        TEXT_BLOCK_H   = 230   # altezza stimata per il blocco testo prezzo/sconto
        TEXT_GAP       = 40    # gap tra prodotto e testo
        
        available_width  = bg_width - (2 * margin)
        content_h        = CONTENT_BOTTOM - CONTENT_TOP   # 1570px

        # Ridimensiona prodotto nel totale disponibile (larghezza piena, altezza = content - testo)
        offer_width, offer_height = offer_img.size
        logger.info(f"🔍 Immagine originale: {offer_width}x{offer_height}")
        max_product_h = content_h - TEXT_BLOCK_H - TEXT_GAP
        ratio = min(available_width / offer_width, max_product_h / offer_height)
        logger.info(f"🔍 Ratio: {ratio}")
        
        new_width  = int(offer_width  * ratio)
        new_height = int(offer_height * ratio)
        
        # Blocco totale (prodotto + gap + testo) leggermente in alto rispetto al centro
        total_block_h = new_height + TEXT_GAP + TEXT_BLOCK_H
        block_start_y = CONTENT_TOP + max(0, (content_h - total_block_h) // 2 - 120)
        product_y     = block_start_y
        TEXT_ZONE_TOP    = product_y + new_height + TEXT_GAP
        TEXT_ZONE_BOTTOM = TEXT_ZONE_TOP + TEXT_BLOCK_H
        
        logger.info(f"🔍 Prodotto: {new_width}x{new_height}, top y={product_y}, bottom={product_y+new_height}")
        logger.info(f"🔍 Testo zona: {TEXT_ZONE_TOP}-{TEXT_ZONE_BOTTOM}, blocco centrato: start={block_start_y}")
        
        logger.info(f"🔄 Ridimensionamento in thread pool: {new_width}x{new_height}...")
        
        # Estrai info prezzo PRIMA di process_image (closure le userà)
        if is_manual_product and manual_prices:
            # Prezzi inseriti manualmente dall'utente nella didascalia
            _price      = manual_prices['price']
            _old_price  = manual_prices['old_price']
            _percentage = manual_prices['percentage']
            _savings    = manual_prices['savings']
            logger.info(f"✏️ Prezzi manuali: attuale={_price}, vecchio={_old_price}, sconto={_percentage}")
        else:
            _price = extract_price(offer_text)
            _savings = extract_savings(offer_text)
            _percentage = extract_discount_percentage(offer_text)
            _old_price = calculate_old_price(_price, _savings)
            if not _percentage and _price and _old_price:
                _percentage = calculate_percentage(_price, _old_price)
                if _percentage:
                    logger.info(f"🧮 Percentuale calcolata automaticamente: {_percentage}")
        logger.info(f"💰 Prezzo: {_price}, Risparmio: {_savings}, %: {_percentage}, Vecchio: {_old_price}")

        # Rileva coupon nel testo del post
        _has_coupon = bool(offer_text and any(
            kw in offer_text.lower()
            for kw in ("scansiona coupon", "sfoglia coupon", "applica coupon", "coupon", "clip coupon")
        ))
        if _has_coupon:
            logger.info("🎟️ Coupon rilevato nel post — aggiunto badge")

        # Funzione per elaborare immagine in thread pool
        def process_image():
            import gc as _gc
            offer_img_resized = None
            result = None
            try:
                thread_log("🎨 [process_image] Inizio ridimensionamento...")
                offer_img_resized = offer_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                thread_log(f"✅ [process_image] Ridimensionata a {new_width}x{new_height}")
                
                x_offset = margin + (available_width - new_width) // 2
                thread_log(f"✅ [process_image] Posizionata in ({x_offset}, {product_y})")
                
                thread_log("🔄 [process_image] Usando background direttamente...")
                result = background
                thread_log(f"✅ [process_image] Background pronto, mode: {result.mode}")
                
                if offer_img_resized.mode == 'RGBA':
                    result.paste(offer_img_resized, (x_offset, product_y), offer_img_resized)
                else:
                    result.paste(offer_img_resized, (x_offset, product_y))
                # Libera subito l'immagine ridimensionata
                offer_img_resized.close()
                offer_img_resized = None
                _gc.collect()
                thread_log("✅ [process_image] Immagine incollata")
                
                if _price or _savings or _percentage:
                    thread_log(f"🎨 [process_image] Overlay testo...")
                    result = draw_price_overlay(result, _price, _savings, _percentage, _old_price,
                                                text_zone_top=TEXT_ZONE_TOP, text_zone_bottom=TEXT_ZONE_BOTTOM)
                
                result = draw_affiliate_label(result, content_bottom=CONTENT_BOTTOM, inner_margin=60)
                if _has_coupon:
                    result = draw_coupon_badge(result)
                thread_log("✅ [process_image] Overlay aggiunto")
                
                thread_log("🔄 [process_image] Salvando JPEG...")
                output_buffer = BytesIO()
                if result.mode != 'RGB':
                    result = result.convert('RGB')
                result.save(output_buffer, format='JPEG', quality=85)
                # Libera il risultato subito dopo il salvataggio
                result.close()
                result = None
                _gc.collect()
                output_buffer.seek(0)
                thread_log(f"✅ [process_image] Buffer OK: {len(output_buffer.getvalue())} bytes")
                return output_buffer
            except Exception as e:
                thread_log(f"❌ [process_image] CRASH: {type(e).__name__}: {str(e)[:200]}")
                raise
            finally:
                if offer_img_resized: offer_img_resized.close()
                if result: result.close()
        
        # Esegui in thread pool
        logger.info("📤 Inviando process_image al thread pool...")
        try:
            output_buffer = await loop.run_in_executor(thread_pool, process_image)
            logger.info(f"✅ Thread pool completato!")
            logger.info(f"📥 Output buffer size: {len(output_buffer.getvalue())} bytes")
        except Exception as e:
            logger.error(f"❌ Thread pool ERRORE: {type(e).__name__}: {e}", exc_info=True)
            await msg.reply_text("❌ Errore durante l'elaborazione dell'immagine. Riprova!")
            await status.delete()
            return
        finally:
            # Libera sempre offer_img e background dalla RAM
            import gc, ctypes
            try: offer_img.close()
            except: pass
            try: background.close()
            except: pass
            gc.collect()
            # Forza restituzione RAM al sistema operativo (evita OOM su Render 512MB)
            try:
                ctypes.CDLL('libc.so.6').malloc_trim(0)
            except Exception:
                pass
        
        logger.info(f"✅ Invio immagine brandizzata (buffer: {len(output_buffer.getvalue())} bytes)...")
        
        # Estrai info PRIMA di inviare
        amazon_link = extract_amazon_link(offer_text)
        
        def _is_amazon_url(u: str) -> bool:
            u_low = u.lower()
            return any(d in u_low for d in ("amazon.", "amzn.to", "amzn.eu", "amzn.com", "a.co/"))

        # Prova anche nelle entità del testo se presenti
        if not amazon_link and msg.entities:
            for ent in msg.entities:
                if ent.type == "url" and msg.text:
                    link = msg.text[ent.offset : ent.offset + ent.length]
                    if _is_amazon_url(link):
                        amazon_link = link
                        logger.info(f"🔗 Link Amazon trovato nelle entità testo: {amazon_link}")
                        break
                elif ent.type == "text_link" and ent.url:
                    if _is_amazon_url(ent.url):
                        amazon_link = ent.url
                        logger.info(f"🔗 Link Amazon trovato in text_link testo: {amazon_link}")
                        break

        # Prova anche nelle entità della caption
        if not amazon_link and msg.caption_entities:
            for ent in msg.caption_entities:
                if ent.type == "url" and msg.caption:
                    link = msg.caption[ent.offset : ent.offset + ent.length]
                    if _is_amazon_url(link):
                        amazon_link = link
                        logger.info(f"🔗 Link Amazon trovato nelle entità caption: {amazon_link}")
                        break
                elif ent.type == "text_link" and ent.url:
                    if _is_amazon_url(ent.url):
                        amazon_link = ent.url
                        logger.info(f"🔗 Link Amazon trovato in text_link caption: {amazon_link}")
                        break

        # Controlla anche link_preview_options come ultima risorsa
        if not amazon_link and msg.link_preview_options and msg.link_preview_options.url:
            if _is_amazon_url(msg.link_preview_options.url):
                amazon_link = msg.link_preview_options.url
                logger.info(f"🔗 Link Amazon trovato in link_preview: {amazon_link}")

        brand_name = extract_brand_name(offer_text)
        price = extract_price(offer_text)
        
        logger.info(f"📝 Info estratte: Link={amazon_link}, Brand={brand_name}, Prezzo={price}")
        
        # INVIO FOTO SUBITO con caption temporanea (velocissimo!)
        temp_caption = ""
        if brand_name:
            temp_caption = f"<code>{brand_name}</code>"
            if price:
                temp_caption += f" - {price}"
            temp_caption += "\n\n⏳"
        
        # Crea shortlink PRIMA di inviare (evita edit_caption)
        short_link = None
        if amazon_link:
            logger.info(f"🔗 Generazione link PostTap per: {amazon_link}")
            try:
                # Forza timeout più alto e headers completi
                import time as _time
                ts = str(int(_time.time()))[-6:]
                base_name = (brand_name[:14] if brand_name else "prod")
                link_name = f"{base_name}-{ts}"
                short_link = await create_posttap_shortlink(amazon_link, name=link_name)
                
                if short_link:
                    logger.info(f"✅ PostTap ha risposto: {short_link}")
                else:
                    logger.warning("⚠️ PostTap ha risposto con successo ma senza link")
                    short_link = amazon_link
            except Exception as e:
                logger.error(f"❌ Errore critico PostTap: {e}")
                short_link = amazon_link
        
        # Se short_link è ancora None (non ritornato dall'API), usa l'originale
        if not short_link and amazon_link:
            short_link = amazon_link
            logger.info("⚠️ Fallback: Uso link Amazon originale")

        # Prepara caption FINALE completa
        final_caption = ""
        if brand_name:
            final_caption += f"<code>{brand_name}</code>"
            if price:
                final_caption += f" - {price}"
            final_caption += "\n\n"
        
        # MOSTRA IL LINK (Shortlink o Originale)
        if short_link:
            # Assicurati che il link sia un URL valido per la visualizzazione
            display_link = short_link
            if not display_link.startswith('http'):
                display_link = f"https://{display_link}"
            final_caption += f"<code>{display_link}</code>"
            
        if not final_caption:
            final_caption = "✨"
        
        # Invia foto
        import gc
        try:
            buffer_to_send = BytesIO(output_buffer.getvalue())
            buffer_to_send.seek(0)
            output_buffer.close()
            gc.collect()

            logger.info("📸 Inviando immagine a Telegram...")
            await msg.reply_photo(
                photo=buffer_to_send,
                caption=final_caption,
                parse_mode="HTML"
            )
            logger.info("✅✅✅ FOTO INVIATA CON SUCCESSO! ✅✅✅")
        except Exception as e:
            logger.error(f"❌ ERRORE SEND_PHOTO: {e}")
            return

        # Elimina status e messaggio originale
        try:
            await status.delete()
        except:
            pass
        try:
            await msg.delete()
        except:
            pass
        
        logger.info("✅ Immagine elaborata con successo")
        
    except Exception as e:
        logger.error(f"❌ Errore handler: {type(e).__name__}: {str(e)[:300]}", exc_info=True)
        try:
            await msg.reply_text("❌ Errore nell'elaborazione dell'immagine. Riprova!")
        except Exception as reply_err:
            logger.error(f"❌ Errore invio messaggio di errore: {reply_err}")

async def error_handler(update, context):
    """Cattura TUTTI gli errori"""
    logger.error(f"🚨 ERRORE GLOBALE: {context.error}", exc_info=context.error)

def build_app(token):
    """Costruisci l'Application con tutti gli handler"""
    app = Application.builder().token(token).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("brand", set_brand))
    app.add_handler(CommandHandler("rinnovalink", cmd_rinnova_cookies))
    app.add_handler(CommandHandler("setcookie", cmd_set_cookie))
    app.add_handler(CommandHandler("testlink", cmd_test_link))
    app.add_handler(MessageHandler(filters.ALL, handler))
    return app


def run_polling_mode(token):
    """Modalità POLLING: il bot chiede direttamente a Telegram i nuovi messaggi.
    Funziona sempre, non dipende da webhook o proxy. Perfetto per Reserved VM."""
    
    load_backgrounds_cache()
    _init_cookies_from_gist()  # legge Gist una volta sola → salva su file
    logger.info("⚡ Cache caricate - Bot ottimizzato!")
    
    logger.info("🗑️ Cancello eventuali webhook registrati...")
    import requests
    try:
        resp = requests.post(
            f'https://api.telegram.org/bot{token}/deleteWebhook',
            json={'drop_pending_updates': False}
        )
        logger.info(f"📡 deleteWebhook: {resp.json()}")
    except Exception as e:
        logger.warning(f"⚠️ Errore cancellazione webhook: {e}")
    
    # Task in background che cancella il webhook ogni 2 minuti
    # (Mastra/Inngest lo ri-registra ogni volta che parte — questo lo blocca)
    def _webhook_watchdog():
        import time
        import requests as _req
        while True:
            time.sleep(20)
            try:
                r = _req.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=10)
                url = r.json().get("result", {}).get("url", "")
                if url:
                    logger.warning(f"⚠️ [Watchdog] Webhook trovato: {url[:60]}... — cancello!")
                    _req.post(f"https://api.telegram.org/bot{token}/deleteWebhook",
                              json={"drop_pending_updates": False}, timeout=10)
                    logger.info("✅ [Watchdog] Webhook cancellato")
            except Exception as e:
                logger.warning(f"⚠️ [Watchdog] Errore: {e}")

    import threading as _threading
    _threading.Thread(target=_webhook_watchdog, daemon=True).start()
    logger.info("🛡️ Webhook watchdog avviato (controlla ogni 2 min)")

    app = build_app(token)
    
    logger.info("🚀 Avvio POLLING - il bot chiede messaggi a Telegram direttamente...")
    logger.info("✅ Bot POLLING pronto! In attesa di messaggi...")
    
    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=['message'],
        poll_interval=1.0,
        timeout=30,
    )


_renewal_state = {
    "phase": "idle",
    "otp_event": threading.Event(),
    "otp_value": None,
    "final_cookies": None,
    "error": None,
    "chat_id": None,
}

_RENEW_HTML_HEAD = """<!DOCTYPE html><html lang="it"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rinnovo Cookie PostTap</title>
<style>body{font-family:Arial,sans-serif;max-width:480px;margin:60px auto;padding:20px;background:#f5f5f5}
.card{background:#fff;border-radius:12px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,.1)}
h2{color:#232f3e}input[type=text]{width:100%;padding:12px;font-size:20px;letter-spacing:6px;
border:2px solid #ddd;border-radius:8px;margin:12px 0;box-sizing:border-box}
button{background:#ff9900;color:#fff;border:none;padding:14px 28px;border-radius:8px;
font-size:16px;cursor:pointer;width:100%}button:hover{background:#e88800}
.ok{color:#2e7d32;font-weight:bold}.err{color:#c62828;font-weight:bold}
.info{color:#555;margin:10px 0}.cookie{font-family:monospace;font-size:11px;word-break:break-all;
background:#f0f0f0;padding:10px;border-radius:6px;margin-top:12px}.spin{text-align:center;font-size:22px;margin:24px}
</style></head><body><div class="card">"""
_RENEW_HTML_TAIL = "</div></body></html>"


async def _run_renewal_login():
    """Esegue il login PostTap via Amazon in background"""
    from playwright.async_api import async_playwright
    email = os.environ.get("POSTTAP_EMAIL", "")
    password = os.environ.get("POSTTAP_PASSWORD", "")
    if not email or not password:
        _renewal_state["phase"] = "error"
        _renewal_state["error"] = "POSTTAP_EMAIL/POSTTAP_PASSWORD non impostati"
        return
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"
            )
            page = await ctx.new_page()

            await page.goto("https://creators.posttap.com/login", timeout=30000)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)
            logger.info(f"📄 Pagina login: {await page.title()} | {page.url}")

            # Step 1: accetta il cookie banner se presente
            try:
                accept_btn = await page.query_selector("button:has-text('Accept All')")
                if not accept_btn:
                    accept_btn = await page.query_selector("button:has-text('Accept')")
                if accept_btn:
                    await accept_btn.click()
                    logger.info("🍪 Cookie banner accettato")
                    await asyncio.sleep(2)
            except Exception:
                pass

            # Step 2: clicca il bottone "Sign in with Amazon" e aspetta la navigazione
            try:
                # Cerca specificamente il pulsante Amazon
                login_btn = None
                for selector in [
                    "button:has-text('Amazon')",
                    "a:has-text('Amazon')",
                    "[data-provider='amazon']",
                    "button:has-text('Sign in')",
                    "button:has-text('Accedi')",
                ]:
                    try:
                        login_btn = await page.wait_for_selector(selector, timeout=5000)
                        if login_btn:
                            logger.info(f"🔍 Pulsante trovato con: {selector}")
                            break
                    except Exception:
                        continue

                if not login_btn:
                    # Fallback: primo pulsante
                    login_btn = await page.wait_for_selector("button", timeout=8000)
                    logger.info("🔍 Usando primo pulsante (fallback)")

                async with page.expect_navigation(timeout=20000):
                    await login_btn.click()
                logger.info(f"🔑 Navigato su: {page.url[:80]}")
            except Exception as e:
                logger.error(f"❌ Bottone login/navigazione fallita: {e}")
                raise

            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)
            logger.info(f"✅ Pagina dopo click: {await page.title()} | {page.url[:80]}")

            # Step 3: compila email
            email_inp = await page.wait_for_selector("#ap_email,input[type='email']", timeout=30000)
            await email_inp.fill(email)
            logger.info("📧 Email inserita")
            await page.keyboard.press("Enter")
            await asyncio.sleep(3)
            logger.info(f"Dopo email: {await page.title()}")

            # Step 4: compila password
            try:
                pwd_inp = await page.wait_for_selector("#ap_password,input[type='password']", timeout=8000)
                await pwd_inp.fill(password)
                logger.info("🔒 Password inserita")
                async with page.expect_navigation(timeout=15000):
                    await page.keyboard.press("Enter")
            except Exception as e:
                logger.warning(f"⚠️ Password/navigazione: {e}")

            # Step 5: controlla 2FA
            await asyncio.sleep(3)
            title = await page.title()
            logger.info(f"🔑 Dopo login — Titolo: {title} | URL: {page.url}")

            if any(k in title.lower() for k in ("two-step", "mfa", "verification", "verifica", "codice", "authentication")):
                logger.info("📱 2FA richiesto - in attesa OTP utente")
                _renewal_state["phase"] = "waiting_otp"
                loop = asyncio.get_event_loop()
                received = await loop.run_in_executor(None, _renewal_state["otp_event"].wait, 600)
                if not received:
                    _renewal_state["phase"] = "error"
                    _renewal_state["error"] = "Timeout 2FA (10 min)"
                    await browser.close()
                    return

                otp = _renewal_state["otp_value"]
                inputs_visible = await page.query_selector_all("input:not([type='hidden'])")
                if inputs_visible:
                    await inputs_visible[0].fill(otp)
                    await page.keyboard.press("Enter")
                await asyncio.sleep(5)

            # Step 6: aspetta redirect su posttap.com (max 40s)
            try:
                await page.wait_for_url("*creators.posttap.com*", timeout=40000)
                logger.info(f"✅ Ritornato su PostTap: {page.url}")
            except Exception:
                logger.warning(f"⚠️ Timeout attesa PostTap — URL: {page.url}")

            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            await asyncio.sleep(3)
            logger.info(f"🌐 URL finale: {page.url} — Titolo: {await page.title()}")

            all_cookies = await ctx.cookies()
            logger.info(f"🍪 Tutti i cookie trovati: {[c['name'] + '@' + c.get('domain','') for c in all_cookies]}")
            # Salva TUTTI i cookie di posttap.com
            posttap_cookies = {c["name"]: c["value"] for c in all_cookies if "posttap.com" in c.get("domain", "")}
            # Fallback: prendi tutti se nessuno corrisponde al dominio
            if not posttap_cookies:
                logger.warning("⚠️ Nessun cookie posttap.com trovato, salvo tutti")
                posttap_cookies = {c["name"]: c["value"] for c in all_cookies}
            await browser.close()

            logger.info(f"🍪 Cookie trovati dopo login: {list(posttap_cookies.keys())}")

            if posttap_cookies:
                cookie_str = "; ".join(f"{k}={v}" for k, v in posttap_cookies.items())
                cookies_file = os.path.join(os.path.dirname(__file__), "posttap_cookies.txt")
                with open(cookies_file, "w") as f:
                    f.write(cookie_str)
                # Salva anche su GitHub Gist (persistente tra riavvii Render)
                save_cookies_to_gist(cookie_str)
                global _posttap_cookies
                _posttap_cookies = None  # reset cache in memoria
                logger.info(f"✅ Nuovi cookie PostTap salvati: {list(posttap_cookies.keys())}")
                _renewal_state["final_cookies"] = cookie_str
                _renewal_state["phase"] = "done"
            else:
                _renewal_state["phase"] = "error"
                _renewal_state["error"] = "Nessun cookie ottenuto. Login fallito?"
    except Exception as e:
        logger.error(f"❌ Errore rinnovo cookie: {e}")
        _renewal_state["phase"] = "error"
        _renewal_state["error"] = str(e)


def _renewal_thread():
    asyncio.run(_run_renewal_login())


def start_health_server():
    """Avvia un mini HTTP server per healthcheck e rinnovo cookie su porta 5000"""
    class HealthHandler(BaseHTTPRequestHandler):
        def _html(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((_RENEW_HTML_HEAD + body + _RENEW_HTML_TAIL).encode())

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == "/renew-cookies":
                # Restart richiesto?
                if query.get("restart"):
                    _renewal_state["phase"] = "idle"
                    _renewal_state["otp_event"].clear()
                    _renewal_state["otp_value"] = None

                ph = _renewal_state["phase"]
                if ph == "idle":
                    _renewal_state["phase"] = "starting"
                    _renewal_state["otp_event"].clear()
                    _renewal_state["otp_value"] = None
                    _renewal_state["final_cookies"] = None
                    _renewal_state["error"] = None
                    t = threading.Thread(target=_renewal_thread, daemon=True)
                    t.start()
                    ph = "starting"

                if ph == "starting":
                    self._html('<h2>🔄 Login PostTap in corso...</h2><p class="info">Apertura Amazon. Attendi qualche secondo.</p><div class="spin">⏳</div><script>setTimeout(()=>location.reload(),3000)</script>')
                elif ph == "waiting_otp":
                    self._html('<h2>📱 Codice 2FA Amazon</h2><p class="info">Inserisci il codice inviato al tuo dispositivo:</p><form method="POST" action="/renew-cookies"><input type="text" name="otp" placeholder="000000" maxlength="8" autofocus autocomplete="one-time-code"><button type="submit">✅ Conferma</button></form>')
                elif ph == "done":
                    short = _renewal_state["final_cookies"][:100] + "..."
                    self._html(f'<h2 class="ok">🎉 Cookie rinnovati!</h2><p class="info">I nuovi cookie PostTap sono stati salvati. Funzioneranno al prossimo link.</p><div class="cookie">{short}</div><p style="margin-top:16px"><a href="/renew-cookies?restart=1">Rinnova di nuovo</a></p>')
                elif ph == "error":
                    self._html(f'<h2 class="err">❌ Errore</h2><p>{_renewal_state["error"]}</p><p><a href="/renew-cookies?restart=1">Riprova</a></p>')

            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'OK')

        def do_POST(self):
            from urllib.parse import urlparse, parse_qs
            path = urlparse(self.path).path
            if path == "/renew-cookies":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                otp = params.get("otp", [""])[0].strip()
                if otp and _renewal_state["phase"] == "waiting_otp":
                    _renewal_state["otp_value"] = otp
                    _renewal_state["otp_event"].set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((_RENEW_HTML_HEAD + '<h2>✅ Codice inviato!</h2><p class="info">Attendendo...</p><div class="spin">⏳</div><script>setTimeout(()=>location.href="/renew-cookies",4000)</script>' + _RENEW_HTML_TAIL).encode())

        def log_message(self, format, *args):
            pass

    port = int(os.getenv('PORT', 5000))
    for attempt in range(10):
        try:
            server = HTTPServer(('0.0.0.0', port), HealthHandler)
            logger.info(f"🌐 Health server avviato su porta {port} (tentativo {attempt+1})")
            server.serve_forever()
            return
        except OSError:
            if attempt < 9:
                logger.info(f"⏳ Porta {port} occupata, riprovo tra 3s (tentativo {attempt+1}/10)...")
                time.sleep(3)
            else:
                logger.warning(f"⚠️ Impossibile avviare health server su porta {port} dopo 10 tentativi")


def main():
    """Avvia il bot - SEMPRE in modalità POLLING (più affidabile)"""
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not token:
        print("\n❌ ERRORE: TELEGRAM_BOT_TOKEN non configurato!")
        return
    
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    
    logger.info("🔧 Modalità bot: POLLING (sempre attivo)")
    logger.info("🚀 Avvio in modalità POLLING...")
    run_polling_mode(token)


if __name__ == '__main__':
    main()
