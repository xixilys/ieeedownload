# IEEE Xplore Venue Harvester

Playwright-based harvest scripts for IEEE Xplore metadata and PDFs, with resumable workflows for JSSC, VLSI, ISCAS, and topic-focused downloads.

## Highlights

- Venue-level harvests for `JSSC`, `ISCAS`, and `VLSI`
- Topic filtering for `CIM`, `AI accelerator`, `processor`, `coprocessor`, `near-memory`, and related directions
- Resumable metadata and PDF downloads
- Local-login and live-session workflows for IEEE institutional access

## Local config

Use a local `.env` file or shell exports for institutional credentials. The repo ignores `.env`, downloads, logs, screenshots, and saved browser state by default.

## Quick Start

1. Create a virtual environment and install dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

2. Configure local credentials.

Copy `.env.example` to `.env` and fill in your own values, or export the variables directly in your shell:

```bash
export IEEE_INST_NAME="Example University"
export IEEE_INST_USERNAME="your-campus-account"
export IEEE_INST_PASSWORD="your-password"
export IEEE_SSO_HOST="login.example.edu"
```

`IEEE_SSO_HOST` is optional, but it helps the login helper recognize your institution's SSO redirect more reliably.

## Workflows

Pick the workflow that matches your access mode:

```bash
python3 login.py
```

This launches a visible Chromium window, completes institutional sign-in, and saves the Playwright storage state to `downloads/ieee_context.json`.

```bash
python3 resume_download_with_manual_login.py
```

This is the most reliable workflow when IEEE PDF access depends on a live browser session. Open a paper PDF once in the browser window, then the script continues downloading in the same session.

```bash
python3 jssc_container_catchup.py --start-year 2020 --end-year 2026
```

By default this writes output under `downloads/jssc_full_harvest`.

Either export `IEEE_INST_*` variables before launching, or point `CREDENTIAL_FILE` to a local file that exists outside the repo:

```bash
export CREDENTIAL_FILE=/absolute/path/to/ieee.env
./run_jssc_catchup_orb.sh --start-year 2020 --end-year 2026
```

The Orb worker writes logs to `jssc_orb_catchup.log`.

## Scripts

- Login/bootstrap helpers for institutional IEEE access.
- Venue and topic harvesters for metadata collection and PDF download.
- Resume scripts for continuing a batch from an already-authenticated browser session.
- Docker and OrbStack wrappers for running long catch-up jobs more safely.

The repository keeps the runnable Python scripts at the top level on purpose so each one can be invoked directly without a package install step. If you want a more conventional `src/` or `scripts/` layout later, we can refactor it after the workflow is stable.

## Outputs

- `downloads/ieee_context.json`: saved Playwright storage state.
- `downloads/venue_harvest_2018_2025/metadata.json`: filtered venue metadata.
- `downloads/venue_harvest_2018_2025/pdfs/`: downloaded PDFs for the venue workflow.
- `downloads/jssc_full_harvest/<year>/metadata.json`: JSSC year-level metadata.
- `downloads/jssc_full_harvest/<year>/.../*.pdf`: JSSC PDFs grouped by issue.
- `downloads/jssc_full_harvest/<year>/issues.json`: issue-level summary data.

These paths are gitignored.

## Notes

IEEE institutional access is often tied to short-lived cookies and SSO redirects. In practice:

- Saved browser state helps, but it is not always enough for PDF downloads.
- Headless request replay may return an HTML interstitial instead of a PDF.
- A visible browser session plus one successful manual PDF open is often the most reliable way to bootstrap long downloads.

That is why this repo includes both automated login helpers and a manual-login resume path.

## Docker First

For long-running harvests, Docker is the recommended way to run the project:

```bash
./run_jssc_catchup_docker.sh --start-year 2020 --end-year 2026
```

This keeps the Playwright environment isolated and avoids host browser/window issues. OrbStack is the next-best option when you want a persistent local VM-style runner.
