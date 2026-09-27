# Guide de déploiement — Assistant de réponse aux questionnaires de conformité

Système de réponse automatique aux questionnaires RSE / sécurité envoyés par les
services achats clients. Chaque réponse est rédigée en français, sourcée
(document + page) et contrôlée avant restitution.

Temps de déploiement : **environ 30 minutes**, dont ~10 minutes de calcul
d'embeddings.

---

## 1. Ce que contient le dossier

```
compliance-rag/
├── GUIDE-DEPLOIEMENT.md          ce document
├── .env.example                  variables d'environnement à renseigner
├── requirements.txt              dépendances Python
│
├── docker/
│   ├── docker-compose.yml        n8n + Postgres/pgvector + serveur de fichiers
│   ├── .env.example              identifiants Postgres (copier en .env)
│   └── stockage/                 fichiers servis sur http://localhost:8088 (non versionné)
│       ├── documents-preuve/     PDF de preuve, copiés par deploy_base.py
│       └── generes/              classeurs Excel produits par n8n
│
├── sql/
│   └── 01_schema.sql             tables, index HNSW/GIN, fonctions de recherche
│
├── scripts/
│   ├── build_registry.py         trouve, lit (OCR compris) et dédoublonne les documents
│   ├── build_answer_bank.py      extrait 173 réponses validées + 129 indicateurs
│   ├── deploy_base.py            ⇦ LE script de déploiement
│   ├── build_workflow.py         régénère le JSON n8n
│   ├── verifier.py               garde-fou (détection + francisation)
│   └── simuler_workflow.py       rejoue la chaîne de traitement hors de n8n
│
├── seed/                         données extraites (JSON, régénérables)
│   ├── documents.json            617 pages de texte, 39 documents
│   ├── answer_bank.json          173 réponses déjà validées
│   ├── kpi.json                  129 indicateurs × 3 exercices
│   └── traductions_cybervadis.json  glossaire FR du référentiel CyberVadis
│
└── workflows/
    ├── Logiclever - Reponses aux questionnaires de conformite VF.json
    │                                 ⇦ LE workflow de production, à importer dans n8n
    └── logiclever_reponses_questionnaires.json
                                      sortie de build_workflow.py (référence régénérable,
                                      identique à VF.json hors notes)
```

---

## 2. Prérequis

| Élément | Détail |
|---|---|
| Docker Desktop | fait tourner n8n, la base Postgres + pgvector et le serveur de fichiers, en local et gratuitement |
| Clé API OpenAI | les 5 étapes de rédaction/contrôle (modèle `gpt-6-luna`) **et** les *embeddings* (`text-embedding-3-large`) |
| n8n | fourni par `docker/docker-compose.yml` (image `n8nio/n8n`) |
| Python | 3.10 ou supérieur, sur le poste qui lance le chargement |

**Coût.** Le chargement initial coûte moins de 0,10 € d'embeddings. Un
questionnaire de *N* questions consomme 4 appels LLM par question (analyse,
reclassement, rédaction, contrôle), plus 1 contrôle de cohérence pour tout le
formulaire, et 1 appel d'embeddings par question : le coût dépend du tarif
OpenAI en vigueur pour `gpt-6-luna`.

| Étape | Modèle | Rôle |
|---|---|---|
| Analyser la question | `gpt-6-luna` (effort de raisonnement *low*) | reformulation + sous-requêtes, tâche cadrée |
| Reclasser les preuves | `gpt-6-luna` | notation 0-10 des candidats |
| **Rédiger la réponse** | **`gpt-6-luna`** | le texte lu par l'acheteur |
| Contrôler la véracité | `gpt-6-luna` | vérification point par point contre les preuves |
| Contrôle de cohérence | `gpt-6-luna` | 1 seul appel par formulaire |

Le modèle se change en un point : la constante `MODELE` en tête de
`scripts/build_workflow.py` (puis report dans VF.json, cf. § 5.2).

