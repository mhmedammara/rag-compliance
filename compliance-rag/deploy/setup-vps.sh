#!/usr/bin/env bash
# =====================================================================
#  Installation complete sur un VPS Ubuntu 24.04 vierge (OVH VPS-1).
#
#    git clone https://github.com/mhmedammara/rag-compliance.git
#    cd rag-compliance/compliance-rag
#    sudo OPENAI_API_KEY="sk-..." bash deploy/setup-vps.sh
#
#  Relancer le script est sans danger (il ne refait que ce qui manque).
#  Sans OPENAI_API_KEY, les conteneurs demarrent mais la base n'est pas chargee.
# =====================================================================
set -euo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKER_DIR="$RACINE/docker"
cd "$RACINE"

[ "$(id -u)" -eq 0 ] || { echo "Lancez avec sudo." >&2; exit 1; }

echo "==> 1/6 Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

echo "==> 2/6 Swap de 2 Go (filet de securite memoire)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

echo "==> 3/6 Pare-feu (SSH, HTTP, HTTPS uniquement)"
if command -v ufw >/dev/null; then
  ufw allow OpenSSH >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
fi

echo "==> 4/6 Configuration (docker/.env)"
if [ ! -f "$DOCKER_DIR/.env" ]; then
  IP="$(curl -4 -fsS https://api.ipify.org)"
  {
    echo "POSTGRES_USER=logiclever"
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)"
    echo "POSTGRES_DB=logiclever"
    echo "DOMAIN=${IP//./-}.sslip.io"
  } > "$DOCKER_DIR/.env"
  chmod 600 "$DOCKER_DIR/.env"
fi
set -a; . "$DOCKER_DIR/.env"; set +a

echo "==> 5/6 Demarrage des conteneurs"
docker volume inspect n8n_data >/dev/null 2>&1 || docker volume create n8n_data >/dev/null
mkdir -p "$DOCKER_DIR/stockage"
chown 1000:1000 "$DOCKER_DIR/stockage"      # utilisateur "node" de n8n
cd "$DOCKER_DIR"
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --wait
cd "$RACINE"

echo "==> 6/6 Chargement de la base de connaissance"
if [ -n "${OPENAI_API_KEY:-}" ]; then
  apt-get install -y python3-venv >/dev/null
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q "psycopg[binary]>=3.1" "requests>=2.31"
  OPENAI_API_KEY="$OPENAI_API_KEY" FICHIERS_URL="https://${DOMAIN}" .venv/bin/python scripts/deploy_base.py
else
  echo "OPENAI_API_KEY absente : etape ignoree. Pour la faire plus tard :"
  echo "  sudo OPENAI_API_KEY=sk-... bash deploy/setup-vps.sh"
fi

cat <<FIN

=====================================================================
 Termine.  n8n :  https://${DOMAIN}
 (1er acces : n8n demande de creer le compte proprietaire. Faites-le
  tout de suite. Le certificat HTTPS peut mettre ~1 min a arriver.)

 Suite : voir deploy/DEPLOIEMENT-VPS.md, etape "Configurer n8n".
=====================================================================
FIN
