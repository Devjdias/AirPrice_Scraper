"""
scraper_passagens.py — Core module of the AirPrice system.

Responsibilities:
- Orchestrate collection runs, routes, platforms, and scheduler cycles.
- Configure and create the Chrome WebDriver, including anti-detection options.
- Persist results to Excel (DADOS_BRUTOS/) and optionally to Google Sheets.
- Provide the Tkinter graphical interface used to operate the system manually.

The LATAM-specific scraper is implemented in `latam_scraper.py`; normalization,
hashing, and record-construction utilities are implemented in `scrapers_utils.py`.
Sensitive values such as credentials and spreadsheet IDs must not be hard-coded
here; they are read from `config/config.ini` through `load_config()`.
"""

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk, filedialog
from tkcalendar import DateEntry
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
import pandas as pd
import os
import time
import sys
import threading
import random
import gzip
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
import gspread
from google.oauth2.service_account import Credentials
import warnings
import configparser

# Import shared utilities
from scrapers_utils import remove_html_log, hash_gz_file, _build_record, interruptible_sleep

warnings.simplefilter("ignore", UserWarning)

# ─── GLOBAL PATHS ──────────────────────────────────────────────────────────

BASE_DIR           = Path(__file__).parent.parent 
RAW_DATA_DIR   = BASE_DIR / "DADOS_BRUTOS"
LOGS_DIR           = BASE_DIR / "LOGS"
CHROME_PROFILES_DIR = LOGS_DIR / "chrome_profiles"
CONFIG_FILE          = BASE_DIR / "config" / "config.ini"

for _folder in [RAW_DATA_DIR, LOGS_DIR, CHROME_PROFILES_DIR]:
    _folder.mkdir(parents=True, exist_ok=True)

# Ensure Python can find the scraper modules in the same directory as this script
SRC_DIR = Path(__file__).parent  
if str(SRC_DIR) not in sys.path:
    sys.path.append(str(SRC_DIR))


# ─── EXPERIMENT CONFIGURATION ─────────────────────────────────────────────

AVAILABLE_ROUTES = {
    "BSB -> GIG ": ("BSB", "GIG"), #(Brasília - Rio de Janeiro)
    "PVH -> BSB ":    ("PVH", "BSB"), #(Porto Velho - Brasília)
    "BSB -> FLN ":  ("BSB", "FLN"), #(Brasília - Florianópolis)
    "JPA -> BSB ":    ("JPA", "BSB"), #(João Pessoa - Brasília)
}

USER_AGENTS = {
    "Chrome Desktop (default)":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36",
    "Chrome Normal":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36",
}

PRIVACY_MODES = [
    "No cookies (clean profile)",
    "Normal (with cookies)",
    "Incognito",
]

# Stable schema: field names are retained to remain compatible with the study dataset.
DATASET_COLUMNS = [
    "id_coleta", "data_hora_local", "janela_horario",
    "plataforma", "url_consultada", "origem", "destino", "data_voo",
    "dias_antecedencia", "preco_brl", "hora_saida", "hora_chegada",
    "modo_privacidade", "user_agent_label", "captcha_detectado",
    "dia_semana_busca", "experimento_id",
]

app_instance = None
stop_event   = threading.Event()


def is_driver_session_invalid(driver):
    if not driver:
        return True
    if getattr(driver, "_invalid_driver_session", False):
        return True
    try:
        _ = driver.current_window_handle
        return False
    except WebDriverException:
        return True


def start_thread(func, *args):
    t = threading.Thread(target=func, args=args, daemon=True)
    t.start()
    return t


def load_antidetect_config(cfg):
    return {
        "humanize":   cfg.getboolean("ANTIDETECT", "humanize", fallback=True),
        "save_html":  cfg.getboolean("ANTIDETECT", "save_html", fallback=False),
        "screenshot": cfg.getboolean("ANTIDETECT", "screenshot", fallback=False),
    }


def load_proxy_config(cfg):
    enabled = cfg.getboolean("PROXY", "enabled", fallback=False)
    raw = cfg.get("PROXY", "list", fallback="")
    proxies = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        proxies.append(line)
    return enabled, proxies


def save_raw_html(driver, platform, experiment_id):
    """Save compressed HTML and return (path, hash_md5); the hash records the saved artifact."""
    if is_driver_session_invalid(driver):
        if app_instance:
            app_instance.log_warn("Raw HTML was not saved: Chrome session was already closed.")
        return None, ""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    nome = f"html_{platform}_{experiment_id}_{ts}.html.gz"
    path = LOGS_DIR / nome
    try:
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(driver.page_source or "")
        h = hash_gz_file(path)
        if app_instance:
            app_instance.log_ok(f"Raw HTML saved: {path.name} | MD5={h}")
        return path, h
    except Exception as e:
        if app_instance:
            app_instance.log_warn(f"Failed to save raw HTML: {e}")
        return None, ""


def save_screenshot(driver, platform, experiment_id, error_tag="error"):
    if is_driver_session_invalid(driver):
        if app_instance:
            app_instance.log_warn("Screenshot was not saved: Chrome session was already closed.")
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    nome = f"{error_tag}_{platform}_{experiment_id}_{ts}.png"
    path = LOGS_DIR / nome
    try:
        driver.save_screenshot(str(path))
        if app_instance:
            app_instance.log_ok(f"Screenshot saved: {path.name}")
    except Exception as e:
        if app_instance:
            app_instance.log_warn(f"Failed to save screenshot: {e}")


# ─── CONFIG ────────────────────────────────────────────────────────────────────

def build_no_data_record(platform, origin, destination, flight_date_str,
                              privacy_mode, user_agent_label, experiment_id):
    current_time = datetime.now()
    flight_date_dt = datetime.strptime(flight_date_str, "%Y-%m-%d")
    url = (
        "https://www.latamairlines.com/br/pt/oferta-voos"
        f"?origin={origin.upper()}"
        f"&outbound={flight_date_str}T00%3A00%3A00.000Z"
        f"&destination={destination.upper()}"
        "&adt=1&chd=0&inf=0&trip=OW&cabin=Y&redemption=false&sort=RECOMMENDED"
    )
    return _build_record(
        platform=platform,
        url=url,
        origin=origin,
        destination=destination,
        flight_date_str=flight_date_str,
        advance_days=(flight_date_dt - current_time).days,
        price_value=None,
        departure_time="N/D",
        arrival_time="N/D",
        privacy_mode=privacy_mode,
        user_agent_label=user_agent_label,
        current_time=current_time,
        experiment_id=experiment_id,
        captcha_detected=False,
    )


def load_config():
    cfg = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE)
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        cfg.write(f)


# ─── SELENIUM ──────────────────────────────────────────────────────────────────

