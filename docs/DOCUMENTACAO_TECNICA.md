# 📖 Technical Documentation and System Architecture

This document describes the technical structure, execution flow, and implementation decisions of the LATAM airfare collection system.

## 1. 🏗️ Architecture Overview

The project is implemented in Python and separates the graphical interface, collection orchestration, the LATAM-specific scraper, and normalization utilities.

- **Graphical interface:** implemented with `tkinter`, `ttk`, `scrolledtext`, and `tkcalendar`. The GUI supports manual collection, scheduler start/stop, Google Sheets configuration, proxy and anti-detection settings, and execution-log monitoring in a visual text area.
- **Background execution:** long-running tasks are started in `threading.Thread` through `start_thread()`, preventing the main window from freezing during browser automation.
- **Scraping backend:** implemented with `Selenium WebDriver` and Chrome. Selenium is used because the LATAM page is dynamic and depends on JavaScript rendering.
- **Local persistence:** collected records are standardized as dictionaries, normalized in a `pandas` `DataFrame`, and exported as `.xlsx` files under `DADOS_BRUTOS/` using `openpyxl`.
- **Cloud persistence:** when configured, Google Sheets upload is performed with `gspread` and `google.oauth2.service_account.Credentials` using a Service Account JSON file.
- **Logs and evidence:** error screenshots and compressed raw HTML can be stored under `LOGS/` according to the flags defined in `config.ini`.

## 2. 🧱 Main Modules

### `scraper_passagens.py`

This is the central module. It contains:

- route constants (`AVAILABLE_ROUTES`), user agents, privacy modes, and dataset columns;
- Chrome WebDriver creation and configuration;
- experiment, platform, route, and scheduler-cycle execution;
- Excel persistence;
- Google Sheets upload;
- safe raw-HTML cleanup with MD5 checking;
- graphical-interface classes.

### `latam_scraper.py`

Contains the LATAM-specific scraper. Its main function is `scrape_latam()`.

It builds a LATAM URL from origin, destination, and flight date, opens the page through Selenium, closes cookie banners when possible, identifies blocking indicators, searches for flight cards, extracts the lowest valid fare, and returns records that follow the dataset schema.

### `scrapers_utils.py`

Provides helper functions for:

- normalizing price strings;
- extracting the lowest fare from flight cards;
- building the final collection record;
- classifying the time-of-day window of a query;
- generating collection IDs;
- applying interruptible sleeps;
- calculating MD5 hashes for compressed HTML files;
- removing HTML logs after integrity checking.

### `coleta_tcc.py`

Academic-execution script. It runs all registered routes for the `[7, 15, 30, 90]` advance-purchase windows, always in headless mode, with one repetition per route and `record_no_data=True`.

## 3. 🔄 Execution Flow

The main execution flow uses the orchestration functions in `scraper_passagens.py`:

1. The GUI, scheduler, or `coleta_tcc.py` creates a `base_config` containing route, flight date, privacy mode, user agent, repetition count, headless option, and experiment identifier.
2. `run_routes_sequentially()` iterates over the selected routes.
3. `run_platforms_sequentially()` iterates over the configured platforms. In the current project state, the implemented platform is `LATAM`.
4. `run_experiment()` creates the browser, calls `scrape_latam()`, and controls driver attempts for each repetition.
5. When a result is returned, the records are accumulated.
6. When no result is returned and `record_no_data=True`, the system creates a standardized row with no fare so that the route/window combination remains represented in the dataset.
7. Finally, `save_and_upload()` saves the Excel file, attempts a Google Sheets upload when credentials are configured, and removes raw HTML artifacts after hash checking.

## 4. 🌐 Browser Configuration

`create_driver()` configures Chrome WebDriver with:

- selectable user agent;
- `Incognito` mode;
- `Normal (with cookies)` mode;
- `No cookies (clean profile)` mode, implemented with a temporary Chrome profile;
- optional headless mode with a `1920x1080` window;
- optional proxy;
- page-load and script timeouts;
- options intended to reduce common automation signals, including `--disable-blink-features=AutomationControlled`, `excludeSwitches=["enable-automation"]`, and an override of `navigator.webdriver`.

The current code does not explicitly block browser image loading.

## 5. 🎯 LATAM Collection Strategy

`scrape_latam()` builds URLs in the following form:

```text
https://www.latamairlines.com/br/pt/oferta-voos?origin=ORIGIN&outbound=DATE...&destination=DESTINATION...
```

After opening the page, the scraper:

- waits through `interruptible_sleep()`, respecting the `humanize` option;
- attempts to close cookie banners;
- checks indicators of blocking, such as reCAPTCHA or challenge pages;
- searches for cards using primary and alternative CSS selectors;
- reloads the page when no cards are found;
- detects LATAM slow/error or unavailability states;
- extracts prices using specific selectors and a textual fallback;
- selects the lowest valid fare;
- extracts departure and arrival times using selectors and regex-based fallbacks;
- builds the final record with fare, route, date, advance-purchase window, times, user agent, privacy mode, and `experimento_id`.

