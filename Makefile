IMAGE := land-survey-scraper
TAG := latest
CONTAINER := land-survey-scraper
# Mount host source into container so changes are reflected; PYTHONPATH so Python uses mounted code
MOUNT := -v $(PWD):/app -e PYTHONPATH=/app/src

.DEFAULT_GOAL := help

.PHONY: help build run shell dev down process

help: ## General | Show this help
	@echo "Land Survey Scraper - available commands:"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(firstword $(MAKEFILE_LIST)) | \
	awk 'BEGIN {FS = ":.*?## "} { \
		idx = index($$2, " | "); \
		if (idx > 0) { g = substr($$2, 1, idx-1); d = substr($$2, idx+3) } else { g = "Other"; d = $$2 } \
		printf "%s\t%s\t%s\n", g, $$1, d \
	}' | sort -t$$'\t' -k1,1 -k2,2 | \
	awk -F$$'\t' 'BEGIN {prev=""} { \
		if ($$1 != prev) { if (prev != "") print ""; printf "  \033[1m%s\033[0m:\n", $$1; prev=$$1 } \
		printf "    make \033[36m%-8s\033[0m %s\n", $$2, $$3 \
	}'
	@echo ""

build: ## Container | Build the Docker image
	docker build -t $(IMAGE):$(TAG) .

run: build ## Container | Build (if needed) and run the container (default CMD)
	docker run --rm $(IMAGE):$(TAG)

shell: build ## Container | Build (if needed) and run a bash shell (source mounted)
	docker run --rm -it $(MOUNT) $(IMAGE):$(TAG) /bin/bash

dev: build ## Container | Build (if needed), run a bash shell with source mounted; container exits when you leave
	docker run --rm -it $(MOUNT) $(IMAGE):$(TAG) /bin/bash

down: ## Container | Stop and remove the container (when run in background with --name $(CONTAINER))
	docker stop $(CONTAINER) 2>/dev/null || true
	docker rm $(CONTAINER) 2>/dev/null || true

process: build ## Scraper | Scrape records for an address: make process ADDRESS="123 Main St, Greeley, CO 80631"
	docker run --rm \
	  --env-file .env \
	  -v $(PWD)/tmp:/app/tmp \
	  $(IMAGE):$(TAG) \
	  uv run land-survey-scraper $(ADDRESS)