def create_driver(privacy_mode="No cookies (clean profile)", headless=False,
                 user_agent_label=None, proxy=None):
    options = webdriver.ChromeOptions()
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    temporary_profile = None

    ua = USER_AGENTS.get(user_agent_label or "") or next(iter(USER_AGENTS.values()))
    options.add_argument(f"--user-agent={ua}")

    if privacy_mode == "Incognito":
        options.add_argument("--incognito")
    elif privacy_mode == "Normal (with cookies)":
        pass
    else:
        temporary_profile = tempfile.mkdtemp(prefix="chrome_profile_")
        options.add_argument(f"--user-data-dir={temporary_profile}")

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-software-rasterizer")

    if proxy:    
        options.add_argument(f"--proxy-server={proxy}")

    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(15)
    if temporary_profile:
        driver._temporary_profile = temporary_profile
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


# ─── EXPERIMENT EXECUTION ──────────────────────────────────────────────────
def run_experiment(app_ctrl, config):
    if stop_event.is_set():
        return

    route_label      = config["route_label"]
    origin, destination = AVAILABLE_ROUTES[route_label]
    platform     = config["platform"]
    flight_date_str    = config["flight_date_str"]
    ua_label        = config["user_agent_label"]
    privacy_mode_value       = config["privacy_mode"]
    exp_id          = config["experiment_id"]
    repetitions      = int(config.get("repetitions", 3))
    headless        = config.get("headless", False)

    cfg = load_config()
    antidetect = load_antidetect_config(cfg)
    proxy_enabled, proxy_list = load_proxy_config(cfg)
    proxy = None
    if proxy_enabled:
        if proxy_list:
            proxy = random.choice(proxy_list)
            app_ctrl.log_info(f"Selected proxy: {proxy}")
        else:
            app_ctrl.log_warn("Proxy is enabled, but the proxy list is empty.")

    app_ctrl.log("-" * 55, "neutral")
    app_ctrl.log_info(f"EXPERIMENT {exp_id}  |  {route_label}")
    app_ctrl.log_info(
        f"Platform: {platform}  |  Flight date: {flight_date_str}  |  Reps: {repetitions}")
    app_ctrl.log_info(f"UA: {ua_label}  |  Privacy: {privacy_mode_value}")
    app_ctrl.log("-" * 55, "neutral")

    all_results = []
    html_logs = []  

    for rep in range(1, repetitions + 1):
        if stop_event.is_set():
            app_ctrl.log_warn("Experiment stopped by the user.")
            break

        app_ctrl.log_info(f"Repetition {rep}/{repetitions}...")
        results = []
        max_driver_attempts = 2

        for driver_attempt in range(1, max_driver_attempts + 1):
            driver = None
            scraper_kwargs = None

            try:
                if driver_attempt > 1:
                    app_ctrl.log_warn(
                        "Restarting browser to retry the same route "
                        f"(attempt {driver_attempt}/{max_driver_attempts})."
                    )

                driver_privacy_mode = privacy_mode_value
                if (
                    privacy_mode_value == "No cookies (clean profile)"
                    and driver_attempt > 1
                ):
                    driver_privacy_mode = "Incognito"
                    app_ctrl.log_warn(
                        "Clean profile returned no price; falling back to "
                        "Incognito, which was the previous behavior for this mode."
                    )

                driver = create_driver(
                    privacy_mode=driver_privacy_mode,
                    headless=headless,
                    user_agent_label=ua_label,
                    proxy=proxy,
                )
                try:
                    app_ctrl.current_driver = driver
                except Exception:
                    pass

                scraper_kwargs = dict(
                    driver=driver, origin=origin, destination=destination,
                    flight_date_str=flight_date_str,
                    experiment_id=f"{exp_id}_R{rep}_T{driver_attempt}",
                    user_agent_label=ua_label,
                    privacy_mode=driver_privacy_mode,
                    log_fn=app_ctrl.log_info,
                    humanize=antidetect["humanize"],
                    should_stop=stop_event.is_set,
                )

                if platform == "LATAM":
                    try:
                        from latam_scraper import scrape_latam
                        results = scrape_latam(**scraper_kwargs)
                    except ImportError as e:
                        app_ctrl.log_err(f"latam_scraper.py not found: {e}")
                        results = []
                else:
                    app_ctrl.log_warn(f"Unknown platform '{platform}'.")
                    results = []

                if not results and antidetect["screenshot"]:
                    save_screenshot(
                        driver,
                        platform,
                        scraper_kwargs["experiment_id"],
                        "no_data",
                    )

                if antidetect["save_html"]:
                    path_h, hash_h = save_raw_html(
                        driver,
                        platform,
                        scraper_kwargs["experiment_id"],
                    )
                    if path_h:
                        html_logs.append((path_h, hash_h))

                if results:
                    break

                if is_driver_session_invalid(driver):
                    app_ctrl.log_warn("Chrome session dropped; a new attempt will be made.")
                elif driver_attempt < max_driver_attempts:
                    app_ctrl.log_warn(
                        "No price was returned in this attempt; retrying the same route."
                    )

            except Exception as e:
                app_ctrl.log_err(f"Error in repetition {rep}: {e}")
                if antidetect["screenshot"] and not is_driver_session_invalid(driver):
                    save_screenshot(driver, platform, f"{exp_id}_R{rep}", "error")
            finally:
                if driver:
                    try:
                        if getattr(app_ctrl, "current_driver", None) is driver:
                            app_ctrl.current_driver = None
                    except Exception:
                        pass
                    temporary_profile = getattr(driver, "_temporary_profile", None)
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    if temporary_profile:
                        shutil.rmtree(temporary_profile, ignore_errors=True)

        all_results.extend(results)
        app_ctrl.log_ok(f"Rep {rep}: {len(results)} record(s) collected.")

        if rep < repetitions and not stop_event.is_set():
            wait_time = random.uniform(8, 20)
            app_ctrl.log_info(f"Waiting {wait_time:.1f}s before the next repetition...")
            interruptible_sleep(wait_time, wait_time, enabled=antidetect["humanize"], should_stop=stop_event.is_set)

    if not all_results and config.get("record_no_data", False):
        all_results.append(
            build_no_data_record(
                platform,
                origin,
                destination,
                flight_date_str,
                privacy_mode_value,
                ua_label,
                exp_id,
            )
        )
        app_ctrl.log_warn(
            "No price found; adding an empty-data row to keep "
            "the route/window combination in the dataset."
        )

    if all_results:
        save_and_upload(app_ctrl, all_results, exp_id, html_logs=html_logs)
    else:
        app_ctrl.log_warn("No data collected in this experiment.")
        clean_html_logs_with_hash(app_ctrl, html_logs)

    app_ctrl.log_ok(
        f"Experiment {exp_id} completed. Total: {len(all_results)} record(s).")
    if config.get("show_dialogs", True):
        app_ctrl.info("Experiment Completed",
                      f"Experiment {exp_id} finished.\n"
                      f"{len(all_results)} records collected.")
    return all_results


# ─── SCHEDULER ────────────────────────────────────────────────────────────────
def run_platforms_sequentially(app_ctrl, platforms, base_config):
    platform_results = []

    for plat in platforms:

        if stop_event.is_set():
            break

        config = {
            **base_config,
            "platform": plat,
            "experiment_id": f"{base_config['experimento_id']}_{plat}",
        }

        try:
            results = run_experiment(app_ctrl, config)
            if results:
                platform_results.extend(results)
        except Exception as e:
            route_label = config.get("route_label", "unknown route")
            app_ctrl.log_err(
                f"Unexpected error in {route_label} / {plat}: {e}. "
                "Scheduler will continue to the next route."
            )
    return platform_results


