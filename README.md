# IEEE Xplore Venue Harvester

Utilities for collecting IEEE Xplore metadata and PDFs for venue-focused harvest jobs, with resumable download workflows for JSSC and related conference proceedings.

## What this repo contains

- `ieee_crawler.py`: interactive search and single-paper download helper.
- `login.py`: saves a Playwright browser session after institutional sign-in succeeds.
- `ieee_auto_login.py`: optional institution-SSO helper driven by local environment variables.
- `bulk_download_by_venue.py`: enumerates venue-year results first, then filters and downloads target topics.
- `resume_download_with_manual_login.py`: resumes batch downloads from a live browser session after manual login.
- `jssc_full_harvest.py`: year-by-year JSSC harvester with resumable metadata and PDF downloads.
- `jssc_container_catchup.py`: catch-up loop for Docker/Xvfb or OrbStack environments.
- `run_jssc_catchup_orb.sh` and `jssc_orb_worker.sh`: OrbStack launcher and worker scripts.
- `vlsi_full_harvest.py`: VLSI-focused batch harvester using the same Playwright session model.

## Public-safe defaults

This repository is prepared for a public GitHub project:

- No account names or passwords are committed.
- Generated downloads, logs, screenshots, and saved browser state stay under ignored paths.
- Institution-specific values have been replaced with environment-driven configuration.

If you use local credentials, keep them outside the repository or in a local `.env` file that is not committed.

## Setup

### 1. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install playwright==1.58.0
python -m playwright install chromium
```

### 2. Configure local credentials

Copy `.env.example` to `.env` and fill in your own values, or export the variables directly in your shell:

```bash
export IEEE_INST_NAME="Example University"
export IEEE_INST_USERNAME="your-campus-account"
export IEEE_INST_PASSWORD="your-password"
export IEEE_SSO_HOST="login.example.edu"
```

`IEEE_SSO_HOST` is optional, but it helps the login helper recognize your institution's SSO redirect more reliably.

## Recommended workflows

### Save a reusable browser session

```bash
python3 login.py
```

This launches a visible Chromium window, completes institutional sign-in, and saves the Playwright storage state to `downloads/ieee_context.json`.

### Resume a batch after manual login

```bash
python3 resume_download_with_manual_login.py
```

This is the most reliable workflow when IEEE PDF access depends on a live browser session. Open a paper PDF once in the browser window, then the script continues downloading in the same session.

### Run the JSSC catch-up loop locally

```bash
python3 jssc_container_catchup.py --start-year 2020 --end-year 2026
```

By default this writes output under `downloads/jssc_full_harvest`.

### Run the JSSC catch-up loop in OrbStack

Either export `IEEE_INST_*` variables before launching, or point `CREDENTIAL_FILE` to a local file that exists outside the repo:

```bash
export CREDENTIAL_FILE=/absolute/path/to/ieee.env
./run_jssc_catchup_orb.sh --start-year 2020 --end-year 2026
```

The Orb worker writes logs to `jssc_orb_catchup.log`.

## Outputs

- `downloads/ieee_context.json`: saved Playwright storage state.
- `downloads/venue_harvest_2018_2025/metadata.json`: filtered venue metadata.
- `downloads/venue_harvest_2018_2025/pdfs/`: downloaded PDFs for the venue workflow.
- `downloads/jssc_full_harvest/<year>/metadata.json`: JSSC year-level metadata.
- `downloads/jssc_full_harvest/<year>/.../*.pdf`: JSSC PDFs grouped by issue.

All of these paths are gitignored.

## Notes on reliability

IEEE institutional access is often tied to short-lived cookies and SSO redirects. In practice:

- Saved browser state helps, but it is not always enough for PDF downloads.
- Headless request replay may return an HTML interstitial instead of a PDF.
- A visible browser session plus one successful manual PDF open is often the most reliable way to bootstrap long downloads.

That is why this repo includes both automated login helpers and a manual-login resume path.
