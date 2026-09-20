# -*- coding: utf-8 -*-
"""
Construit la base de reponses validees (answer_bank) et la table d'indicateurs (kpi)
a partir de :
  - le questionnaire EcoVadis renseigne  (Q -> options retenues -> document + page)
  - les questionnaires clients deja remplis (TotalEnergies / New Camilla, CGI / SBTi)
  - le rapport de performance CyberVadis (score par controle)
  - la liste des indicateurs de performance extra-financiere (valeurs 2023/2024/2025)

Sorties : seed/answer_bank.json, seed/kpi.json
"""
import openpyxl, os, sys, re, json, unicodedata

sys.stdout.reconfigure(encoding="utf-8")

BASE = r"C:\Users\Moham\OneDrive\Desktop\Docs pour RAG"
OUT = os.path.join(BASE, "compliance-rag", "seed")


def resolve(rel):
    rel = rel.replace("\\", "/")
    direct = os.path.join(BASE, rel)
    if os.path.exists(direct):
        return direct
    folder, name = os.path.split(rel)
    d = os.path.join(BASE, folder)
    target = unicodedata.normalize("NFC", name)
    for f in os.listdir(d):
        if unicodedata.normalize("NFC", f) == target:
            return os.path.join(d, f)
    return direct


_gloss = json.load(open(os.path.join(OUT, "traductions_cybervadis.json"),
                        encoding="utf8"))
CHAINES = _gloss["chaines"]


def fr(valeur):
    """Traduction exacte via le glossaire CyberVadis ; chaine inchangee sinon.
    Les oublis sont signales par scripts/verifier_traduction.py."""
    if valeur is None:
        return ""
    v = " ".join(str(valeur).replace("_x000D_", " ").split())
    return CHAINES.get(v, v)


def fr_sous_theme(libelle):
    """'IDENTIFY / COMPLIANCE' -> 'IDENTIFIER / CONFORMITE'"""
    return " / ".join(fr(x.strip()) for x in (libelle or "").split("/") if x.strip())


def clean(v):
    if v is None:
        return ""
    s = " ".join(str(v).split())
    return "" if s in ("S/O", "N/A", "-") else s


def rows_of(path, sheet):
    wb = openpyxl.load_workbook(resolve(path), data_only=True)
    rows = list(wb[sheet].iter_rows(values_only=True))
    wb.close()
    return rows


def pourcentage_fr(valeur):
    """0.37 -> '37 %'   1.0 -> '100 %'   0.1666666667 -> '16,67 %'
    Virgule decimale francaise, 2 decimales au plus, zeros de fin supprimes."""
    x = round(float(valeur) * 100, 2)
    texte = ("%.2f" % x).rstrip("0").rstrip(".")
    return texte.replace(".", ",") + " %"


EST_POURCENTAGE = re.compile(r"%|pourcentage|taux", re.I)
NOMBRE_NU = re.compile(r"^-?\d+(?:[.,]\d+)?$")


def unite_pourcentage(indicateur, texte):
    """Ajoute l'unite manquante sur un indicateur en pourcentage dont la cellule
    n'etait pas formatee (« 15 » -> « 15 % », « 0.0 » -> « 0 % »).

    La magnitude n'est jamais modifiee : seul le libelle de l'unite est ajoute.
    Les valeurs deja converties par cellules_of contiennent « % » et sont donc
    laissees telles quelles."""
    if not texte or "%" in texte or not EST_POURCENTAGE.search(indicateur):
        return texte
    if not NOMBRE_NU.match(texte):
        return texte
    x = float(texte.replace(",", "."))
    return ("%.2f" % x).rstrip("0").rstrip(".").replace(".", ",") + " %"


def cellules_of(path, sheet):
    """Comme rows_of, mais conserve le format de cellule Excel.

    Indispensable pour les indicateurs : une cellule formatee « 0% » est
    stockee par Excel sous forme de fraction (0.37 pour 37 %). Sans le format,
    la valeur brute partirait telle quelle dans le dossier de preuves et le
    modele ecrirait « 0,37 % » — une erreur d'un facteur 100.

    Le format est le seul signal fiable : « Nombre d'evaluations RSE = 1.0 » est
    au format General (un comptage) alors que « Participation au Teletravail
    = 1.0 » est au format 0% (bien 100 %). Le libelle ne permet pas de trancher.
    """
    wb = openpyxl.load_workbook(resolve(path), data_only=True)
    rows = []
    for row in wb[sheet].iter_rows():
        ligne = []
        for cell in row:
            v = cell.value
            if isinstance(v, (int, float)) and "%" in (cell.number_format or ""):
                ligne.append(pourcentage_fr(v))
            else:
                ligne.append(v)
        rows.append(ligne)
    wb.close()
    return rows


