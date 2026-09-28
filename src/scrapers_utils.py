"""Shared extraction, normalization, cancellation, hashing, and record-building utilities."""

import hashlib
import re
import time
import random
from datetime import datetime
from pathlib import Path
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException


# ─── Extraction helpers ────────────────────────────────────────────────────────

def _normalize_price_text(text_value):
    """Normalize a price string and convert it to float."""
    if not text_value:
        return None

    # Normalize whitespace characters and remove common multipliers
    cleaned = text_value.replace("\xa0", "").strip()

    # Remove a currency prefix when present while preserving the numeric value.
    cleaned = re.sub(r'(?i)\b(brl|usd|eur|r\$)\b', '', cleaned)

    # Keep only the main numeric group (e.g., "R$ 1.234,56" -> "1.234,56").
    m = re.search(r'([\d\.\,]+)', cleaned)
    if m:
        cleaned = m.group(1)
    else:
        return None

    # Remove the thousands separator and normalize the decimal separator
    cleaned = re.sub(r'\.(?=\d{3}(?:\D|$))', '', cleaned)
    cleaned = cleaned.replace(',', '.')

    # Keep only digits and the decimal point
    cleaned = re.sub(r'[^\d.]', '', cleaned).strip()

    if not cleaned:
        return None
    if cleaned.count('.') > 2:
        return None

    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_price(card, selector): 
    """
    Extract and convert a card price to float.
    Handles formats such as 'R$ 1.234,56', '1234.56', and '1.234'.
    Returns None if no price is found or conversion fails.
    """
    elements = card.find_elements(By.CSS_SELECTOR, selector)
    if not elements:
        return None

    candidates = []
    for el in elements:
        text_value = (
            el.text
            or el.get_attribute("textContent")
            or el.get_attribute("innerText")
            or el.get_attribute("aria-label")
            or ""
        ).strip()
        if not text_value:
            continue

        # Reject text that clearly describes duration or times
        if re.search(r'\d+\s*h(?:oras?)?|\d+\s*m(?:inutos?)?|\d{1,2}:\d{2}', text_value, re.IGNORECASE):
            # Accept only when an explicit currency marker is present
            if not re.search(r'(?i)\b(r\$|brl|usd|eur)\b', text_value):
                continue

        price = _normalize_price_text(text_value)
        if price is None:
            continue

        has_currency = bool(re.search(r'(?i)\b(r\$|brl|usd|eur)\b', text_value))
        candidates.append((price, has_currency, text_value))

    if not candidates:
        return None

    # Prefer extraction with a currency marker; if several exist, choose the lowest fare
    with_currency = [p for p in candidates if p[1]]
    if with_currency:
        final_price = min(p[0] for p in with_currency)
    else:
        # Handle residual duration-like values that passed the checks above
        final_price = min(p[0] for p in candidates)

    # Ignore values that are clearly invalid for B2C airfare in Brazil
    if final_price <= 0 or final_price < 10:
        return None

    return final_price


def _extract_stops(card, selector):
    """
    Extract the number of stops from a card.
    Returns 0 for a nonstop flight, an int for connecting flights, or None if unavailable.
    """
    try:
        text_value = card.find_element(By.CSS_SELECTOR, selector).text.strip().lower()
    except NoSuchElementException:
        return None

    if not text_value:
        return None

    if any(p in text_value for p in ["direto", "direct", "nonstop", "sem escala"]):
        return 0

    digits = re.findall(r'\d+', text_value)
    if digits:
        return int(digits[0])

    return 1  


# ─── LEGACY FUNCTIONS (file-system hash) ──────────────────────────────

def hash_gz_file(path) -> str:
    """Compute the MD5 hash of a saved compressed file for local verification."""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def remove_html_log(path, expected_hash: str = "", log_fn=None) -> bool:
    """Remove a raw HTML file after upload, checking its hash when available."""
    path = Path(path)
    if not path.exists():
        if log_fn:
            log_fn(f"[LOG] File already removed: {path.name}")
        return False
    if expected_hash and hash_gz_file(path) != expected_hash:
        if log_fn:
            log_fn(f"[LOG] Hash mismatch, preserving: {path.name}")
        return False
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as e:
        if log_fn:
            log_fn(f"[LOG] Failed to remove {path.name}: {e}")
        return False


