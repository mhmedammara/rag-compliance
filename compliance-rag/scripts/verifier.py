# -*- coding: utf-8 -*-
"""
Garde-fou unique du projet. A lancer apres toute modification du corpus,
du glossaire ou du noeud « Normaliser les questions ».

    python scripts/verifier.py

Quatre controles :
  1. detection de la colonne des questions, rejouee sur les trois classeurs
     reels (le code JavaScript du workflow est execute tel quel, hors de n8n) ;
  2. absence d'anglais residuel dans le corpus francise ;
  3. absence d'entree de glossaire jamais utilisee (faute de frappe
     dans une cle : la traduction ne s'appliquerait jamais) ;
  4. pourcentages des indicateurs exprimes lisiblement, et concordants
     avec le Rapport RSE.

Sortie en code 1 des qu'un controle echoue.
"""
import json
import os
import re
import sys
import unicodedata

import dukpy
import openpyxl

sys.stdout.reconfigure(encoding="utf-8")

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(ICI)
BASE = os.path.dirname(RACINE)
SEED = os.path.join(RACINE, "seed")
WF = os.path.join(RACINE, "workflows", "logiclever_reponses_questionnaires.json")


def resoudre(rel):
    """Tolere les formes Unicode NFC / NFD des noms de fichiers OneDrive."""
    dossier, nom = os.path.split(rel.replace("\\", "/"))
    d = os.path.join(BASE, dossier)
    cible = unicodedata.normalize("NFC", nom)
    for f in os.listdir(d):
        if unicodedata.normalize("NFC", f) == cible:
            return os.path.join(d, f)
    return os.path.join(BASE, rel)


# =====================================================================
#  1. Detection de la colonne des questions
# =====================================================================
# chemin, feuille, libelle, debuts attendus de la 1re question detectee.
# La feuille EcoVadis est le cas piege : sa colonne de REPONSES est plus longue
# que sa colonne de questions. C'est elle qui justifie que estQuestion reste
# strict — le score de colonne, lui, a pu etre reduit a un simple comptage.
CAS = [
    ("EXemples remplies de formulaires clients/"
     "[Logiclever] [V0] - Questionnaire RSE New Camilla_OK_06062025.xlsx",
     "Questionnaire RSE", "TotalEnergies / New Camilla", ("Merci de", "Quel")),
    ("EXemples remplies de formulaires clients/SBTi_Questions pour fournisseurs.xlsx",
     "CGI", "CGI / SBTi", ("Votre organisation",)),
    ("a/LOGICLEVER SASU_EV_Questionnaire_spreadsheet.xlsx",
     "Questionnaire", "EcoVadis (colonne piege)",
     ("Veuillez", "Votre entreprise", "Lequel")),
]


def lignes_n8n(chemin, feuille):
    """Reproduit la sortie du noeud « Lire le fichier » (headerRow: false)."""
    wb = openpyxl.load_workbook(resoudre(chemin), data_only=True)
    out = []
    for row in wb[feuille].iter_rows(values_only=True):
        o = {str(i): " ".join(str(c).split())
             for i, c in enumerate(row) if c is not None and str(c).strip()}
        if o:
            out.append({"json": o})
    wb.close()
    return out


def controle_detection():
    wf = json.load(open(WF, encoding="utf8"))
    code = [x for x in wf["nodes"]
            if x["name"] == "Normaliser les questions"][0]["parameters"]["jsCode"]
    anomalies = 0
    print("1. Detection de la colonne des questions")
    for chemin, feuille, libelle, debuts in CAS:
        items = lignes_n8n(chemin, feuille)
        ctx = dukpy.JSInterpreter()
        ctx.evaljs("var __items = %s;" % json.dumps(items, ensure_ascii=False))
        try:
            res = ctx.evaljs("""
            var $input = { all: function () { return __items; },
                           first: function () { return __items[0]; } };
            var $ = function () { return { isExecuted: false,
                                           first: function () { return { json: {} }; },
                                           all: function () { return __items; } }; };
            (function () { %s })();
            """ % code)
        except Exception as e:
            print("   ECHEC  %-28s %s" % (libelle, str(e)[:150]))
            anomalies += 1
            continue
        # la bonne colonne se reconnait au debut de sa premiere question :
        # si le scorer se trompe de colonne, on lit une reponse, pas une question
        ok = len(res) >= 5 and res[0]["json"]["question"].startswith(debuts)
        print("   %s %-28s %d lignes -> %d questions | %s"
              % ("OK    " if ok else "ANOMALIE", libelle, len(items), len(res),
                 res[0]["json"]["question"][:42] if res else "vide"))
        if not ok:
            anomalies += 1
        for x in res[:2]:
            print("          l.%-4s %s" % (x["json"]["ligne"], x["json"]["question"][:78]))
    return anomalies


# =====================================================================
#  2 et 3. Francisation
# =====================================================================
OUTILS = re.compile(r"\b(the|and|of|is|are|how|your|with|for|that|this|from|"
                    r"have|has|been|will|which|their|they)\b", re.I)


def densite_anglaise(texte):
    mots = re.findall(r"[A-Za-z']+", texte)
    if len(mots) < 12:
        return 0.0
    return len(OUTILS.findall(texte)) / len(mots)


