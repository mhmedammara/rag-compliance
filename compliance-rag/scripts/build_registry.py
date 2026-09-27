# -*- coding: utf-8 -*-
"""
Construit le registre canonique des documents de preuve Logiclever.

Decouverte automatique : tout fichier pris en charge (pdf, docx, pptx, xlsx,
images) de a/, b/ et de tout autre dossier de "Docs pour RAG" hors
DOSSIERS_EXCLUS est indexe, sous-dossiers compris. Les pages sans couche
texte (scans, impressions en image) passent par l'OCR (Tesseract).

REGISTRY ne fait plus que fixer a la main les metadonnees d'un fichier (code,
titre, categorie, date) ; sans entree, elles sont deduites du fichier (nom,
proprietes, premiere ligne). Doublons ecartes automatiquement : contenu
identique, ou texte quasi identique (la version la plus recente est
retenue) ; DOUBLONS garde les decisions prises a la main.
Sortie : seed/documents.json  (texte page par page, pret a chunker)
"""
import pymupdf, openpyxl, zipfile, re, os, sys, hashlib, json, unicodedata
import datetime, html

sys.stdout.reconfigure(encoding="utf-8")

# Dossier "Docs pour RAG" (parent de compliance-rag/), deduit de ce fichier.
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(BASE, "compliance-rag", "seed")

# Dossiers de "Docs pour RAG" qui ne contiennent PAS de preuves : le projet
# lui-meme et les questionnaires clients, vierges (Demo, Recette) ou remplis
# (source de la banque de reponses, pas des documents a citer). Tout autre
# dossier est parcouru : deposer un fichier dans a/, b/ ou un nouveau dossier
# suffit a l'indexer. Les dossiers caches (.git...) sont toujours ignores.
DOSSIERS_EXCLUS = {
    "compliance-rag", "Demo", "Recette", "EXemples remplies de formulaires clients",
}

# --------------------------------------------------------------- traduction
# Les deux rapports CyberVadis sont rediges en anglais par l'evaluateur.
# Le glossaire les francise cellule par cellule : la recherche plein texte
# francaise (config fr_unaccent) peut alors les atteindre.
_gloss = json.load(open(os.path.join(OUT, "traductions_cybervadis.json"),
                        encoding="utf8"))
CHAINES = _gloss["chaines"]
PAGES_TRADUITES = _gloss["pages"]
# jamais traduits : codes de controle et noms de fichiers de preuve
NON_TRADUITS = ("Question Code", "Evidence reference")


def traduire(valeur):
    """Correspondance exacte. Une chaine absente du glossaire est renvoyee
    telle quelle : scripts/verifier.py (controle 2) signale les oublis."""
    if valeur is None:
        return ""
    v = " ".join(str(valeur).replace("_x000D_", " ").split())
    return CHAINES.get(v, v)


# ---------------------------------------------------------------- extraction
SEUIL_OCR_MIN = 20  # sous ce nombre de caracteres, on suppose qu'il n'y a pas de vraie couche texte


def pdf_pages(path):
    """PDF, ou image (png, jpg, tiff...) convertie en PDF d'une page : sans
    couche texte, elle passe entierement par l'OCR."""
    d = pymupdf.open(path)
    if not d.is_pdf:
        d = pymupdf.open("pdf", d.convert_to_pdf())
    pages = []
    for i in range(d.page_count):
        page = d[i]
        texte = page.get_text()
        if len(texte.strip()) < SEUIL_OCR_MIN:
            try:
                tp = page.get_textpage_ocr(language="fra+eng", dpi=300, full=True)
                texte_ocr = page.get_text(textpage=tp).strip()
                if len(texte_ocr) > len(texte.strip()):
                    print("   OCR applique : page %d de %s" % (i + 1, os.path.basename(path)))
                    texte = texte_ocr
            except RuntimeError as e:
                print("   ATTENTION OCR indisponible (%s) - page %d de %s reste vide"
                      % (e, i + 1, os.path.basename(path)))
        pages.append(texte)
    d.close()
    return pages