**Une exécution n8n par questionnaire**, quel que soit le nombre de questions.

---

## 3. Étape 1 — Démarrer les conteneurs et préparer le poste

### 3.1 Les conteneurs

```powershell
cd "compliance-rag\docker"
copy .env.example .env        # puis choisissez un mot de passe Postgres
docker compose up -d
docker compose ps             # n8n, postgres (healthy), fichiers : Up
```

| Service | Adresse | Rôle |
|---|---|---|
| `n8n` | http://localhost:5678 | le workflow |
| `postgres` | `127.0.0.1:5433` depuis le poste, `postgres:5432` depuis n8n | image `pgvector/pgvector:pg17` : tables, vecteurs, fonctions de recherche |
| `fichiers` | http://localhost:8088 | nginx en lecture seule sur `docker/stockage` : PDF de preuve et classeurs générés |

> **Migration depuis l'ancien conteneur `docker run`.** Le service `n8n`
> réutilise le volume existant `n8n_data` : workflows, identifiants et clé de
> chiffrement sont conservés. Arrêtez simplement l'ancien conteneur avant
> `docker compose up -d` (`docker stop n8n`), les deux utilisant le port 5678.
>
> Les liens `http://localhost:8088/...` ne fonctionnent que sur ce poste : pour
> un client, joignez le PDF cité plutôt que le lien.

### 3.2 Le poste (scripts Python)

```bash
cd "compliance-rag"
pip install -r requirements.txt
```

Seule la clé OpenAI est à renseigner dans la session :

```powershell
# Windows / PowerShell
$env:OPENAI_API_KEY = "sk-..."
```

```bash
# macOS / Linux
export OPENAI_API_KEY="sk-..."
```

> La connexion Postgres est lue directement dans `docker/.env` (utilisateur,
> mot de passe, base), sur `127.0.0.1:5433` : rien à recopier. `DATABASE_URL`
> ne sert qu'à viser une autre base ; une valeur restée à l'exemple
> (`MOTDEPASSE`, `<...>`) est ignorée. Si vous la renseignez, écrivez bien
> `127.0.0.1` et non `localhost` : sous Windows, la connexion essaie d'abord
> IPv6 et bloque environ deux minutes avant de se rabattre sur IPv4.
>
> Optionnel : `STOCKAGE_DIR` (défaut `compliance-rag/docker/stockage`) et
> `FICHIERS_URL` (défaut `http://localhost:8088`) si vous déplacez le dossier
> servi ou changez le port du serveur de fichiers.

---

## 4. Étape 2 — Charger la base et les vecteurs

Une seule commande fait tout : schéma, découpage, embeddings, chargement, tests.

```bash
python scripts/deploy_base.py
```

Déroulé attendu :

```
[1/5] Application du schema SQL
      tables, index HNSW/GIN et fonctions de recherche en place
[2/5] Documents de preuve et chunks
      39 documents canoniques enregistres
      10 doublons/versions anterieures ecartes
      855 chunks produits
      embeddings chunks : 855/855
[2 bis/5] Fichiers de preuve -> ...\compliance-rag\docker\stockage
      39/39 fichiers copies (servis sur http://localhost:8088)
[3/5] Banque de reponses validees
      173 reponses validees chargees
[4/5] Indicateurs extra-financiers
      ~370 couples indicateur x annee charges
[5/5] Tests de bout en bout
  ? Quel est votre dernier bilan carbone scope 1, 2 et 3 ?
     [reponse_validee ] FAIT/GES-SCOPES               Dernier bilan carbone realise...
     [extrait_document] GES-2023               p.12   Resume executif Scope 2 < 0.1tCO2e...
     [indicateur      ] KPI-RSE                       Reduction des Emissions de GES...
```

Les tests interrogent `recherche_globale`, c'est-à-dire **exactement** ce que le
workflow n8n appelle : s'ils passent, le workflow trouvera. Les six questions
correspondent aux demandes les plus fréquentes des services achats.

