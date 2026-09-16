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
├── sql/
│   └── 01_schema.sql             tables, index HNSW/GIN, fonctions de recherche
│
├── scripts/
│   ├── build_registry.py         extrait les 38 documents (dédoublonnés)
│   ├── build_answer_bank.py      extrait 173 réponses validées + 129 indicateurs
│   ├── deploy_supabase.py        ⇦ LE script de déploiement
│   ├── build_workflow.py         régénère le JSON n8n
│   ├── verifier.py               garde-fou (détection + francisation)
│   └── simuler_workflow.py       rejoue la chaîne de traitement hors de n8n
│
├── seed/                         données extraites (JSON, régénérables)
│   ├── documents.json            614 pages de texte, 38 documents
│   ├── answer_bank.json          173 réponses déjà validées
│   ├── kpi.json                  129 indicateurs × 3 exercices
│   └── traductions_cybervadis.json  glossaire FR du référentiel CyberVadis
│
└── workflows/
    └── logiclever_reponses_questionnaires.json   ⇦ à importer dans n8n
```

---

## 2. Prérequis

| Élément | Détail |
|---|---|
| Projet Supabase | n'importe quelle région ; le plan gratuit suffit pour ce volume |
| Clé API Anthropic | Opus 5, Sonnet 5 et Haiku 4.5 (étagés selon l'étape) |
| Clé API OpenAI | uniquement pour les *embeddings* (Anthropic n'expose pas d'API d'embeddings) |
| n8n | version 1.40 ou supérieure, auto-hébergé ou Cloud |
| Python | 3.10 ou supérieur, sur le poste qui lance le chargement |

**Coût.** Le chargement initial coûte moins de 0,10 € d'embeddings. Un
questionnaire de 40 questions revient à **≈ 2,8 €**.

Les modèles sont étagés : Opus n'est utilisé que pour la rédaction, seul texte
lu par le client. Les étapes mécaniques tournent sur des modèles moins chers.

| Étape | Modèle | Pourquoi |
|---|---|---|
| Analyser la question | `claude-haiku-4-5` | reformulation + sous-requêtes, tâche cadrée |
| Reclasser les preuves | `claude-sonnet-5` | notation 0-10, tâche de tri |
| **Rédiger la réponse** | **`claude-opus-5`** | le texte lu par l'acheteur |
| Contrôler la véracité | `claude-sonnet-5` | vérification point par point |
| Contrôle de cohérence | `claude-sonnet-5` | 1 seul appel par formulaire |

Pour repasser sur Opus partout, remplacer les constantes `HAIKU` / `SONNET` par
`OPUS` en tête de `scripts/build_workflow.py` et régénérer (≈ 6,3 € / formulaire).

**Une exécution n8n par questionnaire**, quel que soit le nombre de questions :
le quota du plan gratuit n'est pas la contrainte.

---

## 3. Étape 1 — Préparer le poste

```bash
cd "compliance-rag"
pip install -r requirements.txt
```

Renseignez les variables d'environnement (copiez `.env.example` puis complétez) :

```powershell
# Windows / PowerShell
$env:SUPABASE_DB_URL = "postgresql://postgres.abcdefgh:MOTDEPASSE@aws-0-eu-west-3.pooler.supabase.com:5432/postgres"
$env:OPENAI_API_KEY  = "sk-..."
```

```bash
# macOS / Linux
export SUPABASE_DB_URL="postgresql://postgres.abcdefgh:MOTDEPASSE@aws-0-eu-west-3.pooler.supabase.com:5432/postgres"
export OPENAI_API_KEY="sk-..."
```

> Où trouver `SUPABASE_DB_URL` : Supabase → **Project Settings** → **Database** →
> **Connection string** → onglet **URI**. Remplacez `[YOUR-PASSWORD]` par le mot
> de passe de la base. Prenez la chaîne **Session pooler** (port 5432).

---

## 4. Étape 2 — Charger la base et les vecteurs

Une seule commande fait tout : schéma, découpage, embeddings, chargement, tests.

```bash
python scripts/deploy_supabase.py
```

Déroulé attendu :

```
[1/5] Application du schema SQL
      tables, index HNSW/GIN et fonctions de recherche en place
[2/5] Documents de preuve et chunks
      38 documents canoniques enregistres
      10 doublons/versions anterieures ecartes
      837 chunks produits
      embeddings chunks : 837/837
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
python scripts/deploy_supabase.py --schema-only   # créer seulement les tables
python scripts/deploy_supabase.py --no-embed      # recharger sans recalculer les vecteurs
python scripts/deploy_supabase.py --test          # relancer uniquement les tests
```

Le script est **idempotent** : vous pouvez le relancer autant de fois que
nécessaire, il remplace le contenu sans dupliquer.

---

## 5. Étape 3 — Configurer n8n

### 5.1 Créer les trois identifiants

Dans n8n → **Credentials** → **New** :

| Type d'identifiant | Champs à renseigner |
|---|---|
| **Supabase API** | Host : `https://VOTRE-PROJET.supabase.co` — Service Role Secret : la clé `service_role` (Supabase → Project Settings → API) |
| **Anthropic** | votre clé API Anthropic |
| **OpenAI** | votre clé API OpenAI (embeddings uniquement) |

