# -*- coding: utf-8 -*-
"""
=========================================================================
 LOGICLEVER - Deploiement de la base de connaissance conformite
=========================================================================
Fait tout en une commande :
  1. applique le schema SQL sur le Postgres local (tables, index, fonctions RRF)
  2. decoupe les 39 documents de preuve en chunks ancres a la page
  2 bis. copie les fichiers de preuve (pdf, docx, xlsx, pptx...) dans le
         dossier servi par le conteneur "fichiers" (docker/stockage) et
         enregistre leur URL dans documents.url_partage
  3. calcule les embeddings (OpenAI text-embedding-3-large, 1536 dims)
  4. charge documents / chunks / reponses validees / indicateurs
  5. lance des tests de bout en bout sur de vraies questions clients

Prerequis :
    docker compose up -d          (dans compliance-rag/docker)
    pip install "psycopg[binary]" requests
    set OPENAI_API_KEY=sk-...
    Connexion Postgres : lue dans docker/.env (port 5433 cote hote).
    (optionnel) set DATABASE_URL=postgresql://USER:MOTDEPASSE@127.0.0.1:5433/BASE
                pour viser une autre base
    (optionnel) set STOCKAGE_DIR=...   defaut : compliance-rag/docker/stockage
    (optionnel) set FICHIERS_URL=...   defaut : http://localhost:8088

Usage :
    python deploy_base.py                  # tout
    python deploy_base.py --schema-only    # seulement le schema
    python deploy_base.py --no-embed       # recharge sans recalculer les vecteurs
    python deploy_base.py --test           # seulement les tests
=========================================================================
"""
import os
import sys
import json
import re
import time
import argparse
import shutil
import unicodedata

sys.stdout.reconfigure(encoding="utf-8")

try:
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg.types.json import Jsonb
except ImportError:
    sys.exit("Installez la dependance :  pip install \"psycopg[binary]\" requests")
try:
    import requests
except ImportError:
    sys.exit("Installez la dependance :  pip install requests")

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(ICI)
BASE = os.path.dirname(RACINE)
SEED = os.path.join(RACINE, "seed")
SQL = os.path.join(RACINE, "sql")

# Sans DATABASE_URL, la connexion est construite depuis docker/.env (les
# identifiants du conteneur postgres), sur le port publie cote hote par
# docker/docker-compose.yml. 127.0.0.1 et non localhost : sous Windows, libpq
# essaie d'abord IPv6 et bloque ~2 minutes avant de se rabattre sur IPv4.
DB_URL = os.environ.get("DATABASE_URL", "")
ENV_DOCKER = os.path.join(RACINE, "docker", ".env")
PORT_HOTE_POSTGRES = 5433
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
MODELE_EMBEDDING = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-large")
DIMENSIONS = 1536          # doit rester aligne avec vector(1536) dans 01_schema.sql
TAILLE_CHUNK = 1400        # caracteres
RECOUVREMENT = 220
LOT_EMBEDDING = 64

# Stockage des PDF de preuve : dossier local servi par le conteneur nginx
# "fichiers" (docker/docker-compose.yml). Chaque document y est copie sous
# "{code_doc}.pdf", et son URL alimente documents.url_partage - c'est ce lien
# qui est ensuite restitue (classeur Excel et chat) pour ouvrir le PDF cite.
# Le sous-dossier generes/ recoit les classeurs ecrits par n8n.
STOCKAGE_DIR = os.environ.get("STOCKAGE_DIR", os.path.join(RACINE, "docker", "stockage"))
FICHIERS_URL = os.environ.get("FICHIERS_URL", "http://localhost:8088").rstrip("/")
DOSSIER_PREUVES = "documents-preuve"


def log(msg):
    print(msg, flush=True)


