# Builds the Playwright engine into a container — for a scheduled job, not
# required for local development (`pip install` directly is simpler there).
#
#   docker build -t tractorsupply-scraper .
#   docker run --rm -v "$PWD/out:/out" --env-file .env tractorsupply-scraper \
#     --category poultry-feed-treats --pages 3 --out /out/poultry
#
# On this site the one client that was served is the Scraping Browser API
# (README), so the image's own Chromium is there for parity; a real run
# passes TRACTORSUPPLY_CDP_ENDPOINT through the environment. Nothing here
# bakes in a credential.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    # Playwright's own apt-get for Chromium's shared-library dependencies —
    # not pip packages, so this has to run as a separate, explicit step.
    && playwright install --with-deps chromium

# Every module playwright_scraper.py imports, transitively, plus diff_runs.py
# as a useful companion in the same image. smoke_test.py checks this list
# against the entrypoint's real import graph: an earlier version omitted
# proxy_pool.py, which the engine imports at module level, so the image died
# with ModuleNotFoundError on every invocation INCLUDING `--help` — a broken
# container that nothing in the repo would have noticed.
COPY env_config.py fingerprint_client.py output_writer.py \
     page_flow.py playwright_scraper.py product_parser.py proxy_pool.py \
     diff_runs.py ./

ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
