# IEEE Xplore Harvester

Playwright-based tooling for IEEE Xplore metadata collection and PDF download, organized as a small library plus runnable scripts, templates, and long-run wrappers.

## Layout

```text
src/ieee_harvest/
  auth.py                  shared IEEE / institutional login helpers
  pdf.py                   PDF download helpers

scripts/
  login.py                 save a reusable IEEE browser session
  interactive_crawler.py   interactive search + single-paper download
  bulk_download_by_venue.py
  bulk_download_topics.py
  resume_download_with_manual_login.py

templates/
  venue_harvester_template.py
  incremental_catchup_template.py

ops/docker/
  Dockerfile
  run_catchup_docker.sh

ops/orb/
  orb_worker.sh
  run_catchup_orb.sh
```

The shared logic lives in `src/`, the direct entrypoints live in `scripts/`, and venue-specific long-running jobs are meant to start from the files in `templates/`.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Local credentials should stay in shell exports or a local `.env`. The repo ignores `.env`, downloads, logs, screenshots, and saved browser state.

Example variables:

```bash
export IEEE_INST_NAME="Example University"
export IEEE_INST_USERNAME="your-campus-account"
export IEEE_INST_PASSWORD="your-password"
export IEEE_SSO_HOST="login.example.edu"
```

## Common Usage

Save a reusable login session:

```bash
python3 scripts/login.py
```

Run the interactive crawler:

```bash
python3 scripts/interactive_crawler.py
```

Resume a batch from a live authenticated browser session:

```bash
python3 scripts/resume_download_with_manual_login.py
```

Run the built-in multi-venue topic workflow:

```bash
python3 scripts/bulk_download_by_venue.py
```

## Template Workflow

The repository no longer keeps JSSC / VLSI / other venue-specific harvesters as first-class scripts. Instead, start from the templates in `templates/`.

1. Duplicate or edit `templates/venue_harvester_template.py` and customize:
   `VENUE_NAME`
   `VENUE_QUERY_TEMPLATES`
   `VENUE_INCLUDE_PATTERNS`
   `VENUE_EXCLUDE_PATTERNS`
   `DEFAULT_OUTPUT_ROOT`
2. Run a one-shot venue harvest:

```bash
python3 templates/venue_harvester_template.py --start-year 2018 --end-year 2025
```

3. For long-running catch-up loops, use `templates/incremental_catchup_template.py`:

```bash
python3 templates/incremental_catchup_template.py --start-year 2018 --end-year 2025
```

## Docker First

For long-running jobs, Docker is the recommended runner:

```bash
export CREDENTIAL_DIR=/absolute/path/to/credentials
./ops/docker/run_catchup_docker.sh --start-year 2018 --end-year 2025
```

By default this runs `templates/incremental_catchup_template.py` inside an isolated Playwright environment. If you create your own template file, point the runner at it:

```bash
export TARGET_SCRIPT=templates/my_venue_catchup.py
./ops/docker/run_catchup_docker.sh --start-year 2018 --end-year 2025
```

OrbStack is supported as a second option:

```bash
export CREDENTIAL_FILE=/absolute/path/to/ieee.env
./ops/orb/run_catchup_orb.sh --start-year 2018 --end-year 2025
```

## Outputs

- `downloads/ieee_context.json`: saved Playwright storage state
- `downloads/venue_harvest_2018_2025/`: output from the built-in multi-venue workflow
- `downloads/topic_harvest_2018_2025/`: output from the built-in topic workflow
- `downloads/venue_template/`: default output for the generic template harvester

## Notes

IEEE institutional access is fragile in practice. Saved browser state helps, but a live browser session plus one successful manual PDF open is often the most reliable bootstrap for bulk downloads. That is why the repo keeps both automated login helpers and a manual resume path.