# =====================================================================
#  Decoupage francais, ancre a la page
# =====================================================================
def normaliser(texte):
    """Nettoie le texte extrait des PDF : ligatures, espaces insecables,
    puces, cesures de fin de ligne, caracteres de remplacement (U+FFFD, laisses
    par l'extraction a la place des pictogrammes, ex. GES-2021 p. 32-33)."""
    t = texte.replace("­", "").replace("​", "").replace("�", "")
    t = t.replace("ﬁ", "fi").replace("ﬂ", "fl")
    t = t.replace(" ", " ").replace(" ", " ")
    t = re.sub(r"([a-zA-Zàâäéèêëîïôöùûüç])-\n([a-zàâäéèêëîïôöùûüç])", r"\1\2", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def morceler(bloc, taille):
    """Coupe un bloc trop long sur des frontieres de mots. Sert de filet de
    securite pour les tableaux exportes en une seule ligne (feuilles Excel,
    registres) que la segmentation en phrases ne sait pas decouper."""
    if len(bloc) <= taille:
        return [bloc]
    out, courant = [], ""
    for mot in bloc.split(" "):
        if len(courant) + len(mot) + 1 > taille and courant:
            out.append(courant)
            courant = ""
        courant += mot + " "
    if courant.strip():
        out.append(courant.strip())
    return out


def phrases(texte):
    """Segmentation en phrases tolerante au francais (abreviations courantes,
    numerotations '3.1.', unites '2,5 %').

    On coupe d'abord sur les sauts de ligne : les documents de preuve
    contiennent beaucoup de tableaux et de listes ou la ponctuation finale
    est absente."""
    protege = texte
    for abbr in ["M.", "Mme", "Dr.", "cf.", "ex.", "art.", "p.", "etc.",
                 "n°", "N°", "Ets.", "Sté."]:
        protege = protege.replace(abbr, abbr.replace(".", "\x00"))
    protege = re.sub(r"(\d)\.(\d)", "\\1\x00\\2", protege)

    sorties = []
    for ligne in protege.split("\n"):
        ligne = ligne.strip()
        if not ligne:
            continue
        for m in re.split(r"(?<=[.!?:])\s+(?=[A-ZÀÂÉÈÊÎÔÙÜÇ0-9•\-])", ligne):
            m = m.replace("\x00", ".").strip()
            if m:
                sorties.extend(morceler(m, TAILLE_CHUNK))
    return sorties


# Un bloc de cloture (date de mise a jour, signataire...) est un fait court et
# dense (nom, role) qui n'a aucun rapport thematique avec ce qui le precede
# (souvent un tableau d'indicateurs ou une longue section). Concatene au
# caractere pres avec ce qui precede, il se retrouve noye dans un chunk dont
# l'embedding et la densite lexicale sont dominees par un contenu sans rapport,
# et perd tout signal de recherche (vectoriel comme plein texte) meme quand la
# question cite le mot-cle exact (ex. "DPO"). On force donc une coupure nette
# (sans chevauchement, pour ne pas re-diluer le futur chunk) des qu'un tel
# marqueur commence une phrase, afin qu'il devienne son propre chunk compact.
MARQUEURS_CLOTURE_PREFIXE = re.compile(
    r"^(date de derni[eè]re mise [aà] jour|valid[ée][e]? le|fait [aà] |"
    r"sign[ée][e]? (par|le) |approuv[ée][e]? (par|le) )",
    re.I,
)
ARTICLE_INITIAL = re.compile(r"^(le|la|les|l['’])\s+", re.I)


def est_marqueur_cloture(phrase):
    """« Signé le / Validé le / Approuvé par » apparaissent aussi dans des
    phrases ordinaires ("...ayant signé le code de conduite") : le prefixe
    seul ne suffit pas a distinguer un vrai bloc de signature. On exige donc,
    selon le marqueur, ce qui accompagne un vrai bloc de cloture et jamais une
    phrase de corps de texte : une date pour "validé le", et pour
    "signé/approuvé par|le" un nom propre ou un role (mot suivant capitalise,
    apres un article eventuel) plutot qu'un nom commun."""
    m = MARQUEURS_CLOTURE_PREFIXE.match(phrase)
    if not m:
        return False
    tete = m.group(1).lower()
    reste = phrase[m.end():].lstrip()
    if tete.startswith("date de"):
        return True
    if tete.startswith("valid"):
        return reste[:1].isdigit()
    reste = ARTICLE_INITIAL.sub("", reste)
    return reste[:1].isupper()


def decouper_document(doc):
    """Un chunk ne traverse jamais une frontiere de page sans le dire :
    page_debut / page_fin sont conserves pour produire la citation exacte."""
    chunks = []
    entete = "[%s | %s | %s]" % (doc["titre"], doc["categorie"], doc["code_doc"])
    tampon, page_debut, page_courante = "", None, None
    # Vrai des qu'un bloc de cloture ouvre le tampon courant : ce tampon doit
    # rester exempte du seuil minimal quel que soit ce qui le videra ensuite
    # (autre rupture, depassement de taille, ou fin de document).
    tampon_est_cloture = False

    def vider(seuil_min=80):
        nonlocal tampon, page_debut, tampon_est_cloture
        contenu = tampon.strip()
        if len(contenu) >= (1 if tampon_est_cloture else seuil_min):
            chunks.append({
                "content": "%s (page %s)\n%s" % (
                    entete,
                    str(page_debut) if page_debut == page_courante
                    else "%s-%s" % (page_debut, page_courante),
                    contenu),
                "page_debut": page_debut,
                "page_fin": page_courante,
            })
        tampon = ""
        tampon_est_cloture = False

    for num_page, brut in enumerate(doc["pages"], start=1):
        texte = normaliser(brut)
        if not texte:
            continue
        if page_debut is None:
            page_debut = num_page
        page_courante = num_page
        for ph in phrases(texte):
            est_marqueur = est_marqueur_cloture(ph.strip())
            depasse_taille = len(tampon) + len(ph) + 1 > TAILLE_CHUNK and tampon
            rupture_cloture = tampon and est_marqueur
            if depasse_taille or rupture_cloture:
                if rupture_cloture:
                    vider()
                else:
                    queue = tampon[-RECOUVREMENT:]
                    vider()
                    tampon = queue + " "
                page_debut = num_page
            if est_marqueur:
                # Un bloc de cloture est court par nature (nom, date, role) :
                # le garder tel quel plutot que de l'ecarter comme un fragment
                # negligeable, sinon on perd le fait au lieu de simplement le
                # rendre moins dense. Marque avant l'ajout, meme si le tampon
                # etait deja vide (rien a vider), pour couvrir ce cas aussi.
                tampon_est_cloture = True
            tampon += ph + " "
    vider()
    return chunks


# =====================================================================
#  Embeddings
# =====================================================================
ATTENTE_MAX = 60          # plafond du backoff exponentiel (secondes)
TENTATIVES_MAX = 30       # abandon seulement apres ~30 echecs consecutifs


def embed(textes, tentative=0):
    """Appelle l'API OpenAI Embeddings par lots, avec reprise sur erreur.

    Insiste sur les 429/5xx : backoff exponentiel plafonne a ATTENTE_MAX,
    et respecte l'entete Retry-After renvoye par OpenAI quand il est present.
    N'abandonne qu'apres TENTATIVES_MAX echecs consecutifs, pour ne pas
    tuer un import de plusieurs milliers de chunks sur un simple pic de
    rate limit."""
    if not OPENAI_KEY:
        sys.exit("OPENAI_API_KEY absente de l'environnement.")
    try:
        r = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": "Bearer " + OPENAI_KEY,
                     "Content-Type": "application/json"},
            json={"model": MODELE_EMBEDDING, "input": textes, "dimensions": DIMENSIONS},
            timeout=180,
        )
    except requests.exceptions.RequestException as e:
        if tentative >= TENTATIVES_MAX:
            raise
        attente = min(2 ** tentative, ATTENTE_MAX)
        log("      erreur reseau (%s), nouvelle tentative dans %ss" % (e, attente))
        time.sleep(attente)
        return embed(textes, tentative + 1)

    if r.status_code == 429 or r.status_code >= 500:
        if tentative >= TENTATIVES_MAX:
            r.raise_for_status()
        retry_after = r.headers.get("Retry-After")
        if retry_after:
            try:
                attente = max(float(retry_after), 1)
            except ValueError:
                attente = min(2 ** tentative, ATTENTE_MAX)
        else:
            attente = min(2 ** tentative, ATTENTE_MAX)
        log("      API surchargee (%s), tentative %d/%d, nouvelle tentative dans %ss"
            % (r.status_code, tentative + 1, TENTATIVES_MAX, attente))
        time.sleep(attente)
        return embed(textes, tentative + 1)
    if r.status_code != 200:
        raise RuntimeError("OpenAI %s : %s" % (r.status_code, r.text[:400]))
    donnees = sorted(r.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in donnees]


