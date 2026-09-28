"""LATAM-specific browser automation and fare extraction logic for AirPrice."""

import re
import time
import unicodedata
from datetime import datetime

from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    WebDriverException,
)
from selenium.webdriver.common.by import By

from scrapers_utils import (
    _build_record,
    extract_lowest_price,
    CollectionCancelled,
    _check_cancellation,
    interruptible_sleep,
    _normalize_price_text,
)


URL_TEMPLATE = (
    "https://www.latamairlines.com/br/pt/oferta-voos"
    "?origin={origin}"
    "&outbound={data}T00%3A00%3A00.000Z"
    "&destination={destination}"
    "&adt=1&chd=0&inf=0&trip=OW&cabin=Y&redemption=false&sort=RECOMMENDED"
)

SELECTORS = {
    "flight_card": "[data-testid^='wrapper-card-flight-']",
    "flight_card_alt": "div[class*='cardFlight'], div[class*='FlightCard']",
    "price": "[data-testid='wrapper-currency-amount'], [data-testid='wrapper-currency-amount'] span",
    "price_alt": "span[class*='price'], span[class*='Price'], div[class*='amount']",
    "departure_time": "[data-testid$='-origin'] span",
    "arrival_time": "[data-testid$='-destination'] span",
    "spinner": "[class*='loading'], [class*='spinner'], [aria-label*='Loading']",
}

COOKIE_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "button[id*='accept']",
    "button[class*='accept']",
]

BLOCKING_SELECTORS = [
    "iframe[src*='recaptcha'][src*='bframe']",
    "[id='challenge-stage']",
    "[id='cf-challenge-running']",
]

MAX_LOAD_ATTEMPTS = 4
TIMEOUT_CARDS_S = 55
TIMEOUT_SPINNER_S = 20
MIN_USEFUL_HTML_SIZE = 5000

def _strip_accents(text_value):
    return "".join(
        c for c in unicodedata.normalize("NFD", text_value)
        if unicodedata.category(c) != "Mn"
    )

def _page_snapshot(driver):
    try:
        html = driver.page_source or ""
    except Exception:
        html = ""
    try:
        page_title = driver.title or ""
    except Exception:
        page_title = ""
    try:
        current_url_value = driver.current_url or ""
    except Exception:
        current_url_value = ""

    text_value = f"{page_title}\n{html}".lower()
    return {
        "title": page_title.strip() or "N/D",
        "url": current_url_value,
        "html_len": len(html),
        "text": text_value,
        "text_without_accents": _strip_accents(text_value),
    }

def _detect_blocking(driver):
    snap = _page_snapshot(driver)
    text_value = snap["text"]
    text_without_accents = snap["text_without_accents"]

    if "acesso negado" in text_without_accents or "access denied" in text_value:
        return True

    for sel in BLOCKING_SELECTORS:
        try:
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return True
        except Exception:
            pass
    return False

def _is_slow_page(driver):
    snap = _page_snapshot(driver)
    text_value = snap["text"]
    text_without_accents = snap["text_without_accents"]
    return (
        "busca esta demorando" in text_without_accents
        or "search is taking longer" in text_value
        or "tivemos um problema com os resultados" in text_without_accents
        or "tempo-resultados-busca" in text_value
    )

def _diagnose_page_state(driver):
    snap = _page_snapshot(driver)
    text_value = snap["text"]
    text_without_accents = snap["text_without_accents"]

    reasons = []
    if snap["html_len"] < MIN_USEFUL_HTML_SIZE:
        reasons.append(f"HTML too small ({snap['html_len']} bytes)")
    if "acesso negado" in text_without_accents or "access denied" in text_value:
        reasons.append("possible access denied")
    if "configuracoes de cookies" in text_without_accents:
        reasons.append("cookie banner blocking the page")
    if _is_slow_page(driver):
        reasons.append("LATAM slow/error page")
    unavailability_detected = any(p in text_without_accents for p in (
        "nao encontramos voos",
        "nao ha voos",
        "sem voos",
        "no flights",
        "we could not find flights",
    ))
    if unavailability_detected and not _is_slow_page(driver):
        reasons.append("flight unavailable message")
    if "oferta-voos" not in snap["url"]:
        reasons.append(f"Unexpected URL: {snap['url']}")

    if not reasons:
        reasons.append("cards/prices did not appear within the timeout")

    return snap, "; ".join(reasons)

