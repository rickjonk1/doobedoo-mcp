#!/bin/bash
# Haal een nieuwe versie op en herstart de app. Draait op de server, via SSM.
# Gebruik: doobedoo-deploy <tag>
set -euo pipefail
source /etc/doobedoo/instellingen        # REGISTER, DOMEIN, BEHEER_EMAIL, REGIO
TAG="${1:-latest}"
export AWS_DEFAULT_REGION="$REGIO"

# Geheimen: één keer op de server zelf gemaakt en in Parameter Store (KMS)
# gezet. Ze gaan nooit via GitHub of het deploylogboek.
for naam in SECRET_KEY FLASK_SECRET; do
  if ! aws ssm get-parameter --name "/doobedoo/$naam" >/dev/null 2>&1; then
    waarde=$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')
    aws ssm put-parameter --name "/doobedoo/$naam" --type SecureString --value "$waarde" >/dev/null
    echo "Nieuw geheim /doobedoo/$naam aangemaakt."
  fi
done

umask 077
{
  echo "APP_ENV=production"
  echo "DATABASE_URL=sqlite:////data/doobedoo.db"
  echo "PUBLIEKE_URL=https://$DOMEIN"
  echo "ADMIN_EMAIL=$BEHEER_EMAIL"
  echo "AWS_DEFAULT_REGION=$REGIO"
  for naam in SECRET_KEY FLASK_SECRET; do
    echo "$naam=$(aws ssm get-parameter --name "/doobedoo/$naam" --with-decryption \
                    --query Parameter.Value --output text)"
  done
} > /etc/doobedoo/app.env

aws ecr get-login-password | docker login --username AWS --password-stdin "${REGISTER%%/*}" >/dev/null 2>&1
docker pull -q "$REGISTER:$TAG" >/dev/null
docker network inspect doobedoo >/dev/null 2>&1 || docker network create doobedoo >/dev/null

docker rm -f app >/dev/null 2>&1 || true
docker run -d --name app --network doobedoo --restart unless-stopped \
  --env-file /etc/doobedoo/app.env \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 700m --pids-limit 256 \
  -v /srv/doobedoo/data:/data \
  --log-opt max-size=10m --log-opt max-file=5 \
  "$REGISTER:$TAG" >/dev/null

# Caddy (TLS) alleen herstarten als de Caddyfile veranderd is.
nieuw=$(sha256sum /etc/doobedoo/Caddyfile | cut -d' ' -f1)
oud=$(cat /etc/doobedoo/Caddyfile.sha 2>/dev/null || true)
if [ "$nieuw" != "$oud" ] || ! docker ps --format '{{.Names}}' | grep -qx caddy; then
  docker rm -f caddy >/dev/null 2>&1 || true
  docker run -d --name caddy --network doobedoo --restart unless-stopped \
    -p 80:80 -p 443:443 -p 443:443/udp \
    -e DOMEIN="$DOMEIN" -e BEHEER_EMAIL="$BEHEER_EMAIL" \
    --cap-drop ALL --cap-add NET_BIND_SERVICE --security-opt no-new-privileges \
    -v /etc/doobedoo/Caddyfile:/etc/caddy/Caddyfile:ro \
    -v /srv/doobedoo/caddy:/data \
    --log-opt max-size=10m --log-opt max-file=3 \
    caddy:2.8.4-alpine >/dev/null
  echo "$nieuw" > /etc/doobedoo/Caddyfile.sha
fi

for _ in $(seq 1 30); do
  if docker exec app python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/gezond',timeout=2)" 2>/dev/null; then
    echo "App draait (versie $TAG)."
    docker image prune -af --filter "until=168h" >/dev/null 2>&1 || true
    exit 0
  fi
  sleep 2
done
echo "De app start niet. Laatste logregels:"
docker logs --tail 40 app 2>&1 | grep -v -i "instellen via" || true
exit 1
