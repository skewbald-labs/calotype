#!/usr/bin/env bash
# Build Calotype in Cloud Build and deploy it to Cloud Run.
#
#   calotype/deploy/deploy.sh --project PROJECT_ID \
#     --service-account calotype-run@PROJECT_ID.iam.gserviceaccount.com [options]
#
# Run --help for the options. --dry-run prints every gcloud command and runs
# none of them. deploy/README.md lists the IAM roles the deployer and the
# runtime service account need, and what this costs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CALOTYPE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Settings: environment variables first, flags override them.
PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-calotype}"
RUNTIME_SA="${RUNTIME_SA:-}"
BUILD_SA="${BUILD_SA:-}"
REPOSITORY="${REPOSITORY:-calotype}"
IMAGE_NAME="${IMAGE_NAME:-calotype}"
TAG="${TAG:-}"
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-}"
# Collection prefix in Firestore. A second service (for example a Stripe
# sandbox) uses its own prefix so its keys and usage never mix with the main one.
FIRESTORE_PREFIX="${FIRESTORE_PREFIX:-}"
# Requests per instance. The same number becomes CALOTYPE_MAX_CONCURRENCY, the
# size of the app's capture semaphore, so requests queue at Cloud Run's load
# balancer rather than inside the app.
CONCURRENCY="${CONCURRENCY:-4}"
PUBLIC_URL="${PUBLIC_URL:-}"
STRIPE=0
STRIPE_KEY_SECRET="${STRIPE_KEY_SECRET:-calotype-stripe-secret-key}"
STRIPE_WEBHOOK_SECRET_NAME="${STRIPE_WEBHOOK_SECRET_NAME:-calotype-stripe-webhook-secret}"
SECRET_VERSION="${SECRET_VERSION:-latest}"
STRIPE_PRICE_STARTER="${STRIPE_PRICE_STARTER:-}"
STRIPE_PRICE_PRO="${STRIPE_PRICE_PRO:-}"
EMAIL_NOTICES=0
EMAIL_TOKEN_SECRET_NAME="${EMAIL_TOKEN_SECRET_NAME:-calotype-email-token-secret}"
OAUTH=0
OAUTH_SECRET_NAME="${OAUTH_SECRET_NAME:-calotype-oauth-secret}"
# With --stripe: STRIPE_AUTOMATIC_TAX=1 turns on Stripe Tax in Checkout. Set it only
# once the account has a tax registration (Texas) and its head office address.
# With --credits (which needs --stripe): prepaid credit by machine payment, POST /v1/credits.
# STRIPE_PROFILE_ID is the Stripe profile id (profile_...); it is not a secret.
CREDITS=0
STRIPE_PROFILE_ID="${STRIPE_PROFILE_ID:-}"
DRY_RUN=0
SKIP_SETUP=0