def embed_tout(textes, libelle):
    vecteurs = []
    total = len(textes)
    for i in range(0, total, LOT_EMBEDDING):
        lot = [t[:8000] for t in textes[i:i + LOT_EMBEDDING]]
        vecteurs.extend(embed(lot))
        log("   %s : %d/%d" % (libelle, min(i + LOT_EMBEDDING, total), total))
    return vecteurs


def vecteur_sql(v):
    """pgvector accepte la forme textuelle '[0.1,0.2,...]'. Un vecteur repris
    tel quel de la base (--no-embed) est deja sous cette forme."""
    if isinstance(v, str):
        return v
    return "[" + ",".join("%.7g" % x for x in v) + "]" if v else None


def vecteurs_repris(textes, existants, libelle):
    """Mode --no-embed : reprend, pour chaque texte, le vecteur deja en base
    pour ce texte exact (aucun appel API). Un texte nouveau ou modifie reste
    sans vecteur, et on le dit : la recherche vectorielle ne le verra pas tant
    que le chargement n'aura pas ete relance sans --no-embed."""
    vecteurs = [existants.get(t) for t in textes]
    manquants = sum(1 for v in vecteurs if v is None)
    log("      %s : %d vecteur(s) repris sans appel API" % (libelle, len(textes) - manquants))
    if manquants:
        log("      ATTENTION %d texte(s) nouveau(x) ou modifie(s) sans vecteur : relancez "
            "deploy_base.py sans --no-embed pour les vectoriser." % manquants)
    return vecteurs