Options utiles :

```bash
python scripts/deploy_base.py --schema-only   # créer seulement les tables
python scripts/deploy_base.py --no-embed      # recharger sans recalculer les vecteurs
python scripts/deploy_base.py --test          # relancer uniquement les tests
```

Le script est **idempotent** : vous pouvez le relancer autant de fois que
nécessaire, il remplace le contenu sans dupliquer.

---

## 5. Étape 3 — Configurer n8n

### 5.1 Créer les deux identifiants

Dans n8n → **Credentials** → **New** :

| Type d'identifiant | Champs à renseigner |
|---|---|
| **Postgres** | Host : `postgres` — Database, User, Password : ceux de `docker/.env` — Port : `5432` — SSL : `disable` |
| **OpenAI** | votre clé API OpenAI (modèles de chat et embeddings) |

> Host `postgres` est le nom du service Docker : n8n et la base partagent le
> réseau du `docker-compose.yml`. Le port 5433 n'est utilisé que depuis le poste.

### 5.2 Importer le workflow

n8n → **Workflows** → **Import from File** →
`workflows/Logiclever - Reponses aux questionnaires de conformite VF.json`

C'est le workflow de production : **34 nœuds fonctionnels** et 8 notes
explicatives, qui décrivent chaque étape sur le canevas.
`logiclever_reponses_questionnaires.json` (sortie de `build_workflow.py`) en
est la version régénérable ; toute modification se fait dans
`build_workflow.py`, puis se reporte à l'identique dans VF.json.

### 5.3 Rattacher les identifiants

n8n ne peut pas deviner quel identifiant utiliser. Ouvrez chaque nœud ci-dessous
et sélectionnez-le dans la liste déroulante :

- **Postgres** — nœuds `Recherche` et `Journaliser la campagne`
- **OpenAI** — nœud `Vectoriser les requetes` et les cinq nœuds `Modele : …`

### 5.4 Vérifier l'URL des fichiers

Le nœud **`Parametres`** porte `fichiers_url` (`http://localhost:8088`), utilisé
par `Terminer` pour rediriger vers le classeur écrit dans
`docker/stockage/generes/`. Ne le modifiez que si vous changez le port du
service `fichiers`.

Les deux seuils de confiance (`seuil_haute_confiance`, `seuil_a_verifier`) y
sont réglables. Le niveau retenu pour une réponse est **le plus prudent** entre
celui qu'attribue le contrôleur et celui que donnent ces seuils ; une réponse
sans source citable, ou bâtie sur le repli, ne dépasse jamais `a_verifier`.
Toute modification se reporte aussi dans `Preparer la question` (branche chat).

### 5.5 Activer

**Save**, puis bascule **Active** en haut à droite.

---

## 6. Utilisation

### 6.1 Un questionnaire complet (usage principal)

Le nœud `Depot d'un questionnaire` expose un formulaire web — c'est la seule
porte d'entrée. Son URL est affichée dans le nœud (onglet **Production URL**).

1. Ouvrez l'URL, saisissez le nom du client, déposez le fichier `.xlsx` reçu.
   Si les questions ne sont pas sur le **premier onglet**, indiquez le nom exact
   de l'onglet dans le champ « Onglet (optionnel) » (un questionnaire réparti
   sur deux onglets se traite en deux dépôts).
2. Le workflow détecte seul la colonne contenant les questions — il n'y a pas de
   gabarit à respecter, chaque client a le sien. Un formulaire qui liste des
   **exigences à cocher Oui / Non** plutôt que des questions (« Chiffrement des
   données personnelles | Oui / Non ») est aussi reconnu : chaque exigence est
   traitée comme « mettez-vous en œuvre cette mesure, et comment ? ».
3. En fin de traitement, un classeur `reponses-questionnaire.xlsx` est produit :