usage() {
  cat <<'EOF'
Usage: deploy.sh --project PROJECT_ID --service-account RUNTIME_SA_EMAIL [options]

Required:
  --project ID                 Google Cloud project id (env PROJECT_ID)
  --service-account EMAIL      service account the Cloud Run service runs as (env RUNTIME_SA)

Options:
  --region REGION              Cloud Run, Artifact Registry and build region (default us-central1; env REGION)
  --service NAME               Cloud Run service name (default calotype; env SERVICE)
  --build-service-account EMAIL  run the Cloud Build job as this account (env BUILD_SA)
  --repository NAME            Artifact Registry repository (default calotype; env REPOSITORY)
  --tag TAG                    image tag (default: git commit, plus -dirty-<time> if calotype/ has changes)
  --firestore-location LOC     location for a new Firestore database (default: the region)
  --firestore-prefix P         Firestore collection prefix (default: the app's, calotype_; env FIRESTORE_PREFIX)
  --concurrency N              requests per instance and the app's semaphore size (default 4)
  --public-url URL             public origin for Stripe redirects (sets CALOTYPE_PUBLIC_URL)
  --stripe                     turn billing on: Stripe secrets from Secret Manager, price ids from
                               STRIPE_PRICE_STARTER and STRIPE_PRICE_PRO (environment)
                               (STRIPE_AUTOMATIC_TAX=1 also turns on Stripe Tax in Checkout)
  --credits                    with --stripe: turn on POST /v1/credits (prepaid credit by machine
                               payment, cards only). Needs STRIPE_PROFILE_ID=profile_... (environment).
                               Read deploy/README.md, "Prepaid credit", before using it
  --email-notices              turn quota emails on: CALOTYPE_EMAIL_TOKEN_SECRET from the Secret Manager
                               secret calotype-email-token-secret (env EMAIL_TOKEN_SECRET_NAME). Pass it on
                               every deploy once on: a deploy without it removes the secret from the service
  --oauth                      turn OAuth sign-in on /mcp on: CALOTYPE_OAUTH_SECRET from the Secret Manager
                               secret calotype-oauth-secret (env OAUTH_SECRET_NAME). Pass it on every deploy
                               once on, like --email-notices
  --secret-version V           Secret Manager version for the secrets (default latest)
  --skip-setup                 skip enabling APIs and creating the repository and database
  --dry-run                    print the gcloud commands, run nothing
  -h, --help                   this text
EOF
}

die() { printf 'deploy.sh: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

# Print a command, quoted so it can be pasted, and run it unless --dry-run.
run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  if [[ $DRY_RUN -eq 0 ]]; then
    "$@"
  fi
}

# ensure WHAT CHECK_CMD... :: CREATE_CMD...
# Runs CHECK_CMD; if it reports the resource missing, runs CREATE_CMD. Any other
# failure (no permission, API off) stops the script rather than guessing.
ensure() {
  local what="$1"
  shift
  local check=() create=()
  while [[ $# -gt 0 && "$1" != "::" ]]; do
    check+=("$1")
    shift
  done
  [[ $# -gt 0 ]] || die "ensure: no '::' between the check and the create command"
  shift
  create=("$@")
  if [[ $DRY_RUN -eq 1 ]]; then
    say "# $what: created only if this check reports it missing"
    printf '+'; printf ' %q' "${check[@]}"; printf '\n'
    printf '+'; printf ' %q' "${create[@]}"; printf '\n'
    return
  fi
  local out
  if out="$("${check[@]}" 2>&1)"; then
    say "# $what: exists"
  elif grep -qiE 'NOT_FOUND|not found|does not exist' <<<"$out"; then
    say "# $what: missing, creating"
    run "${create[@]}"
  else
    printf '%s\n' "$out" >&2
    die "could not check $what (see the error above)"
  fi
}

need_value() { [[ $# -ge 2 && -n "$2" ]] || die "$1 needs a value"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) need_value "$@"; PROJECT_ID="$2"; shift 2 ;;
    --project=*) PROJECT_ID="${1#*=}"; shift ;;
    --region) need_value "$@"; REGION="$2"; shift 2 ;;
    --region=*) REGION="${1#*=}"; shift ;;
    --service) need_value "$@"; SERVICE="$2"; shift 2 ;;
    --service=*) SERVICE="${1#*=}"; shift ;;
    --service-account) need_value "$@"; RUNTIME_SA="$2"; shift 2 ;;
    --service-account=*) RUNTIME_SA="${1#*=}"; shift ;;
    --build-service-account) need_value "$@"; BUILD_SA="$2"; shift 2 ;;
    --build-service-account=*) BUILD_SA="${1#*=}"; shift ;;
    --repository) need_value "$@"; REPOSITORY="$2"; shift 2 ;;
    --repository=*) REPOSITORY="${1#*=}"; shift ;;
    --tag) need_value "$@"; TAG="$2"; shift 2 ;;
    --tag=*) TAG="${1#*=}"; shift ;;
    --firestore-location) need_value "$@"; FIRESTORE_LOCATION="$2"; shift 2 ;;
    --firestore-location=*) FIRESTORE_LOCATION="${1#*=}"; shift ;;
    --firestore-prefix) need_value "$@"; FIRESTORE_PREFIX="$2"; shift 2 ;;
    --firestore-prefix=*) FIRESTORE_PREFIX="${1#*=}"; shift ;;
    --concurrency) need_value "$@"; CONCURRENCY="$2"; shift 2 ;;
    --concurrency=*) CONCURRENCY="${1#*=}"; shift ;;
    --public-url) need_value "$@"; PUBLIC_URL="$2"; shift 2 ;;
    --public-url=*) PUBLIC_URL="${1#*=}"; shift ;;
    --secret-version) need_value "$@"; SECRET_VERSION="$2"; shift 2 ;;
    --secret-version=*) SECRET_VERSION="${1#*=}"; shift ;;
    --stripe) STRIPE=1; shift ;;
    --credits) CREDITS=1; shift ;;
    --email-notices) EMAIL_NOTICES=1; shift ;;
    --oauth) OAUTH=1; shift ;;
    --skip-setup) SKIP_SETUP=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