# ------------------------------------------------------- 1. EcoVadis
# Structure : une question occupe un bloc de lignes.
# Colonnes A-I ne sont remplies que sur la 1re ligne du bloc (theme, code, libelle).
# Chaque ligne suivante = une option de reponse (J) ; M="Oui" si retenue ;
# N = nom du document justificatif, O = commentaire/reponse, P = pages.
COL = dict(theme=0, indicateur=2, nom=4, code=5, question=8, option=9,
           retenu=12, doc=13, commentaire=14, pages=15, validite=16)


def parse_ecovadis():
    rows = rows_of(r"a\LOGICLEVER SASU_EV_Questionnaire_spreadsheet.xlsx", "Questionnaire")[1:]
    blocs, cur = [], None
    for r in rows:
        g = lambda k: clean(r[COL[k]]) if COL[k] < len(r) else ""
        if g("code"):                                   # nouvelle question
            cur = {"code": g("code"), "theme": g("theme"), "indicateur": g("indicateur"),
                   "nom": g("nom"), "question": g("question"), "preuves": []}
            blocs.append(cur)
        if cur is None:
            continue
        if g("retenu").lower().startswith("oui") or g("doc") or g("commentaire"):
            option, doc = g("option"), g("doc")
            comm = g("commentaire")
            # les feuilles EcoVadis prefixent les commentaires par ces libelles
            comm = re.sub(r"^(Mes commentaires sur le document|Ma r.ponse)\s*:?\s*", "", comm)
            if not (doc or comm):
                continue
            cur["preuves"].append({
                "option": option, "document": doc, "commentaire": comm,
                "pages": g("pages"), "validite": g("validite"),
                "retenu": g("retenu").lower().startswith("oui"),
            })

    entries = []
    for b in blocs:
        preuves = [p for p in b["preuves"] if p["retenu"] or p["commentaire"]]
        if not preuves:
            continue
        # reponse consolidee : options retenues + commentaires distincts
        options = [p["option"] for p in preuves if p["retenu"] and p["option"]]
        comms, vus = [], set()
        for p in preuves:
            c = p["commentaire"]
            if c and c[:80] not in vus:
                vus.add(c[:80])
                comms.append(c)
        reponse = ""
        if options:
            reponse += "Elements retenus : " + " ; ".join(dict.fromkeys(options)) + ".\n"
        reponse += "\n".join(comms)
        sources = [{"document": p["document"], "pages": p["pages"], "option": p["option"]}
                   for p in preuves if p["document"]]
        entries.append({
            "source_ref": "ECOVADIS/" + b["code"],
            "origine": "ecovadis",
            "theme": b["theme"], "sous_theme": b["indicateur"],
            "question": b["question"] or b["nom"],
            "intitule": b["nom"],
            "reponse": reponse.strip(),
            "sources": sources,
            "fiabilite": "validee_ecovadis",
        })
    return entries


# ------------------------------------------------------- 2. formulaires clients
def parse_new_camilla():
    """Questionnaire RSE TotalEnergies / New Camilla. Le tableau demarre en colonne C (index 2).
    Une question occupe 2 lignes : la ligne 'N deg' porte les entetes de reponse,
    la ligne suivante porte les valeurs saisies."""
    rows = rows_of("EXemples remplies de formulaires clients/"
                   "[Logiclever] [V0] - Questionnaire RSE New Camilla_OK_06062025.xlsx",
                   "Questionnaire RSE")
    ENTETES = {"oui / non", "organisme", "indice", "note obtenue", "justification",
               "score obtenu", "annee n", "annee 2030", "annee x", "production",
               "emballage", "logistique", "maritime", "aerien", "train",
               "au sein de votre entreprise", "au sein de la prestation",
               "langage utilise (ex python, java, ruby)", "reponse"}

    def norm(x):
        return unicodedata.normalize("NFKD", x.lower()).encode("ascii", "ignore").decode()

    out, section = [], ""
    for i, r in enumerate(rows):
        c = [clean(x) for x in r] + [""] * 25
        if c[2].lower().startswith("section"):
            section = c[2]
            continue
        num = c[2]
        if not re.fullmatch(r"\d+(\.0)?", num):
            continue
        num = num.replace(".0", "")
        thematique, question = c[3], c[4]
        if not question:
            continue
        vals = []
        for cell in c[5:]:
            if cell and norm(cell) not in ENTETES:
                vals.append(cell)
        if i + 1 < len(rows):
            n = [clean(x) for x in rows[i + 1]] + [""] * 25
            if not re.fullmatch(r"\d+(\.0)?", n[2]):
                for cell in n[2:]:
                    if cell and norm(cell) not in ENTETES:
                        vals.append(cell)
        vals = [v for v in dict.fromkeys(vals) if v]
        if not vals:
            continue
        out.append({
            "source_ref": "CLIENT/NEW-CAMILLA/Q%s" % num,
            "origine": "formulaire_client",
            "theme": thematique or section, "sous_theme": section,
            "question": question, "intitule": "Questionnaire RSE TotalEnergies (New Camilla)",
            "reponse": " | ".join(vals),
            "sources": [], "fiabilite": "reponse_envoyee_client",
        })
    return out