| Colonne | Contenu |
|---|---|
| Ligne | le numéro de ligne de la question **dans le fichier du client** (pour y reporter la réponse) |
| Question | la question du client, telle quelle |
| Réponse | la réponse rédigée, à copier dans le formulaire client |
| Réponse courte | Oui / Non / la valeur seule, pour les cases à cocher (vidée si le contrôle de cohérence a réécrit la réponse : à compléter) |
| Sources (document et page) | la citation à fournir au client, avec le lien local (`http://localhost:8088/documents-preuve/…`) du fichier (`{code_doc}.pdf`/`.docx`/`.xlsx`/`.pptx`) quand il est disponible |
| Confiance | note de 0 à 1 |
| Niveau | `haute` / `moyenne` / `a_verifier` / `sans_reponse` |
| **A relire** | `OUI` sur toute ligne qui n'est pas en confiance haute |
| Alerte | ce que le contrôleur a corrigé ou signalé |

**Relisez les lignes marquées `OUI`.** Une ligne n'est en confiance haute que
si le contrôleur **et** les seuils le permettent, et qu'elle cite au moins une
preuve du registre.

### 6.2 Une question isolée

Le nœud `Question du chat` expose un chat (URL dans le nœud) : on y pose une
question, la réponse revient sourcée, avec sa confiance et ses alertes. Elle suit
la même chaîne que le formulaire (analyse, recherche, rédaction, contrôle), sans
la passe de cohérence ni le classeur Excel, et n'est pas journalisée.

---

## 7. Comment le système répond

L'enchaînement est **déterministe** : aucun agent ne choisit ses outils. Toutes
les sources sont interrogées à chaque question, ce qui garantit qu'une même
question posée deux fois donne la même réponse.

```
Question du client
      │
      ▼
1. Analyse            reformule et produit 2 à 4 sous-requêtes dans
      │               le vocabulaire des documents internes
      ▼
2. Vectorisation      OpenAI, 1 appel pour toutes les sous-requêtes
      │
      ▼
3. Recherche          UN SEUL appel : recherche_globale interroge côté SQL
      │               la banque de réponses, les documents (hybride vectoriel
      │               + plein texte français), les indicateurs et le registre,
      │               puis fusionne les rangs (RRF) entre sous-requêtes
      ▼
4. Reclassement       note chaque candidat de 0 à 10 pour cette question ;
      │               seuls les ≥ 4 sont retenus. Si aucun ne passe, repli
      │               sur les 5 meilleurs du RRF plutôt que de renoncer
      ▼
5. Rédaction          rédige en français, avec citations
      │
      ▼
6. Contrôle           second passage : chaque affirmation est confrontée
      │               aux preuves ; corrige, note, alerte
      ▼
   (boucle sur la question suivante)
      │
      ▼
7. Cohérence          UN SEUL appel pour tout le formulaire : traque les
      │               contradictions entre réponses
      ▼
8. Excel + journal Postgres
```

**Pourquoi une chaîne plutôt qu'un agent.** Un agent qui choisit ses outils
interroge la table d'indicateurs une fois sur deux : la même question ne donne
pas la même réponse d'une semaine à l'autre. Sur un document opposable à un
client, c'est un défaut. Le coût d'interroger systématiquement toutes les
sources est marginal — c'est un seul appel SQL.

**Pourquoi la fusion est en SQL.** `recherche_globale` compose les trois
fonctions de recherche et applique le RRF côté base. Le workflow n'a donc aucun
code de tri à maintenir, et la logique de recherche se teste directement en
SQL (`docker compose exec postgres psql`), sans lancer n8n.

**La passe de cohérence** (étape 7) est ce que le traitement question par
question ne peut structurellement pas faire. Une contradiction entre la ligne 7
et la ligne 23 d'un même formulaire est repérée immédiatement par un acheteur et
discrédite l'ensemble des réponses. Le contrôle ne voit que les réponses, pas
les preuves : il peut donc **nuancer ou retirer** une affirmation, jamais en
ajouter une.