# =====================================================================
#  Chargement
# =====================================================================
def charger_json(nom):
    with open(os.path.join(SEED, nom), encoding="utf8") as f:
        return json.load(f)


def resoudre_fichier(rel):
    """Meme tolerance NFC/NFD que build_registry.py : certains fichiers
    OneDrive stockent les accents en forme decomposee."""
    rel = rel.replace("\\", "/")
    direct = os.path.join(BASE, rel)
    if os.path.exists(direct):
        return direct
    dossier, nom = os.path.split(rel)
    d = os.path.join(BASE, dossier)
    if not os.path.isdir(d):
        return direct
    cible = unicodedata.normalize("NFC", nom)
    for f in os.listdir(d):
        if unicodedata.normalize("NFC", f) == cible:
            return os.path.join(d, f)
    return direct


TYPES_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    # images indexees par OCR (build_registry.py)
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}


def copier_fichiers_preuve(cur):
    """Copie chaque fichier de preuve (pdf, docx, xlsx, pptx...) dans le
    dossier servi par le conteneur "fichiers" et enregistre son URL dans
    documents.url_partage. Idempotent (ecrase la copie precedente)."""
    log("\n[2 bis/5] Fichiers de preuve -> %s" % STOCKAGE_DIR)
    dossier = os.path.join(STOCKAGE_DIR, DOSSIER_PREUVES)
    os.makedirs(dossier, exist_ok=True)
    # Cible du noeud n8n "Televerser le classeur" (/files/generes).
    os.makedirs(os.path.join(STOCKAGE_DIR, "generes"), exist_ok=True)
    docs = charger_json("documents.json")["documents"]

    n = 0
    for d in docs:
        _, ext = os.path.splitext(d["fichier_source"].lower())
        if ext not in TYPES_MIME:
            log("      TYPE NON SUPPORTE (%s) : %s" % (ext or "?", d["fichier_source"]))
            continue
        chemin = resoudre_fichier(d["fichier_source"])
        if not os.path.exists(chemin):
            log("      MANQUANT : %s" % d["fichier_source"])
            continue
        nom_objet = "%s%s" % (d["code_doc"], ext)
        shutil.copy2(chemin, os.path.join(dossier, nom_objet))
        url = "%s/%s/%s" % (FICHIERS_URL, DOSSIER_PREUVES, nom_objet)
        cur.execute("update public.documents set url_partage = %s where code_doc = %s",
                    (url, d["code_doc"]))
        n += 1
    log("      %d/%d fichiers copies (servis sur %s)" % (n, len(docs), FICHIERS_URL))


