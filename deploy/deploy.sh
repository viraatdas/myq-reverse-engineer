#!/usr/bin/env bash
#
# Deploy the MyQ API to AWS Lambda behind a Function URL.
#
#   ./deploy/deploy.sh
#
# Idempotent: safe to re-run. Creates on first run, updates afterwards.
#
# Why Lambda rather than a VM:
#   * The endpoint gets a valid HTTPS certificate for free, which iOS Shortcuts
#     needs. A bare EC2 IP would be plain HTTP unless you also buy a domain.
#   * Effectively $0/month at garage-door request volumes.
#   * Tokens live in SSM Parameter Store, so a refreshed token survives a cold
#     start. That is the failure mode that kills file-based token storage.
#
# The public entry point is an API Gateway HTTP API rather than a Lambda
# Function URL. Function URLs are the simpler option, but many AWS
# Organizations (including this one) block public Function URLs outright, which
# surfaces as an opaque 403 before the function is ever invoked. API Gateway is
# not subject to that control.
#
# Requires: aws CLI (configured), uv, zip.

set -euo pipefail

FUNCTION_NAME="${FUNCTION_NAME:-myq-api}"
REGION="${AWS_REGION:-us-east-1}"
ROLE_NAME="${ROLE_NAME:-${FUNCTION_NAME}-role}"
SSM_PARAMETER="${SSM_PARAMETER:-/myq/tokens}"
RUNTIME="python3.13"
ARCH="arm64"                 # Graviton: cheaper per ms than x86_64
MEMORY_MB=512
# Long enough for ?wait=true to poll the door through a full open/close cycle.
TIMEOUT_S=90

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${REPO_ROOT}/.build"
ZIP_PATH="${BUILD_DIR}/function.zip"

cd "${REPO_ROOT}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

for tool in aws uv zip; do
  command -v "$tool" >/dev/null || die "$tool is required but not installed"
done

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
log "AWS account ${ACCOUNT_ID}, region ${REGION}"

# ---------------------------------------------------------------- API key ----
# Sourced from .env so the local CLI and the deployed function agree.
if [ -f .env ]; then
  API_KEY="$(grep -E '^API_KEY=' .env | head -1 | cut -d= -f2- | tr -d '"'"'"' ' || true)"
fi
if [ -z "${API_KEY:-}" ] || [ "${API_KEY}" = "your-secure-api-key-here" ]; then
  die "Set a real API_KEY in .env before deploying.
     Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
fi

# -------------------------------------------------------------- build zip ----
log "Building deployment package (${ARCH})"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/pkg"

# Cross-compile dependency wheels for the Lambda runtime, not this laptop.
# boto3 is excluded on purpose — the runtime already ships it.
uv pip install \
  --target "${BUILD_DIR}/pkg" \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.13 \
  --only-binary=:all: \
  --quiet \
  -r requirements.txt

cp -r myq "${BUILD_DIR}/pkg/myq"
find "${BUILD_DIR}/pkg" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUILD_DIR}/pkg" -name '*.dist-info' -type d -prune -exec rm -rf {} + 2>/dev/null || true

(cd "${BUILD_DIR}/pkg" && zip -qr "${ZIP_PATH}" .)
log "Package: $(du -h "${ZIP_PATH}" | cut -f1)"

# ------------------------------------------------------------------- IAM ----
TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

# Least privilege: this role may read and write exactly one SSM parameter.
ACCESS_POLICY="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:PutParameter"],
      "Resource": "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter${SSM_PARAMETER}"
    },
    {
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:Encrypt"],
      "Resource": "*",
      "Condition": {
        "StringEquals": {"kms:ViaService": "ssm.${REGION}.amazonaws.com"}
      }
    }
  ]
}
JSON
)"

if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  log "IAM role ${ROLE_NAME} exists"
else
  log "Creating IAM role ${ROLE_NAME}"
  aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "${TRUST_POLICY}" \
    --description "Execution role for the MyQ garage door API" >/dev/null
  aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  log "Waiting for IAM role to propagate"
  sleep 12
fi

aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name myq-ssm-tokens \
  --policy-document "${ACCESS_POLICY}"

ROLE_ARN="$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)"

# ------------------------------------------------------------------ SSM ------
# Create a placeholder so the function has something to read before login.
if ! aws ssm get-parameter --name "${SSM_PARAMETER}" --region "${REGION}" >/dev/null 2>&1; then
  log "Creating SSM parameter ${SSM_PARAMETER}"
  aws ssm put-parameter \
    --name "${SSM_PARAMETER}" \
    --value '{}' \
    --type SecureString \
    --description "MyQ OAuth tokens (rotated by the API on refresh)" \
    --region "${REGION}" >/dev/null