## 8. Les garde-fous contre la sur-déclaration

Le système est conçu pour répondre **positivement et complètement**, sans jamais
affirmer ce que Logiclever ne détient pas. Plusieurs protections se cumulent :

1. **À l'analyse** — le modèle relève les « points de vigilance » : toute
   formulation de la question qui pousserait à sur-déclarer.
2. **À la rédaction** — chaque affirmation factuelle doit être portée par une
   preuve du dossier ; aucune certification, label ou engagement non étayé ne
   peut être revendiqué ; une évaluation externe (EcoVadis, CyberVadis, Great
   Place to Work) n'est jamais présentée comme une certification ; la force
   d'une affirmation est respectée (« objectif » ≠ « résultat », « aligné
   avec » ≠ « certifié selon »).
3. **Au contrôle** — un second passage relit la réponse contre les preuves et
   corrige toute affirmation non étayée, chiffre déformé ou source mal citée,
   y compris la réponse courte (Oui / Non) si le sens de la réponse change.
4. **Au classement** — le niveau de confiance ne peut pas être plus favorable
   que ce que permettent les seuils, et une réponse sans preuve citable reste
   à relire (§ 5.4).

Les faits sensibles ne sont **pas** écrits dans les prompts, où ils
vieilliraient sans que personne ne s'en aperçoive : ils vivent dans la banque de
réponses, sourcés. L'entrée `FAIT/CERTIFICATIONS` indique ainsi explicitement
que Logiclever n'est certifiée ni ISO 9001, ni ISO 14001, ni ISO 27001, et
remonte en tête sur toute question de certification. Les faits socles ne
recopient pas les scores globaux des évaluations externes : le modèle les lit
dans le rapport cité (EcoVadis, CyberVadis), seule source à jour.

Les indicateurs en retrait de leur objectif sont eux aussi traités
explicitement — valeur réelle, dispositif en place, échéance — plutôt qu'omis :
un acheteur qui découvre l'écart après coup perd confiance dans l'ensemble des
réponses.

---

## 9. Le corpus chargé

**39 documents canoniques, 617 pages.** Les dossiers `a` et `b` contiennent
50 fichiers : 39 indexés, 1 utilisé seulement comme source de la banque de
réponses (le questionnaire EcoVadis au format tableur), et 10 doublons ou
versions antérieures écartés :

| Fichier écarté | Retenu à la place | Motif |
|---|---|---|
| `b/01683f7a…` | POL-ETHIQUE | contenu identique (md5) |
| `b/3a5376ab…` | POL-ACHATS | contenu identique (md5) |
| `b/9f66bd44…` | POL-ENV | identique hors métadonnées |
| `b/3014a600…` | CV-EXEC-2025 | contenu identique (md5) |
| `b/4c4427dd…` | SSI-REVUES | contenu identique (md5) |
| `b/4513cf24…` | MATERIALITE | export PDF du même document |
| `b/81b0925d…` | KPI-RSE | export PDF du même tableur |
| `a/26b3f14b….xlsx` | CV-PERF-2025 | contenu identique (md5) |
| `b/50a1f57c…` | POL-SOCIAL | version V1.2, la V1.3 est retenue |
| `b/64db581f…` | POL-SSI | version antérieure, la version E est retenue |

`GES-2021` (bilan carbone 2021) est conservé au statut `historique` : la
recherche ne le remonte que pour une question qui vise une année, et il reste
citable. Un document passé à `obsolete` (ou retiré de `REGISTRY`) n'est plus ni
recherché ni citable.

Les deux rapports **CyberVadis** (`CV-EXEC-2025`, `CV-PERF-2025`) sont rédigés en
anglais par l'évaluateur : ils sont **traduits en français à l'extraction**, via
le glossaire `seed/traductions_cybervadis.json` (387 entrées). Sans cela, la
recherche plein texte française ne pourrait jamais les atteindre — or c'est le
thème où Logiclever a ses meilleurs résultats.