def docx_pages(path):
    z = zipfile.ZipFile(path)
    s = z.read("word/document.xml").decode("utf8", "ignore")
    s = re.sub(r"</w:p>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return ["\n".join(l.strip() for l in s.split("\n") if l.strip())]


def pptx_pages(path):
    z = zipfile.ZipFile(path)
    names = sorted(
        [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml", n)],
        key=lambda n: int(re.search(r"(\d+)", n).group(1)),
    )
    out = []
    for n in names:
        s = z.read(n).decode("utf8", "ignore")
        s = re.sub(r"<[^>]+>", " ", s)
        out.append(" ".join(s.split()))
    return out


def xlsx_pages(path, traduire_cellules=False):
    wb = openpyxl.load_workbook(path, data_only=True)
    out = []
    for ws in wb.worksheets:
        lines = []
        entetes = []
        for n, row in enumerate(ws.iter_rows(values_only=True)):
            if n == 0:
                entetes = [(" ".join(str(c).split()) if c is not None else "")
                           for c in row]
            cells = []
            for i, c in enumerate(row):
                if c is None or not str(c).strip():
                    continue
                brut = " ".join(str(c).replace("_x000D_", " ").split())
                entete = entetes[i] if i < len(entetes) else ""
                # la ligne d'entete est toujours traduite ; la regle NON_TRADUITS
                # ne protege que les valeurs (codes de controle, noms de fichiers)
                protege = n > 0 and entete in NON_TRADUITS
                cells.append(brut if (not traduire_cellules or protege)
                             else traduire(brut))
            if cells:
                lines.append(" | ".join(cells))
        out.append("\n".join(lines))
    wb.close()
    return out


READERS = {".pdf": pdf_pages, ".docx": docx_pages, ".pptx": pptx_pages, ".xlsx": xlsx_pages,
           ".png": pdf_pages, ".jpg": pdf_pages, ".jpeg": pdf_pages,
           ".tif": pdf_pages, ".tiff": pdf_pages, ".bmp": pdf_pages}


def read_any(path, code_doc=None, langue_source="fr"):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        pages = xlsx_pages(path, traduire_cellules=(langue_source == "en"))
    else:
        pages = READERS[ext](path)
    if langue_source == "en":
        # page entiere reecrite en francais (documents non tabulaires)
        pages = [PAGES_TRADUITES.get("%s#%d" % (code_doc, i + 1), p)
                 for i, p in enumerate(pages)]
    return pages


def resolve(rel):
    """Resolut un chemin relatif en tolerant les formes Unicode NFC / NFD
    (certains fichiers OneDrive stockent les accents en forme decomposee)."""
    rel = rel.replace("\\", "/")
    direct = os.path.join(BASE, rel)
    if os.path.exists(direct):
        return direct
    folder, name = os.path.split(rel)
    d = os.path.join(BASE, folder)
    if not os.path.isdir(d):
        return direct
    target = unicodedata.normalize("NFC", name)
    for f in os.listdir(d):
        if unicodedata.normalize("NFC", f) == target:
            return os.path.join(d, f)
    return direct


# ---------------------------------------------------------------- registre
# Metadonnees fixees a la main, prioritaires sur la deduction automatique :
# code_doc, chemin source, titre metier, categorie, date de reference, statut
# [, langue source]. Ces code_doc sont cites par la banque de reponses et les
# indicateurs : ne pas les renommer. Un fichier absent d'ici est indexe quand
# meme, avec des metadonnees deduites ; y ajouter une entree les corrige.
REGISTRY = [
    ("RSE-RAPPORT-2025", r"a\Audit-RSE-2025.pdf",
     "Rapport RSE - Indicateurs 2025", "rapport_rse", "2026-02-27", "actif"),
    ("GES-2023", "a/Logiclever - 2023 - Bilan d'émissions de gaz à effet de serre - version 9 (2025-01-13T16_12_44.938Z) (2).pdf",
     "Bilan d'emissions de GES 2023 (Greenly)", "bilan_carbone", "2025-01-13", "actif"),
    ("GES-2021", "a/Logiclever - Bilan d'émissions de gaz à effet de serre (1).pdf",
     "Bilan d'emissions de GES 2021 (Greenly)", "bilan_carbone", "2022-09-29", "historique"),
    ("POL-SOCIAL", r"a\Politique-social-et-droit-de-l_Homme.pdf",
     "Politique RSE A / Social et droits humains (V1.3)", "politique", "2024-09-16", "actif"),
    ("POL-ENV", r"a\Politique-Environnement.pdf",
     "Politique RSE B / Environnement (V1.1)", "politique", "2024-09-16", "actif"),
    ("POL-ACHATS", r"a\Politique-Achat-Responsable.pdf",
     "Politique RSE C / Achats responsables (V1.1)", "politique", "2024-09-16", "actif"),
    ("POL-ETHIQUE", r"a\Politique-Ethique.pdf",
     "Politique RSE D / Ethique (V2.0)", "politique", "2024-09-16", "actif"),
    ("POL-SSI", r"a\Politique_SSI_Logiclever_E.pdf",
     "Politique de Securite des Systemes d'Information (version E)", "politique", "2026-02-09", "actif"),
    ("CODE-CONDUITE", r"b\7cf3fb38-88b0-493e-85e4-3d8e76c788d7.pdf",
     "Code de Conduite Logiclever", "politique", "2024-09-23", "actif"),
    ("MATERIALITE", "a/Analyse de Matérialité.docx",
     "Analyse de materialite Logiclever", "gouvernance", "2024-09-16", "actif"),
    ("MATRICE-MAT", "a/Matrice de matérialité Logiclever.pptx",
     "Matrice de materialite Logiclever", "gouvernance", "2024-09-16", "actif"),
    ("GOUVERNANCE", r"b\37e29354-4b7f-4913-ab12-89f7cb42cca7.pdf",
     "Gouvernance de Logiclever", "gouvernance", "2024-10-08", "actif"),
    ("CV-EXEC-2025", r"a\Executive report - logiclever - 21Oct2025-1640.pdf",
     "CyberVadis - Resume executif 2025 (860/1000)", "certification", "2025-10-21", "actif", "en"),
    ("CV-PERF-2025", r"a\Performance report - logiclever - V29 - 21Oct2025.xlsx",
     "CyberVadis - Rapport de performance V29 (detail par controle)", "certification", "2025-10-21", "actif", "en"),
    ("EV-QUEST", r"a\LOGICLEVER SASU_EV_Questionnaire_pdf.pdf",
     "EcoVadis - Questionnaire complet renseigne", "certification", "2026-01-01", "actif"),
    ("GPTW-CERT", r"b\8816088d-cea4-4795-99c3-2db6f59615e9.pdf",
     "Certificat Great Place To Work 2023-2024", "certification", "2023-06-01", "actif"),
    ("KPI-RSE", "a/LOGICLEVER RSE - Liste des indicateurs de performance extra-financière.xlsx",
     "Liste des indicateurs de performance extra-financiere (GRI)", "indicateurs", "2026-02-22", "actif"),
    ("KPI-MALADIE", "a/LOGICLEVER RSE - Suivi jours arrêts maladie.pdf",
     "Suivi des jours d'arret maladie", "indicateurs", "2026-02-22", "actif"),
    ("ONBOARDING", r"b\0a9bd6df-99c7-4e00-96d5-74e3e7aa7c93.pdf",
     "On-Boarding Logiclever 2026", "process_rh", "2026-01-01", "actif"),
    ("RECRUTEMENT", r"b\91531c22-6331-4dfd-b723-8ba1cb8c3ecf.pdf",
     "P31 - Processus de recrutement salarie", "process_rh", "2024-09-17", "actif"),
    ("SALAIRES", r"b\6bfe9445-2a1a-4cee-9be8-00434e81b530.pdf",
     "P33-A - Augmenter les salaires (jury des salaires)", "process_rh", "2024-07-03", "actif"),
    ("CONFLITS", r"b\4a334178-26da-49c8-8163-cbcf18dbecda.pdf",
     "P02-A - Gerer et resoudre les conflits", "process_rh", "2024-09-23", "actif"),
    ("CRECHE", r"b\53ba4095-1f11-46a0-aef1-5033a331d94b.pdf",
     "P05-G - Creche d'entreprise", "process_rh", "2023-10-27", "actif"),
    ("PRIME-TRANSPORT", r"b\832f68e9-9ffe-40a9-944c-763319dc1800.pdf",
     "DUE relative a la prime transport", "process_rh", "2024-02-23", "actif"),
    ("DIVERSITE", r"b\581f0df8-96db-4519-a365-273cc91881a3.pdf",
     "Diversite et Inclusion chez Logiclever", "programme", "2024-09-26", "actif"),
    ("STRESS", r"b\1c6ecd63-e4ed-4dd9-8b5b-b10eba886140.pdf",
     "Procedure de sensibilisation au stress", "formation", "2024-10-22", "actif"),
    ("FORM-SANTE", r"b\9ba9bddc-767b-4367-a6f8-4dbc5920a997.pdf",
     "Formation Sante, Securite et Bonnes Pratiques de Travail", "formation", "2025-12-14", "actif"),
    ("FORM-SOCIAL-ENG", r"b\86c507d7-27e2-4e4a-8e77-89f53636aa34.pdf",
     "Formation SSI - Sensibilisation a l'ingenierie sociale", "formation", "2025-11-04", "actif"),
    ("ZERO-DECHET", r"b\86a07d03-3a66-43bd-8cf0-465202535441.pdf",
     "Logiclever, entreprise Zero dechet", "programme", "2026-03-17", "actif"),
    ("DECHETS-DANG", r"b\27632e4c-0e72-4936-8dca-4ea44d0f6447.pdf",
     "Registre de suivi des dechets dangereux", "registre", "2026-03-24", "actif"),
    ("SSI-REVUES", r"b\06804683-b219-498c-854c-449075b598da.pdf",
     "Registre des revues SSI (acces, politiques)", "registre", "2026-02-01", "actif"),
    ("SSI-PLAN", r"b\26e96dd4-83cb-4bcb-b5f3-9867217957f2.pdf",
     "Plan d'actions SSI et revues reglementaires", "registre", "2026-02-09", "actif"),
    ("SSI-RISQUES", r"b\4454d18e-9a0d-4a20-83da-1681a2279739.pdf",
     "Registre des risques SSI (criticite I x P)", "registre", "2025-12-15", "actif"),
    ("SSI-FORM-COLLAB", r"b\4acff9fd-9201-442f-a27e-4a32e7926406.pdf",
     "Suivi des formations SSI par collaborateur", "registre", "2025-12-15", "actif"),
    ("SSI-FOURNISSEURS", r"b\545acca1-d31e-4796-bda9-5263c2f2b1d8.pdf",
     "Evaluation SSI des fournisseurs (niveau de dependance)", "registre", "2026-05-01", "actif"),
    ("SSI-ACCES", r"b\9b241eb1-bee2-42c2-885e-de0fafd6ea93.pdf",
     "Registre et frequence de revue des acces", "registre", "2026-05-01", "actif"),
    ("CONTRAT-TYPE", r"b\99af8fd5-dc58-429d-a4ec-b4c8b1fcde27.pdf",
     "Contrat de prestations de services - modele client 2026", "contrat", "2026-01-01", "actif"),
    ("QUI-SOMMES-NOUS", r"b\22700d77-b437-44dc-aa06-3f9065c0da6e.pdf",
     "Qui sommes-nous - presentation Logiclever", "presentation", "2024-01-01", "actif"),
    ("EV-FICHE-2026", "a/Fiche d'évaluation ECOVADIS 27 mai 2026.pdf",
     "EcoVadis - Fiche d'evaluation 2026 (Gold, 84/100)", "certification", "2026-05-27", "actif"),
]

# doublons ecartes a la main : chemin -> (code du document canonique retenu,
# motif). Les copies exactes et versions quasi identiques sont aussi
# detectees seules (voir ecarter_doublons) ; cette liste garde les cas deja
# tranches, dont ceux que la detection ne voit pas (export PDF d'un tableur).
DOUBLONS = {
    r"b\01683f7a-f983-4aa9-81c0-c8a031edecd2.pdf": ("POL-ETHIQUE", "contenu identique (md5)"),
    r"b\3a5376ab-05a3-464d-b6b2-c83bd0622019.pdf": ("POL-ACHATS", "contenu identique (md5)"),
    r"b\9f66bd44-dbdb-4ba3-88a5-c17cf0b705eb.pdf": ("POL-ENV", "contenu identique hors metadonnees"),
    r"b\3014a600-efba-463b-8d2d-e98b4bcd26ad.pdf": ("CV-EXEC-2025", "contenu identique (md5)"),
    r"b\4c4427dd-a6f1-44ff-8dd4-76b6aaf8f2a4.pdf": ("SSI-REVUES", "contenu identique (md5)"),
    r"b\4513cf24-10de-4251-986b-686b5d67a469.pdf": ("MATERIALITE", "export PDF du meme document"),
    r"b\81b0925d-83e4-454f-a873-e1ce42165a2e.pdf": ("KPI-RSE", "export PDF du meme tableur"),
    r"a\26b3f14b-b19e-4a9f-a1c0-13db4905fbf6.xlsx": ("CV-PERF-2025", "contenu identique (md5)"),
    r"b\50a1f57c-2fc4-4791-b12b-913439732050.pdf": ("POL-SOCIAL", "version anterieure V1.2, V1.3 retenue"),
    r"b\64db581f-366d-4569-9032-945e3296b89d.pdf": ("POL-SSI", "version anterieure, version E retenue"),
}


# Fichiers des dossiers de preuve volontairement hors du registre : ils
# servent de SOURCE a la banque de reponses, pas de document a citer.
SOURCES_SEULEMENT = {
    "a/LOGICLEVER SASU_EV_Questionnaire_spreadsheet.xlsx":
        "source des 30 reponses EcoVadis (build_answer_bank.py) ; son jumeau PDF "
        "EV-QUEST est, lui, indexe et citable",
}


# ---------------------------------------------------- metadonnees deduites
# Tout est deduit par regles, sans modele de langage : nom du fichier,
# proprietes du document, premiere ligne de texte.
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# noms donnes par un appareil ou un logiciel : ils ne disent rien du contenu
NOM_GENERIQUE = re.compile(r"^(scan|img|image|dsc|photo|document|doc|fichier|file|"
                           r"sans titre|untitled|numerisation)[\s_-]*\d*$", re.I)
# horodatage d'export et marqueur de copie : "(2025-01-13T16_12_44.938Z)", " (2)"
HORODATAGE = re.compile(r"\(?\d{4}-\d{2}-\d{2}T[\d_:.]+Z?\)?")
COPIE = re.compile(r"\s*\(\d+\)\s*$")
# jamais dans un code : mots vides, nom de l'entreprise (redondant partout)
MOTS_VIDES = {"LE", "LA", "LES", "DE", "DU", "DES", "AU", "AUX", "ET", "EN", "SUR",
              "POUR", "PAR", "UN", "UNE", "THE", "OF", "AND", "FOR", "TO",
              "LOGICLEVER", "SASU", "SAS"}
ANNEE = re.compile(r"(19|20)\d{2}")
# premiere regle qui correspond (titre + nom du fichier, sans accents)
CATEGORIES = [
    ("bilan_carbone", r"bilan (d )?(emissions|carbone|ges)|gaz a effet de serre|empreinte carbone"),
    ("certification", r"certifi|label|ecovadis|cybervadis|great ?place ?to ?work|attestation|medaille|iso \d{4,5}"),
    ("politique", r"politique|charte|code de conduite"),
    ("registre", r"registre"),
    ("formation", r"formation|sensibilisation"),
    ("indicateurs", r"indicateur|kpi|tableau de bord"),
    ("rapport_rse", r"rapport (rse|de durabilite|extra financier)|dpef"),
    ("process_rh", r"processus|procedure|recrutement|on ?boarding|salaire|conges?|teletravail"),
    ("contrat", r"contrat|conditions generales|cgv"),
    ("gouvernance", r"gouvernance|materialite"),
    ("presentation", r"presentation|qui sommes nous|plaquette"),
]
# Jaccard des suites de 5 mots au-dessus duquel deux fichiers sont deux
# versions d'un meme document. Calibre sur le corpus du 2026-09-27 : deux
# documents distincts ne depassent pas 0,06 ; les versions et exports ecartes
# a la main (DOUBLONS) sont entre 0,76 et 1, sauf l'export PDF d'un tableur
# (0,44), que seule la liste manuelle ecarte.
SEUIL_QUASI_IDENTIQUE = 0.6
TEXTE_MIN = 30  # en dessous, rien d'exploitable n'a ete extrait
# Part minimale des mots d'un document ajoute automatiquement (3 lettres et
# plus) deja presents dans le corpus du registre manuel. Le "texte" que l'OCR
# tire d'une photo ou d'un logo tombe a 0,27 ; un vrai document, meme en
# anglais ou sur un sujet nouveau, depasse 0,59 (calibre le 2026-09-27).
SEUIL_LISIBLE = 0.4


def ascii_min(s):
    """Minuscules sans accents ni ponctuation : base des comparaisons."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", s))


def nom_parlant(stem):
    """Le nom du fichier decrit-il son contenu ? Non pour b/<uuid>.pdf ou
    scan0001.jpg."""
    return (not UUID.match(stem) and not NOM_GENERIQUE.match(stem)
            and re.search(r"[^\W\d_]{3}", stem) is not None)


def nettoyer_nom(stem):
    s = HORODATAGE.sub(" ", stem)
    while COPIE.search(s):
        s = COPIE.sub("", s)
    return " ".join(s.replace("_", " ").split()).strip(" -")


def date_plausible(d):
    try:
        return 1990 <= datetime.date.fromisoformat(d).year <= datetime.date.today().year + 1
    except (TypeError, ValueError):
        return False


def date_pdf(valeur):
    """"D:20260927152306+02'00'" -> "2026-09-27"."""
    m = re.match(r"(?:D:)?(\d{4})(\d{2})(\d{2})", valeur or "")
    return "%s-%s-%s" % m.groups() if m else None


def proprietes(path):
    """(titre, date) lus dans les proprietes du fichier : dictionnaire info du
    PDF, docProps/core.xml des fichiers Office. Date par defaut : derniere
    modification du fichier sur le disque."""
    ext = os.path.splitext(path)[1].lower()
    titre, dates = "", []
    try:
        if ext == ".pdf":
            with pymupdf.open(path) as d:
                meta = d.metadata or {}
            titre = meta.get("title") or ""
            dates = [date_pdf(meta.get("modDate")), date_pdf(meta.get("creationDate"))]
        elif ext in (".docx", ".pptx", ".xlsx"):
            with zipfile.ZipFile(path) as z:
                core = z.read("docProps/core.xml").decode("utf8", "ignore")
            m = re.search(r"<dc:title>([^<]*)</dc:title>", core)
            titre = html.unescape(m.group(1)) if m else ""
            for balise in ("modified", "created"):
                m = re.search(r"<dcterms:%s[^>]*>(\d{4}-\d{2}-\d{2})" % balise, core)
                if m:
                    dates.append(m.group(1))
    except (KeyError, RuntimeError, ValueError, zipfile.BadZipFile):
        pass
    for d in dates:
        if date_plausible(d):
            return titre.strip(), d
    return titre.strip(), datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat()


def titre_valable(t):
    """Titre des proprietes, s'il dit quelque chose ("Microsoft Word - x.docx"
    -> "x" ; "Logiclever" seul -> rien)."""
    t = re.sub(r"^Microsoft (Word|PowerPoint|Excel) - ", "", t or "")
    t = re.sub(r"\.(docx?|pptx?|xlsx?|pdf)$", "", t, flags=re.I).strip()
    if len(re.findall(r"[^\W\d_]{3,}", t)) < 2 or len(t) > 150 or UUID.match(t):
        return ""
    return t


def premiere_ligne(pages):
    """Premiere ligne des deux premieres pages qui ressemble a un titre."""
    for page in pages[:2]:
        for ligne in page.splitlines():
            ligne = " ".join(ligne.split()).strip(" -:|")
            if not 12 <= len(ligne) <= 120 or re.search(r"https?:|www\.", ligne):
                continue
            if (len(re.findall(r"[^\W\d_]{2,}", ligne)) >= 3
                    and sum(c.isalpha() for c in ligne) >= 0.6 * len(ligne)):
                return ligne
    return ""


def titre_auto(stem, titre_meta, pages):
    if nom_parlant(stem):
        return nettoyer_nom(stem)
    return titre_valable(titre_meta) or premiere_ligne(pages) or "Document %s" % stem[:8]


def categorie_auto(*libelles):
    texte = ascii_min(" ".join(libelles))
    for categorie, motif in CATEGORIES:
        if re.search(motif, texte):
            return categorie
    return "document"


def code_auto(libelle, pris):
    """"Logiclever - 2023 - Bilan d'emissions de gaz..." -> BILAN-EMISSIONS-
    GAZ-EFFET-2023. Seule une annee peut y porter 4 chiffres : la recherche
    lit l'exercice couvert dans le premier nombre a 4 chiffres du code."""
    jetons = ascii_min(libelle).upper().split()
    annees = [j for j in jetons if ANNEE.fullmatch(j)]
    mots = [j for j in jetons if j not in MOTS_VIDES and len(j) >= 2
            and not j.isdigit() and not re.search(r"\d{4}", j)][:4]
    while len(mots) > 1 and len("-".join(mots)) > 36:
        mots.pop()
    base = "-".join(mots + annees[:1]) or "DOC"
    code, n = base, 2
    while code in pris:
        code, n = "%s-%d" % (base, n), n + 1
    pris.add(code)
    return code


def mots_lisibles(pages):
    return [m for m in ascii_min(" ".join(pages)).split() if len(m) >= 3 and not m.isdigit()]


def empreinte(pages):
    """Suites de 5 mots du texte normalise : deux versions d'un meme document
    en partagent l'essentiel, deux documents distincts presque aucune."""
    mots = ascii_min(" ".join(pages)).split()
    return {" ".join(mots[i:i + 5]) for i in range(len(mots) - 4)}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def md5_fichier(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def document(code, titre, cat, rel, date_ref, statut, langue_source, pages):
    full = "\n".join(pages)
    return {
        "code_doc": code, "titre": titre, "categorie": cat,
        "fichier_source": rel, "date_reference": date_ref,
        "statut": statut, "langue_source": langue_source,
        "nb_pages": len(pages), "md5": hashlib.md5(full.encode("utf8", "ignore")).hexdigest(),
        "pages": pages,
    }


# ---------------------------------------------------------------- decouverte
def lister_fichiers():
    """Chemins relatifs a BASE (a/x.pdf, tels que sur le disque) de tous les
    fichiers des dossiers de preuve, sous-dossiers compris, tries."""
    trouves = []
    for dossier in sorted(os.listdir(BASE)):
        racine = os.path.join(BASE, dossier)
        if (not os.path.isdir(racine) or dossier in DOSSIERS_EXCLUS
                or dossier.startswith((".", "_"))):
            continue
        for rep, sous, fichiers in os.walk(racine):
            sous[:] = sorted(s for s in sous if not s.startswith((".", "_")))
            for f in sorted(fichiers):
                if f.startswith(("~$", ".")) or f.lower() == "desktop.ini":
                    continue
                trouves.append(os.path.relpath(os.path.join(rep, f), BASE).replace("\\", "/"))
    return trouves


def ecarter_doublons(candidats):
    """candidats : [{"doc", "manuel"}], manuels d'abord. Renvoie (retenus,
    ecartes, alertes), ecartes = [(candidat, canonique, motif)]. Un document
    du registre manuel n'est jamais ecarte : son code_doc est cite ailleurs."""
    def rang(c):  # plus petit = prefere comme exemplaire canonique
        rel = c["doc"]["fichier_source"]
        stem = os.path.splitext(os.path.basename(rel))[0]
        return (0 if c["manuel"] else 1, 0 if nom_parlant(stem) else 1, rel.count("/"), rel)

    def recent_ancien(a, b):  # a date egale, l'exemplaire prefere compte comme recent
        da, db = a["doc"]["date_reference"] or "", b["doc"]["date_reference"] or ""
        if da != db:
            return (a, b) if da > db else (b, a)
        return (a, b) if rang(a) <= rang(b) else (b, a)

    ecartes, alertes, ecartes_ids, signales = [], [], set(), set()
    # 1. copie exacte : memes octets, ou meme texte extrait sous un autre
    #    format. Les octets comptent aussi : un document traduit (CyberVadis)
    #    n'a plus le texte de sa copie brute.
    for empreinte_exacte in (lambda c: c["md5_fichier"], lambda c: c["doc"]["md5"]):
        groupes = {}
        for c in candidats:
            if id(c) not in ecartes_ids:
                groupes.setdefault(empreinte_exacte(c), []).append(c)
        for groupe in groupes.values():
            groupe.sort(key=rang)
            for c in groupe[1:]:
                if not c["manuel"]:
                    ecartes.append((c, groupe[0], "contenu identique (md5)"))
                    ecartes_ids.add(id(c))
                elif (id(c), id(groupe[0])) not in signales:
                    signales.add((id(c), id(groupe[0])))
                    print("  ! %s a le meme contenu que %s"
                          % (c["doc"]["code_doc"], groupe[0]["doc"]["code_doc"]))
    restants = [c for c in candidats if id(c) not in ecartes_ids]

    # 2. texte quasi identique : versions successives d'un meme document
    if any(not c["manuel"] for c in restants):
        for c in restants:
            c["empreinte"] = empreinte(c["doc"]["pages"])
        for i, a in enumerate(restants):
            for b in restants[i + 1:]:
                if (a["manuel"] and b["manuel"]) or id(a) in ecartes_ids or id(b) in ecartes_ids:
                    continue
                j = jaccard(a["empreinte"], b["empreinte"])
                if j < SEUIL_QUASI_IDENTIQUE:
                    continue
                recent, ancien = recent_ancien(a, b)
                if ancien["manuel"]:
                    # un document du registre reste ; le plus recent aussi,
                    # c'est a l'utilisateur de basculer l'entree REGISTRY
                    alertes.append("%s ressemble a %.0f %% a %s, plus ancien : nouvelle version ? "
                                   "Pour la substituer, pointez l'entree REGISTRY %s vers %s"
                                   % (recent["doc"]["fichier_source"], j * 100,
                                      ancien["doc"]["code_doc"], ancien["doc"]["code_doc"],
                                      recent["doc"]["fichier_source"]))
                    continue
                motif = "quasi identique (%.0f %%), version la plus recente retenue" % (j * 100)
                ecartes.append((ancien, recent, motif))
                ecartes_ids.add(id(ancien))
    # un canonique peut avoir ete ecarte a son tour (copie d'une version
    # anterieure) : on remonte jusqu'a l'exemplaire finalement retenu
    canon_de = {id(c): canon for c, canon, _ in ecartes}
    resolus = []
    for c, canon, motif in ecartes:
        while id(canon) in canon_de:
            canon = canon_de[id(canon)]
        resolus.append((c, canon, motif))
    retenus = [c for c in candidats if id(c) not in ecartes_ids]
    return retenus, resolus, alertes


def main():
    nfc = lambda x: unicodedata.normalize("NFC", x.replace("\\", "/"))
    fichiers = lister_fichiers()
    print("Dossiers parcourus : %s"
          % ", ".join(sorted({f.split("/")[0] + "/" for f in fichiers})))
    connus = ({nfc(e[1]) for e in REGISTRY} | {nfc(k) for k in DOUBLONS}
              | {nfc(k) for k in SOURCES_SEULEMENT})

    # 1. registre manuel, dans son ordre (sortie stable d'un lancement a l'autre)
    candidats = []
    for entree in REGISTRY:
        code, rel, titre, cat, date_ref, statut = entree[:6]
        langue_source = entree[6] if len(entree) > 6 else "fr"
        path = resolve(rel)
        if not os.path.exists(path):
            print("MANQUANT :", rel)
            continue
        pages = read_any(path, code, langue_source)
        candidats.append({"manuel": True, "md5_fichier": md5_fichier(path), "doc": document(
            code, titre, cat, rel.replace("\\", "/"), date_ref, statut, langue_source, pages)})

    # 2. tout autre fichier des dossiers de preuve : metadonnees deduites
    vocabulaire = set()
    for c in candidats:
        vocabulaire.update(mots_lisibles(c["doc"]["pages"]))
    non_pris, sans_texte, illisibles = [], [], []
    for rel in fichiers:
        if nfc(rel) in connus:
            continue
        stem, ext = os.path.splitext(os.path.basename(rel))
        if ext.lower() not in READERS:
            non_pris.append(rel)
            continue
        path = os.path.join(BASE, rel)
        pages = read_any(path)
        if len("".join(pages).strip()) < TEXTE_MIN:
            sans_texte.append(rel)
            continue
        mots = mots_lisibles(pages)
        lisible = sum(m in vocabulaire for m in mots) / max(1, len(mots))
        if vocabulaire and lisible < SEUIL_LISIBLE:
            illisibles.append((rel, lisible))
            continue
        titre_meta, date_ref = proprietes(path)
        titre = titre_auto(stem, titre_meta, pages)
        candidats.append({"manuel": False, "md5_fichier": md5_fichier(path), "doc": document(
            None, titre, categorie_auto(titre, stem), rel, date_ref, "actif", "fr", pages)})

    retenus, ecartes, alertes = ecarter_doublons(candidats)
    # codes attribues apres le dedoublonnage : un doublon ecarte ne decale
    # pas les suffixes (-2, -3) des autres d'un lancement a l'autre
    pris = {e[0] for e in REGISTRY}
    for c in retenus:
        if c["doc"]["code_doc"] is None:
            c["doc"]["code_doc"] = code_auto(c["doc"]["titre"], pris)
    docs = [c["doc"] for c in retenus]
    auto = [c["doc"] for c in retenus if not c["manuel"]]

    reg = {
        "documents": docs,
        "doublons_ecartes": [
            {"fichier": k.replace("\\", "/"), "canonique": v[0], "motif": v[1]}
            for k, v in DOUBLONS.items()
        ] + [
            {"fichier": c["doc"]["fichier_source"], "canonique": canon["doc"]["code_doc"],
             "motif": motif + " (detecte automatiquement)"}
            for c, canon, motif in ecartes
        ],
    }
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "documents.json"), "w", encoding="utf8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)

    print("\n%d documents canoniques (%d du registre manuel, %d ajoutes automatiquement)"
          % (len(docs), len(docs) - len(auto), len(auto)))
    print("%d doublons ecartes (%d a la main, %d automatiquement)"
          % (len(reg["doublons_ecartes"]), len(DOUBLONS), len(ecartes)))
    print("%d pages au total" % sum(d["nb_pages"] for d in docs))
    traduits = [d["code_doc"] for d in docs if d["langue_source"] == "en"]
    print("%d documents traduits depuis l'anglais : %s"
          % (len(traduits), ", ".join(traduits)))
    print("%d fichier(s) utilise(s) comme source seulement, non indexe(s)"
          % len(SOURCES_SEULEMENT))

    if auto:
        print("\nAjoutes automatiquement (metadonnees deduites ; a fixer dans REGISTRY si besoin) :")
        for d in auto:
            print("   %-32s %s" % (d["code_doc"], d["fichier_source"]))
            print("   %-32s titre : %s | %s | %s | %d page(s)"
                  % ("", d["titre"], d["categorie"], d["date_reference"], d["nb_pages"]))
    for c, canon, motif in ecartes:
        print("   doublon ecarte : %s -> %s (%s)"
              % (c["doc"]["fichier_source"], canon["doc"]["code_doc"], motif))
    for a in alertes:
        print("\nATTENTION - " + a)
    if sans_texte:
        print("\nATTENTION - aucun texte extrait, meme par OCR (non indexe) :")
        for f in sans_texte:
            print("   ", f)
    if illisibles:
        print("\nATTENTION - texte illisible, non indexe (OCR d'une photo ou d'un logo ?) ; "
              "une entree REGISTRY force l'indexation :")
        for f, lisible in illisibles:
            print("    %s (%.0f %% de mots connus)" % (f, lisible * 100))
    if non_pris:
        print("\nIgnores (format non pris en charge) :")
        for f in non_pris:
            print("   ", f)


if __name__ == "__main__":
    main()