fi

# --------------------------------------------------------------- Lambda ------
# AWS_REGION is a reserved Lambda variable and is injected automatically;
# pydantic-settings picks it up as `aws_region`, so it is not set here.
ENV_VARS="Variables={API_KEY=${API_KEY},TOKEN_STORE=ssm,SSM_PARAMETER=${SSM_PARAMETER},LOG_LEVEL=INFO}"

if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${REGION}" >/dev/null 2>&1; then
  log "Updating function code"
  aws lambda update-function-code \
    --function-name "${FUNCTION_NAME}" \
    --zip-file "fileb://${ZIP_PATH}" \
    --region "${REGION}" >/dev/null
  aws lambda wait function-updated --function-name "${FUNCTION_NAME}" --region "${REGION}"

  log "Updating function configuration"
  aws lambda update-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --handler myq.lambda_handler.handler \
    --runtime "${RUNTIME}" \
    --role "${ROLE_ARN}" \
    --timeout "${TIMEOUT_S}" \
    --memory-size "${MEMORY_MB}" \
    --environment "${ENV_VARS}" \
    --region "${REGION}" >/dev/null
  aws lambda wait function-updated --function-name "${FUNCTION_NAME}" --region "${REGION}"
else
  log "Creating function ${FUNCTION_NAME}"
  aws lambda create-function \
    --function-name "${FUNCTION_NAME}" \
    --runtime "${RUNTIME}" \
    --architectures "${ARCH}" \
    --role "${ROLE_ARN}" \
    --handler myq.lambda_handler.handler \
    --zip-file "fileb://${ZIP_PATH}" \
    --timeout "${TIMEOUT_S}" \
    --memory-size "${MEMORY_MB}" \
    --environment "${ENV_VARS}" \
    --description "MyQ garage door REST API for iOS Shortcuts" \
    --region "${REGION}" >/dev/null
  aws lambda wait function-active --function-name "${FUNCTION_NAME}" --region "${REGION}"
fi

# ------------------------------------------------------------ API Gateway ----
# No authorizer: authentication is the API key checked inside the app. An IAM
# authorizer would require SigV4 request signing, which iOS Shortcuts cannot do.
FUNCTION_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

API_ID="$(aws apigatewayv2 get-apis --region "${REGION}" \
  --query "Items[?Name=='${FUNCTION_NAME}'].ApiId | [0]" --output text)"

if [ "${API_ID}" = "None" ] || [ -z "${API_ID}" ]; then
  log "Creating HTTP API gateway"
  # --target quick-creates the proxy integration, a $default catch-all route
  # and an auto-deploying $default stage.
  API_ID="$(aws apigatewayv2 create-api \
    --name "${FUNCTION_NAME}" \
    --protocol-type HTTP \
    --target "${FUNCTION_ARN}" \
    --region "${REGION}" \
    --query ApiId --output text)"
else
  log "HTTP API gateway ${API_ID} exists"
fi

# Allow this API (any stage, any route) to invoke the function. Re-running is
# fine: a duplicate statement id is not an error worth failing the deploy over.
aws lambda add-permission \
  --function-name "${FUNCTION_NAME}" \
  --statement-id "apigw-${API_ID}" \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/*" \
  --region "${REGION}" >/dev/null 2>&1 || true

FUNCTION_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com"

# -------------------------------------------------------------- verify -------
log "Waiting for the deployment to go live"
sleep 3

HEALTH="$(curl -fsS --max-time 45 "${FUNCTION_URL}/health" || echo '')"
if [ -z "${HEALTH}" ]; then
  die "Health check failed. Inspect logs with:
     aws logs tail /aws/lambda/${FUNCTION_NAME} --since 5m --region ${REGION}"
fi

echo
log "Deployed"
echo "  Function: ${FUNCTION_NAME} (${ARCH}, ${RUNTIME})"
echo "  Gateway:  ${API_ID}"
echo "  URL:      ${FUNCTION_URL}"
echo "  Tokens:   ssm:${SSM_PARAMETER}"
echo "  Health:   ${HEALTH}"
echo

if echo "${HEALTH}" | grep -q '"authenticated": *false'; then
  warn "No MyQ tokens stored yet. Run:"
  echo "     python -m myq.cli login && python -m myq.cli push-tokens"
fi

echo "Try it:"
echo "  curl \"${FUNCTION_URL}/status?key=\$API_KEY\""