# --- check the inputs ---------------------------------------------------------------

[[ -n "$PROJECT_ID" ]] || { usage >&2; die "--project is required"; }
[[ -n "$RUNTIME_SA" ]] || { usage >&2; die "--service-account is required"; }
[[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "not a project id: $PROJECT_ID"
[[ "$REGION" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || die "not a region: $REGION"
[[ "$SERVICE" =~ ^[a-z]([-a-z0-9]{0,47}[a-z0-9])?$ ]] || die "not a Cloud Run service name: $SERVICE"
[[ "$REPOSITORY" =~ ^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$ ]] || die "not a repository name: $REPOSITORY"
[[ "$IMAGE_NAME" =~ ^[a-z0-9]([-._a-z0-9]{0,126}[a-z0-9])?$ ]] || die "not an image name: $IMAGE_NAME"
sa_pattern='^[a-z0-9][-a-z0-9]{0,62}@[-a-z0-9.]+\.gserviceaccount\.com$'
[[ "$RUNTIME_SA" =~ $sa_pattern ]] || die "not a service account email: $RUNTIME_SA"
if [[ "$RUNTIME_SA" == *-compute@developer.gserviceaccount.com ]]; then
  die "use a dedicated runtime service account, not the Compute Engine default (see deploy/README.md)"
fi
if [[ -n "$BUILD_SA" && ! "$BUILD_SA" =~ $sa_pattern ]]; then
  die "not a service account email: $BUILD_SA"
fi
if ! [[ "$CONCURRENCY" =~ ^[0-9]{1,2}$ ]] || ((CONCURRENCY < 1 || CONCURRENCY > 80)); then
  die "--concurrency must be 1 to 80"
fi
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-$REGION}"
[[ "$FIRESTORE_LOCATION" =~ ^[a-z0-9-]+$ ]] || die "not a Firestore location: $FIRESTORE_LOCATION"
if [[ -n "$PUBLIC_URL" ]]; then
  [[ "$PUBLIC_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] \
    || die "--public-url must be an https origin with no path, like https://calotype.example"
fi
if [[ $STRIPE -eq 1 ]]; then
  [[ "$STRIPE_PRICE_STARTER" =~ ^price_[A-Za-z0-9]+$ ]] || die "--stripe needs STRIPE_PRICE_STARTER=price_..."
  [[ "$STRIPE_PRICE_PRO" =~ ^price_[A-Za-z0-9]+$ ]] || die "--stripe needs STRIPE_PRICE_PRO=price_..."
fi
if [[ $STRIPE -eq 1 || $EMAIL_NOTICES -eq 1 || $OAUTH -eq 1 ]]; then
  [[ "$SECRET_VERSION" =~ ^(latest|[1-9][0-9]*)$ ]] || die "--secret-version must be latest or a number"
fi
if [[ $EMAIL_NOTICES -eq 1 ]]; then
  [[ "$EMAIL_TOKEN_SECRET_NAME" =~ ^[A-Za-z0-9_-]{1,255}$ ]] || die "not a secret name: $EMAIL_TOKEN_SECRET_NAME"
fi
if [[ $OAUTH -eq 1 ]]; then
  [[ "$OAUTH_SECRET_NAME" =~ ^[A-Za-z0-9_-]{1,255}$ ]] || die "not a secret name: $OAUTH_SECRET_NAME"
fi

if [[ $CREDITS -eq 1 ]]; then
  [[ $STRIPE -eq 1 ]] || die "--credits needs --stripe (the same Stripe secret key takes the payment)"
  [[ "$STRIPE_PROFILE_ID" =~ ^profile_[A-Za-z0-9_]+$ ]] || die "--credits needs STRIPE_PROFILE_ID=profile_..."
fi

if [[ -z "$TAG" ]]; then
  if sha="$(git -C "$CALOTYPE_DIR" rev-parse --short=12 HEAD 2>/dev/null)"; then
    TAG="$sha"
    if [[ -n "$(git -C "$CALOTYPE_DIR" status --porcelain -- . 2>/dev/null)" ]]; then
      TAG="$sha-dirty-$(date -u +%Y%m%d%H%M%S)"
    fi
  else
    TAG="build-$(date -u +%Y%m%d%H%M%S)"
  fi
fi
[[ "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "not an image tag: $TAG"

IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$IMAGE_NAME:$TAG"

if [[ $DRY_RUN -eq 0 ]]; then
  command -v gcloud >/dev/null 2>&1 || die "gcloud is not installed"
else
  say "# Dry run: nothing below is executed."
fi
say "# Project $PROJECT_ID, region $REGION, service $SERVICE"
say "# Image $IMAGE"

# --- one-time setup (idempotent) -------------------------------------------------------

if [[ $SKIP_SETUP -eq 0 ]]; then
  apis=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com firestore.googleapis.com)
  if [[ $STRIPE -eq 1 || $EMAIL_NOTICES -eq 1 || $OAUTH -eq 1 ]]; then
    apis+=(secretmanager.googleapis.com)
  fi
  run gcloud services enable "${apis[@]}" --project "$PROJECT_ID"

  ensure "Artifact Registry repository $REPOSITORY" \
    gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" --project "$PROJECT_ID" \
    :: gcloud artifacts repositories create "$REPOSITORY" --repository-format docker --location "$REGION" \
    --description "Calotype images" --project "$PROJECT_ID"

  ensure "Firestore (default) database" \
    gcloud firestore databases describe --database "(default)" --project "$PROJECT_ID" --format "value(name)" \
    :: gcloud firestore databases create --database "(default)" --location "$FIRESTORE_LOCATION" \
    --type firestore-native --project "$PROJECT_ID"

  if [[ $STRIPE -eq 1 ]]; then
    # Only checked, never created: the values are put in by a person, not by this script.
    for secret in "$STRIPE_KEY_SECRET" "$STRIPE_WEBHOOK_SECRET_NAME"; do
      run gcloud secrets describe "$secret" --project "$PROJECT_ID" --format "value(name)"
    done
  fi
  if [[ $EMAIL_NOTICES -eq 1 ]]; then
    run gcloud secrets describe "$EMAIL_TOKEN_SECRET_NAME" --project "$PROJECT_ID" --format "value(name)"
  fi
  if [[ $OAUTH -eq 1 ]]; then
    run gcloud secrets describe "$OAUTH_SECRET_NAME" --project "$PROJECT_ID" --format "value(name)"
  fi
else
  say "# --skip-setup: APIs, repository and database are assumed to exist"
fi

# --- build -----------------------------------------------------------------------------

build=(gcloud builds submit "$CALOTYPE_DIR"
  --config "$SCRIPT_DIR/cloudbuild.yaml"
  --substitutions "_REGION=$REGION,_REPOSITORY=$REPOSITORY,_IMAGE=$IMAGE_NAME,_TAG=$TAG"
  --region "$REGION"
  --project "$PROJECT_ID")
if [[ -n "$BUILD_SA" ]]; then
  build+=(--service-account "projects/$PROJECT_ID/serviceAccounts/$BUILD_SA")
fi
run "${build[@]}"

# --- deploy ----------------------------------------------------------------------------

env_vars="CALOTYPE_STORAGE=firestore,CALOTYPE_TRUSTED_PROXY_HOPS=1,CALOTYPE_MAX_CONCURRENCY=$CONCURRENCY"
if [[ -n "$PUBLIC_URL" ]]; then
  env_vars+=",CALOTYPE_PUBLIC_URL=$PUBLIC_URL"
fi
if [[ -n "$FIRESTORE_PREFIX" ]]; then
  [[ "$FIRESTORE_PREFIX" =~ ^[a-z][a-z0-9_]{0,40}$ ]] || die "--firestore-prefix must be lower-case letters, digits and _"
  env_vars+=",CALOTYPE_FIRESTORE_COLLECTION_PREFIX=$FIRESTORE_PREFIX"
fi

deploy=(gcloud run deploy "$SERVICE"
  --image "$IMAGE"
  --project "$PROJECT_ID"
  --region "$REGION"
  --service-account "$RUNTIME_SA"
  --no-invoker-iam-check
  --execution-environment gen2
  --cpu 1
  --memory 2Gi
  --concurrency "$CONCURRENCY"
  --min-instances 0
  --max-instances 2
  --timeout 60
  --port 8080
  --labels app=calotype)

if [[ $STRIPE -eq 1 ]]; then
  # Price ids are not secret. The two secrets are references into Secret
  # Manager, resolved by Cloud Run at start; their values never pass through here.
  env_vars+=",STRIPE_PRICE_STARTER=$STRIPE_PRICE_STARTER,STRIPE_PRICE_PRO=$STRIPE_PRICE_PRO"
  if [[ -n "${STRIPE_AUTOMATIC_TAX:-}" ]]; then
    [[ "$STRIPE_AUTOMATIC_TAX" =~ ^(1|0|true|false)$ ]] || die "STRIPE_AUTOMATIC_TAX must be 1, 0, true or false"
    env_vars+=",STRIPE_AUTOMATIC_TAX=$STRIPE_AUTOMATIC_TAX"
  fi
  if [[ $CREDITS -eq 1 ]]; then
    # Cards through shared payment tokens only; the stablecoin method is not built.
    env_vars+=",CALOTYPE_CREDITS=1,CALOTYPE_CREDITS_CARD=1,STRIPE_PROFILE_ID=$STRIPE_PROFILE_ID"
  fi
  secrets="STRIPE_SECRET_KEY=$STRIPE_KEY_SECRET:$SECRET_VERSION,STRIPE_WEBHOOK_SECRET=$STRIPE_WEBHOOK_SECRET_NAME:$SECRET_VERSION"
else
  # Billing off: no Stripe secret references, so the billing endpoints answer 503.
  secrets=""
fi
if [[ $EMAIL_NOTICES -eq 1 ]]; then
  # Signs the removal links in quota emails (calotype/notices.py). The mail key is not here:
  # the skewbald-checks job sends the emails.
  secrets+="${secrets:+,}CALOTYPE_EMAIL_TOKEN_SECRET=$EMAIL_TOKEN_SECRET_NAME:$SECRET_VERSION"
fi
if [[ $OAUTH -eq 1 ]]; then
  # Signs OAuth client ids and the consent form (calotype/oauth.py). Without it OAuth is off.
  secrets+="${secrets:+,}CALOTYPE_OAUTH_SECRET=$OAUTH_SECRET_NAME:$SECRET_VERSION"
fi
if [[ -n "$secrets" ]]; then
  # --set-secrets replaces the whole list, so every secret the service uses is named here.
  deploy+=(--set-secrets "$secrets")
else
  deploy+=(--clear-secrets)
fi
deploy+=(--set-env-vars "$env_vars")

run "${deploy[@]}"

if [[ $DRY_RUN -eq 0 ]]; then
  url="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')"
  say "# Deployed: $url"
  say "# Check: curl -fsS $url/health"
fi
