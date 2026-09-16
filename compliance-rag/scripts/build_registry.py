# -*- coding: utf-8 -*-
"""
Construit le registre canonique des documents de preuve Logiclever.
Deduplique par hash de contenu + regles de version explicites.
Sortie : seed/documents.json  (texte page par page, pret a chunker)
"""
import pymupdf, openpyxl, zipfile, re, os, sys, hashlib, json, unicodedata

sys.stdout.reconfigure(encoding="utf-8")

BASE = r"C:\Users\Moham\OneDrive\Desktop\Docs pour RAG"
OUT = os.path.join(BASE, "compliance-rag", "seed")

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
    telle quelle : le script verifier_traduction.py signale les oublis."""
    if valeur is None:
        return ""
    v = " ".join(str(valeur).replace("_x000D_", " ").split())
    return CHAINES.get(v, v)


# ---------------------------------------------------------------- extraction
SEUIL_OCR_MIN = 20  # sous ce nombre de caracteres, on suppose qu'il n'y a pas de vraie couche texte


def pdf_pages(path):
    d = pymupdf.open(path)
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


READERS = {".pdf": pdf_pages, ".docx": docx_pages, ".pptx": pptx_pages, ".xlsx": xlsx_pages}


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
    import unicodedata
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
# code_doc, chemin source, titre metier, categorie, date de reference, statut
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

# doublons ecartes : chemin -> (code du document canonique retenu, motif)
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


# Fichiers presents dans a/ ou b/ mais volontairement hors du registre : ils
# servent de SOURCE a la banque de reponses, pas de document a citer. Les
# declarer ici reconcilie le compte (49 fichiers = 38 canoniques + 10 doublons
# + 1 source) et evite qu'on croie plus tard a un oubli.
SOURCES_SEULEMENT = {
    "a/LOGICLEVER SASU_EV_Questionnaire_spreadsheet.xlsx":
        "source des 30 reponses EcoVadis (build_answer_bank.py) ; son jumeau PDF "
        "EV-QUEST est, lui, indexe et citable",
}


def main():
    docs, seen = [], {}
    for entree in REGISTRY:
        code, rel, titre, cat, date_ref, statut = entree[:6]
        langue_source = entree[6] if len(entree) > 6 else "fr"
        path = resolve(rel)
        if not os.path.exists(path):
            print("MANQUANT :", rel)
            continue
        pages = read_any(path, code, langue_source)
        full = "\n".join(pages)
        h = hashlib.md5(full.encode("utf8", "ignore")).hexdigest()
        if h in seen:
            print("  ! %s a le meme contenu que %s" % (code, seen[h]))
        seen[h] = code
        docs.append({
            "code_doc": code, "titre": titre, "categorie": cat,
            "fichier_source": rel.replace("\\", "/"), "date_reference": date_ref,
            "statut": statut, "langue_source": langue_source,
            "nb_pages": len(pages), "md5": h, "pages": pages,
        })
    reg = {
        "documents": docs,
        "doublons_ecartes": [
            {"fichier": k.replace("\\", "/"), "canonique": v[0], "motif": v[1]}
            for k, v in DOUBLONS.items()
        ],
    }
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "documents.json"), "w", encoding="utf8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)
    print("\n%d documents canoniques, %d doublons ecartes" % (len(docs), len(DOUBLONS)))
    print("%d pages au total" % sum(d["nb_pages"] for d in docs))
    traduits = [d["code_doc"] for d in docs if d["langue_source"] == "en"]
    print("%d documents traduits depuis l'anglais : %s"
          % (len(traduits), ", ".join(traduits)))
    print("%d fichier(s) utilise(s) comme source seulement, non indexe(s)"
          % len(SOURCES_SEULEMENT))

    # Tout fichier de a/ ou b/ doit etre soit indexe, soit declare doublon, soit
    # declare source. Tout le reste est un oubli, et on le signale.
    nfc = lambda x: unicodedata.normalize("NFC", x)
    connus = ({nfc(d["fichier_source"]) for d in docs}
              | {nfc(k.replace(chr(92), "/")) for k in DOUBLONS}
              | {nfc(k) for k in SOURCES_SEULEMENT})
    sur_disque = {nfc(dossier + "/" + f)
                  for dossier in ("a", "b")
                  for f in os.listdir(os.path.join(BASE, dossier))}
    oublies = sorted(sur_disque - connus)
    if oublies:
        print("")
        print("ATTENTION - fichier(s) ni indexe(s) ni declare(s) :")
        for o in oublies:
            print("   ", o)
    else:
        print("%d fichiers de a/ et b/ : tous indexes, doublons ou sources"
              % len(sur_disque))


main()
