#!/bin/bash
# Diagnostic script to test Secret Manager access
echo "=== Testing metadata server ==="
ACCESS_TOKEN=$(curl -sf -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token','EMPTY'))")

echo "Access token length: ${#ACCESS_TOKEN}"
if [ -z "$ACCESS_TOKEN" ] || [ "$ACCESS_TOKEN" = "EMPTY" ]; then
  echo "ERROR: access token is empty"
  exit 1
fi

echo "=== Testing Secret Manager ==="
SECRET_RESPONSE=$(curl -sf -H "Authorization: Bearer $ACCESS_TOKEN" \
  "https://secretmanager.googleapis.com/v1/projects/ammopt/secrets/github-pat/versions/latest:access")

echo "Secret API response length: ${#SECRET_RESPONSE}"
echo "First 200 chars: ${SECRET_RESPONSE:0:200}"

PAT=$(echo "$SECRET_RESPONSE" | python3 -c "import sys,json,base64; d=json.load(sys.stdin); print(base64.b64decode(d['payload']['data']).decode())" 2>&1)
echo "PAT length: ${#PAT}"
echo "PAT first 4 chars: ${PAT:0:4}"

if [ -z "$PAT" ]; then
  echo "ERROR: PAT is empty"
  exit 1
fi

echo "=== Testing git clone with PAT ==="
git clone https://$PAT@github.com/jaoz/amm-challenge.git /tmp/amm-test --depth=1 --branch Search-powell-style
echo "Clone result: $?"
ls /tmp/amm-test/Cursor/tools/ | head -5
