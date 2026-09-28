"""
coleta_tcc.py — Standardized academic collection script for the AirPrice system.

Runs all routes registered in `AVAILABLE_ROUTES` without the graphical interface
for the study advance-purchase windows (7, 15, 30, and 90 days), always in
headless mode and with empty-row recording when a route returns no fare
(`record_no_data`).

The goal is not to identify the best purchase date, but to record the observed
fare for each combination of route and advance-purchase window.

Usage:
    python src/coleta_tcc.py
"""

import sys
from datetime import datetime, timedelta

from scraper_passagens import (
    AVAILABLE_ROUTES,
    run_routes_sequentially,
    stop_event,
)


class ConsoleController:
    """Replaces the GUI with terminal output while preserving the same logging interface."""

    def log(self, msg, _level="info"):
        print(msg)

    def log_ok(self, msg):
        print(f"[OK] {msg}")

    def log_err(self, msg):
        print(f"[ERROR] {msg}")

    def log_warn(self, msg):
        print(f"[WARN] {msg}")

    def log_info(self, msg):
        print(f"[INFO] {msg}")

    def info(self, title, msg):
        print(f"{title}: {msg}")

    def warn(self, title, msg):
        print(f"{title}: {msg}")

    def error(self, title, msg):
        print(f"{title}: {msg}")

def main():
    """Runs all routes for the [7, 15, 30, 90]-day windows and prints the final summary."""
    routes = list(AVAILABLE_ROUTES.keys())
    advance_windows = [7, 15, 30, 90]

    stop_event.clear()
    controller = ConsoleController()

    controller.log_info("Starting collection for 4 routes and 4 advance-purchase windows.")

    all_results = []

    for advance_window in advance_windows:
        flight_date = (datetime.now() + timedelta(days=advance_window)).strftime("%Y-%m-%d")
        controller.log_info("\n==============================================")
        controller.log_info(f"Searching {advance_window} days in advance (Flight: {flight_date})")
        controller.log_info("==============================================")

        base_config = {
            "flight_date_str": flight_date,
            "user_agent_label": "Chrome Desktop (default)",
            "privacy_mode": "No cookies (clean profile)",
            "experiment_id": f"EXTRACAO_{advance_window}D",
            "repetitions": 1,
            "headless": True,
            "show_dialogs": False,
            "record_no_data": True,
        }

        results = run_routes_sequentially(
            controller,
            routes,
            ["LATAM"],
            base_config,
        )
        all_results.extend(results)
        controller.log_ok(
            f"Collection for the {advance_window}-day window finished with {len(results)} record(s)."
        )

    controller.log_ok(
        f"\nCOLLECTION COMPLETE! A total of {len(all_results)} records were collected."
    )

if __name__ == "__main__":
    main()