Wait control is implemented with loops, `find_elements()`, and interruptible sleeps.

## 6. 🗃️ Data Structure

The final dataset follows the columns defined in `DATASET_COLUMNS`:

```text
id_coleta, data_hora_local, janela_horario, plataforma, url_consultada,
origem, destino, data_voo, dias_antecedencia, preco_brl, hora_saida,
hora_chegada, modo_privacidade, user_agent_label, captcha_detectado,
dia_semana_busca, experimento_id
```

These field names are preserved in Portuguese in the translated code because they constitute the existing dataset schema and are used by the study data.

Before saving or uploading, `normalize_dataset_columns()` ensures that all expected columns are present and ordered consistently.

## 7. 📊 Excel and Google Sheets

`save_excel()` writes an `.xlsx` file under `DADOS_BRUTOS/` using a filename based on the experiment ID and timestamp.

When `credentials_file` and `spreadsheet_id` are configured in `config/config.ini`, `upload_to_google_sheets()`:

1. authenticates the Service Account;
2. opens the spreadsheet by ID;
3. locates or creates the configured worksheet;
4. creates the header when the worksheet is empty;
5. appends new rows below the last existing row.

If credentials are not configured, the system retains only the local Excel file and records a warning in the log.

## 8. 🧹 Evidence, Raw HTML, and Cleanup

Evidence options are defined under `[ANTIDETECT]` in `config.ini`:

- `screenshot=True`: stores a screenshot under `LOGS/` when an attempt returns no data or when an error occurs while the browser session remains valid.
- `save_html=True`: saves the current page HTML as `.html.gz` under `LOGS/` and computes the file's MD5 hash.

After data persistence, raw HTML files associated with the experiment are removed by `remove_html_log()` only after the expected MD5 hash is recalculated and compared. This reduces the risk of deleting a different file from the one that was recorded.

In the current implementation, HTML cleanup is called at the end of `save_and_upload()`. When Google Sheets upload succeeds, an immediate cleanup also occurs inside the success block, followed by the general cleanup call. As a result, the log may show one successful removal followed by messages indicating that the same file had already been removed.

## 9. ⏱️ Scheduler

The scheduler runs in a separate thread and executes continuous cycles until a stop signal is received.

Each cycle:

- iterates over the configured advance-purchase windows;
- computes the flight date dynamically with `datetime.now() + window`;
- runs the selected routes;
- uses `record_no_data=True`;
- waits for the next cycle using the configured interval and a random jitter.

Stop control uses `stop_event`, which allows the scheduler to terminate and also interrupts humanized waits.

## 10. 🎓 Academic Collection (`coleta_tcc.py`)

`coleta_tcc.py` executes the standardized protocol with:

- routes: all routes registered in `AVAILABLE_ROUTES`;
- platform: `LATAM`;
- windows: `[7, 15, 30, 90]`;
- repetitions: `1`;
- browser: headless;
- privacy mode: `No cookies (clean profile)`;
- no-data recording: enabled.

The purpose of this routine is not to identify the best date to purchase a ticket, but to record the fare observed for a route at a specified advance-purchase window.

## 11. 🗺️ UML Modeling

The documentation is complemented by diagrams under `data/` (`.png` for direct viewing and `.pdf` for the corresponding downloadable version):

- **Use Case Diagram** ([png](../data/diagrama_casos_de_uso.png) · [pdf](../data/use-case-diagram.pdf)): Researcher/Operator interactions with the system, including manual collection, automatic scheduling, history/re-upload to Google Sheets, settings, and batch execution through `coleta_tcc.py`.
- **Class Diagram** ([png](../data/diagrama_classes.png) · [pdf](../data/class-diagram.pdf)): `ScraperApp` and its Tkinter pages (`StartPage`, `ExperimentPage`, `SchedulerPage`, `HistoryPage`, and `SettingsPage`), their relationship with the orchestration functions in `scraper_passagens.py`, and dependencies on `latam_scraper.scrape_latam()` and `scrapers_utils.py` utilities.
- **Sequence Diagram (complete)** ([png](../data/sequence_diagram.png) · [pdf](../data/sequence-diagram-full.pdf)): end-to-end collection flow, including driver retry (up to two attempts, with an Incognito fallback), blocking/CAPTCHA handling, the wait loop for flight cards, Excel persistence, optional Google Sheets upload, and raw-HTML cleanup after MD5 checking.
- **Sequence Diagram (simplified)** ([png](../data/diagram_sequence.png) · [pdf](../data/sequence-diagram-summary.pdf)): previous simplified view showing only the happy path (User → Interface → Scheduler → Collection Module → LATAM → Google Sheets), retained as a quick reference.

## 12. ⚠️ Current Limitations

- Only LATAM is implemented.
- Documentation and publication workflows should avoid committing `config.ini`, credential JSON files, `DADOS_BRUTOS/`, `LOGS/`, and Python cache files.
- Raw HTML is transient evidence: it can be created, checked through MD5, and removed within the same execution flow.