def appliquer_schema(cur):
    log("\n[1/5] Application du schema SQL")
    with open(os.path.join(SQL, "01_schema.sql"), encoding="utf8") as f:
        schema = f.read()
    try:
        cur.execute(schema)
    except psycopg.Error as e:
        # Postgres renvoie une position en caracteres dans un fichier de 550
        # lignes : on la traduit en numero de ligne avec son contexte, sinon
        # le message est inexploitable sans relire tout le schema.
        pos = getattr(e.diag, "statement_position", None)
        log("\n      ECHEC DE L'APPLICATION DU SCHEMA")
        log("      %s" % str(e).strip().splitlines()[0])
        if pos:
            avant = schema[: int(pos) - 1]
            ligne = avant.count("\n") + 1
            lignes = schema.splitlines()
            log("      ligne %d :" % ligne)
            for i in range(max(0, ligne - 3), min(len(lignes), ligne + 2)):
                log("      %s %4d | %s"
                    % (">>" if i == ligne - 1 else "  ", i + 1, lignes[i]))
        raise SystemExit("\n      Corrigez sql/01_schema.sql puis relancez.")
    log("      tables, index HNSW/GIN et fonctions de recherche en place")


def charger_documents(cur, avec_embeddings=True):
    log("\n[2/5] Documents de preuve et chunks")
    reg = charger_json("documents.json")
    docs = reg["documents"]

    existants = {}
    if not avec_embeddings:
        cur.execute("select content, embedding::text from public.documents_rag "
                    "where embedding is not null")
        existants = dict(cur.fetchall())
    cur.execute("truncate table public.documents_rag")
    for d in docs:
        cur.execute("""
            insert into public.documents
              (code_doc, titre, categorie, fichier_source, date_reference,
               statut, langue_source, nb_pages, md5)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (code_doc) do update set
              titre = excluded.titre, categorie = excluded.categorie,
              fichier_source = excluded.fichier_source,
              date_reference = excluded.date_reference, statut = excluded.statut,
              langue_source = excluded.langue_source,
              nb_pages = excluded.nb_pages, md5 = excluded.md5
        """, (d["code_doc"], d["titre"], d["categorie"], d["fichier_source"],
              d["date_reference"], d["statut"], d.get("langue_source", "fr"),
              d["nb_pages"], d["md5"]))
    log("      %d documents canoniques enregistres" % len(docs))
    log("      %d doublons/versions anterieures ecartes" % len(reg["doublons_ecartes"]))
    # Un document retire de REGISTRY resterait sinon en base, "actif" et
    # citable (lien compris), sans plus aucun extrait indexe. On le passe a
    # "obsolete" plutot que de le supprimer : les indicateurs peuvent encore
    # le referencer, et le registre de citations l'ignore desormais.
    cur.execute("update public.documents set statut = 'obsolete' "
                "where statut <> 'obsolete' and not (code_doc = any(%s)) returning code_doc",
                ([d["code_doc"] for d in docs],))
    retires = [r[0] for r in cur.fetchall()]
    if retires:
        log("      %d document(s) absent(s) du registre passe(s) a obsolete : %s"
            % (len(retires), ", ".join(retires)))

    tous = []
    for d in docs:
        for i, ch in enumerate(decouper_document(d)):
            tous.append({
                "content": ch["content"],
                "metadata": {
                    "code_doc": d["code_doc"], "titre": d["titre"],
                    "categorie": d["categorie"], "statut": d["statut"],
                    "date_reference": d["date_reference"],
                    "fichier_source": d["fichier_source"],
                    "page_debut": ch["page_debut"], "page_fin": ch["page_fin"],
                    "ordre": i, "type": "document",
                    "langue_source": d.get("langue_source", "fr"),
                },
            })
    log("      %d chunks produits" % len(tous))

    textes = [c["content"] for c in tous]
    vecteurs = (embed_tout(textes, "      embeddings chunks") if avec_embeddings
                else vecteurs_repris(textes, existants, "chunks"))

    with cur.copy("copy public.documents_rag (content, metadata, embedding) "
                  "from stdin") as copie:
        for c, v in zip(tous, vecteurs):
            copie.write_row((c["content"], Jsonb(c["metadata"]), vecteur_sql(v)))
    log("      chunks charges dans documents_rag")


