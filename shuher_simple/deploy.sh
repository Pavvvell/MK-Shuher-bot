#!/bin/sh
set -eu
if [ ! -f deploy.env.yaml ]; then
  echo "Create private deploy.env.yaml first (see README)."
  exit 1
fi
if ! command -v gcloud >/dev/null 2>&1; then
  echo "Install Google Cloud CLI (gcloud) and authenticate first."
  exit 1
fi
gcloud config set project mk-shuher-bot
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com sheets.googleapis.com
gcloud run deploy shuher-lite \
  --source . \
  --region=europe-west3 \
  --service-account=shuher-sheets@mk-shuher-bot.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --cpu=1 --memory=512Mi --concurrency=1 --max-instances=1 --min-instances=0 \
  --env-vars-file=deploy.env.yaml
URL=$(gcloud run services describe shuher-lite --region=europe-west3 --format='value(status.url)')
echo "Shuher Lite URL: $URL"
echo "Health (should show google-sheets):"
curl --fail --silent --show-error "$URL/health"
printf '\nService is deployed with Telegram webhook DISABLED.\n'
printf 'After you verify the URL, run: sh activate.sh\n'
