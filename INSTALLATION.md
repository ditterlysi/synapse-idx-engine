# Installation

This is the single authoritative installation and upgrade guide for IDX Disclosure Digest.

## Requirements

- Python 3.11 or newer. The container uses Python 3.12.
- Tesseract OCR with English and Indonesian language data.
- Chromium installed through Playwright when the legacy/manual research application needs browser-backed IDX access.
- An OpenRouter API key for legacy `idx-digest` model-backed research summaries.
- For source-neutral Synapse ingestion, either a Gemini Developer API key or an OpenRouter API key, depending on `AI_PROVIDER`.

The legacy application can download and extract disclosures without an OpenRouter key when `--skip-llm` is used.

## macOS

Install system OCR dependencies:

```bash
brew install tesseract tesseract-lang
```

Create an isolated environment and install the project:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
python -m playwright install chromium
cp .env.example .env
```

Edit `.env` and configure the provider required by the workflow you intend to run. Do not commit the populated file.

## Debian or Ubuntu

Install Python and OCR dependencies using the package manager appropriate for the distribution. For current Debian/Ubuntu releases:

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv tesseract-ocr tesseract-ocr-ind
```

Then install the application:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
python -m playwright install chromium
cp .env.example .env
```

Playwright may report additional operating-system packages required by Chromium. Install those packages using its displayed command or the official Playwright instructions for the host distribution.

## Installer script

An existing checkout can use the maintained installation helper:

```bash
chmod +x install_or_upgrade.sh
./install_or_upgrade.sh
python -m playwright install chromium
```

The script validates Python, refreshes packaging tools, installs the current checkout, and runs `verify_install.py`. It does not overwrite `.env` or delete `data/`.

## Development installation

Install the lint and test tools as well as runtime dependencies:

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest -q
```

## Configuration

Create the runtime configuration once:

```bash
cp .env.example .env
```

### Legacy research application

The existing `idx-digest` GUI/CLI model path remains OpenRouter-backed:

```env
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=replace_me
OPENROUTER_MODEL=deepseek/deepseek-v4-flash-0731
OPENROUTER_PROVIDER=deepinfra
IDX_TRANSPORT=auto
IDX_BROWSER_HEADLESS=false
DATA_DIR=./data
```

Keep fallback disabled when disclosure text must remain pinned to the configured OpenRouter provider. Optional OpenRouter attribution headers are `OPENROUTER_HTTP_REFERER` and `OPENROUTER_APP_TITLE`.

### Source-neutral Synapse ingestion with Gemini

For `synapse-idx-engine manual-import` or a future approved `DisclosureSource` adapter:

```env
AI_PROVIDER=gemini
GEMINI_API_KEY=replace_me
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta
GEMINI_MODEL=gemini-3.5-flash-lite
SYNAPSE_INTERNAL_BASE_URL=https://<your-synapse-origin>
SYNAPSE_INGESTION_SECRET=<runtime-secret>
SYNAPSE_DAILY_ENABLED=false
```

Only the selected AI provider's API key is required by the source-neutral manual path. Provider resolution uses an isolated settings copy, and Synapse provenance is mapped to the backend/model that actually executes the analysis.

The direct Gemini adapter uses GenerateContent structured output (`responseMimeType` + `responseJsonSchema`) and preserves the engine's existing local schema validation.

### Source-neutral Synapse ingestion with OpenRouter

```env
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=replace_me
OPENROUTER_MODEL=deepseek/deepseek-v4-flash-0731
OPENROUTER_PROVIDER=deepinfra
SYNAPSE_INTERNAL_BASE_URL=https://<your-synapse-origin>
SYNAPSE_INGESTION_SECRET=<runtime-secret>
SYNAPSE_DAILY_ENABLED=false
```

Automated IDX website collection and scheduled Synapse collection remain disabled until an approved/licensed source adapter is integrated.

Do not commit `.env`. Do not paste browser cookies into source code. `IDX_COOKIE` is an emergency runtime option for explicit legacy/manual diagnostics only.

## Verify the installation

Run all local checks from the activated environment:

```bash
python verify_install.py
python -c 'import idx_digest; print(idx_digest.__version__)'
idx-digest --help
python -m idx_digest --help
synapse-idx-engine --help
```

Verify OCR languages when OCR is required:

```bash
tesseract --list-langs
```

The output should include `eng` and `ind`.

Exercise the legacy pipeline without model cost:

```bash
idx-digest run \
  --start 2026-08-05 \
  --end 2026-08-05 \
  --ticker ANTM \
  --skip-llm \
  --max-announcements 2
```

Validate conservative Synapse integration settings without contacting IDX or Synapse:

```bash
synapse-idx-engine doctor
```

## Launch

Local GUI:

```bash
idx-digest gui
```

Legacy CLI example:

```bash
idx-digest run --start 2026-08-01 --end 2026-08-05 --ticker ANTM
```

The GUI defaults to `http://127.0.0.1:8787`. It has no authentication and should remain local.

## Docker

Build the image:

```bash
docker build -t idx-disclosure-digest .
```

Run a CLI command with persistent application data and environment configuration:

```bash
docker run --rm \
  --env-file .env \
  -v "$PWD/data:/app/data" \
  idx-disclosure-digest \
  run --start 2026-08-05 --end 2026-08-05 --ticker ANTM
```

The image includes Tesseract English and Indonesian OCR data. Browser-backed transport may require additional Chromium setup; local installation is recommended when interactive legacy/manual verification is expected.

## Upgrade an existing checkout

Preserve `.env` and `data/`, update the source tree, then reinstall:

```bash
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
python verify_install.py
pytest -q
```

Database migrations are additive and run automatically. Do not delete the SQLite database, downloaded attachments, extracted text, prompt profiles, or browser profile during a normal upgrade.

Existing OpenRouter configuration remains compatible because `AI_PROVIDER` defaults to `openrouter` when not specified.

## Troubleshooting

### `idx-digest` is not found

Activate the environment and refresh the shell command cache:

```bash
source .venv/bin/activate
hash -r
python -m idx_digest --help
```

### Manual Synapse import says the provider key is missing

Check `AI_PROVIDER`. When it is `gemini`, configure `GEMINI_API_KEY`; when it is `openrouter`, configure `OPENROUTER_API_KEY`. The unselected provider key is not required.

### Gemini returns a structured-output request error

The source-neutral adapter must use Gemini GenerateContent fields `responseMimeType` and `responseJsonSchema`. Do not send OpenAI/OpenRouter `response_format` directly to the Gemini endpoint.

### IDX returns a verification page in the legacy research application

Use the automatic transport with a visible browser:

```env
IDX_TRANSPORT=auto
IDX_BROWSER_HEADLESS=false
```

Complete the verification in the Chromium window. The profile under `data/browser-profile/` is reused. This workflow does not bypass CAPTCHAs and is not the Synapse scheduled-ingestion path.

### A previously working headless legacy session fails

Set `IDX_BROWSER_HEADLESS=false`, complete verification once, then try headless mode again. Do not use this as a scheduled Synapse collection workaround.

### OCR produces little text

Confirm `eng` and `ind` are installed, and check `OCR_LANG=ind+eng` in `.env`. Native PDF extraction is always attempted before OCR.

### OpenRouter calls fail instead of changing providers

That is expected when `OPENROUTER_ALLOW_FALLBACKS=false`. Restore the configured provider or explicitly change the provider policy after reviewing the data-routing implications.

### An interrupted run appears incomplete

Committed files and summaries remain usable. Open the GUI and resume the run, use `idx-digest recover`, or use `idx-digest reduce-cached` when announcement summaries are already complete.
