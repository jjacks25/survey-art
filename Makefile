PROJECT := survey-art
REGION ?= us-west-2
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)

# Run uv / python / aws inside containers so the host needs neither installed.
UV_IMAGE := ghcr.io/astral-sh/uv:python3.13-bookworm-slim
UV := docker run --rm -v $(PWD):/w -w /w $(UV_IMAGE) uv
# Deploy runner: boto3 only (no heavy workspace install), with host AWS creds mounted.
AWS_ENVS := -e AWS_PROFILE -e AWS_REGION=$(REGION) -e AWS_DEFAULT_REGION=$(REGION) \
            -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN
DEPLOY := docker run --rm -v $(PWD):/w -w /w -v $(HOME)/.aws:/root/.aws:ro $(AWS_ENVS) \
          $(UV_IMAGE) uv run --no-project --with boto3 python

.DEFAULT_GOAL := help

.PHONY: help up down logs process test lint fmt lock \
        sh-api sh-worker sh-web sh-localstack \
        build-push deploy _deploy-help \
        bootstrap network ecr backend frontend all web diff destroy

help: ## General | Show this help
	@echo "Survey Art - available commands:"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(firstword $(MAKEFILE_LIST)) | \
	awk 'BEGIN {FS = ":.*?## "} { \
		idx = index($$2, " | "); \
		if (idx > 0) { g = substr($$2, 1, idx-1); d = substr($$2, idx+3) } else { g = "Other"; d = $$2 } \
		printf "%s\t%s\t%s\n", g, $$1, d \
	}' | sort -t$$'\t' -k1,1 -k2,2 | \
	awk -F$$'\t' 'BEGIN {prev=""} { \
		if ($$1 != prev) { if (prev != "") print ""; printf "  \033[1m%s\033[0m:\n", $$1; prev=$$1 } \
		printf "    make \033[36m%-12s\033[0m %s\n", $$2, $$3 \
	}'
	@echo ""

# ---------------- Local stack (docker-compose) ----------------
up: ## Local | Build and start the full local stack (web, api, worker, localstack)
	docker compose up -d --build
	@echo ""
	@echo "  web:  http://localhost:5173"
	@echo "  api:  http://localhost:8000"
	@echo ""

down: ## Local | Stop and remove the local stack (and volumes)
	docker compose down -v

logs: ## Local | Tail logs from the local stack
	docker compose logs -f

process: ## Local | Run the scraper CLI directly: make process ADDRESS="123 Main St" [ARGS=...]
	docker compose run --rm --no-deps \
	  -e JOB_ID= worker \
	  uv run survey-art "$(ADDRESS)" $(ARGS)

sh-api: ## Local | Bash into the running api container
	docker compose exec api bash

sh-worker: ## Local | Bash into the running worker container
	docker compose exec worker bash

sh-web: ## Local | Shell into the running web container
	docker compose exec web sh

sh-localstack: ## Local | Bash into the running localstack container
	docker compose exec localstack bash

# ---------------- Quality ----------------
test: ## Quality | Run the Python test suite (pytest, in a container)
	$(UV) run --group dev pytest

lint: ## Quality | Lint Python sources with ruff (check + format --check)
	$(UV) run --group dev ruff check apps/worker apps/api packages infra
	$(UV) run --group dev ruff format --check apps/worker apps/api packages infra

fmt: ## Quality | Auto-format Python sources with ruff
	$(UV) run --group dev ruff format apps/worker apps/api packages infra

lock: ## Quality | Refresh the uv workspace lockfile
	$(UV) lock

# ---------------- AWS deploy (change-set driven) ----------------
build-push: ## Deploy | Build and push the api + worker images to ECR (TAG defaults to git sha)
	REGION=$(REGION) PROJECT=$(PROJECT) TAG=$(TAG) bash infra/build_push.sh

# `make deploy <word>` — the words below are consumed by the `deploy` recipe as an
# argument (via MAKECMDGOALS), not built as their own targets, so give them empty
# recipes rather than leaving them as unknown targets.
bootstrap network ecr backend frontend all web diff destroy: ;

DEPLOY_ARG := $(word 2,$(MAKECMDGOALS))

deploy: ## Deploy | AWS deploy — `make deploy <bootstrap|network|ecr|backend|frontend|all|web|diff|destroy|help>`
ifeq ($(DEPLOY_ARG),)
	@$(MAKE) --no-print-directory _deploy-help
else ifeq ($(DEPLOY_ARG),help)
	@$(MAKE) --no-print-directory _deploy-help
else ifeq ($(DEPLOY_ARG),bootstrap)
	$(DEPLOY) infra/deploy.py --bootstrap --region $(REGION) $(ARGS)
else ifeq ($(DEPLOY_ARG),web)
	docker run --rm -v $(PWD)/apps/web:/app -w /app node:20-slim sh -c "npm install && npm run build"
	$(DEPLOY) infra/deploy.py --web --region $(REGION) $(ARGS)
else ifeq ($(DEPLOY_ARG),diff)
	$(DEPLOY) infra/deploy.py --all --region $(REGION) --diff $(ARGS)
else ifeq ($(DEPLOY_ARG),destroy)
	$(DEPLOY) infra/deploy.py --all --region $(REGION) --destroy $(ARGS)
else ifeq ($(DEPLOY_ARG),all)
	$(DEPLOY) infra/deploy.py --bootstrap --region $(REGION)
	$(DEPLOY) infra/deploy.py --stack network --region $(REGION)
	$(DEPLOY) infra/deploy.py --stack ecr --region $(REGION)
	$(MAKE) build-push TAG=$(TAG)
	$(DEPLOY) infra/deploy.py --stack backend --region $(REGION) \
	  --param ApiImageTag=$(TAG) --param WorkerImageTag=$(TAG) $(ARGS)
	$(DEPLOY) infra/deploy.py --stack frontend --region $(REGION)
	docker run --rm -v $(PWD)/apps/web:/app -w /app node:20-slim sh -c "npm install && npm run build"
	$(DEPLOY) infra/deploy.py --web --region $(REGION)
else ifneq (,$(filter $(DEPLOY_ARG),network ecr backend frontend))
	$(DEPLOY) infra/deploy.py --stack $(DEPLOY_ARG) --region $(REGION) $(ARGS)
else
	@echo "Unknown deploy target: '$(DEPLOY_ARG)'" >&2
	@$(MAKE) --no-print-directory _deploy-help
	@exit 1
endif

_deploy-help:
	@echo "Usage: make deploy <target> [REGION=$(REGION)] [TAG=<sha>] [ARGS=\"--extra --flags\"]"
	@echo ""
	@echo "  bootstrap   One-time: create the CloudFormation template bucket"
	@echo "  network     Deploy the network stack (VPC)"
	@echo "  ecr         Deploy the ecr stack (api + worker repositories)"
	@echo "  backend     Deploy the backend stack (API, worker, jobs, auth)"
	@echo "  frontend    Deploy the frontend stack (S3 site, CloudFront)"
	@echo "  all         Deploy everything from scratch: bootstrap -> network -> ecr -> build+push -> backend+frontend -> web"
	@echo "  web         Build the SPA and publish it (config.json, S3 sync, CloudFront invalidation)"
	@echo "  diff        Preview change sets for network+ecr+backend+frontend without executing"
	@echo "  destroy     Delete the app stacks (frontend, backend, ecr, network) — bootstrap is left intact"
	@echo "  help        Show this message"
	@echo ""
	@echo "ARGS is passed through to infra/deploy.py, e.g.:"
	@echo "  make deploy backend ARGS=\"--param ApiImageTag=abc123\""
