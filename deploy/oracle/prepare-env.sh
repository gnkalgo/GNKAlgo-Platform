#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f docker-compose.yml || ! -f .env.example ]]; then
  echo "Run this script from the GNKAlgo-Platform repository root." >&2
  exit 1
fi

if [[ -e .env ]]; then
  echo ".env already exists; refusing to overwrite it." >&2
  exit 1
fi

for command in openssl docker git; do
  command -v "$command" >/dev/null || { echo "Missing required command: $command" >&2; exit 1; }
done

read -r -p "ACME/operations email [gnkalgo.admin@gmail.com]: " acme_email
acme_email="${acme_email:-gnkalgo.admin@gmail.com}"
read -r -p "SMTP host [smtp.gmail.com]: " smtp_host
smtp_host="${smtp_host:-smtp.gmail.com}"
read -r -p "SMTP port [587]: " smtp_port
smtp_port="${smtp_port:-587}"
read -r -p "SMTP username [gnkalgo.admin@gmail.com]: " smtp_username
smtp_username="${smtp_username:-gnkalgo.admin@gmail.com}"
read -r -s -p "SMTP app password: " smtp_password
echo

if [[ -z "$smtp_password" ]]; then
  echo "SMTP password is required in production." >&2
  exit 1
fi

# Gmail app passwords may be displayed with spaces. Compose values must remain
# single-line, so remove spaces and reject comment/newline characters.
smtp_password="${smtp_password// /}"
for value_name in acme_email smtp_host smtp_port smtp_username smtp_password; do
  value="${!value_name}"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* || "$value" == *'#'* ]]; then
    echo "Invalid character in $value_name." >&2
    exit 1
  fi
done

postgres_password="$(openssl rand -hex 32)"
jwt_secret="$(openssl rand -hex 64)"
api_key_pepper="$(openssl rand -hex 64)"
field_encryption_key="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\r\n')"

# Fernet requires exactly 32 bytes encoded with URL-safe base64. Fail before
# writing .env if the host OpenSSL output is not in the expected form.
if [[ ! "$field_encryption_key" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
  echo "Failed to generate a valid FIELD_ENCRYPTION_KEY." >&2
  exit 1
fi

umask 077
{
  printf 'ENVIRONMENT=production\n'
  printf 'ACME_EMAIL=%s\n' "$acme_email"
  printf 'POSTGRES_PASSWORD=%s\n' "$postgres_password"
  printf 'JWT_SECRET=%s\n' "$jwt_secret"
  printf 'FIELD_ENCRYPTION_KEY=%s\n' "$field_encryption_key"
  printf 'API_KEY_PEPPER=%s\n' "$api_key_pepper"
  printf 'FRONTEND_URL=https://www.gnkalgo.com\n'
  printf 'CORS_ORIGINS=https://www.gnkalgo.com,https://gnkalgo.com\n'
  printf 'COOKIE_SECURE=true\n'
  printf 'EXPOSE_DEV_TOKENS=false\n'
  printf 'SMTP_HOST=%s\n' "$smtp_host"
  printf 'SMTP_PORT=%s\n' "$smtp_port"
  printf 'SMTP_USERNAME=%s\n' "$smtp_username"
  printf 'SMTP_PASSWORD=%s\n' "$smtp_password"
  printf 'SMTP_FROM=gnkalgo.admin@gmail.com\n'
  printf 'DHAN_CLIENT_ID=\n'
  printf 'DHAN_APP_ID=\n'
  printf 'DHAN_APP_SECRET=\n'
  printf 'DHAN_REDIRECT_URI=https://api.gnkalgo.com/api/v1/brokers/dhan/callback\n'
  printf 'FYERS_CLIENT_ID=\n'
  printf 'FYERS_CLIENT_SECRET=\n'
  printf 'FYERS_REDIRECT_URI=https://api.gnkalgo.com/api/v1/brokers/fyers/callback\n'
  printf 'UPSTOX_CLIENT_ID=\n'
  printf 'UPSTOX_CLIENT_SECRET=\n'
  printf 'UPSTOX_REDIRECT_URI=https://api.gnkalgo.com/api/v1/brokers/upstox/callback\n'
} > .env
chmod 600 .env

echo
echo "Created $(pwd)/.env with generated database, JWT, encryption, and API-key secrets."
echo "Broker IDs/secrets remain blank. Add provider-issued values before enabling each broker."
echo "Do not print, copy to chat, or commit .env."
echo
echo "Next checks:"
echo "  docker compose config --quiet"
echo "  getent ahostsv4 gnkalgo.com www.gnkalgo.com api.gnkalgo.com"