# Reponses reinjectees depuis reponses_generees apres relecture humaine (voir
# GUIDE-DEPLOIEMENT.md, « Capitaliser sur les questionnaires traites ») : elles
# ne viennent pas de answer_bank.json, un rechargement ne doit donc pas les
# effacer.
FIABILITE_CAPITALISEE = "capitalisee"


def texte_reponse(question, intitule, reponse):
    """Texte vectorise pour une reponse validee (meme formule partout, pour
    que --no-embed retrouve le vecteur d'un texte inchange)."""
    return "%s\n%s\n%s" % (question, intitule or "", (reponse or "")[:3000])


def charger_reponses(cur, avec_embeddings=True):
    log("\n[3/5] Banque de reponses validees")
    bank = charger_json("answer_bank.json")

    existants = {}
    if not avec_embeddings:
        cur.execute("select question, intitule, reponse, embedding::text "
                    "from public.reponses_validees where embedding is not null")
        existants = {texte_reponse(q, i, r): v for q, i, r, v in cur.fetchall()}
    cur.execute("delete from public.reponses_validees where fiabilite is distinct from %s",
                (FIABILITE_CAPITALISEE,))

    textes = [texte_reponse(e["question"], e.get("intitule"), e["reponse"]) for e in bank]
    vecteurs = (embed_tout(textes, "      embeddings reponses") if avec_embeddings
                else vecteurs_repris(textes, existants, "reponses"))

    for e, v in zip(bank, vecteurs):
        cur.execute("""
            insert into public.reponses_validees
              (source_ref, origine, theme, sous_theme, intitule, question,
               question_source, reponse, sources, fiabilite, statut, embedding)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict do nothing
        """, (e["source_ref"], e["origine"], e.get("theme"), e.get("sous_theme"),
              e.get("intitule"), e["question"], e.get("question_source"), e["reponse"],
              Jsonb(e.get("sources", [])), e.get("fiabilite"),
              e.get("statut", "actif"), vecteur_sql(v)))

    # Reponses capitalisees : conservees, et vectorisees si elles ne le sont
    # pas encore (insertion SQL directe, sans vecteur).
    cur.execute("select id, question, intitule, reponse from public.reponses_validees "
                "where fiabilite = %s and embedding is null", (FIABILITE_CAPITALISEE,))
    a_vectoriser = cur.fetchall()
    if a_vectoriser and avec_embeddings:
        nouveaux = embed_tout([texte_reponse(q, i, r) for _, q, i, r in a_vectoriser],
                              "      embeddings reponses capitalisees")
        for (id_, _, _, _), v in zip(a_vectoriser, nouveaux):
            cur.execute("update public.reponses_validees set embedding = %s where id = %s",
                        (vecteur_sql(v), id_))
    elif a_vectoriser:
        log("      ATTENTION %d reponse(s) capitalisee(s) sans vecteur : relancez sans "
            "--no-embed pour les vectoriser." % len(a_vectoriser))

    cur.execute("select count(*) filter (where fiabilite is distinct from %s), "
                "count(*) filter (where fiabilite = %s) from public.reponses_validees",
                (FIABILITE_CAPITALISEE, FIABILITE_CAPITALISEE))
    n_bank, n_cap = cur.fetchone()
    log("      %d reponses validees chargees (%d ignoree(s) comme doublon)"
        % (n_bank, len(bank) - n_bank))
    if n_cap:
        log("      %d reponse(s) capitalisee(s) conservee(s)" % n_cap)