def parse_sbti():
    rows = rows_of("EXemples remplies de formulaires clients/"
                   "SBTi_Questions pour fournisseurs.xlsx", "CGI")
    out = []
    for r in rows:
        q = clean(r[0] if len(r) > 0 else "")
        a = clean(r[1] if len(r) > 1 else "")
        if not q or not a or q.lower() == "question" or len(q) < 12:
            continue
        out.append({
            "source_ref": "CLIENT/CGI-SBTI",
            "origine": "formulaire_client",
            "theme": "Environnement", "sous_theme": "Climat / SBTi",
            "question": q, "intitule": "Évaluation climat & décarbonation fournisseur (CGI)",
            "reponse": a, "sources": [], "fiabilite": "reponse_envoyee_client",
        })
    return out


# ------------------------------------------------------- 3. CyberVadis
def parse_cybervadis():
    """Feuille 'Scores' : Function|Criteria|Question(code)|Question description|Score
    Feuille 'Performance report' : detail des controles evalues + documents de preuve."""
    out = []
    fonction = critere = ""
    scores = {}
    for r in rows_of(r"a\Performance report - logiclever - V29 - 21Oct2025.xlsx", "Scores")[1:]:
        c = [clean(x) for x in r] + [""] * 6
        fn, cr, code, desc, score = c[0], c[1], c[2], c[3], c[4].replace(".0", "")
        if fn:
            fonction = fn
            continue
        if cr:
            critere = cr
            continue
        if not code:
            continue
        scores[code] = score
        out.append({
            "source_ref": "CYBERVADIS/" + code,
            "origine": "cybervadis",
            "theme": "Cybersécurité",
            "sous_theme": fr_sous_theme("%s / %s" % (fonction, critere)),
            "question": fr(desc),
            "question_source": desc,
            "intitule": "Contrôle CyberVadis %s" % code,
            "reponse": "Contrôle évalué par CyberVadis (rapport du 21/10/2025). "
                       "Score obtenu : %s/1000. Score global Logiclever : 860/1000 "
                       "(niveau Mature ; moyenne des entreprises évaluées : 654)." % score,
            "sources": [{"document": "CV-PERF-2025", "pages": "", "option": ""}],
            "fiabilite": "validee_cybervadis",
        })

    # detail des mesures declarees + preuves fournies, regroupe par code de question
    detail = {}
    for r in rows_of(r"a\Performance report - logiclever - V29 - 21Oct2025.xlsx",
                     "Performance report")[1:]:
        c = [clean(x) for x in r] + [""] * 20
        code, quest, control = c[3], c[4], c[5]
        implemente, resultat, preuve = c[6], c[8], c[14]
        point_fort = c[11]
        if not code or not control:
            continue
        d = detail.setdefault(code, {
            "question": fr(quest), "question_source": quest,
            "fonction": fr(c[0]), "critere": fr(c[1]), "focus": fr(c[2]),
            "controles": [], "points_forts": [], "preuves": set()})
        d["controles"].append(
            "- %s (déclarée mise en œuvre : %s ; résultat de l'évaluation : %s)"
            % (fr(control), fr(implemente) or "sans objet", fr(resultat) or "sans objet"))
        if point_fort:
            pf = fr(point_fort)
            if pf not in d["points_forts"]:
                d["points_forts"].append(pf)
        # "_x000D_" est l'artefact qu'openpyxl laisse pour un retour chariot
        # dans une cellule Excel ; sans le neutraliser, et avec "." autorise
        # dans le corps du nom de fichier, le "+" glouton de la regex avale
        # tout jusqu'au DERNIER ".pdf" de la cellule et colle plusieurs noms de
        # fichiers ensemble (ex. preuve fournie deux fois de suite). "." est
        # volontairement exclu du corps : un nom de fichier ne peut alors plus
        # jamais "sauter" par-dessus un ".pdf" deja rencontre.
        preuve_nette = (preuve or "").replace("_x000D_", " ")
        for m in re.findall(r"[\[\]\w\-\_ À-ſ]+\.pdf", preuve_nette):
            d["preuves"].add(m.strip())

    for code, d in detail.items():
        corps = ("Mesures déclarées par Logiclever et validées lors de l'évaluation "
                 "CyberVadis 2025 (score du contrôle : %s/1000) :\n%s"
                 % (scores.get(code, "sans objet"), "\n".join(d["controles"])))
        if d["points_forts"]:
            # Les points forts constates par l'evaluateur sont l'element le plus
            # directement reutilisable face a un acheteur : on les met en avant.
            corps += ("\n\nPoints forts constatés par l'évaluateur :\n"
                      + "\n".join("- " + pf for pf in d["points_forts"]))
        out.append({
            "source_ref": "CYBERVADIS-DETAIL/" + code,
            "origine": "cybervadis",
            "theme": "Cybersécurité",
            "sous_theme": " / ".join(x for x in (d["fonction"], d["critere"], d["focus"]) if x),
            "question": d["question"],
            "question_source": d["question_source"],
            "intitule": "Mesures déclarées et validées - %s" % code,
            "reponse": corps,
            "sources": ([{"document": "CV-PERF-2025", "pages": "", "option": ""}] +
                        [{"document": p, "pages": "", "option": "preuve fournie a CyberVadis"}
                         for p in sorted(d["preuves"])]),
            "fiabilite": "validee_cybervadis",
        })
    return out


