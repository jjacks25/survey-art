#!/usr/bin/env bash
# Build and push the api + worker container images to ECR.
# Requires: docker + AWS credentials on the host (aws CLI is run via a container).
#
#   REGION=us-west-2 PROJECT=survey-art TAG=$(git rev-parse --short HEAD) bash infra/build_push.sh
set -euo pipefail

REGION="${REGION:-us-west-2}"
PROJECT="${PROJECT:-survey-art}"
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"

# Resolve account id via the AWS CLI running in a container (host stays CLI-free).
awscli() {
  docker run --rm -v "$HOME/.aws:/root/.aws:ro" \
    -e AWS_PROFILE -e AWS_REGION="$REGION" -e AWS_DEFAULT_REGION="$REGION" \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
    amazon/aws-cli:latest "$@"
}

ACCOUNT_ID="$(awscli sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo ">> Logging in to ECR ${REGISTRY}"
awscli ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

# --platform linux/amd64: Lambda's image Architectures default to x86_64, but this
# builds fine on arm64 hosts (Apple Silicon) too via buildx/QEMU emulation.
# --provenance=false --sbom=false: buildx otherwise attaches an attestation manifest,
# turning the pushed image into an OCI manifest *list* — Lambda's container image
# support only understands a single Docker v2 schema2 (or plain OCI) image manifest,
# not a list, and fails at deploy time with "image manifest ... is not supported".
BUILDX_ARGS=(--platform linux/amd64 --provenance=false --sbom=false --push)

echo ">> Building + pushing api:${TAG}"
docker buildx build "${BUILDX_ARGS[@]}" -f apps/api/Dockerfile \
  -t "${REGISTRY}/${PROJECT}/api:${TAG}" -t "${REGISTRY}/${PROJECT}/api:latest" .

echo ">> Building + pushing worker:${TAG}"
docker buildx build "${BUILDX_ARGS[@]}" -f Dockerfile \
  -t "${REGISTRY}/${PROJECT}/worker:${TAG}" -t "${REGISTRY}/${PROJECT}/worker:latest" .

echo ">> Done. Deploy with:  make deploy backend ARGS='--param ApiImageTag=${TAG} --param WorkerImageTag=${TAG}'"