> La clé `service_role` contourne les politiques RLS : elle ne doit jamais
> sortir de n8n. Les tables ne contiennent que des données destinées à être
> partagées avec les clients, mais la clé, elle, donne un accès complet au projet.

### 5.2 Importer le workflow

n8n → **Workflows** → **Import from File** →
`workflows/logiclever_reponses_questionnaires.json`

Le workflow importé compte **28 nœuds fonctionnels** et 8 notes explicatives.
Les notes décrivent chaque étape directement sur le canevas.

### 5.3 Rattacher les identifiants

n8n ne peut pas deviner quel identifiant utiliser. Ouvrez chaque nœud ci-dessous
et sélectionnez-le dans la liste déroulante :

- **Supabase API** — nœuds `Recherche` et `Journaliser la campagne`
- **OpenAI** — nœud `Vectoriser les requetes`
- **Anthropic** — les cinq nœuds `Modele : …`

### 5.4 Renseigner l'URL du projet

Ouvrez le nœud **`Parametres`** et remplacez la valeur de `supabase_url` :

```
https://VOTRE-PROJET.supabase.co
```

C'est le **seul** paramètre à modifier : tous les autres nœuds lisent cette
valeur. Les trois seuils de confiance y sont également réglables.

### 5.5 Activer

**Save**, puis bascule **Active** en haut à droite.

---

## 6. Utilisation

### 6.1 Un questionnaire complet (usage principal)

Le nœud `Depot d'un questionnaire` expose un formulaire web — c'est la seule
porte d'entrée. Son URL est affichée dans le nœud (onglet **Production URL**).

1. Ouvrez l'URL, saisissez le nom du client, déposez le fichier `.xlsx` reçu.
2. Le workflow détecte seul la colonne contenant les questions — il n'y a pas de
   gabarit à respecter, chaque client a le sien.
3. En fin de traitement, un classeur `reponses-questionnaire.xlsx` est produit :

| Colonne | Contenu |
|---|---|
| Question | la question du client, telle quelle |
| Réponse | la réponse rédigée, à copier dans le formulaire client |
| Réponse courte | Oui / Non / la valeur seule, pour les cases à cocher |
| Sources (document et page) | la citation à fournir au client |
| Confiance | note de 0 à 1 |
| Niveau | `haute` / `moyenne` / `a_verifier` / `sans_reponse` |
| **A relire** | `OUI` sur toute ligne qui n'est pas en confiance haute |
| Alerte | ce que le contrôleur a corrigé ou signalé |

**Relisez les lignes marquées `OUI`.** Les lignes en confiance haute sont
adossées à une preuve directe et datée.

### 6.2 Une question isolée