def charger_indicateurs(cur):
    log("\n[4/5] Indicateurs extra-financiers")
    kpi = charger_json("kpi.json")
    cur.execute("truncate table public.indicateurs restart identity")
    cur.execute("select code_doc from public.documents")
    connus = {r[0] for r in cur.fetchall()}
    n = 0
    for k in kpi:
        code = k.get("source_document")
        code = code if code in connus else None
        for annee, brut in k["valeurs"].items():
            if brut is None or str(brut).strip() == "":
                continue
            cur.execute("""
                insert into public.indicateurs
                  (theme, contribue_a, enjeu_materiel, indicateur, norme_gri, objectif,
                   methode, annee, valeur_texte, statut, code_doc, date_maj)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict do nothing
            """, (k.get("theme"), k.get("contribue_a"), k.get("enjeu_materiel"),
                  k["indicateur"], k.get("norme_gri"), k.get("objectif"), k.get("methode"),
                  int(annee), str(brut), k.get("statut"), code, k.get("date_maj")))
            n += 1
    cur.execute("select count(*) from public.indicateurs")
    charges = cur.fetchone()[0]
    log("      %d couples indicateur x annee charges" % charges)
    if charges < n:
        # Doublon sur (indicateur, annee, methode, objectif) : l'index unique
        # l'ecarte silencieusement ; on le rend visible.
        log("      ATTENTION %d valeur(s) ignoree(s) comme doublon d'un indicateur deja "
            "charge pour la meme annee." % (n - charges))


# =====================================================================
#  Tests de bout en bout
# =====================================================================
QUESTIONS_TEST = [
    "Quel est votre dernier bilan carbone scope 1, 2 et 3 ?",
    "Quelles mesures avez-vous mises en place pour controler la conformite de vos fournisseurs ?",
    "Etes-vous certifies ISO 9001 ?",
    "Quel est votre taux de turnover et votre pourcentage de femmes dans l'entreprise ?",
    "Comment gerez-vous les acces utilisateurs et la revue des habilitations ?",
    "Disposez-vous d'une politique de lutte contre la corruption ?",
]


def tester(cur):
    """Interroge recherche_globale, c'est-a-dire exactement ce que le
    workflow n8n appelle : si ces tests passent, le workflow trouvera."""
    log("\n[5/5] Tests de bout en bout")
    if not OPENAI_KEY:
        # Sans cle, on garde un vecteur constant non nul : la moitie plein texte
        # de la recherche fonctionne, et surtout cela prouve que la fonction SQL
        # s'execute. C'est le seul controle qui valide recherche_globale.
        log("      (pas de cle OpenAI : vecteur constant, seule la recherche")
        log("       plein texte francaise est exercee)")
    for q in QUESTIONS_TEST:
        log("\n  ? %s" % q)
        vecteur = embed([q])[0] if OPENAI_KEY else [0.01] * DIMENSIONS
        cur.execute("select public.recherche_globale(%s::jsonb, %s::jsonb, null, 6)",
                    (json.dumps([q]), json.dumps([vecteur])))
        res = cur.fetchone()[0] or {}
        candidats = res.get("candidats") or []
        if not candidats:
            log("     AUCUN RESULTAT - la base est-elle chargee ?")
            continue
        for c in candidats:
            page = ("p.%s" % c["page_debut"]) if c.get("page_debut") else ""
            log("     [%-16s] %-22s %-6s %s"
                % (c["type"], str(c["reference"])[:22], page,
                   " ".join(str(c["texte"]).split())[:76]))