def run_routes_sequentially(app_ctrl, routes, platforms, base_config):
    total_routes = len(routes)
    route_results = []

    for idx, route_label in enumerate(routes, 1):
        if stop_event.is_set():
            app_ctrl.log_warn(
                f"Scheduler stopped. {idx - 1}/{total_routes} route(s) processed."
            )
            break

        route_config = {
            **base_config,
            "route_label": route_label,
            "experiment_id": f"{base_config['experimento_id']}_ROTA{idx}",
        }

        app_ctrl.log_info(f"\nScheduler - Route {idx}/{total_routes}: {route_label}")
        results = run_platforms_sequentially(app_ctrl, platforms, route_config)
        if results:
            route_results.extend(results)

        if idx < total_routes and not stop_event.is_set():
            wait_time = random.uniform(10, 20)
            app_ctrl.log_info(f"Scheduler - waiting {wait_time:.1f}s before the next route...")
            interruptible_sleep(wait_time, wait_time, enabled=True, should_stop=stop_event.is_set)
    return route_results

def run_scheduler(app_ctrl, config):
    try:
        stop_event.clear()
        platforms = config["platforms"]
        configured_routes = config.get("routes") or list(AVAILABLE_ROUTES.keys())
        routes = [route for route in configured_routes if route in AVAILABLE_ROUTES]
        intervalo = int(config.get("interval_minutes", 60))
        jitter    = int(config.get("jitter_minutes", 8))
        cycle     = 0

        if not routes:
            app_ctrl.log_err("Scheduler did not start: no valid route selected.")
            app_ctrl.set_scheduler_status(False)
            return

        app_ctrl.log_ok(
            f"Scheduler started: {len(routes)} route(s), 1 repetition per route, "
            f"{len(config.get('janelas_dias', [30]))} window(s), "
            f"interval={intervalo}min +-{jitter}min"
        )
        app_ctrl.set_scheduler_status(True)

        advance_windows = config.get("advance_windows_days", [30])

        while not stop_event.is_set():
            cycle += 1
            app_ctrl.log_info(f"\n======== SCHEDULER - STARTING CYCLE {cycle} ========")
            
            for advance_window in advance_windows:
                if stop_event.is_set():
                    break
                    
                dynamic_flight_date = (datetime.now() + timedelta(days=advance_window)).strftime("%Y-%m-%d")
                
                cycle_config = {
                    **config,
                    "flight_date_str": dynamic_flight_date,
                    "experiment_id": f"{config['experimento_id']}_C{cycle}_{advance_window}D",
                    "record_no_data": True,
                }
                
                app_ctrl.log_info(f"\nSearching {advance_window} days ahead (Flight: {dynamic_flight_date})")
                run_routes_sequentially(app_ctrl, routes, platforms, cycle_config)

            if stop_event.is_set():
                break

            actual_wait = (intervalo * 60) + random.uniform(-jitter * 60, jitter * 60)
            actual_wait = max(60, actual_wait)
            next_run = datetime.now() + timedelta(seconds=actual_wait)
            
            app_ctrl.log_info(f"\n[!] Cycle {cycle} finished. Next collection at ~{next_run.strftime('%H:%M:%S')}")
            app_ctrl.frames["SchedulerPage"].set_next_run(next_run.strftime("%H:%M:%S"))

            for _ in range(int(actual_wait)):
                if stop_event.is_set():
                    break
                time.sleep(1)

    except Exception as e:
        app_ctrl.log_err(f"Critical error in Scheduler Thread: {str(e)}")
        import traceback
        app_ctrl.log_err(traceback.format_exc())
    finally:
        app_ctrl.log_warn("Scheduler stopped.")
        app_ctrl.set_scheduler_status(False)


# ─── SAVE EXCEL + GOOGLE SHEETS ─────────────────────────────────────────────

def save_excel(df, exp_id):
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    nome = RAW_DATA_DIR / f"passagens_{exp_id}_{ts}.xlsx"
    df.to_excel(nome, index=False, engine="openpyxl")
    if app_instance:
        app_instance.log_ok(f"Excel saved: {nome.name}")
    return nome


def clean_html_logs_with_hash(app_ctrl, html_logs):
    """Remove raw HTML files only after recalculating and checking their MD5 hashes."""
    if not html_logs:
        return

    app_ctrl.log_info(
        f"[LOG] Verifying hash for {len(html_logs)} HTML log(s) before deletion..."
    )
    removed_count = 0
    for path_h, hash_h in html_logs:
        if remove_html_log(path_h, expected_hash=hash_h, log_fn=app_ctrl.log_ok):
            removed_count += 1

    app_ctrl.log_ok(
        f"[LOG] {removed_count}/{len(html_logs)} HTML log(s) deleted "
        "with confirmed hash."
    )


def upload_to_google_sheets(app_ctrl, df, credentials_file, spreadsheet_id, worksheet_name="Data"):
    if app_ctrl is None:
        class _ConsoleAppCtrl:
            def log_info(self, msg): print(msg)
            def log_ok(self, msg): print(msg)
            def log_err(self, msg): print(msg)
            def error(self, title, msg): print(f"{title}: {msg}")

        app_ctrl = _ConsoleAppCtrl()

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    if not Path(credentials_file).exists():
        app_ctrl.log_err(f"[GSheets] Credential file not found: {credentials_file}")
        app_ctrl.error(
            "Google Sheets",
            f"Credential file not found:\n{credentials_file}",
        )
        return False

    try:
        credentials  = Credentials.from_service_account_file(credentials_file, scopes=scopes)
        client = gspread.authorize(credentials)
        sheet  = client.open_by_key(spreadsheet_id)

        try:
            worksheet = sheet.worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = sheet.add_worksheet(
                title=worksheet_name, rows=5000, cols=len(DATASET_COLUMNS)
            )
            app_ctrl.log_info(f"[GSheets] Worksheet '{worksheet_name}' created.")

        # Prepare the DataFrame for upload
        upload_df = df.copy()
        for col in upload_df.select_dtypes(
            include=["datetime64[ns]", "datetime"]
        ).columns:
            upload_df[col] = upload_df[col].dt.strftime("%Y-%m-%d %H:%M:%S")
        upload_df = upload_df.fillna("")

        new_rows = upload_df.values.tolist()

        # ── APPEND LOGIC ──────────────────────────────────────────────────
        existing_data = worksheet.get_all_values()

        if not existing_data:
            worksheet.update(
                range_name="A1",
                values=[upload_df.columns.tolist()] + new_rows,
            )
            app_ctrl.log_ok(
                f"[GSheets] Worksheet '{worksheet_name}' initialized with "
                f"{len(new_rows)} row(s) + header."
            )
        else:
            next_row = len(existing_data) + 1
            worksheet.update(
                range_name=f"A{next_row}",
                values=new_rows,
            )
            total = next_row - 1 + len(new_rows)
            app_ctrl.log_ok(
                f"[GSheets] +{len(new_rows)} row(s) added "
                f"(from row {next_row} onward | "
                f"accumulated total: {total} rows)."
            )
        # ─────────────────────────────────────────────────────────────────────

        return True

    except gspread.exceptions.SpreadsheetNotFound:
        app_ctrl.log_err(f"[GSheets] Spreadsheet not found: {spreadsheet_id}")
        app_ctrl.error(
            "Google Sheets",
            "Spreadsheet not found. Check the ID and sharing permissions.",
        )
        return False
    except Exception as e:
        app_ctrl.log_err(f"[GSheets] Error: {e}")
        app_ctrl.error("Google Sheets", f"Error sending data:\n{e}")
        return False


