#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

HARVEST_SCRIPT = Path(__file__).resolve().parent / "venue_harvester_template.py"
PYTHON = sys.executable
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "downloads" / "venue_template"

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
    done = sum(1 for r in data if r.get("pdf_downloaded"))
    missing = total - done
    return {"total": total, "done": done, "missing": missing}


def run_batch(output_root: Path, year: int, batch_size: int, headless: bool = False) -> int:
    cmd = [
        PYTHON,
        str(HARVEST_SCRIPT),
        "--start-year",
        str(year),
        "--end-year",
        str(year),
        "--max-downloads-per-year",
        str(batch_size),
        "--output-root",
        str(output_root),
    ]
    if headless:
        cmd.append("--headless")
    logger.info("Launching batch for year=%s batch_size=%s headless=%s", year, batch_size, headless)
    logger.info("Command: %s", " ".join(cmd))
    proc = subprocess.run(cmd)
    logger.info("Batch finished for year=%s returncode=%s", year, proc.returncode)
    return proc.returncode


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2018)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--cooldown-seconds", type=int, default=180)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--max-stalled-batches", type=int, default=2)
    parser.add_argument("--headless", action="store_true", help="Run child Playwright harvest batches in headless mode")
    args = parser.parse_args()

    for year in range(args.start_year, args.end_year + 1):
        stalled_batches = 0
        while True:
            before = year_stats(args.output_root, year)
            logger.info("Year=%s before batch stats: %s", year, before)

            if before["missing"] == 0 and before["total"] is not None:
                logger.info("Year=%s already complete; moving on.", year)
                break

            rc = run_batch(args.output_root, year, args.batch_size, headless=args.headless)
            after = year_stats(args.output_root, year)
            logger.info("Year=%s after batch stats: %s", year, after)

            if rc != 0:
                raise RuntimeError(f"Batch failed for year={year} with returncode={rc}")

            before_missing = before["missing"]
            after_missing = after["missing"]
            progressed = None
            if before_missing is not None and after_missing is not None:
                progressed = before_missing - after_missing
                logger.info("Year=%s batch progress: missing %s -> %s (delta=%s)", year, before_missing, after_missing, progressed)

            if after_missing == 0 and after["total"] is not None:
                logger.info("Year=%s completed.", year)
                break

            if progressed is not None and progressed <= 0:
                stalled_batches += 1
                logger.warning("Year=%s made no progress this batch (stalled_batches=%s/%s)", year, stalled_batches, args.max_stalled_batches)
                if stalled_batches >= args.max_stalled_batches:
                    logger.error(f"Year={year} stalled for {stalled_batches} consecutive batches. Skipping to next year."); break
            else:
                stalled_batches = 0

            logger.info("Cooling down for %ss before next batch...", args.cooldown_seconds)
            time.sleep(args.cooldown_seconds)

    logger.info("All requested years completed: %s-%s", args.start_year, args.end_year)


if __name__ == "__main__":
    main()
