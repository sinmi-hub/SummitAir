#!/usr/bin/env bash
# Creates billable resources. Run only after explicit hosting-cost approval.
set -euo pipefail
[[ "${APPROVE_NEW_HOSTING_CHARGES:-}" == yes ]] || { echo 'Hosting-cost approval is required.'; exit 1; }
: "${PROJECT:?Set your Google Cloud project ID}"
ZONE=us-central1-a
REGION=us-central1
NAME=summitair-sip

gcloud services enable compute.googleapis.com --project="$PROJECT"
gcloud compute networks describe "$NAME" --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud compute networks create "$NAME" --project="$PROJECT" --subnet-mode=custom
gcloud compute networks subnets describe "$NAME" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud compute networks subnets create "$NAME" --project="$PROJECT" --network="$NAME" --region="$REGION" --range=10.84.0.0/28
gcloud compute firewall-rules describe summitair-https --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud compute firewall-rules create summitair-https --project="$PROJECT" --network="$NAME" --allow=tcp:80,tcp:443 --source-ranges=0.0.0.0/0 --target-tags=summitair
gcloud compute firewall-rules describe summitair-iap-ssh --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud compute firewall-rules create summitair-iap-ssh --project="$PROJECT" --network="$NAME" --allow=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=summitair
gcloud compute addresses describe "$NAME" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud compute addresses create "$NAME" --region="$REGION" --project="$PROJECT"
SUMMITAIR_IP=$(gcloud compute addresses describe "$NAME" --region="$REGION" --project="$PROJECT" --format='value(address)')
gcloud compute instances describe "$NAME" --zone="$ZONE" --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud compute instances create "$NAME" --project="$PROJECT" --zone="$ZONE" \
    --machine-type=e2-small --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-size=10GB --boot-disk-type=pd-standard --subnet="$NAME" \
    --address="$SUMMITAIR_IP" --tags=summitair --no-service-account --no-scopes \
    --metadata=block-project-ssh-keys=true --labels=app=summitair
printf 'Host: %s; IP: %s\n' "$NAME" "$SUMMITAIR_IP"
