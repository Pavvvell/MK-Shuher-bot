#!/bin/sh
set -eu
URL=$(gcloud run services describe shuher-lite --region=europe-west3 --format='value(status.url)')
if [ -z "$URL" ]; then
  echo "No Cloud Run URL found. Deploy first: sh deploy.sh"
  exit 1
fi
printf 'WARNING: This will switch the existing @shuher_work_bot Telegram webhook to Google Cloud Run.\n'
printf 'Old Railway service must not re-register the webhook.\n'
printf 'Run activation? Type exactly ACTIVATE: '
read ANSWER
if [ "$ANSWER" != "ACTIVATE" ]; then
  echo "Cancelled."
  exit 1
fi
gcloud run services update shuher-lite --region=europe-west3 \
  --update-env-vars="PUBLIC_BASE_URL=$URL,REGISTER_TELEGRAM_WEBHOOK=true"
curl --fail --silent --show-error "$URL/health"
printf '\nWebhook registration is triggered during startup. Test /start in Telegram.\n'