def resume(cur):
    log("\n" + "=" * 62)
    for table, libelle in [("documents", "documents de preuve"),
                           ("documents_rag", "chunks indexes"),
                           ("reponses_validees", "reponses validees"),
                           ("indicateurs", "valeurs d'indicateurs")]:
        cur.execute("select count(*) from public.%s" % table)
        log("  %-24s %6d" % (libelle, cur.fetchone()[0]))
    cur.execute("select count(*) from public.documents_rag where embedding is not null")
    log("  %-24s %6d" % ("chunks vectorises", cur.fetchone()[0]))
    log("=" * 62)


def lire_env(chemin):
    """Lit un fichier KEY=VALEUR au format .env de docker compose."""
    valeurs = {}
    if not os.path.isfile(chemin):
        return valeurs
    with open(chemin, encoding="utf-8-sig") as f:
        for ligne in f:
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#") or "=" not in ligne:
                continue
            cle, valeur = ligne.split("=", 1)
            valeur = valeur.strip()
            if len(valeur) >= 2 and valeur[0] == valeur[-1] and valeur[0] in "'\"":
                valeur = valeur[1:-1]
            valeurs[cle.strip()] = valeur
    return valeurs


def parametres_connexion():
    """DATABASE_URL si elle est renseignee, sinon les identifiants de
    docker/.env. Une DATABASE_URL restee a sa valeur d'exemple (MOTDEPASSE,
    <...>) est ignoree plutot que de faire echouer la connexion."""
    url = DB_URL.strip()
    if url and not re.search(r"[<>\s]|MOTDEPASSE", url):
        return url, "DATABASE_URL"
    if url:
        log("  DATABASE_URL contient un texte d'exemple : ignoree, identifiants")
        log("  lus dans docker/.env.")
    env = lire_env(ENV_DOCKER)
    if not env.get("POSTGRES_PASSWORD"):
        sys.exit("Identifiants Postgres introuvables : ni DATABASE_URL, ni "
                 "POSTGRES_PASSWORD dans %s." % ENV_DOCKER)
    utilisateur = env.get("POSTGRES_USER") or "logiclever"
    base = env.get("POSTGRES_DB") or "logiclever"
    conninfo = make_conninfo(host="127.0.0.1", port=PORT_HOTE_POSTGRES,
                             user=utilisateur, password=env["POSTGRES_PASSWORD"],
                             dbname=base)
    return conninfo, "docker/.env, %s@127.0.0.1:%d/%s" % (
        utilisateur, PORT_HOTE_POSTGRES, base)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema-only", action="store_true")
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    if not (args.test or args.schema_only or args.no_embed) and not OPENAI_KEY:
        sys.exit("OPENAI_API_KEY absente de l'environnement (necessaire aux vecteurs).\n"
                 "  PowerShell :  $env:OPENAI_API_KEY = \"sk-...\"\n"
                 "ou relancez avec --no-embed pour recharger sans recalculer les vecteurs.")

    conninfo, origine = parametres_connexion()
    log("Connexion a Postgres (%s)..." % origine)
    with psycopg.connect(conninfo, autocommit=False) as conn:
        with conn.cursor() as cur:
            if args.test:
                tester(cur)
                resume(cur)
                return
            appliquer_schema(cur)
            conn.commit()
            if args.schema_only:
                log("\nSchema applique. Relancez sans --schema-only pour charger les donnees.")
                return
            avec = not args.no_embed
            charger_documents(cur, avec)
            conn.commit()
            copier_fichiers_preuve(cur)
            conn.commit()
            charger_reponses(cur, avec)
            conn.commit()
            charger_indicateurs(cur)
            conn.commit()
            cur.execute("analyze public.documents_rag")
            cur.execute("analyze public.reponses_validees")
            cur.execute("analyze public.indicateurs")
            conn.commit()
            if avec:
                tester(cur)
            resume(cur)
            log("\nBase prete. Importez les workflows n8n du dossier workflows/.")


if __name__ == "__main__":
    main()