def _click_element(driver, element, should_stop=None):
    _check_cancellation(should_stop)
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
        element,
    )
    interruptible_sleep(0.4, 0.8, enabled=True, should_stop=should_stop)
    _check_cancellation(should_stop)
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)

def _close_cookie_banner(driver, log_fn, privacy_mode, should_stop=None):
    # PT-BR page text is intentionally retained because it is matched against the live LATAM DOM.
    _check_cancellation(should_stop)
    snap = _page_snapshot(driver)
    if "configuracoes de cookies" not in snap["text_without_accents"]:
        return False

    prefer_reject = privacy_mode != "Normal (with cookies)"
    preferred_texts = (
        ["recusar todos os cookies", "rejeitar todos", "reject all"]
        if prefer_reject
        else ["aceite todos os cookies", "aceitar todos", "accept all"]
    )
    action = "rejected" if prefer_reject else "accepted"

    for button_text in preferred_texts:
        xpath = (
            "//button[contains(translate(normalize-space(.), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ', "
            "'abcdefghijklmnopqrstuvwxyzáàâãéèêíìîóòôõúùûç'), "
            f"'{button_text}')]"
        )
        try:
            buttons = driver.find_elements(By.XPATH, xpath)
            for button in buttons:
                _check_cancellation(should_stop)
                if button.is_displayed() and button.is_enabled():
                    _click_element(driver, button, should_stop=should_stop)
                    log_fn(f"Cookie banner {action} using button text.")
                    interruptible_sleep(1.0, 2.0, enabled=True, should_stop=should_stop)
                    return True
        except Exception:
            pass

    for selector in COOKIE_SELECTORS:
        try:
            buttons = driver.find_elements(By.CSS_SELECTOR, selector)
            for button in buttons:
                _check_cancellation(should_stop)
                if button.is_displayed() and button.is_enabled():
                    _click_element(driver, button, should_stop=should_stop)
                    log_fn("Cookie banner closed using CSS selector.")
                    interruptible_sleep(1.0, 2.0, enabled=True, should_stop=should_stop)
                    return True
        except Exception:
            pass

    xpaths = [
        "//button[contains(translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ', "
        "'abcdefghijklmnopqrstuvwxyzáàâãéèêíìîóòôõúùûç'), "
        "'aceite todos os cookies')]",
        "//button[contains(translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ', "
        "'abcdefghijklmnopqrstuvwxyzáàâãéèêíìîóòôõúùûç'), "
        "'aceitar todos')]",
        "//button[contains(translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        "'accept all')]",
    ]

    for xpath in xpaths:
        try:
            buttons = driver.find_elements(By.XPATH, xpath)
            for button in buttons:
                _check_cancellation(should_stop)
                if button.is_displayed() and button.is_enabled():
                    _click_element(driver, button, should_stop=should_stop)
                    log_fn("Cookie banner closed using button text.")
                    interruptible_sleep(1.0, 2.0, enabled=True, should_stop=should_stop)
                    return True
        except Exception:
            pass

    log_fn("Cookie banner detected, but the button could not be clicked.")
    return False

def _click_retry_search(driver, log_fn, should_stop=None):
    """Try to leave the LATAM results-error screen without abandoning the route."""
    _check_cancellation(should_stop)
    candidates = [
        (By.CSS_SELECTOR, "[data-testid='back-button']"),
        (
            By.XPATH,
            "//button[contains(translate(normalize-space(.), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZÃÃ€Ã‚ÃƒÃ‰ÃˆÃŠÃÃŒÃŽÃ“Ã’Ã”Ã•ÃšÃ™Ã›Ã‡', "
            "'abcdefghijklmnopqrstuvwxyzÃ¡Ã Ã¢Ã£Ã©Ã¨ÃªÃ­Ã¬Ã®Ã³Ã²Ã´ÃµÃºÃ¹Ã»Ã§'), "
            "'realizar outra busca')]",
        ),
        (
            By.XPATH,
            "//button[contains(translate(normalize-space(.), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
            "'try again')]",
        ),
    ]

    for by, selector in candidates:
        try:
            for button in driver.find_elements(by, selector):
                _check_cancellation(should_stop)
                if button.is_displayed() and button.is_enabled():
                    log_fn("LATAM error screen detected; clicking 'Try another search'.")
                    _click_element(driver, button, should_stop=should_stop)
                    interruptible_sleep(4, 7, enabled=True, should_stop=should_stop)
                    return True
        except Exception:
            pass

    return False

