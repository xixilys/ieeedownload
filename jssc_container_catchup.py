#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional

from jssc_full_harvest import DEFAULT_OUTPUT_ROOT, JSSCHarvester


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def year_stats(output_root: Path, year: int) -> Dict[str, Optional[int]]:
    meta = output_root / str(year) / "metadata.json"
    if not meta.exists():
        return {"total": None, "done": None, "missing": None}
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed reading metadata for year=%s: %s", year, e)
        return {"total": None, "done": None, "missing": None}
    total = len(data)
    done = sum(1 for record in data if record.get("pdf_downloaded"))
    return {"total": total, "done": done, "missing": total - done}


def close_harvester(harvester: Optional[JSSCHarvester]) -> None:
    if harvester is None:
        return
    try:
        harvester.close()
    except Exception as e:
        logger.warning("Failed closing harvester cleanly: %s", e)


def create_harvester(
    output_root: Path,
    *,
    state_file: Optional[Path] = None,
    headless: bool = False,
) -> JSSCHarvester:
    if not headless:
        logger.info("Launching containerized Chromium inside Xvfb; no macOS window will appear.")
    return JSSCHarvester(
        output_root=output_root,
        state_file=state_file,
        headless=headless,
        hide_browser=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the JSSC catch-up loop inside Docker/Xvfb so the host macOS desktop never gets a browser window."
    )
    parser.add_argument("--start-year", type=int, default=2020)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--cooldown-seconds", type=int, default=90)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--full-cycle-sleep-seconds", type=int, default=300)
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Optional explicit Playwright storage state file to reuse inside the container.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Use Playwright headless mode inside the container instead of headed Chromium under Xvfb.",
    )
    args = parser.parse_args()

    output_root = Path(args.output_root)
    state_file = Path(args.state_file) if args.state_file else None
    harvester: Optional[JSSCHarvester] = None

    try:
        while True:
            if harvester is None:
                harvester = create_harvester(
                    output_root,
                    state_file=state_file,
                    headless=args.headless,
                )

            cycle_made_progress = False
            remaining_any = False
            cycle_interrupted = False

            for year in range(args.start_year, args.end_year + 1):
                before = year_stats(output_root, year)
                logger.info("Year=%s before batch stats: %s", year, before)

                if before["missing"] == 0 and before["total"] is not None:
                    logger.info("Year=%s already complete; moving on.", year)
                    continue

                remaining_any = True

                try:
                    harvester.run(year, year, args.batch_size)
                except Exception as e:
                    logger.warning(
                        "Batch failed for year=%s: %s; recreating browser after %ss cooldown.",
                        year,
                        e,
                        args.cooldown_seconds,
                    )
                    close_harvester(harvester)
                    harvester = None
                    cycle_interrupted = True
                    time.sleep(args.cooldown_seconds)
                    break

                after = year_stats(output_root, year)
                logger.info("Year=%s after batch stats: %s", year, after)

                before_missing = before["missing"]
                after_missing = after["missing"]
                before_done = before["done"] or 0
                after_done = after["done"] or 0
                progressed = None
                if before_missing is not None and after_missing is not None:
                    progressed = before_missing - after_missing
                elif after_done >= before_done:
                    progressed = after_done - before_done

                if progressed is not None:
                    logger.info(
                        "Year=%s batch progress: done %s -> %s (delta=%s)",
                        year,
                        before_done,
                        after_done,
                        progressed,
                    )

                if progressed is not None and progressed > 0:
                    cycle_made_progress = True

                if after["missing"] == 0 and after["total"] is not None:
                    logger.info("Year=%s completed.", year)
                else:
                    remaining_any = True
                    logger.info(
                        "Cooling down for %ss before next year/batch...",
                        args.cooldown_seconds,
                    )
                    time.sleep(args.cooldown_seconds)

            if not remaining_any:
                logger.info(
                    "All requested years completed: %s-%s",
                    args.start_year,
                    args.end_year,
                )
                return

            if cycle_interrupted:
                logger.info("Resuming remaining years with a fresh browser session.")
                continue

            if cycle_made_progress:
                logger.info("Full cycle made progress; starting next catch-up cycle immediately.")
                continue

            logger.info(
                "Full cycle made no progress; sleeping %ss before retrying all remaining years.",
                args.full_cycle_sleep_seconds,
            )
            time.sleep(args.full_cycle_sleep_seconds)
    finally:
        close_harvester(harvester)


if __name__ == "__main__":
    main()
