# ✈️ AirPrice — Airfare Scraper

> Python system for automated LATAM airfare collection, with local Excel export and optional Google Sheets upload.

The project was developed to support academic analyses of airfare variation, especially across advance-purchase windows. It can be operated through the graphical interface or through the standardized collection script.

## ✨ Main Features

- **Graphical interface:** GUI implemented with `tkinter`, `ttk`, `scrolledtext`, and `tkcalendar`, supporting manual collection, scheduling, file history, credential settings, and a visual execution log.
- **LATAM collection:** browser automation with `Selenium WebDriver` and Chrome.
- **Headless execution:** option to run the browser without displaying a window during automated collection.
- **Scheduler:** automatic cycles over selected routes, configurable advance-purchase windows, interval, and jitter.
- **Academic collection:** `coleta_tcc.py` runs all registered routes for the 7-, 15-, 30-, and 90-day windows.
- **Local export:** generation of `.xlsx` files in `DADOS_BRUTOS/`.
- **Google Sheets:** optional upload through `gspread` and a Service Account.
- **Execution evidence:** optional screenshots and compressed raw HTML stored in `LOGS/`.
- **Browser configuration:** clean-profile mode without cookies, incognito mode, configurable user agent, humanized waits, and optional proxy support.

## 🧩 Requirements

- Python 3.10 or later.
- Google Chrome installed.
- Internet access.
- Optional: a Google Cloud Service Account for Google Sheets integration.

## 🚀 Installation