def normalize_dataset_columns(df):
    df = df.copy()
    for col in DATASET_COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[DATASET_COLUMNS]


def save_and_upload(app_ctrl, results, exp_id, html_logs=None):
    """
    Save the Excel file, upload to Google Sheets, and, after upload confirmation,
    remove the HTML log files after verifying their MD5 hashes.
    """
    df = pd.DataFrame(results)
    df = normalize_dataset_columns(df)

    save_excel(df, exp_id)

    cfg   = load_config()
    credentials_path  = cfg.get("GSHEETS", "credentials_file", fallback="")
    id_pl = cfg.get("GSHEETS", "spreadsheet_id",         fallback="")
    worksheet   = cfg.get("GSHEETS", "worksheet_name",             fallback="Data")

    if credentials_path and id_pl:
        app_ctrl.log_info("[GSheets] Sending to Google Sheets...")
        ok = upload_to_google_sheets(app_ctrl, df, credentials_path, id_pl, worksheet)
        if ok:
            app_ctrl.info("Google Sheets", "Data sent to Google Sheets successfully!")
            # ── Safely remove HTML logs after confirmed upload ──────────
            if html_logs:
                app_ctrl.log_info(
                    f"[LOG] Deleting {len(html_logs)} HTML log file(s) "
                    f"(hash verified before deletion)..."
                )
                removed_count = 0
                for path_h, hash_h in html_logs:
                    if remove_html_log(path_h, expected_hash=hash_h,
                                         log_fn=app_ctrl.log_ok):
                        removed_count += 1
                app_ctrl.log_ok(
                    f"[LOG] {removed_count}/{len(html_logs)} HTML log(s) deleted "
                    f"with confirmed integrity."
                )
            # ─────────────────────────────────────────────────────────────────
    else:
        app_ctrl.log_warn(
            "[GSheets] Credentials are not configured. Skipping automatic upload."
        )

    clean_html_logs_with_hash(app_ctrl, html_logs)


def upload_file_to_sheets(app_ctrl, xlsx_path):
    cfg   = load_config()
    credentials_path  = cfg.get("GSHEETS", "credentials_file", fallback="")
    id_pl = cfg.get("GSHEETS", "spreadsheet_id",         fallback="")
    worksheet   = cfg.get("GSHEETS", "worksheet_name",             fallback="Data")

    if not credentials_path or not id_pl:
        app_ctrl.warn("Google Sheets", "Configure credentials before sending.")
        return

    try:
        df = pd.read_excel(xlsx_path)
        df = normalize_dataset_columns(df)
        app_ctrl.log_info(f"[GSheets] Sending {Path(xlsx_path).name}...")
        upload_to_google_sheets(app_ctrl, df, credentials_path, id_pl, worksheet)
    except Exception as e:
        app_ctrl.log_err(f"[GSheets] Error loading file: {e}")


# ─── GUI ───────────────────────────────────────────────────────────────────────

class ScraperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Flight Fare Scraper")
        self.geometry("900x800")
        self.minsize(720, 620)
        self.resizable(True, True)
        self.configure(bg="#F0F4F8")

        self.stop_event        = stop_event
        self.scheduler_running = False
        self.current_driver    = None

        global app_instance
        app_instance = self

        self._setup_styles()

        container = ttk.Frame(self)
        container.pack(side="top", fill="both", expand=True, padx=15, pady=10)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.frames = {}
        for PageClass in (
            StartPage, ExperimentPage, SchedulerPage, HistoryPage, SettingsPage
        ):
            name  = PageClass.__name__
            frame = PageClass(parent=container, controller=self)
            self.frames[name] = frame
            frame.grid(row=0, column=0, sticky="nsew")

       
        log_frame = ttk.LabelFrame(self, text="Execution Log", padding="8 4 8 4")
        log_frame.pack(side="bottom", fill="x", padx=50, pady=(0, 0))
        
        
        log_frame.configure(height=600)
        log_frame.pack_propagate(False)

        self.log_widget = scrolledtext.ScrolledText(
            log_frame, height=14, wrap=tk.WORD, state="disabled",
            font=("Consolas", 9), bg="#0D1117", fg="#58D68D",
            insertbackground="white", relief=tk.FLAT, bd=0,
        )
        self.log_widget.pack(fill="both", expand=True, padx=2, pady=2)

        self.log_widget.tag_config("ok",     foreground="#58D68D")
        self.log_widget.tag_config("error",   foreground="#E74C3C")
        self.log_widget.tag_config("warning",  foreground="#F39C12")
        self.log_widget.tag_config("info",   foreground="#5DADE2")
        self.log_widget.tag_config("neutral", foreground="#BDC3C7")

        self.show_frame("StartPage")
        self.log_ok("System started. Welcome to Flight Fare Scraper!")
        self.log_info("Configure the parameters and start a collection or scheduler.")

    def _setup_styles(self):
        s = ttk.Style(self)
        s.theme_use("alt")

        s.configure("TFrame",      background="#F0F4F8")
        s.configure("TLabelframe", background="#F0F4F8")
        s.configure("TLabelframe.Label",
                    font=("Segoe UI", 10, "bold"), foreground="#1A3A5C")
        s.configure("TLabel",
                    background="#F0F4F8", font=("Segoe UI", 10), foreground="#2C3E50")
        s.configure("TEntry",       font=("Segoe UI", 10), fieldbackground="white")
        s.configure("TCombobox",    font=("Segoe UI", 10))
        s.configure("TCheckbutton", background="#F0F4F8", font=("Segoe UI", 10))
        s.configure("TSpinbox",     font=("Segoe UI", 10))

        btn_defs = {
            "Green":  ("#27AE60", "#1E8449"),
            "Blue":   ("#2980B9", "#1F618D"),
            "Orange": ("#E67E22", "#CA6F1E"),
            "Red":    ("#E74C3C", "#CB4335"),
            "Purple": ("#8E44AD", "#76448A"),
            "Teal":   ("#16A085", "#117A65"),
            "Gray":   ("#7F8C8D", "#626567"),
        }
        for name, (bg, active) in btn_defs.items():
            key = f"C.{name}.TButton"
            s.configure(key, background=bg, foreground="white",
                        font=("Segoe UI", 10, "bold"), borderwidth=0,
                        relief="flat", padding=(10, 6))
            s.map(key,
                  background=[("active", active), ("disabled", "#BDC3C7")],
                  foreground=[("disabled", "#ECF0F1")])

    def show_frame(self, name):
        self.frames[name].tkraise()

    def log(self, message, tag="neutral"):
        def _insert():
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_widget.configure(state="normal")
            self.log_widget.insert(tk.END, f"[{ts}] {message}\n", tag)
            self.log_widget.see(tk.END)
            self.log_widget.configure(state="disabled")
        self.after(0, _insert)

    def log_ok(self,   msg): self.log(f"[OK]   {msg}", "ok")
    def log_err(self,  msg): self.log(f"[ERR]  {msg}", "error")
    def log_warn(self, msg): self.log(f"[WARN] {msg}", "warning")
    def log_info(self, msg): self.log(f"[INFO] {msg}", "info")

    def info(self,  title, msg): self.after(0, lambda: messagebox.showinfo(title, msg))
    def warn(self,  title, msg): self.after(0, lambda: messagebox.showwarning(title, msg))
    def error(self, title, msg): self.after(0, lambda: messagebox.showerror(title, msg))

    def log_message_gui(self, msg):          self.log_info(msg)
    def show_info_messagebox(self, t, m):    self.info(t, m)
    def show_warning_messagebox(self, t, m): self.warn(t, m)
    def show_error_messagebox(self, t, m):   self.error(t, m)

    def request_stop(self, message_text="Stop signal sent."):
        self.stop_event.set()
        self.log_warn(message_text)
        driver = self.current_driver
        if driver:
            start_thread(self._close_active_driver, driver)

    def _close_active_driver(self, driver):
        try:
            driver.quit()
        except Exception:
            pass

    def set_scheduler_status(self, running):
        self.scheduler_running = running
        self.after(0, lambda: [
            self.frames["SchedulerPage"].update_status_badge(running),
            self.frames["StartPage"].update_scheduler_badge(running),
        ])