def _url_retry(url, attempt):
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_retry={attempt}_{int(time.time())}"

def _element_text(el):
    return (
        el.text
        or el.get_attribute("textContent")
        or el.get_attribute("innerText")
        or el.get_attribute("aria-label")
        or ""
    ).strip()

def _text_by_selector(card, selector):
    try:
        for el in card.find_elements(By.CSS_SELECTOR, selector):
            text_value = _element_text(el)
            if text_value:
                return text_value
    except Exception:
        pass
    return "N/D"

def _card_text(card):
    return _element_text(card)

def _time_by_regex(text_value, pattern):
    m = re.search(pattern, _strip_accents(text_value), flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return "N/D"

def _extract_time(card, selector, text_fallback, default_fallback):
    text_value = _text_by_selector(card, selector)
    m = re.search(r"\b\d{1,2}:\d{2}\b", text_value)
    if m:
        return m.group(0)
    return _time_by_regex(text_fallback, default_fallback)

def _extract_times_from_text(text_value):
    times_found = re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text_value)
    normalized = []
    for hour_value, minute in times_found:
        value_num = f"{int(hour_value):02d}:{minute}"
        if value_num not in normalized:
            normalized.append(value_num)
    return normalized

def _extract_lowest_price_from_text(cards, log_fn=None):
    prices = []
    for idx, card in enumerate(cards):
        text_value = _card_text(card)
        matches = []
        for pattern in (
            r"(?:pre[c\u00e7]o.*?a partir de|brl|r\$)\s*([\d\.]+,\d{2})",
            r"(?:pre[c\u00e7]o.*?a partir de|brl|r\$)\s*([\d\.]{2,})",
        ):
            matches.extend(re.findall(pattern, text_value, flags=re.IGNORECASE))
        for raw_value in matches:
            price = _normalize_price_text(raw_value)
            if price is not None and price >= 80:
                prices.append({"price": price, "card_index": idx})

    if not prices:
        return {}

    best = min(prices, key=lambda item: item["price"])
    if log_fn:
        log_fn(
            f"[PRICE] Text fallback: {len(prices)} price(s) | "
            f"Lowest: R$ {best['preco']:.2f} (card #{best['card_index']})"
        )
    return {
        "price": best["price"],
        "card_index": best["card_index"],
        "total_prices": len(prices),
    }

def _extract_lowest_price_with_fallback(cards, log_fn):
    lowest_price_info = extract_lowest_price(cards, SELECTORS["price"], log_fn=log_fn)
    if lowest_price_info:
        return lowest_price_info

    log_fn("[PRICE] Main selector found no price; trying alternate selector.")
    lowest_price_info = extract_lowest_price(cards, SELECTORS["price_alt"], log_fn=log_fn)
    if lowest_price_info:
        return lowest_price_info

    return _extract_lowest_price_from_text(cards, log_fn=log_fn)

def _wait_for_spinner(driver, log_fn, should_stop=None):
    log_fn("Waiting for loading to finish...")
    end_time = time.time() + TIMEOUT_SPINNER_S

    while time.time() < end_time:
        _check_cancellation(should_stop)
        try:
            if not driver.find_elements(By.CSS_SELECTOR, SELECTORS["spinner"]):
                return
        except Exception:
            return
        time.sleep(0.5)

    log_fn("Spinner did not disappear within the limit; continuing diagnostics.")

def _find_cards(driver, log_fn, log=True):
    cards = driver.find_elements(By.CSS_SELECTOR, SELECTORS["flight_card"])
    if log:
        log_fn(f"Cards found with main selector: {len(cards)}")
    if cards:
        return cards

    cards = driver.find_elements(By.CSS_SELECTOR, SELECTORS["flight_card_alt"])
    if log:
        log_fn(f"Cards found with alternate selector: {len(cards)}")
    if cards:
        return cards

    cards = _find_cards_by_price(driver)
    if log:
        log_fn(f"Cards found by price fallback: {len(cards)}")
    return cards