Access the anonymous repository at [AirPrice](https://anonymous.4open.science/r/AirPrice-8FB3) and download the project using **Download Repository (ZIP)** at the top of the page.

After downloading, extract the ZIP archive.

On Windows, open a terminal in the project directory and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r config\requirements.txt
```

On Linux/macOS, install Google Chrome first. On Linux distributions that do not provide `tkinter` by default, also install the corresponding system package, such as `python3-tk` on Ubuntu/Debian. Then run:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r config/requirements.txt
```

## 📦 Official Source and Supplementary Package

The GitHub repository referenced by the accompanying paper should be treated as the public project source. It should contain the source code, documentation, license, installation instructions, `config/config.example.ini`, and public supplementary materials maintained under `data/`.

To generate a clean supplementary ZIP from the project root, run:

```powershell
.\scripts\build_submission_zip.ps1
```

The file will be created at:

```text
dist/airprice-software.zip
```

The package is assembled from a whitelist and includes source code, documentation, public supplementary materials from `data/`, instructions, the license, dependencies, and the public example configuration. It excludes `config/config.ini`, credentials, `DADOS_BRUTOS/`, `LOGS/`, Python caches, virtual environments, uncurated local artifacts, screenshots generated under `LOGS/`, and HTML dumps.

## ▶️ Running AirPrice

### Graphical Interface

```powershell
python src\scraper_passagens.py
```

The GUI supports manual collection, scheduler configuration, credential settings, execution-evidence options, and the upload of historical files to Google Sheets.

### Standardized Academic Collection

```powershell
python src\coleta_tcc.py
```

This script runs all routes registered in `AVAILABLE_ROUTES` for the `[7, 15, 30, 90]` advance-purchase windows, in headless mode, with one repetition per route and no-data recording enabled.

## 🎬 Video Demonstration

A demonstration of the graphical interface is available at:

[Watch the demonstration video](https://l1nk.dev/OhEzdX2)

## ⚙️ Configuration

The local configuration file is:

```text
config/config.ini
```

Create it from the template:

```powershell
copy config\config.example.ini config\config.ini
```

Example:

```ini
[GSHEETS]
credentials_file = credentials/your-service-account-key.json
spreadsheet_id = YOUR_SPREADSHEET_ID
worksheet_name = Data

[ANTIDETECT]
humanize = True
save_html = False
screenshot = False

[PROXY]
enabled = False
list =
```

To use Google Sheets, create a Service Account in Google Cloud, download its JSON credential file, and share the target spreadsheet with the Service Account email address.

## 📤 Generated Outputs

- `DADOS_BRUTOS/`: Excel files generated for each experiment.
- `LOGS/`: screenshots, compressed raw HTML, and temporary Chrome profiles.
- Google Sheets: when configured, rows are appended to the selected worksheet.

When `save_html=True`, the system stores HTML as `.html.gz`, computes its MD5 hash, and may remove the file after data persistence after checking the hash before deletion.

## 📂 Project Structure

```text
AirPrice/
|
|-- config/
|   |-- config.example.ini
|   `-- requirements.txt
|
|-- src/
|   |-- scraper_passagens.py
|   |-- latam_scraper.py
|   |-- scrapers_utils.py
|   `-- coleta_tcc.py
|
|-- docs/
|   `-- DOCUMENTACAO_TECNICA.md
|
|-- data/
|   |-- analise.xlsx
|   |-- architecture.pdf
|   |-- use-case-diagram.pdf
|   |-- diagrama_casos_de_uso.png
|   |-- class-diagram.pdf
|   |-- diagrama_classes.png
|   |-- sequence-diagram-summary.pdf
|   |-- diagram_sequence.png
|   |-- sequence-diagram-full.pdf
|   |-- sequence_diagram.png
|   |-- hist&expo.png
|   |-- home.png
|   |-- manual.png
|   |-- scheduler.png
|   |-- sequence_diagram.png
|   `-- settings.png
|
|-- scripts/
|   `-- build_submission_zip.ps1
|
|-- DADOS_BRUTOS/
|-- LOGS/
|
|-- README.md
|-- SECURITY.md
`-- LICENSE
```

## 🧱 Code Modules

- `scraper_passagens.py`: graphical interface, WebDriver creation, collection orchestration, scheduler, Excel persistence, and Google Sheets upload.
- `latam_scraper.py`: LATAM-specific scraper responsible for opening the page, locating flight cards, and extracting the lowest valid fare.
- `scrapers_utils.py`: price normalization, record construction, ID generation, interruptible sleeps, and HTML hashing/cleanup utilities.
- `coleta_tcc.py`: methodological execution over all routes and advance-purchase windows used in the study.

## 📖 Technical Documentation

For architecture, execution flow, dataset structure, evidence handling, and current limitations, see:

[Technical Documentation](docs/DOCUMENTACAO_TECNICA.md)

## 📖 UML Modeling

The technical documentation references the following diagrams under `data/` (`.png` for direct browser viewing and `.pdf` for the corresponding downloadable version):

| 📊 **Use Case Diagram** ([png](data/diagrama_casos_de_uso.png) · [pdf](data/use-case-diagram.pdf)): operator interactions with the tool, including manual collection, scheduling, history/re-upload to Google Sheets, and settings.
| 🏗️ **Class Diagram** ([png](data/diagrama_classes.png) · [pdf](data/class-diagram.pdf)): static software structure, including `ScraperApp`, its Tkinter pages, the collection orchestrator in `scraper_passagens.py`, `LatamScraper`, and scraper utilities.
| 🔄 **Sequence Diagram (complete)** ([png](data/sequence_diagram.png) · [pdf](data/sequence-diagram-full.pdf)): end-to-end collection pipeline, including driver creation, retry after an invalid session, blocking/CAPTCHA detection, Excel persistence, optional Google Sheets upload, and raw-HTML cleanup after MD5 checking.
| 🔄 **Sequence Diagram (simplified)** ([png](data/diagram_sequence.png) · [pdf](data/sequence-diagram-summary.pdf)): happy-path overview from User → GUI → Scheduler → Collection Module → LATAM → Google Sheets.

## 🗂️ Supplementary Materials

- [Analysis spreadsheet](data/analise.xlsx): complete dataset included with the study materials.
- [Architecture diagram](data/architecture.pdf): overview of software modules and their relationships.
- GUI screenshots: [home](data/home.png), [manual collection](data/manual.png), [scheduler](data/scheduler.png), [history and export](<data/hist&expo.png>), and [settings](data/settings.png).

## 🔒 Security

Before publishing or versioning the project, verify that you do not expose:

- Google credential JSON files;
- private spreadsheet IDs;
- `config/config.ini` with real local settings;
- generated spreadsheets under `DADOS_BRUTOS/`, unless publication is intentional;
- screenshots or raw HTML from `LOGS/`, which may contain browsing data;
- proxy lists, passwords, or tokens.

See also [SECURITY.md](SECURITY.md).

## 📄 License

This project is distributed under the **GNU General Public License v3.0 or later** (`GPL-3.0-or-later`). See [LICENSE](LICENSE).