# ─── START PAGE ───────────────────────────────────────────────────────────

class StartPage(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller

        ttk.Label(self,
                  text="AirPrice Scraper",
                  font=("Segoe UI", 20, "bold"),
                  foreground="#1A3A5C").pack(pady=(28, 4))
        ttk.Label(self,
                  text="Created by Anonymous",
                  font=("Segoe UI", 11, "italic"),
                  foreground="#6A7879").pack(pady=(0, 22))

        nav = ttk.LabelFrame(self, text="System Modules", padding="18 12 18 12")
        nav.pack(padx=50, fill="x")

        buttons = [
            ("Manual Collection",
             "ExperimentPage", "C.Green.TButton",
             "Runs a collection for specific route, date, and parameters"),
            ("Automatic Scheduler",
             "SchedulerPage",   "C.Blue.TButton",
             "Continuous high-frequency collection (hourly or more)"),
            ("History & Export",
             "HistoryPage",   "C.Teal.TButton",
             "View generated files and send them to Google Sheets"),
            ("Settings",
             "SettingsPage",      "C.Gray.TButton",
             "Google Sheets, proxies, anti-detection, and preferences"),
        ]

        for label, page, style, hint in buttons:
            row = ttk.Frame(nav)
            row.pack(fill="x", pady=5)
            ttk.Button(row, text=label, style=style, width=28,
                       command=lambda p=page: controller.show_frame(p)
                       ).pack(side="left", ipady=5)
            ttk.Label(row, text=hint,
                      font=("Segoe UI", 9),
                      foreground="#475353").pack(side="left", padx=14)

        badge_row = ttk.Frame(self)
        badge_row.pack(pady=18)
        ttk.Label(badge_row, text="Scheduler:",
                  font=("Segoe UI", 10)).pack(side="left")
        self.badge_label = ttk.Label(
            badge_row, text="  INACTIVE  ",
            font=("Segoe UI", 10, "bold"),
            background="#E74C3C", foreground="white",
            relief="flat", padding=(6, 2),
        )
        self.badge_label.pack(side="left", padx=8)

        ttk.Button(self, text="Stop Current Process",
                   style="C.Red.TButton",
                   command=self._stop).pack(pady=(0, 10), ipadx=12, ipady=4)

    def update_scheduler_badge(self, running):
        if running:
            self.badge_label.configure(text="  ACTIVE  ",   background="#27AE60")
        else:
            self.badge_label.configure(text="  INACTIVE  ", background="#E74C3C")

    def _stop(self):
        self.controller.request_stop("Stop signal sent to all processes.")


# ─── MANUAL COLLECTION PAGE ──────────────────────────────────────────────────

class ExperimentPage(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller

        ttk.Label(self, text="Manual Flight Collection",
                  font=("Segoe UI", 15, "bold"), foreground="#1A3A5C").pack(pady=(20, 14))

        form = ttk.LabelFrame(
            self, text="Experiment Parameters", padding="16 10 16 10"
        )
        form.pack(padx=30, fill="x")
        
        form.columnconfigure(1, weight=1)

        def lbl_entry(row, text, default):
            ttk.Label(form, text=text).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 10))
            var = tk.StringVar(value=default)
            ttk.Entry(form, textvariable=var, font=("Segoe UI", 10)).grid(row=row, column=1, sticky="ew", pady=5)
            return var

        def lbl_combo(row, text, values, default):
            ttk.Label(form, text=text).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 10))
            var = tk.StringVar(value=default)
            ttk.Combobox(form, textvariable=var, values=values, state="readonly").grid(row=row, column=1, sticky="ew", pady=5)
            return var

        self.exp_id_var = lbl_entry(0, "Experiment ID:", "E1")
        
        # ─── Route Selection ───────────────────────────────
        ttk.Label(form, text="Routes:").grid(row=1, column=0, sticky="nw", pady=5, padx=(0, 10))
        routes_frame = ttk.Frame(form)
        routes_frame.grid(row=1, column=1, sticky="ew", pady=5)
        
        self.route_vars = {}
        for i, route in enumerate(list(AVAILABLE_ROUTES.keys())):
            var = tk.BooleanVar(value=True)
            self.route_vars[route] = var
            ttk.Checkbutton(routes_frame, text=route, variable=var).pack(side="left", padx=(0, 15), pady=2)
        
        btn_todas = ttk.Button(routes_frame, text="All", width=6, command=self._select_all_routes)
        btn_todas.pack(side="left", padx=(10, 5))
        
        btn_nenhuma = ttk.Button(routes_frame, text="None", width=8, command=self._select_no_routes)
        btn_nenhuma.pack(side="left", padx=(0, 5))

        ttk.Label(form, text="Platform:").grid(row=2, column=0, sticky="w", pady=5, padx=(0, 10))
        ttk.Label(form, text="LATAM (fixed)", font=("Segoe UI", 10, "bold")).grid(row=2, column=1, sticky="w", pady=5)

        # Calendar
        ttk.Label(form, text="Flight Date:").grid(row=3, column=0, sticky="w", pady=5, padx=(0, 10))
        self.calendar = DateEntry(
            form, background="darkblue", foreground="white", borderwidth=2,
            date_pattern="mm/dd/yyyy", locale="en_US", mindate=datetime.now()
        )
        self.calendar.grid(row=3, column=1, sticky="ew", pady=5)

        ua_keys = list(USER_AGENTS.keys())
        ua_default = ua_keys[0] if ua_keys else ""
        self.ua_var   = lbl_combo(4, "User-Agent:", ua_keys, ua_default)
        self.priv_var = lbl_combo(5, "Privacy Mode:", PRIVACY_MODES, "No cookies (clean profile)")

        ttk.Label(form, text="Repetitions (min. 1):").grid(row=6, column=0, sticky="w", pady=5, padx=(0, 10))
        self.rep_var = tk.IntVar(value=3)
        ttk.Spinbox(form, from_=3, to=10, textvariable=self.rep_var, font=("Segoe UI", 10)).grid(row=6, column=1, sticky="ew", pady=5)

        ttk.Label(form, text="Headless Mode (no window):").grid(row=7, column=0, sticky="w", pady=5, padx=(0, 10))
        self.headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, variable=self.headless_var).grid(row=7, column=1, sticky="w", pady=5)

        btn_row = ttk.Frame(self)
        btn_row.pack(pady=16, padx=30, fill="x")
        ttk.Button(btn_row, text="Start Collection", style="C.Green.TButton", command=self._start).pack(side="left", expand=True, fill="x", padx=(0, 6), ipady=5)
        ttk.Button(btn_row, text="Stop", style="C.Red.TButton", command=lambda: self.controller.request_stop("Stop signal sent.")).pack(side="left", expand=True, fill="x", padx=(6, 0), ipady=5)
        ttk.Button(self, text="<- Back to Home", style="C.Gray.TButton", command=lambda: controller.show_frame("StartPage")).pack(pady=(0, 10))

    def _select_all_routes(self):
        for var in self.route_vars.values():
            var.set(True)

    def _select_no_routes(self):
        for var in self.route_vars.values():
            var.set(False)

    def _start(self):
        try:
            flight_date = self.calendar.get_date().strftime("%Y-%m-%d")
        except ValueError:
            self.controller.error("Invalid Date", "Use the calendar to select a date.")
            return

        selected_routes = [route for route, var in self.route_vars.items() if var.get()]
        if not selected_routes:
            self.controller.error("No Route Selected", "Select at least one route to collect data.")
            return

        selected_platforms = ["LATAM"]
        self.controller.stop_event.clear()

        def job_sequencial():
            total_records = 0
            routes_with_data = 0
            for idx, route_label in enumerate(selected_routes, 1):
                if stop_event.is_set():
                    self.controller.log_warn(f"Collection stopped by the user. {idx}/{len(selected_routes)} routes processed.")
                    break
                base_config = {
                    "route_label": route_label, "flight_date_str": flight_date,
                    "privacy_mode": self.priv_var.get(), "user_agent_label": self.ua_var.get(),
                    "experiment_id": f"{self.exp_id_var.get().strip()}_R{idx}",
                    "repetitions": self.rep_var.get(), "headless": self.headless_var.get(),
                    "show_dialogs": False,
                }
                self.controller.log_info(f"\n=== ROUTE {idx}/{len(selected_routes)}: {route_label} ===")
                results = run_platforms_sequentially(self.controller, selected_platforms, base_config)
                total_records += len(results)
                if results:
                    routes_with_data += 1
                if idx < len(selected_routes) and not stop_event.is_set():
                    wait_time = random.uniform(10, 20)
                    self.controller.log_info(f"Waiting {wait_time:.1f}s before the next route...")
                    interruptible_sleep(wait_time, wait_time, enabled=True, should_stop=stop_event.is_set)
            
            if not stop_event.is_set():
                summary = f"Collection completed: {routes_with_data}/{len(selected_routes)} route(s) with data, {total_records} saved record(s)."
                self.controller.log_ok(f"\n{summary}")
                self.controller.info("Collection Complete", summary)

        start_thread(job_sequencial)