def _find_cards_by_price(driver):
    candidates = []
    seen = set()
    xpaths = [
        "//*[contains(normalize-space(.), 'R$')]",
        "//*[contains(translate(normalize-space(.), 'abcdefghijklmnopqrstuvwxyz', "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'BRL')]",
    ]

    for xpath in xpaths:
        try:
            elements = driver.find_elements(By.XPATH, xpath)
        except Exception:
            continue

        for element in elements[:80]:
            try:
                card = driver.execute_script(
                    """
                    let el = arguments[0];
                    for (let i = 0; i < 8 && el; i++, el = el.parentElement) {
                        const txt = (el.innerText || el.textContent || '').trim();
                        if (txt.length > 80 && txt.length < 4000 &&
                            /(?:R\\$|BRL)/i.test(txt) &&
                            /\\b\\d{1,2}:\\d{2}\\b/.test(txt)) {
                            return el;
                        }
                    }
                    return arguments[0];
                    """,
                    element,
                )
                if not card:
                    continue
                field_key = card.id or card.get_attribute("data-testid") or card.get_attribute("outerHTML")[:200]
                if field_key in seen:
                    continue
                seen.add(field_key)
                candidates.append(card)
            except Exception:
                continue

    return candidates

def _wait_for_cards(driver, log_fn, should_stop=None):
    log_fn(f"Waiting up to {TIMEOUT_CARDS_S}s for cards...")
    end_time = time.time() + TIMEOUT_CARDS_S

    while time.time() < end_time:
        _check_cancellation(should_stop)
        cards = _find_cards(driver, log_fn, log=False)
        if cards:
            break
        if _is_slow_page(driver):
            log_fn("LATAM error/slow page appeared while waiting for cards.")
            break
        time.sleep(0.7)
    else:
        log_fn("Timed out waiting for cards; checking anyway.")
        snap, reason = _diagnose_page_state(driver)
        log_fn(
            f"Partial diagnosis: {reason} | title='{snap['titulo']}' | "
            f"html={snap['html_len']} bytes"
        )

    _wait_for_spinner(driver, log_fn, should_stop=should_stop)
    return _find_cards(driver, log_fn)