**173 réponses déjà validées**, qui constituent la source la plus fiable :

| Origine | Nombre | Poids de confiance |
|---|---|---|
| Faits socles vérifiés manuellement | 11 | 1,00 |
| Questionnaire EcoVadis renseigné | 30 | 0,92 |
| Contrôles CyberVadis (scores + mesures validées) | 95 | 0,88 |
| Formulaires clients déjà envoyés (TotalEnergies, CGI) | 37 | 0,85 |

**129 indicateurs** sur les exercices 2023, 2024 et 2025, avec norme GRI,
objectif, méthode de calcul et document source.

---

## 10. Faire vivre le système

### Ajouter ou mettre à jour un document

1. Déposez le fichier dans `a/`, `b/` ou n'importe quel autre dossier de
   `Docs pour RAG` (sous-dossiers compris), sauf `compliance-rag/`, `Demo/`,
   `Recette/` et `EXemples remplies de formulaires clients/` : ce sont des
   questionnaires, pas des preuves (liste `DOSSIERS_EXCLUS`).
   Formats lus : PDF, Word (`.docx`), PowerPoint (`.pptx`), Excel (`.xlsx`) et
   images (`.png`, `.jpg`, `.tif`, `.bmp`). Une page scannée ou imprimée en
   image passe par l'OCR (Tesseract, français + anglais).
2. Relancez :

```bash
python scripts/build_registry.py
python scripts/deploy_base.py
```

`build_registry.py` affiche chaque document ajouté automatiquement avec le code,
le titre, la catégorie et la date qu'il en a déduits : nom du fichier (ou, pour
un nom sans signification comme `b/<uuid>.pdf`, titre du document ou première
ligne), propriétés du fichier pour la date. Aucun modèle de langage n'intervient.
Pour corriger ces métadonnées, ajoutez une ligne dans la liste `REGISTRY` (code,
chemin, titre, catégorie, date, statut) : elle prime sur la déduction. Les
codes déjà présents dans `REGISTRY` sont cités par la banque de réponses : ne
les renommez pas.

Les doublons sont écartés seuls : copie exacte (mêmes octets ou même texte),
ou texte identique à plus de 60 % (version antérieure : la plus récente est
gardée). Deux exceptions, signalées par un avertissement :

- un fichier qui ressemble à un document de `REGISTRY` mais plus récent est
  indexé **à côté** de l'ancien. S'il le remplace, pointez l'entrée `REGISTRY`
  vers le nouveau fichier (le code, donc les citations, sont conservés) ;
- une image dont l'OCR ne donne que du bruit (photo, logo) n'est pas indexée.
  Une entrée `REGISTRY` force son indexation.