# ─── SCHEDULER PAGE ──────────────────────────────────────────────────────

class SchedulerPage(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller

        ttk.Label(self, text="High-Frequency Scheduler",
                  font=("Segoe UI", 15, "bold"), foreground="#1A3A5C").pack(pady=(20, 14))

        badge_row = ttk.Frame(self)
        badge_row.pack(pady=(0, 10))
        ttk.Label(badge_row, text="Status:", font=("Segoe UI", 11)).pack(side="left")
        self.status_badge = ttk.Label(
            badge_row, text="  INACTIVE  ", font=("Segoe UI", 11, "bold"),
            background="#E74C3C", foreground="white", relief="flat", padding=(8, 3)
        )
        self.status_badge.pack(side="left", padx=10)

        cfg_frame = ttk.LabelFrame(
            self, text="Scheduler Settings", padding="16 10 16 10"
        )
        cfg_frame.pack(padx=30, fill="x")
        
        cfg_frame.columnconfigure(1, weight=1)
        cfg_frame.columnconfigure(3, weight=1)

        ttk.Label(cfg_frame, text="Frequency preset:").grid(row=0, column=0, sticky="w", pady=6, padx=(0, 10))
        self.preset_var = tk.StringVar(value="medium (1h)")
        preset_cb = ttk.Combobox(
            cfg_frame, textvariable=self.preset_var,
            values=["low (3h)", "medium (1h)", "high (30 min)", "maximum (15 min)"],
            state="readonly"
        )
        preset_cb.grid(row=0, column=1, columnspan=3, sticky="ew", pady=6)
        preset_cb.bind("<<ComboboxSelected>>", self._apply_preset)

        ttk.Label(cfg_frame, text="Interval (min):").grid(row=1, column=0, sticky="w", pady=6, padx=(0, 10))
        self.interval_var = tk.IntVar(value=60)
        ttk.Spinbox(cfg_frame, from_=5, to=360, textvariable=self.interval_var, font=("Segoe UI", 10)).grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(cfg_frame, text="Jitter +/- (min):").grid(row=1, column=2, sticky="w", pady=6, padx=(20, 10))
        self.jitter_var = tk.IntVar(value=8)
        ttk.Spinbox(cfg_frame, from_=0, to=30, textvariable=self.jitter_var, font=("Segoe UI", 10)).grid(row=1, column=3, sticky="ew", pady=6)

        ttk.Label(cfg_frame, text="Routes:").grid(row=2, column=0, sticky="w", pady=6, padx=(0, 10))
        routes_frame = ttk.Frame(cfg_frame)
        routes_frame.grid(row=2, column=1, columnspan=3, sticky="ew", pady=6)

        self.scheduler_route_vars = {}
        for route in list(AVAILABLE_ROUTES.keys()):
            var = tk.BooleanVar(value=True)
            self.scheduler_route_vars[route] = var
            ttk.Checkbutton(routes_frame, text=route, variable=var).pack(side="left", padx=(0, 12), pady=2)

        ttk.Button(routes_frame, text="All", width=6, command=self._select_all_scheduler_routes).pack(side="left", padx=(8, 5))
        ttk.Button(routes_frame, text="None", width=8, command=self._select_no_scheduler_routes).pack(side="left", padx=(0, 5))

        ttk.Label(cfg_frame, text="Platform:").grid(row=3, column=0, sticky="w", pady=6, padx=(0, 10))
        ttk.Label(cfg_frame, text="LATAM (fixed)", font=("Segoe UI", 10, "bold")).grid(row=3, column=1, sticky="w", pady=6)

        ttk.Label(cfg_frame, text="User-Agent:").grid(row=4, column=0, sticky="w", pady=6, padx=(0, 10))
        self.ua_var = tk.StringVar(value="Chrome Desktop (default)")
        ttk.Combobox(
            cfg_frame, textvariable=self.ua_var, values=list(USER_AGENTS.keys()), state="readonly"
        ).grid(row=4, column=1, columnspan=3, sticky="ew", pady=6)

        ttk.Label(cfg_frame, text="Privacy Mode:").grid(row=5, column=0, sticky="w", pady=6, padx=(0, 10))
        self.priv_var = tk.StringVar(value="No cookies (clean profile)")
        ttk.Combobox(
            cfg_frame, textvariable=self.priv_var, values=PRIVACY_MODES, state="readonly"
        ).grid(row=5, column=1, columnspan=3, sticky="ew", pady=6)

        ttk.Label(cfg_frame, text="Windows (advance):").grid(row=6, column=0, sticky="w", pady=6, padx=(0, 10))
        windows_frame = ttk.Frame(cfg_frame)
        windows_frame.grid(row=6, column=1, columnspan=3, sticky="ew", pady=6)

        self.window_vars = {}
        for days_list in [7, 15, 30, 90]:
            var = tk.BooleanVar(value=True)
            self.window_vars[days_list] = var
            ttk.Checkbutton(windows_frame, text=f"{days_list} days", variable=var).pack(side="left", padx=(0, 15), pady=2)

        ttk.Label(cfg_frame, text="Experiment ID:").grid(row=7, column=0, sticky="w", pady=6, padx=(0, 10))
        self.exp_var = tk.StringVar(value="SCHED-01")
        ttk.Entry(cfg_frame, textvariable=self.exp_var, font=("Segoe UI", 10)).grid(row=7, column=1, columnspan=3, sticky="ew", pady=6)

        ttk.Label(cfg_frame, text="Headless Mode (no window):").grid(row=8, column=0, sticky="w", pady=6, padx=(0, 10))
        self.headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg_frame, variable=self.headless_var).grid(row=8, column=1, sticky="w", pady=6)

        self.next_run_label = ttk.Label(self, text="", font=("Segoe UI", 9, "italic"), foreground="#7F8C8D")
        self.next_run_label.pack(pady=(8, 0))

        btn_row = ttk.Frame(self)
        btn_row.pack(pady=14, padx=30, fill="x")

        self.start_btn = ttk.Button(btn_row, text="Start Scheduler", style="C.Green.TButton", command=self._start_scheduler)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 6), ipady=5)

        self.stop_btn = ttk.Button(btn_row, text="Stop Scheduler", style="C.Red.TButton", command=self._stop_scheduler, state="disabled")
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=(6, 0), ipady=5)

        ttk.Button(self, text="<- Back to Home", style="C.Gray.TButton", command=lambda: controller.show_frame("StartPage")).pack(pady=(0, 10))

    def _apply_preset(self, event=None):
        presets = {
            "low (3h)":             (180, 15),
            "medium (1h)":          (60,  8),
            "high (30 min)":        (30,  5),
            "maximum (15 min)":     (15,  2),
        }
        iv, jv = presets.get(self.preset_var.get(), (60, 8))
        self.interval_var.set(iv)
        self.jitter_var.set(jv)

    def _select_all_scheduler_routes(self):
        for var in self.scheduler_route_vars.values():
            var.set(True)

    def _select_no_scheduler_routes(self):
        for var in self.scheduler_route_vars.values():
            var.set(False)

    def _start_scheduler(self):
        platforms = ["LATAM"]
        selected_routes = [route for route, var in self.scheduler_route_vars.items() if var.get()]

        if not selected_routes:
            self.controller.error("No Route Selected", "Select at least one route to start the scheduler.")
            return
            
        selected_windows = [days_list for days_list, var in self.window_vars.items() if var.get()]
        if not selected_windows:
            self.controller.error("No Window Selected", "Select at least one advance window.")
            return

        config = {
            "routes":            selected_routes,
            "platforms":      platforms,
            "advance_windows_days":     selected_windows,
            "user_agent_label": self.ua_var.get(),
            "privacy_mode": self.priv_var.get(),
            "experiment_id":   self.exp_var.get().strip() or "AGEND-01",
            "repetitions":       1,
            "headless":         self.headless_var.get(),
            "interval_minutes":    self.interval_var.get(),
            "jitter_minutes":       self.jitter_var.get(),
            "show_dialogs":     False,
        }
        self.controller.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        start_thread(run_scheduler, self.controller, config)

    def _stop_scheduler(self):
        self.controller.request_stop("Stop signal sent to the scheduler...")
        self.stop_btn.configure(state="disabled")
        self.start_btn.configure(state="normal")

    def update_status_badge(self, running):
        if running:
            self.status_badge.configure(text="  ACTIVE  ",   background="#27AE60")
        else:
            self.status_badge.configure(text="  INACTIVE  ", background="#E74C3C")
            self.next_run_label.configure(text="")
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")

    def set_next_run(self, time_str):
        self.after(0, lambda: self.next_run_label.configure(text=f"Next collection scheduled at {time_str}"))