def _classify_time_window(hour_value):
    # These labels are retained for compatibility with the existing study dataset.
    if hour_value < 6:  return "madrugada (00-06h)"
    if hour_value < 12: return "manha (06-12h)"
    if hour_value < 18: return "tarde (12-18h)"
    return "noite (18-24h)"


class CollectionCancelled(Exception):
    """Interrupt the current collection when the user clicks Stop."""


def _is_cancelled(should_stop):
    return bool(should_stop and should_stop())


def _check_cancellation(should_stop):
    if _is_cancelled(should_stop):
        raise CollectionCancelled()


def interruptible_sleep(min_s, max_s=None, enabled=True, should_stop=None):
    if not enabled:
        _check_cancellation(should_stop)
        return

    if max_s is None:
        max_s = min_s

    end_time = time.time() + max(0, min_s if min_s == max_s else min_s + ((max_s - min_s) * 0.5))
    if max_s != min_s:
        end_time = time.time() + random.uniform(min_s, max_s)

    while time.time() < end_time:
        _check_cancellation(should_stop)
        time.sleep(min(0.3, end_time - time.time()))
    _check_cancellation(should_stop)


def _generate_id(platform, origin, destination, flight_date_str):
    raw = f"{platform}{origin}{destination}{flight_date_str}{datetime.utcnow().isoformat()}"
    return hashlib.md5(raw.encode()).hexdigest()[:10].upper()


# ─── Lowest-price extractor for a list of cards ──────────────────────────

def extract_lowest_price(cards, price_selector, log_fn=None):
    """
    Extract the LOWEST price from ALL available cards.
    
    Returns:
        dict: {
            'price': float (lowest price found),
            'card_index': int (index of the card with the lowest price),
            'total_prices': int (number of prices found)
        }
        or {} if no valid price is found
    """
    found_prices = []
    
    def _log(msg):
        if log_fn:
            log_fn(msg)
    
    for idx, card in enumerate(cards):
        try:
            price = _extract_price(card, price_selector)
            if price is not None:
                found_prices.append({
                    'price': price,
                    'card_index': idx
                })
        except Exception as e:
            _log(f"[PRICE] Error extracting price from card {idx}: {e}")
    
    if not found_prices:
        return {}
    
    # Sort by price in ascending order and return the lowest value
    best = min(found_prices, key=lambda x: x['price'])
    result = {
        'price': best['price'],
        'card_index': best['card_index'],
        'total_prices': len(found_prices),
    }
    
    _log(f"[PRICE] Collected {len(found_prices)} price(s) | Lowest: R$ {best['price']:.2f} (card #{best['card_index']})")
    
    return result


# ─── Standard record construction ────────────────────────────────────────────

def _build_record(platform, url, origin, destination, flight_date_str, advance_days,
                     price_value, departure_time, arrival_time,
                     privacy_mode, user_agent_label,
                     current_time, experiment_id, captcha_detected=False):
    """
    Build the standard dictionary for a collection record.
    The structure is aligned with DATASET_COLUMNS.
    """
    return {
        "id_coleta":            _generate_id(platform, origin, destination, flight_date_str),
        "data_hora_local":      current_time.strftime("%Y-%m-%d %H:%M:%S"),
        "janela_horario":       _classify_time_window(current_time.hour),
        "plataforma":           platform,
        "url_consultada":       url,
        "origem":               origin.upper(),
        "destino":              destination.upper(),
        "data_voo":             flight_date_str,
        "dias_antecedencia":    advance_days,
        "preco_brl":            price_value,
        "hora_saida":           departure_time,
        "hora_chegada":         arrival_time,
        "modo_privacidade":     privacy_mode,
        "user_agent_label":     user_agent_label,
        "captcha_detectado":    captcha_detected,
        "dia_semana_busca":     current_time.strftime("%A"),
        "experimento_id":       experiment_id,
    }