# ------------------------------------------------------- 4. indicateurs (KPI)
def parse_kpi():
    out = []
    # cellules_of (et non rows_of) : le format Excel identifie les pourcentages
    rows = cellules_of("a/LOGICLEVER RSE - Liste des indicateurs de performance "
                       "extra-financi\u00e8re.xlsx", "2024 2025")
    # entete en ligne 2 : Theme|Contribue a|Enjeux|Indicateur|GRI|Objectif|Methode|2023|2024|2025|statut|...
    for r in rows[2:]:
        c = [clean(x) for x in r] + [""] * 14
        theme, contrib, enjeu, indic, gri, objectif, methode = c[0], c[1], c[2], c[3], c[4], c[5], c[6]
        v23, v24, v25, statut = c[7], c[8], c[9], c[10]
        if not indic:
            continue
        # cellules non formatees : l'unite manque, la valeur est deja en points
        v23, v24, v25 = (unite_pourcentage(indic, v23), unite_pourcentage(indic, v24),
                         unite_pourcentage(indic, v25))
        objectif = unite_pourcentage(indic, objectif)
        out.append({
            "theme": theme, "contribue_a": contrib, "enjeu_materiel": enjeu,
            "indicateur": indic, "norme_gri": gri, "objectif": objectif,
            "methode": methode,
            "valeurs": {"2023": v23, "2024": v24, "2025": v25},
            "statut": statut,
            "source_document": "KPI-RSE",
            "date_maj": "2026-02-22",
        })
    # arrets maladie
    for lib, v23, v24, v25 in [
        ("Nombre de jours d'arret maladie d'origine non professionnelle", "105", "179", "485"),
        ("Nombre de jours d'arret maladie d'origine professionnelle", "0", "0", "0"),
    ]:
        out.append({
            "theme": "Labor & Human Rights", "contribue_a": "Sante et Securite",
            "enjeu_materiel": "Fidelisation et satisfaction des salaries",
            "indicateur": lib, "norme_gri": "GRI 403-9", "objectif": "",
            "methode": "Suivi RH", "valeurs": {"2023": v23, "2024": v24, "2025": v25},
            "statut": "ok", "source_document": "KPI-MALADIE", "date_maj": "2026-02-22",
        })
    return out