Déposez un fichier d'une seule ligne dans le même formulaire. Il n'y a pas
d'endpoint dédié : un webhook aurait renvoyé le classeur Excel plutôt que la
réponse, et aurait fait tourner le contrôle de cohérence sur une seule réponse.
Si vous voulez malgré tout une API JSON, ajoutez un déclencheur **Webhook** et un
nœud **Respond to Webhook** branché après `Consolider la reponse`.

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
5. Rédaction          Opus rédige en français, avec citations
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
8. Excel + journal Supabase
```

**Pourquoi une chaîne plutôt qu'un agent.** Un agent qui choisit ses outils
interroge la table d'indicateurs une fois sur deux : la même question ne donne
pas la même réponse d'une semaine à l'autre. Sur un document opposable à un
client, c'est un défaut. Le coût d'interroger systématiquement toutes les
sources est marginal — c'est un seul appel SQL.

**Pourquoi la fusion est en SQL.** `recherche_globale` compose les trois
fonctions de recherche et applique le RRF côté base. Le workflow n'a donc aucun
code de tri à maintenir, et la logique de recherche se teste directement dans
l'éditeur SQL Supabase, sans lancer n8n.

**La passe de cohérence** (étape 7) est ce que le traitement question par
question ne peut structurellement pas faire. Une contradiction entre la ligne 7
et la ligne 23 d'un même formulaire est repérée immédiatement par un acheteur et
discrédite l'ensemble des réponses. Le contrôle ne voit que les réponses, pas
les preuves : il peut donc **nuancer ou retirer** une affirmation, jamais en
ajouter une.

## 8. Les garde-fous contre la sur-déclaration

Le système est conçu pour répondre **positivement et complètement**, sans jamais
affirmer ce que Logiclever ne détient pas. Trois protections se cumulent :

1. **À l'analyse** — si la question touche une certification ISO, la SBTi ou le
   CSE, le point est signalé avant même la recherche.
2. **À la rédaction** — l'instruction interdit de revendiquer ISO 9001, ISO 14001,
   ISO 27001, un objectif validé SBTi ou l'existence d'un CSE, et impose la
   contrepartie réelle. Exemple de sortie attendue sur l'ISO 27001 :
   > « Logiclever n'est pas certifiée ISO 27001. Sa Politique de Sécurité des
   > Systèmes d'Information est toutefois construite sur le référentiel ISO 27001,
   > un objectif de certification y est inscrit, et le dispositif a été évalué par
   > un tiers indépendant : CyberVadis attribue 860/1000, niveau Mature. »
3. **Au contrôle** — un second passage relit la réponse contre les preuves et
   traite toute formulation ambiguë comme une erreur à corriger.

L'entrée `FAIT/CERTIFICATIONS` de la banque de réponses porte explicitement la
mention « ne jamais répondre oui à une question de certification ISO ». Elle
remonte en tête sur toute question de certification.

Les indicateurs en retrait de leur objectif sont eux aussi traités
explicitement — valeur réelle, dispositif en place, échéance — plutôt qu'omis :
un acheteur qui découvre l'écart après coup perd confiance dans l'ensemble des
réponses.

---

## 9. Le corpus chargé

**38 documents canoniques, 614 pages.** Les dossiers `a` et `b` contenaient
53 fichiers ; 10 doublons et versions antérieures ont été écartés :

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

1. Déposez le fichier dans `a/` ou `b/`.
2. Ajoutez une ligne dans la liste `REGISTRY` de `scripts/build_registry.py`
   (code, chemin, titre, catégorie, date, statut). Si le document remplace une
   version antérieure, passez l'ancienne à `"obsolete"` ou déclarez-la dans
   `DOUBLONS` — la recherche ignore par défaut tout ce qui n'est pas `actif`.
3. Relancez :

```bash
python scripts/build_registry.py
python scripts/deploy_supabase.py
```

### Mettre à jour les chiffres annuels

Remplacez le fichier `LOGICLEVER RSE - Liste des indicateurs…xlsx`, puis :

```bash
python scripts/build_answer_bank.py
python scripts/verifier.py
python scripts/deploy_supabase.py --no-embed
```

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
prioritaire pour toute question similaire :

```sql
insert into reponses_validees
  (source_ref, origine, theme, question, reponse, sources, fiabilite)
select 'CLIENT/' || g.client || '/' || g.ligne,
       'formulaire_client', null, g.question,
       coalesce(g.reponse_finale, g.reponse), g.sources, 'reponse_envoyee_client'
from reponses_generees g
where g.valide_par is not null
on conflict do nothing;
```

Relancez ensuite `deploy_supabase.py` pour vectoriser les nouvelles entrées.

### Corriger une traduction CyberVadis

Toutes les traductions vivent dans `seed/traductions_cybervadis.json`, une simple
correspondance `chaîne anglaise → chaîne française`. Corrigez la valeur, puis :

```bash
python scripts/build_registry.py      # met à jour les chunks documentaires
python scripts/build_answer_bank.py   # met à jour la banque de réponses
python scripts/deploy_supabase.py     # recharge et re-vectorise
```

Une correction se propage donc aux deux usages d'un seul geste. Une chaîne
absente du glossaire est laissée telle quelle — `verifier.py` la signale.

### Lancer le garde-fou

```bash
python scripts/verifier.py
```

Quatre contrôles : la détection de la colonne des questions (rejouée sur les
trois classeurs réels, en exécutant le JavaScript du workflow hors de n8n),
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
| `Impossible d'identifier la colonne des questions` | fichier atypique (questions en lignes, plusieurs onglets) | le nœud `Lire le fichier` ne lit que le **premier onglet** : réordonnez le classeur, ou extrayez les questions dans un fichier d'une colonne |
| Toutes les réponses en `a_verifier` | base vide ou URL Supabase erronée | `python scripts/deploy_supabase.py --test` |
| `column "embedding" ... expression is of type text` | dimension d'embedding désalignée | `DIMENSIONS` (script) et `vector(1536)` (schéma) doivent coïncider |
| Recherche plein texte muette sur les accents | configuration `fr_unaccent` absente | `python scripts/deploy_supabase.py --schema-only` |
| Une réponse cite un document inattendu | le reclassement n'a rien retenu et le repli s'est déclenché | la colonne `Alerte` porte « recherche élargie » ; la ligne est en `a_verifier` par construction |
| Le workflow tourne longtemps | ~20 à 30 s par question, en série | comportement normal : 40 questions ≈ 15 min. **Ne pas toucher à `batchSize`** — voir § 12 |

Requêtes de contrôle direct dans l'éditeur SQL Supabase :

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
- Le **cache de prompt Anthropic n'est pas exploitable** : le nœud Anthropic de
  n8n n'expose pas de paramètre `cache_control`. Le levier de coût disponible est
  l'étagement des modèles décrit au § 2.