`deploy_base.py` sans option re-vectorise tout le corpus (quelques centimes
d'embeddings OpenAI) : c'est nécessaire dès qu'un document est ajouté, car
`--no-embed` refuse les passages qui n'ont pas encore de vecteur.

### Mettre à jour les chiffres annuels

Remplacez le fichier `LOGICLEVER RSE - Liste des indicateurs…xlsx`, puis :

```bash
python scripts/build_answer_bank.py
python scripts/verifier.py
python scripts/deploy_base.py --no-embed
```

`--no-embed` n'appelle pas l'API d'embeddings : il **reprend le vecteur déjà en
base de tout texte inchangé** (les indicateurs, eux, n'ont pas de vecteur). Si
un texte a été ajouté ou modifié, le script indique combien sont restés sans
vecteur : relancez alors `deploy_base.py` sans l'option.

> **Pourcentages — à savoir avant de modifier le tableur.** Excel enregistre une
> cellule formatée `0%` sous forme de **fraction** : `0,37` pour 37 %. Le script
> lit le *format de cellule* pour lever l'ambiguïté, car le libellé ne suffit
> pas — « Nombre d'évaluations RSE = 1 » est un comptage, « Participation au
> télétravail = 1 » vaut 100 %.
>
> Le tableur mélange les deux conventions : certains pourcentages sont saisis en
> fraction avec le format `%`, d'autres en points entiers au format `General`
> (`15` pour 15 %). Les deux sont gérés. **Saisissez de préférence en appliquant
> le format pourcentage d'Excel**, et lancez `verifier.py` après toute mise à
> jour : son 4ᵉ contrôle recoupe trois valeurs avec le Rapport RSE et échoue si
> une fraction brute subsiste. Sans cela, le modèle écrirait « 0,37 % » au lieu
> de « 37 % » — une erreur d'un facteur 100 dans une réponse client.

### Capitaliser sur les questionnaires traités

Chaque réponse est journalisée dans `reponses_generees`. Après relecture, une
bonne réponse peut rejoindre la banque — elle deviendra alors la source
prioritaire pour toute question similaire. Marquez d'abord les réponses
validées (en corrigeant le texte si besoin) :

```sql
update reponses_generees
set valide_par = 'Initiales du relecteur',
    reponse_finale = 'Texte validé, si différent de la réponse générée'
where id in (…);
```

puis versez-les dans la banque :

```sql
insert into reponses_validees
  (source_ref, origine, theme, question, reponse, sources, fiabilite)
select 'CAPITALISE/' || g.client || '/' || g.id,
       'formulaire_client', null, g.question,
       coalesce(g.reponse_finale, g.reponse),
       -- la banque attend {document, pages, option} ; le journal stocke {document, page, ...}
       coalesce((select jsonb_agg(jsonb_build_object(
                   'document', s->>'document', 'pages', coalesce(s->>'page', ''),
                   'option', ''))
                 from jsonb_array_elements(g.sources) s), '[]'::jsonb),
       'capitalisee'
from reponses_generees g
where g.valide_par is not null
on conflict do nothing;
```

Relancez ensuite `deploy_base.py` pour vectoriser les nouvelles entrées. Les
réponses `capitalisee` ne viennent pas de `answer_bank.json` : les
rechargements les conservent (et les vectorisent si besoin) au lieu de les
effacer.

### Références documentaires non résolues

`build_answer_bank.py` rattache chaque document cité par EcoVadis / CyberVadis
au `code_doc` du registre, et liste en fin d'exécution les libellés qu'il n'a
pas su rattacher : le workflow les écarte des citations. Si l'un d'eux désigne
en réalité un document du registre, ajoutez-le à `ALIAS_DOCUMENTS` (même
fichier) puis relancez `build_answer_bank.py` et `deploy_base.py --no-embed`.

### Corriger une traduction CyberVadis

Toutes les traductions vivent dans `seed/traductions_cybervadis.json`, une simple
correspondance `chaîne anglaise → chaîne française`. Corrigez la valeur, puis :

```bash
python scripts/build_registry.py      # met à jour les chunks documentaires
python scripts/build_answer_bank.py   # met à jour la banque de réponses
python scripts/deploy_base.py         # recharge et re-vectorise
```

Une correction se propage donc aux deux usages d'un seul geste. Une chaîne
absente du glossaire est laissée telle quelle — `verifier.py` la signale.

### Lancer le garde-fou

```bash
python scripts/verifier.py
```

Quatre contrôles : la détection de la colonne des questions (rejouée sur sept
classeurs réels, en exécutant le JavaScript du workflow hors de n8n, avec
vérification que la colonne « Ligne » désigne bien la ligne Excel),
l'absence d'anglais résiduel, l'absence d'entrée de glossaire jamais utilisée —
le symptôme d'une clé mal orthographiée — et l'expression correcte des
pourcentages, recoupée avec le Rapport RSE. Sortie en code 1 si anomalie.

### Rejouer la chaîne de traitement

```bash
python scripts/simuler_workflow.py
```

Exécute les nœuds Code du workflow hors de n8n, avec des données de la même forme
qu'en production : dossier de preuves, repli, consolidation, cohérence, classeur.
Aucun appel LLM, aucune exécution n8n consommée — c'est le test à lancer après
toute modification de `build_workflow.py`, avant de réimporter dans n8n.

---

## 11. Diagnostic

| Symptôme | Cause probable | Correctif |
|---|---|---|
| `Aucune question detectee dans l'onglet lu` | les questions sont sur un autre onglet, ou en lignes plutôt qu'en colonne | renseignez le champ « Onglet (optionnel) » ; sinon extrayez les questions dans un fichier d'une colonne |
| `Le questionnaire depasse 3000 lignes` | feuille plus longue que la plage lue | scindez le fichier, ou augmentez `LIGNES_LUES` dans `build_workflow.py` |
| Le nœud `Recherche` échoue (erreur Postgres) | conteneur `postgres` arrêté, base vide ou credential Postgres erronée | `docker compose ps`, puis `python scripts/deploy_base.py --test` |
| Beaucoup de réponses en `a_verifier` avec « Reponse sans source documentaire » | vecteurs absents (chargement fait avec `--no-embed` sur une base neuve) ou références de la banque non résolues | relancez `deploy_base.py` sans option ; voir « Références documentaires non résolues » |
| `column "embedding" ... expression is of type text` | dimension d'embedding désalignée | `DIMENSIONS` (script) et `vector(1536)` (schéma) doivent coïncider |
| Recherche plein texte muette sur les accents | configuration `fr_unaccent` absente | `python scripts/deploy_base.py --schema-only` |
| Une réponse cite un document inattendu | le reclassement n'a rien retenu et le repli s'est déclenché | la colonne `Alerte` porte « recherche élargie » ; la ligne est en `a_verifier` par construction (§ 5.4) |
| Le workflow tourne longtemps | ~20 à 30 s par question, en série | comportement normal : 40 questions ≈ 15 min. **Ne pas toucher à `batchSize`** — voir § 12 |

Requêtes de contrôle direct (`cd docker` puis `docker compose exec postgres psql -U logiclever logiclever`) :

```sql
-- volumétrie
select 'documents' t, count(*) from documents
union all select 'chunks', count(*) from documents_rag
union all select 'reponses', count(*) from reponses_validees
union all select 'indicateurs', count(*) from indicateurs;

-- la recherche plein texte française répond-elle ?
select code_doc, page_debut, left(content, 120)
from documents_rag d
join documents doc on doc.code_doc = d.metadata->>'code_doc'
where d.tsv @@ websearch_to_tsquery('public.fr_unaccent', 'emissions gaz effet serre')
limit 5;

-- qualité de la dernière campagne
select niveau, count(*), round(avg(confiance)::numeric, 3)
from reponses_generees
where cree_le > now() - interval '1 day'
group by niveau;
```

---

## 12. Limites connues

- Le **bilan carbone le plus récent porte sur 2023** (publié en janvier 2025).
  L'exercice suivant est prévu en 2026 : la réponse mentionne la fréquence
  biennale plutôt que de laisser croire à une donnée plus fraîche.
- Le classeur restitué contient les réponses, **pas le formulaire client
  rempli** : la mise en forme varie trop d'un client à l'autre. Le report se
  fait par copier-coller depuis la colonne « Réponse ».
- Les questions sont traitées **en série** (une à la fois). Un questionnaire de
  40 questions prend environ 15 minutes. Augmenter `batchSize` du nœud
  `Chaque question` casserait les références `$('…').first()` des nœuds Code, qui
  supposent un item par passage : ce n'est pas un réglage, c'est une refonte.
- Les **liens vers les preuves** (`http://localhost:8088/…`) ne fonctionnent que
  sur ce poste : pour un client, joignez le fichier cité.
- Un seul onglet est lu par dépôt : un questionnaire réparti sur plusieurs
  onglets se traite en plusieurs dépôts (champ « Onglet »).