# ------------------------------------------------------- 5. faits socles
# Chiffres cles verifies dans les documents, avec la citation exacte.
FAITS = [
    ("FAIT/GES-SCOPES", "Environnement", "Bilan carbone",
     "Quel est votre dernier bilan carbone (scopes 1, 2 et 3) ?",
     "Dernier bilan carbone réalisé : année 2023, publié le 13/01/2025, réalisé avec Greenly "
     "sur les 3 scopes (méthode GHG Protocol, contrôle opérationnel).\n"
     "- Scope 1 : < 0,1 tCO2e\n- Scope 2 : < 0,1 tCO2e\n- Scope 3 : 183 tCO2e (100 % comptabilisé)\n"
     "- Total : 183 tCO2e, soit 1,6 tCO2e par collaborateur et 14 tCO2e par M€ de chiffre d'affaires.\n"
     "Principaux postes : déplacements 72 tCO2e (39 %), nourriture et boissons 55 tCO2e (30 %), "
     "achats de services 20 tCO2e (11 %), activités et événements 10 tCO2e (5,5 %).\n"
     "Le bilan précédent portait sur 2021 (1,8 tCO2e/collaborateur) : la trajectoire représente "
     "-11,11 % par collaborateur. Fréquence : tous les deux ans (prochain exercice 2026).",
     [("GES-2023", "12"), ("GES-2023", "14"), ("GES-2023", "50-53"), ("GES-2021", "1")]),

    ("FAIT/GES-OBJECTIFS", "Environnement", "Bilan carbone",
     "Quels sont vos objectifs de réduction des émissions de GES ?",
     "Objectif de réduction de 2 % par an et par collaborateur sur les émissions totales, "
     "et réduction absolue de 2 % par an sur les scopes 1, 2 et 3. "
     "Horizon annoncé aux clients : scope 3 < 120 tCO2e en 2030, neutralité carbone visée en 2030. "
     "Logiclever connaît l'initiative SBTi mais n'a pas d'objectif approuvé SBTi à ce jour "
     "(démarche à l'étude, trajectoires déjà mesurées et auditées).",
     [("GES-2023", "29"), ("KPI-RSE", "1")]),

    ("FAIT/CERTIFICATIONS", "General", "Certifications",
     "De quelles certifications et labels disposez-vous ? Avez-vous l'ISO 9001 / ISO 14001 / ISO 27001 ?",
     "Évaluations et labels détenus par Logiclever :\n"
     "- EcoVadis : score global 79/100, 98e percentile (top 2 % des entreprises évaluées).\n"
     "- CyberVadis : 860/1000, niveau Mature (benchmark moyen 654), rapport du 21/10/2025.\n"
     "- Great Place To Work : entreprise certifiée (3 certifications), Trust Index 96 %, "
     "perception globale 98 %, 2e au classement France 2023 des entreprises de 50 à 250 salariés, "
     "1er au classement France 2021 des entreprises de moins de 50 salariés.\n"
     "- Signataire de la Charte Numérique Responsable de l'INR, de la Charte engagement citoyen "
     "des entreprises et de la Charte régionale des valeurs de la République et de la laïcité.\n"
     "- Respect des 10 principes du Pacte Mondial des Nations Unies.\n"
     "- Convention collective Syntec (IDCC 1486).\n"
     "IMPORTANT : Logiclever n'est PAS certifiée ISO 9001, ISO 14001 ni ISO 27001. "
     "La Politique SSI est construite sur le référentiel ISO 27001 et un objectif de certification "
     "y est inscrit, mais la certification n'est pas obtenue à ce jour. "
     "Ne jamais répondre « oui » à une question de certification ISO.",
     [("CV-EXEC-2025", "1"), ("GPTW-CERT", "1"), ("POL-SSI", "3-4")]),

    ("FAIT/IDENTITE", "General", "Informations entreprise",
     "Quelles sont les informations générales sur votre entreprise (activité, effectif, sites) ?",
     "Logiclever SASU, sans société mère ni groupe. Activité unique : conseil en systèmes "
     "d'information (ESN / conseil IT). Effectif : 50 salariés, répartis sur 2 sites "
     "(48 à Paris, 2 à Lyon). Aucune activité de production, d'emballage ou de logistique. "
     "Entreprise libérée depuis 2018. Environ 90 % du chiffre d'affaires est réalisé avec des "
     "clients du secteur de l'énergie. Contact RSE / ESG : Mohammed Ammara et Isselt Champion.",
     [("EV-QUEST", "1"), ("MATERIALITE", "1"), ("GOUVERNANCE", "2")]),

    ("FAIT/FOURNISSEURS", "Achats responsables", "Controle des fournisseurs",
     "Quelles mesures avez-vous mises en place pour contrôler la conformité de vos fournisseurs "
     "et sous-traitants ?",
     "Dispositif de maîtrise de la chaîne d'approvisionnement :\n"
     "- Politique Achats responsables formalisée (V1.1, 16/09/2024) applicable à tous les "
     "fournisseurs, sous-traitants et partenaires.\n"
     "- Clauses RSE dans 100 % des nouveaux contrats fournisseurs, ainsi que des clauses de "
     "non-concurrence, de confidentialité, anti-blanchiment et anti-corruption.\n"
     "- Code de Conduite diffusé aux fournisseurs et freelances ; signature demandée.\n"
     "- Vérification KYC des freelances : taux de conformité 100 % (pièce d'identité et Kbis exigés "
     "avant tout contrat).\n"
     "- Évaluation SSI des fournisseurs formalisée dans un registre : niveau de dépendance "
     "(faible / modéré / critique), sensibilité des données, garanties et incidents, avec revue "
     "périodique (registre mis à jour le 01/05/2026).\n"
     "- Nombre de prestataires suspectés de recours au travail forcé ou au travail des enfants : 0.\n"
     "- Critère de neutralité carbone intégré à la qualification des fournisseurs.\n"
     "Point de vigilance : le taux de fournisseurs ayant effectivement signé le code de conduite "
     "est encore de 0 % (déploiement lancé en 2024) ; ne pas surdéclarer cet indicateur.",
     [("POL-ACHATS", "1-20"), ("SSI-FOURNISSEURS", "1"), ("CODE-CONDUITE", "1-5"),
      ("KPI-RSE", "1"), ("RSE-RAPPORT-2025", "8-9")]),

    ("FAIT/SSI", "Cybersecurite", "Securite des systemes d'information",
     "Quelle est votre organisation en matière de sécurité des systèmes d'information ?",
     "Politique de Sécurité des Systèmes d'Information (version E) formalisée, validée par la "
     "direction, appuyée sur les référentiels ISO 27001 et le RGPD, avec un RSSI et un DPO "
     "désignés et un comité de pilotage de la sécurité.\n"
     "- Revue annuelle de la politique SSI et de la politique RGPD (prochaine échéance 09/02/2027).\n"
     "- Analyse de risques SSI formalisée avec cotation criticité = impact x probabilité et "
     "plan de traitement (registre mis à jour le 15/12/2025).\n"
     "- Revue des accès par service tous les 2 à 6 mois selon l'outil (Google Workspace, Drive, "
     "Payfit, Boondmanager, Slack, Pennylane, Amundi).\n"
     "- 100 % des employés formés aux bonnes pratiques de cybersécurité en 2025 (58 % en 2024) ; "
     "formations dédiées à l'ingénierie sociale, au BYOD et à la gestion des données sensibles, "
     "avec suivi nominatif.\n"
     "- Sauvegardes automatiques et permanentes des données critiques (Google Workspace).\n"
     "- Nombre d'incidents de sécurité de l'information confirmés : 0 sur 2023, 2024 et 2025.\n"
     "- Évaluation externe CyberVadis : 860/1000 (Mature), scores de 1000/1000 sur la conformité "
     "légale, la protection des données personnelles, la gouvernance, l'analyse de risques et les RH.",
     [("POL-SSI", "1-39"), ("SSI-RISQUES", "1"), ("SSI-ACCES", "1"), ("SSI-REVUES", "1"),
      ("SSI-PLAN", "1"), ("CV-EXEC-2025", "1"), ("CV-PERF-2025", "1")]),

    ("FAIT/RGPD", "Cybersecurite", "Donnees personnelles",
     "Comment assurez-vous la conformité au RGPD ?",
     "Le RGPD est intégré à la Politique SSI (chapitre 3) : DPO désigné, registre des activités "
     "de traitement, principes de transparence, de minimisation et de durée de conservation "
     "raisonnable, encadrement des transferts à des tiers, et procédure de notification des "
     "violations de données personnelles. Revue annuelle de la politique RGPD par le DPO "
     "(prochaine échéance 09/02/2027). CyberVadis attribue 1000/1000 à l'ensemble des contrôles "
     "de protection des données personnelles (engagement, rôles, formation, cartographie des "
     "traitements, transparence, durée de conservation, transferts aux tiers, notification des "
     "violations).",
     [("POL-SSI", "6-8"), ("SSI-PLAN", "1"), ("CV-PERF-2025", "1")]),

    ("FAIT/GOUVERNANCE-RSE", "General", "Gouvernance RSE",
     "Comment est organisée la gouvernance RSE ? Avez-vous un référent ESG ?",
     "Logiclever est une entreprise libérée depuis 2018. La RSE est portée par un groupe de "
     "contribution RSE (COP) de 9 personnes, avec des référents identifiés "
     "(Mohammed Ammara, Isselt Champion) et un reporting annuel formalisé. "
     "La politique RSE est structurée en 4 volets : Social et droits de l'Homme, Environnement, "
     "Achats responsables, Éthique. Une analyse de matérialité a été réalisée (16/09/2024) et "
     "identifie 10 enjeux matériels. Les indicateurs suivent le référentiel GRI et sont publiés "
     "annuellement dans le Rapport RSE. 11 actions d'amélioration RSE mises en place en 2025.",
     [("GOUVERNANCE", "1-9"), ("MATERIALITE", "1"), ("MATRICE-MAT", "1"),
      ("RSE-RAPPORT-2025", "2"), ("KPI-RSE", "1")]),

    ("FAIT/ETHIQUE", "Ethique", "Lutte anti-corruption",
     "Quel est votre dispositif éthique et anti-corruption (Sapin II) ?",
     "Politique RSE D / Éthique (V2.0) et Code de Conduite formalisés et diffusés à 100 % des "
     "employés. Dispositif de signalement en place. Résultats 2025 : 0 incident de corruption "
     "confirmé, 0 cas de fraude, 0 cas de blanchiment, 0 conflit d'intérêt confirmé, "
     "0 cas de concurrence déloyale, 0 transaction financière inhabituelle détectée. "
     "Formation : 20 % des employés formés aux risques de corruption et 16,67 % à la lutte "
     "anti-fraude en 2025 (objectif 50 %) ; 33,33 % de la direction générale formée à la "
     "reconnaissance des pratiques de blanchiment (objectif 60 %). "
     "Les taux de formation restent en dessous des objectifs : ne pas les surdéclarer.",
     [("POL-ETHIQUE", "1-22"), ("CODE-CONDUITE", "1-5"), ("RSE-RAPPORT-2025", "9-10")]),

    ("FAIT/SOCIAL", "Social & Droits de l'Homme", "Conditions de travail",
     "Quels sont vos indicateurs sociaux (CDI, égalité, turnover, handicap) ?",
     "Indicateurs 2025 : 100 % des effectifs en CDI ; âge moyen 34,42 ans ; turnover 14 % "
     "(37,9 % en 2024) ; 100 % des employés payés au-delà du salaire minimum ; "
     "36,6 % de femmes dans l'entreprise (34 % en 2024) ; écart de rémunération homme-femme par "
     "grade compris entre -3,83 % et +5,84 % ; 2 % d'employés en situation de handicap ; "
     "0 situation de harcèlement détectée ; 0 prestataire suspecté de travail forcé ou "
     "de travail des enfants ; âge minimum du plus jeune salarié 24 ans. "
     "Absence de syndicat : PV de carence CSE. Budget formation consommé en 2025 : 39 687 € ; "
     "33 employés coachés en interne ; 100 % des employés ont reçu les politiques RSE. "
     "Convention collective Syntec (IDCC 1486). Télétravail accessible à 100 % des collaborateurs.",
     [("RSE-RAPPORT-2025", "3-5"), ("KPI-RSE", "1"), ("POL-SOCIAL", "1-25")]),

    ("FAIT/NUMERIQUE-RESPONSABLE", "Environnement", "Numerique responsable",
     "Quelles sont vos actions en matière de numérique responsable et d'écoconception ?",
     "Logiclever est signataire de la Charte Numérique Responsable de l'INR. "
     "Le SI a été évalué avec le référentiel général d'écoconception des services numériques "
     "(RGESN, numerique.gouv) : score global 77 %. "
     "Politique BYOD : 98 % des collaborateurs utilisent leur propre poste, ce qui évite l'achat "
     "et le renouvellement de matériel. Quantité de données hébergées : 82,84 Go (objectif < 1 To). "
     "Quantité de papier acheté : 0 €. Formation : 37 % des salariés sensibilisés au numérique "
     "responsable et 8 % certifiés (objectifs respectifs 75 % et 33 %) ; 35 formations "
     "Numérique Responsable suivies. Consultants formés à la sobriété numérique via les "
     "ressources MOOC de l'INR.",
     [("POL-ENV", "1-14"), ("RSE-RAPPORT-2025", "6-8"), ("KPI-RSE", "1")]),
]