def scrape_latam(driver, origin, destination, flight_date_str,
                 experiment_id="E0", user_agent_label="Chrome Desktop (default)",
                 privacy_mode="No cookies (clean profile)", log_fn=None,
                 humanize=True, should_stop=None):

    def _log(msg):
        if log_fn:
            log_fn(f"[LATAM] {msg}")

    results = []
    start_time = time.time()
    captcha = False

    url = URL_TEMPLATE.format(
        origin=origin.upper(),
        destination=destination.upper(),
        data=flight_date_str,
    )

    try:
        _check_cancellation(should_stop)
        _log(f"Opening: {url}")
        driver.get(url)
        interruptible_sleep(5, 9, enabled=humanize, should_stop=should_stop)
        _close_cookie_banner(driver, _log, privacy_mode, should_stop=should_stop)

        _check_cancellation(should_stop)
        captcha = _detect_blocking(driver)
        if captcha:
            _log("Real block detected!")

        if not captcha:
            cards = []
            for attempt in range(1, MAX_LOAD_ATTEMPTS + 1):
                _check_cancellation(should_stop)
                if attempt > 1:
                    retry_wait = 18 + (attempt * 7)
                    _log(
                        f"Attempt {attempt}/{MAX_LOAD_ATTEMPTS}: "
                        f"reloading in {retry_wait}s..."
                    )
                    interruptible_sleep(
                        retry_wait,
                        retry_wait + 5,
                        enabled=humanize,
                        should_stop=should_stop,
                    )
                    _check_cancellation(should_stop)
                    driver.get(_url_retry(url, attempt))
                    interruptible_sleep(5, 9, enabled=humanize, should_stop=should_stop)
                    _close_cookie_banner(
                        driver,
                        _log,
                        privacy_mode,
                        should_stop=should_stop,
                    )

                    _check_cancellation(should_stop)
                    captcha = _detect_blocking(driver)
                    if captcha:
                        _log("Real block detected after reload.")
                        break

                cards = _wait_for_cards(driver, _log, should_stop=should_stop)
                if cards:
                    break

                if _is_slow_page(driver):
                    if _click_retry_search(driver, _log, should_stop=should_stop):
                        _close_cookie_banner(
                            driver,
                            _log,
                            privacy_mode,
                            should_stop=should_stop,
                        )
                        cards = _wait_for_cards(driver, _log, should_stop=should_stop)
                        if cards:
                            break
                    else:
                        _log("LATAM error screen without an actionable button; trying a new load.")

                snap, reason = _diagnose_page_state(driver)
                _log(
                    f"No cards on attempt {attempt}: {reason} | "
                    f"title='{snap['titulo']}' | html={snap['html_len']} bytes"
                )

                if (
                    "flight unavailable message" in reason
                    and "LATAM slow/error page" not in reason
                ):
                    _log("Stopping attempts: LATAM reported flight unavailability.")
                    break

            if cards:
                _log(f"OK: {len(cards)} offer(s) found.")

                current_time = datetime.now()
                flight_date_dt = datetime.strptime(flight_date_str, "%Y-%m-%d")
                advance_days = (flight_date_dt - current_time).days

                lowest_price_info = _extract_lowest_price_with_fallback(cards, _log)

                if lowest_price_info:
                    best_card = cards[lowest_price_info["card_index"]]
                    price_value = lowest_price_info["price"]

                    try:
                        card_text_value = _card_text(best_card)
                        departure_time = _extract_time(
                            best_card,
                            SELECTORS["departure_time"],
                            card_text_value,
                            r"hora de sa[i\u00ed]da\s+(\d{1,2}:\d{2})",
                        )
                        arrival_time = _extract_time(
                            best_card,
                            SELECTORS["arrival_time"],
                            card_text_value,
                            r"hora de chegada\s+(\d{1,2}:\d{2})",
                        )
                        fallback_times = _extract_times_from_text(card_text_value)
                        if departure_time == "N/D" and fallback_times:
                            departure_time = fallback_times[0]
                        if arrival_time == "N/D" and len(fallback_times) > 1:
                            arrival_time = fallback_times[1]

                        best_offer = _build_record(
                            platform="LATAM",
                            url=url,
                            origin=origin,
                            destination=destination,
                            flight_date_str=flight_date_str,
                            advance_days=advance_days,
                            price_value=price_value,
                            departure_time=departure_time,
                            arrival_time=arrival_time,
                            privacy_mode=privacy_mode,
                            user_agent_label=user_agent_label,
                            current_time=current_time,
                            experiment_id=experiment_id,
                            captcha_detected=False
                        )

                        results.append(best_offer)
                        _log(
                            f"OK: LATAM collection OK: {origin}->{destination} | "
                            f"R$ {price_value:.2f} | "
                            f"{lowest_price_info['total_precos']} flights analyzed"
                        )
                    except Exception as e:
                        _log(f"Error processing best offer: {e}")
                else:
                    snap, reason = _diagnose_page_state(driver)
                    _log(
                        f"Warning: no valid price found for "
                        f"{origin}->{destination} on {flight_date_str}. "
                        f"Diagnosis: {reason} | title='{snap['titulo']}' | "
                        f"html={snap['html_len']} bytes"
                    )
            else:
                snap, reason = _diagnose_page_state(driver)
                _log(
                    f"No flight card found for {origin}->{destination}. "
                    f"Final diagnosis: {reason} | title='{snap['titulo']}' | "
                    f"html={snap['html_len']} bytes"
                )

    except (InvalidSessionIdException, NoSuchWindowException) as e:
        try:
            driver._invalid_driver_session = True
        except Exception:
            pass
        _log(
            "Chrome session was closed during collection. "
            "The route will be retried with a new browser."
        )
        _log(f"Technical summary: {e.__class__.__name__}")
    except WebDriverException as e:
        message_text = str(e).lower()
        if (
            "invalid session id" in message_text
            or "target window already closed" in message_text
            or "not connected to devtools" in message_text
            or "web view not found" in message_text
        ):
            try:
                driver._invalid_driver_session = True
            except Exception:
                pass
            _log(
                "Chrome lost the Selenium connection during collection. "
                "The route will be retried with a new browser."
            )
            _log(f"Technical summary: {e.__class__.__name__}")
        else:
            _log(f"General Selenium error during collection: {e}")
    except CollectionCancelled:
        _log("Collection canceled by the user.")
    except Exception as e:
        _log(f"General collection error: {e}")
        import traceback
        _log(f"Traceback: {traceback.format_exc()}")

    if captcha and not results:
        _log("Collection blocked by CAPTCHA; no empty record will be saved.")

    _log(
        f"Collection completed: {len(results)} record(s) | "
        f"{round((time.time() - start_time) * 1000)}ms"
    )
    return results
