# Déploiement sur un VPS OVH (VPS-1 : 2 vCore, 4 Go, 40 Go)

## 1. À la commande chez OVH

- **Système : Ubuntu 24.04 LTS, OS seul.** Pas d'image « n8n » ni « Docker »
  préinstallée : cette pile a son propre `docker-compose.yml` (n8n + Postgres
  pgvector + Caddy) et le script ci-dessous installe Docker lui-même.
- Datacenter en Europe (Gravelines / Roubaix).
- Authentification : **clé SSH** (ajoutez la vôtre dans l'espace client OVH).

## 2. Installation (une commande)

Connectez-vous (`ssh ubuntu@IP_DU_VPS`, ou `root@` selon l'image), puis :

```bash
git clone https://github.com/mhmedammara/rag-compliance.git
cd rag-compliance/compliance-rag
sudo OPENAI_API_KEY="sk-..." bash deploy/setup-vps.sh
```

Le script : installe Docker, ajoute 2 Go de swap, ouvre uniquement les ports
22/80/443, génère `docker/.env` (mot de passe Postgres aléatoire), démarre
n8n + Postgres + Caddy, puis charge la base de connaissance (embeddings OpenAI).
Il est relançable sans danger.

**Pas de nom de domaine ?** Le script utilise `IP-avec-tirets.sslip.io`
(ex. `51-210-12-34.sslip.io`), un nom gratuit qui pointe vers l'IP du VPS ;
Caddy obtient un vrai certificat HTTPS. Pour un domaine à vous plus tard :
créez un enregistrement DNS `A` vers l'IP, changez `DOMAIN=` dans
`docker/.env`, puis relancez la commande `docker compose` de la section 5.

> Le dépôt est privé ? Utilisez un jeton GitHub pour le `git clone`, ou
> copiez le dossier avec `scp -r`.

## 3. Configurer n8n

Ouvrez `https://<DOMAIN>` (affiché à la fin du script) et **créez tout de suite
le compte propriétaire**. Puis suivez le § 5 de `GUIDE-DEPLOIEMENT.md`, avec
ces valeurs :

| Credential | Valeurs |
|---|---|
| Postgres | Host `postgres`, Port `5432`, Database/User/Password : voir `docker/.env`, SSL `disable` |
| OpenAI | votre clé API |

Importez `workflows/Logiclever - Reponses aux questionnaires de conformite VF.json`.

## 4. Fichiers de preuve

Les documents de preuve sont **publics** : `https://<DOMAIN>/documents-preuve/`
(les liens `url_partage` de la base pointent là, ils fonctionnent donc pour vos
clients). Les classeurs générés (`/generes`, réponses clients) ne sont **pas**
exposés ; le port 8088 n'écoute que sur `127.0.0.1`. Pour les parcourir :

```bash
ssh -L 8088:127.0.0.1:8088 ubuntu@IP_DU_VPS     # puis http://localhost:8088
```


## 5. Exploitation

```bash
cd ~/rag-compliance/compliance-rag/docker
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps        # état
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f n8n
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d     # appliquer un changement
```

**Sauvegarde** (à faire avant toute mise à jour ; clé de chiffrement n8n incluse
dans `n8n_data`) :

```bash
docker run --rm -v n8n_data:/d -v "$PWD":/b alpine tar czf /b/n8n_data.tgz -C /d .
docker compose exec postgres pg_dump -U logiclever logiclever | gzip > logiclever.sql.gz
```

Mémoire : `free -h` ; la pile consomme ~1 Go sur les 4 Go du VPS.