def parse_faits():
    return [{
        "source_ref": ref, "origine": "fait_socle", "theme": theme, "sous_theme": sous,
        "question": q, "intitule": sous, "reponse": rep,
        "sources": [{"document": d, "pages": p, "option": ""} for d, p in srcs],
        "fiabilite": "verifiee_manuellement",
    } for ref, theme, sous, q, rep, srcs in FAITS]


# ------------------------------------------------------- 6. resolution des documents
def normaliser_pour_correspondance(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def construire_resolveur_documents():
    """Associe chaque reference brute de document (telle que tapee dans une
    cellule EcoVadis/CyberVadis) au code_doc reel du registre documents.json.

    Sans cette etape, sources[].document est copie tel quel depuis la feuille
    source : parfois deja un code_doc par coincidence, souvent un titre
    approximatif ou un nom de fichier brut/mal encode qui ne correspond a
    rien de connu (voir compliance-rag/GUIDE-DEPLOIEMENT.md pour l'historique
    des remappages faits a la main directement dans answer_bank.json, qu'une
    regeneration sans cette fonction ecraserait silencieusement). Trois
    passes, de la plus sure a la plus permissive : code exact, titre exact
    (normalise), puis inclusion partielle du titre dans le texte brut ou
    l'inverse."""
    with open(os.path.join(OUT, "documents.json"), encoding="utf8") as f:
        registre = json.load(f)["documents"]
    par_code = {d["code_doc"].lower(): d["code_doc"] for d in registre}

    # Certaines references brutes citent le nom du fichier source (souvent
    # l'acronyme du document : "Politique_SSI_Logiclever_E...") plutot que son
    # titre lisible (qui l'ecrit en toutes lettres : "...Systemes
    # d'Information"). Sans lexique commun entre les deux formes, ni
    # l'inclusion ni le recouvrement de mots ne peuvent les rapprocher : on
    # ajoute donc aussi le nom de fichier comme deuxieme libelle de
    # correspondance par document, en ignorant les fichiers du dossier "b/"
    # anonymises en UUID (b/<uuid>.pdf) qui n'apportent aucun signal.
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    libelles = []
    for d in registre:
        libelles.append((d["titre"], d["code_doc"]))
        nom = os.path.splitext(os.path.basename(d["fichier_source"]))[0]
        if not uuid_re.match(nom):
            libelles.append((nom, d["code_doc"]))

    par_titre = {}
    for texte, code in libelles:
        par_titre.setdefault(normaliser_pour_correspondance(texte), code)
    # Chaque cote (texte brut, libelle reel) porte souvent sa propre decoration
    # (prefixe "Politique RSE A /", suffixe "Logiclever 2026", version,
    # horodatage...) que l'inclusion stricte ne tolere pas si elle apparait
    # des DEUX cotes a la fois. Un recouvrement de mots significatifs (4+
    # lettres) est plus tolerant a cette decoration tout en restant sur : les
    # 4 politiques RSE (social/environnement/achats/ethique) partagent
    # "politique" mais se distinguent chacune par un mot-cle propre.
    titres_mots = [(normaliser_pour_correspondance(texte),
                     frozenset(w for w in normaliser_pour_correspondance(texte).split() if len(w) >= 4),
                     code) for texte, code in libelles]

    def resoudre(brut):
        """Renvoie (valeur, methode). methode vaut "code"/"titre"/"inclusion"
        pour une correspondance sure, "mots" pour un recouvrement partiel a
        double-verifier, ou "aucune" (valeur alors laissee telle quelle)."""
        b = (brut or "").strip()
        if not b:
            return b, "code"
        if b.lower() in par_code:
            return par_code[b.lower()], "code"
        bn = normaliser_pour_correspondance(b)
        if bn in par_titre:
            return par_titre[bn], "titre"
        for titre_norm, code in par_titre.items():
            if len(titre_norm) > 6 and (titre_norm in bn or bn in titre_norm):
                return code, "inclusion"
        mots_b = frozenset(w for w in bn.split() if len(w) >= 4)
        if mots_b:
            meilleur = None
            for _, mots_t, code in titres_mots:
                if not mots_t:
                    continue
                inter = mots_b & mots_t
                ratio = len(inter) / min(len(mots_b), len(mots_t))
                if len(inter) >= 3 and ratio >= 0.6 and (meilleur is None or ratio > meilleur[0]):
                    meilleur = (ratio, code)
            if meilleur:
                return meilleur[1], "mots"
        return b, "aucune"

    return resoudre


def resoudre_sources_bank(bank):
    """Reecrit sources[].document avec le code_doc reel quand on le trouve.
    Rapporte separement (sans rien faire echouer) : les references restees
    introuvables, et celles resolues par simple recouvrement de mots (la
    passe la plus permissive) pour verification humaine plutot que de les
    faire passer aussi silencieusement qu'une correspondance exacte."""
    resoudre = construire_resolveur_documents()
    non_resolues = {}
    a_verifier = {}
    for e in bank:
        for s in e.get("sources", []):
            brut = s.get("document")
            valeur, methode = resoudre(brut)
            s["document"] = valeur
            if methode == "aucune":
                non_resolues.setdefault(valeur, []).append(e["source_ref"])
            elif methode == "mots":
                a_verifier.setdefault((brut, valeur), []).append(e["source_ref"])
    return non_resolues, a_verifier


def main():
    ev, nc, sb, cv, ft = (parse_ecovadis(), parse_new_camilla(), parse_sbti(),
                          parse_cybervadis(), parse_faits())
    bank = ft + ev + nc + sb + cv
    for i, e in enumerate(bank, 1):
        e["id"] = i
    non_resolues, a_verifier = resoudre_sources_bank(bank)
    kpi = parse_kpi()
    os.makedirs(OUT, exist_ok=True)
    json.dump(bank, open(os.path.join(OUT, "answer_bank.json"), "w", encoding="utf8"),
              ensure_ascii=False, indent=1)
    json.dump(kpi, open(os.path.join(OUT, "kpi.json"), "w", encoding="utf8"),
              ensure_ascii=False, indent=1)
    print("answer_bank : %d entrees" % len(bank))
    for lib, n in [("faits socles", len(ft)), ("EcoVadis", len(ev)),
                   ("New Camilla", len(nc)), ("CGI/SBTi", len(sb)), ("CyberVadis", len(cv))]:
        print("   %-14s %d" % (lib, n))
    print("kpi        : %d indicateurs" % len(kpi))
    if non_resolues:
        total = sum(len(v) for v in non_resolues.values())
        print("\nATTENTION : %d reference(s) documentaire(s) sur les %d au total n'ont pas ete "
              "resolues vers un code_doc reel (%d valeur(s) brute(s) distincte(s)) - elles "
              "resteront affichees telles quelles au client et sans lien telechargeable :"
              % (total, sum(len(e.get("sources", [])) for e in bank), len(non_resolues)))
        for valeur, refs in sorted(non_resolues.items(), key=lambda kv: -len(kv[1]))[:30]:
            print("   [%dx] %s  (ex. %s)" % (len(refs), valeur[:100], refs[0]))
        if len(non_resolues) > 30:
            print("   ... et %d autre(s) valeur(s) distincte(s)." % (len(non_resolues) - 30))
    else:
        print("\nToutes les references documentaires ont ete resolues vers un code_doc reel.")
    if a_verifier:
        print("\nA VERIFIER : %d correspondance(s) trouvee(s) par recouvrement de mots "
              "(moins sures qu'un code ou un titre exact) :" % len(a_verifier))
        for (brut, code), refs in sorted(a_verifier.items(), key=lambda kv: -len(kv[1])):
            print("   %s  ->  %s  (ex. %s)" % (brut[:80], code, refs[0]))


main()