def controle_francisation():
    anomalies = 0
    gloss = json.load(open(os.path.join(SEED, "traductions_cybervadis.json"),
                           encoding="utf8"))
    chaines = gloss["chaines"]

    print("\n2. Anglais residuel dans le corpus")
    suspects = []

    docs = json.load(open(os.path.join(SEED, "documents.json"), encoding="utf8"))
    for d in docs["documents"]:
        for i, page in enumerate(d["pages"], 1):
            if densite_anglaise(page) > 0.09:
                suspects.append("document %s page %d" % (d["code_doc"], i))

    bank = json.load(open(os.path.join(SEED, "answer_bank.json"), encoding="utf8"))
    for e in bank:
        # question_source contient volontairement l'original anglais
        for champ in ("question", "reponse"):
            if densite_anglaise(e.get(champ) or "") > 0.09:
                suspects.append("%s (%s)" % (e["source_ref"], champ))

    if suspects:
        anomalies += 1
        print("   ANOMALIE %d passages restent en anglais :" % len(suspects))
        for s in suspects[:12]:
            print("            - %s" % s)
    else:
        print("   OK     aucun passage anglais detecte")

    print("\n3. Entrees de glossaire inutilisees")
    corpus = "\n".join(p for d in docs["documents"] for p in d["pages"])
    corpus += "\n" + "\n".join((e.get("question") or "") + "\n" + (e.get("reponse") or "")
                               + "\n" + (e.get("sous_theme") or "") for e in bank)
    # une traduction est « utilisee » si sa valeur francaise apparait dans le corpus
    inutilisees = [k for k, v in chaines.items()
                   if len(v) > 12 and v not in corpus]
    if inutilisees:
        anomalies += 1
        print("   ANOMALIE %d entrees ne servent jamais (cle mal orthographiee ?) :"
              % len(inutilisees))
        for k in inutilisees[:12]:
            print("            - %s" % k[:96])
    else:
        print("   OK     les %d entrees longues du glossaire sont toutes utilisees"
              % sum(1 for v in chaines.values() if len(v) > 12))
    return anomalies


# =====================================================================
#  4. Pourcentages des indicateurs
# =====================================================================
# Excel stocke une cellule formatee « 0% » sous forme de fraction : 0.37 pour
# 37 %. Si la fraction brute arrive au modele, il ecrit « 0,37 % » — une erreur
# d'un facteur 100 dans une reponse client, que ni le controle de veracite (le
# dossier contient la meme fraction) ni le controle de coherence (il ne voit que
# les reponses) ne peuvent rattraper.
POURCENTAGE = re.compile(r"%|pourcentage|taux", re.I)

# Valeurs publiees dans le Rapport RSE 2025 : deux sources independantes du meme
# chiffre doivent concorder. C'est le controle qu'aucune heuristique ne simule.
CONCORDANCE = [
    ("% de salaries ayant suivi la sensibilisation", "2025", "37 %"),
    ("% de salaries ayant obtenu la certification", "2025", "8 %"),
    ("Participation au Teletravail", "2025", "100 %"),
]


def sans_accents(t):
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()


def controle_pourcentages():
    anomalies = 0
    kpi = json.load(open(os.path.join(SEED, "kpi.json"), encoding="utf8"))

    print("\n4. Pourcentages des indicateurs")
    fractions = []
    for e in kpi:
        if not POURCENTAGE.search(e["indicateur"]):
            continue
        for annee, v in e["valeurs"].items():
            if v is None:
                continue
            texte = str(v).strip()
            if re.fullmatch(r"-?0[.,]\d+|-?[01][.,]0+", texte):
                fractions.append("%s (%s) = %s" % (e["indicateur"][:56], annee, texte))
    if fractions:
        anomalies += 1
        print("   ANOMALIE %d valeurs restent en fraction brute :" % len(fractions))
        for f in fractions[:10]:
            print("            - %s" % f)
    else:
        print("   OK     aucune fraction brute sur un indicateur en pourcentage")

    index = {sans_accents(e["indicateur"]).lower(): e for e in kpi}
    ecarts = []
    for libelle, annee, attendu in CONCORDANCE:
        cle = sans_accents(libelle).lower()
        e = next((v for k, v in index.items() if k.startswith(cle)), None)
        if e is None:
            ecarts.append("%s : indicateur absent" % libelle)
            continue
        obtenu = str(e["valeurs"].get(annee, "")).strip()
        if obtenu != attendu:
            ecarts.append("%s (%s) : %s attendu, %s obtenu"
                          % (libelle, annee, attendu, obtenu or "vide"))
    if ecarts:
        anomalies += 1
        print("   ANOMALIE discordance avec le Rapport RSE 2025 :")
        for x in ecarts:
            print("            - %s" % x)
    else:
        print("   OK     les %d valeurs recoupees concordent avec le Rapport RSE"
              % len(CONCORDANCE))
    return anomalies


def main():
    total = (controle_detection() + controle_francisation()
             + controle_pourcentages())
    print("\n%s" % ("Tous les controles passent."
                    if total == 0 else "%d controle(s) en anomalie." % total))
    return total


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