# ─── HISTORY PAGE ──────────────────────────────────────────────────────

class HistoryPage(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller

        ttk.Label(self, text="History & Export",
                  font=("Segoe UI", 15, "bold"), foreground="#1A3A5C").pack(pady=(20, 4))
        ttk.Label(self, text=f"Files in: {RAW_DATA_DIR}",
                  font=("Segoe UI", 9), foreground="#7F8C8D").pack(pady=(0, 10))

        list_frame = ttk.LabelFrame(self, text="Generated Excel Files", padding="10 8")
        list_frame.pack(padx=30, fill="both", expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        self.listbox = tk.Listbox(
            list_frame,
            bg="white", fg="#2C3E50",
            selectbackground="#2980B9", selectforeground="white",
            font=("Consolas", 10), relief=tk.FLAT, borderwidth=1,
            activestyle="none",
            yscrollcommand=scrollbar.set,
        )
        scrollbar.configure(command=self.listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.listbox.pack(fill="both", expand=True, padx=2, pady=2)

        btn_row = ttk.Frame(self)
        btn_row.pack(pady=10, fill="x", padx=30)

        ttk.Button(btn_row, text="Refresh List", style="C.Blue.TButton",
                   command=self._refresh
                   ).pack(side="left", expand=True, fill="x", padx=(0, 6), ipady=5)
        ttk.Button(btn_row, text="Send to Google Sheets", style="C.Green.TButton",
                   command=self._upload_selected
                   ).pack(side="left", expand=True, fill="x", padx=(6, 6), ipady=5)
        ttk.Button(
            btn_row, text="Open Folder", style="C.Orange.TButton",
            command=lambda: (
                os.startfile(RAW_DATA_DIR) if os.name == "nt"
                else os.system(f'open "{RAW_DATA_DIR}"')
            ),
        ).pack(side="left", expand=True, fill="x", padx=(6, 0), ipady=5)

        ttk.Button(self, text="<- Back to Home", style="C.Gray.TButton",
                   command=lambda: controller.show_frame("StartPage")).pack(pady=(0, 10))

        self._refresh()

    def _refresh(self):
        self.listbox.delete(0, tk.END)
        files = sorted(
            RAW_DATA_DIR.glob("*.xlsx"),
            key=os.path.getmtime, reverse=True,
        )
        for f in files:
            self.listbox.insert(tk.END, f.name)
        self.controller.log_info(
            f"History: {len(files)} file(s) found."
        )

    def _upload_selected(self):
        sel = self.listbox.curselection()
        if not sel:
            self.controller.warn("History", "Select a file from the list.")
            return
        nome = self.listbox.get(sel[0])
        path = str(RAW_DATA_DIR / nome)
        self.controller.log_info(f"Sending {nome} to Google Sheets...")
        start_thread(upload_file_to_sheets, self.controller, path)


# ─── SETTINGS PAGE ──────────────────────────────────────────────────

class SettingsPage(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller

        ttk.Label(self, text="Experiment Settings",
                  font=("Segoe UI", 15, "bold"), foreground="#1A3A5C").pack(pady=(20, 14))

        cfg = load_config()

        # Google Sheets
        gs_frame = ttk.LabelFrame(self, text="Google Sheets", padding="16 10 16 10")
        gs_frame.pack(padx=30, fill="x")
        gs_frame.columnconfigure(1, weight=1)

        def lbl_entry(parent, row, text, default):
            ttk.Label(parent, text=text).grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=5
            )
            var = tk.StringVar(value=default)
            ttk.Entry(parent, textvariable=var, font=("Segoe UI", 10)
                      ).grid(row=row, column=1, sticky="ew", pady=5)
            return var

        self.cred_var = lbl_entry(
            gs_frame, 0, "Credentials file (.json):",
            cfg.get("GSHEETS", "credentials_file", fallback=""),
        )
        self.plan_var = lbl_entry(
            gs_frame, 1, "Google Sheets Spreadsheet ID:",
            cfg.get("GSHEETS", "spreadsheet_id", fallback=""),
        )
        self.worksheet_var = lbl_entry(
            gs_frame, 2, "Worksheet name:",
            cfg.get("GSHEETS", "worksheet_name", fallback="Data"),
        )

        ttk.Button(gs_frame, text="Select credential (.json)",
                   style="C.Gray.TButton",
                   command=self._select_credentials_file
                   ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0), ipady=4)

        # Anti-detection settings
        ad_frame = ttk.LabelFrame(self, text="Anti-detection", padding="14 10 14 10")
        ad_frame.pack(padx=30, pady=10, fill="x")

        self.humanize_var   = tk.BooleanVar(
            value=cfg.getboolean("ANTIDETECT", "humanize", fallback=True)
        )
        self.save_html_var  = tk.BooleanVar(
            value=cfg.getboolean("ANTIDETECT", "save_html", fallback=True)
        )
        self.screenshot_var = tk.BooleanVar(
            value=cfg.getboolean("ANTIDETECT", "screenshot", fallback=False)
        )

        ttk.Checkbutton(ad_frame,
                        text="Human-like delays between requests (recommended)",
                        variable=self.humanize_var).pack(anchor="w", pady=2)
        ttk.Checkbutton(ad_frame,
                        text="Save compressed raw HTML for each collection",
                        variable=self.save_html_var).pack(anchor="w", pady=2)
        ttk.Checkbutton(ad_frame,
                        text="Automatic screenshot on error",
                        variable=self.screenshot_var).pack(anchor="w", pady=2)

        # Proxies
        proxy_frame = ttk.LabelFrame(
            self, text="Proxies (optional)", padding="14 10 14 10"
        )
        proxy_frame.pack(padx=30, pady=(0, 10), fill="x")

        self.proxy_enabled = tk.BooleanVar(
            value=cfg.getboolean("PROXY", "enabled", fallback=False)
        )
        ttk.Checkbutton(proxy_frame, text="Enable rotating proxies",
                        variable=self.proxy_enabled).pack(anchor="w", pady=(0, 4))

        ttk.Label(proxy_frame, text="List (host:port - one per line):",
                  font=("Segoe UI", 9)).pack(anchor="w")
        self.proxy_list = tk.Text(
            proxy_frame, height=3, font=("Consolas", 9),
            bg="white", relief=tk.FLAT, bd=1,
        )
        self.proxy_list.pack(fill="x", pady=4)
        proxy_default = cfg.get("PROXY", "list", fallback="").strip()
        if proxy_default:
            self.proxy_list.insert("1.0", proxy_default)
        else:
            self.proxy_list.insert("1.0", "# Example: 192.168.1.1:8080\n# user:pass@host:port")

        ttk.Button(self, text="Save Settings",
                   style="C.Blue.TButton",
                   command=self._save).pack(pady=10, ipadx=10, ipady=5)

        ttk.Button(self, text="<- Back to Home", style="C.Gray.TButton",
                   command=lambda: controller.show_frame("StartPage")).pack(pady=(0, 10))

    def _select_credentials_file(self):
        path = filedialog.askopenfilename(
            title="Select Google credential (.json)",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.cred_var.set(path)

    def _save(self):
        cfg = load_config()
        if not cfg.has_section("GSHEETS"):
            cfg.add_section("GSHEETS")
        cfg["GSHEETS"]["credentials_file"] = self.cred_var.get()
        cfg["GSHEETS"]["spreadsheet_id"]         = self.plan_var.get()
        cfg["GSHEETS"]["worksheet_name"]            = self.worksheet_var.get()

        if not cfg.has_section("ANTIDETECT"):
            cfg.add_section("ANTIDETECT")
        cfg["ANTIDETECT"]["humanize"]   = str(self.humanize_var.get())
        cfg["ANTIDETECT"]["save_html"]  = str(self.save_html_var.get())
        cfg["ANTIDETECT"]["screenshot"] = str(self.screenshot_var.get())

        if not cfg.has_section("PROXY"):
            cfg.add_section("PROXY")
        cfg["PROXY"]["enabled"] = str(self.proxy_enabled.get())
        cfg["PROXY"]["list"]    = self.proxy_list.get("1.0", tk.END).strip()

        save_config(cfg)
        self.controller.log_ok("Settings saved successfully.")
        self.controller.info("Settings", "Saved successfully!")


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = ScraperApp()
    app.mainloop()
