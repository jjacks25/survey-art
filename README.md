# Land Survey Scraper

Scrape county websites for land survey data.

## Setup

- **Python**: 3.12+
- **Package manager**: [uv](https://docs.astral.sh/uv/)

```bash
# Install uv (if needed): curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync          # create .venv and install deps
uv run pytest    # run tests
uv run ruff check src tests   # lint
```

## Docker

```bash
docker build -t land-survey-scraper .
docker run --rm land-survey-scraper   # prints version
```

Override the default command to run your own entrypoint.
