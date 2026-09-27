# -*- coding: utf-8 -*-
"""
Rejoue les noeuds Code du workflow n8n hors de n8n, avec des donnees de la
meme forme que celles produites en production, et verifie le resultat.

Complement de verifier.py : celui-ci controle la detection des questions et la
francisation, celui-la controle la chaine de traitement d'une question de bout
en bout (dossier de preuves -> consolidation -> coherence -> classeur).

    python scripts/simuler_workflow.py
"""
import json
import os
import sys

import dukpy

sys.stdout.reconfigure(encoding="utf-8")

ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(ICI)
WF = os.path.join(RACINE, "workflows", "logiclever_reponses_questionnaires.json")


def noeuds_code():
    wf = json.load(open(WF, encoding="utf8"))
    return {x["name"]: x["parameters"]["jsCode"]
            for x in wf["nodes"] if x["type"] == "n8n-nodes-base.code"}


def executer(code, entree, contexte):
    """Execute un noeud Code avec $input / $json / $('Noeud') simules."""
    ctx = dukpy.JSInterpreter()
    ctx.evaljs("var __in = %s; var __ctx = %s;"
               % (json.dumps(entree, ensure_ascii=False),
                  json.dumps(contexte, ensure_ascii=False)))
    return ctx.evaljs("""
    var $input = {
      all: function () { return __in; },
      first: function () { return __in[0]; }
    };
    var $json = __in[0] ? __in[0].json : {};
    var __optionnels = ["Preparer la question"];
    var $ = function (nom) {
      var items = __ctx[nom];
      if (!items) {
        if (__optionnels.indexOf(nom) === -1) {
          throw new Error("Noeud absent du contexte simule : " + nom);
        }
        // Noeud legitimement non execute dans cette branche (le chat, hors
        // formulaire) : reproduit isExecuted=false comme le ferait n8n pour
        // un noeud jamais atteint. Tout autre nom absent est une vraie
        // erreur (typo, noeud renomme) et doit planter, pas s'effacer.
        return { isExecuted: false, item: undefined,
                 first: function () { return undefined; },
                 all: function () { return []; } };
      }
      return {
        isExecuted: true,
        item: items[0],
        first: function () { return items[0]; },
        all: function () { return items; }
      };
    };
    (function () { %s })();
    """ % code)


# ---------------------------------------------------------------- donnees
QUESTION = {"json": {
    "ligne": 7, "section": "Environnement", "client": "TotalEnergies",
    "intitule": "Questionnaire RSE", "consignes": "",
    "question": "Quel est votre dernier bilan carbone scope 1, 2 et 3 ?",
    "total": 2}}

ANALYSE = {"json": {"output": {
    "question_normalisee": "Quel est le dernier bilan carbone de Logiclever "
                           "sur les scopes 1, 2 et 3 ?",
    "sous_requetes": ["bilan emissions gaz a effet de serre scope 1 2 3 tCO2e",
                      "objectif reduction emissions trajectoire"],
    "type_reponse": "chiffre", "annee_cible": None,
    "points_de_vigilance": ["Pas d'objectif valide SBTi"]}}}

# forme exacte renvoyee par recherche_globale
RECHERCHE = {"json": {
    "candidats": [
        {"index": 1, "type": "reponse_validee", "reference": "FAIT/GES-SCOPES",
         "titre": "Bilan carbone", "page_debut": None, "page_fin": None,
         "question_source": None,
         "texte": "Dernier bilan carbone realise : annee 2023...",
         "sources": [{"document": "GES-2023", "pages": "12", "option": ""},
                     {"document": "GES-2023", "pages": "14", "option": ""}],
         "score": 0.0762},
        {"index": 2, "type": "extrait_document", "reference": "GES-2023",
         "titre": "Bilan d'emissions de GES 2023 (Greenly)",
         "page_debut": 12, "page_fin": 13, "question_source": None,
         "texte": "Resume executif Scope 2 < 0.1tCO2e 183tCO2e...",
         "sources": None, "score": 0.0431},
        {"index": 3, "type": "indicateur", "reference": "KPI-RSE",
         "titre": "Reduction des Emissions de GES", "page_debut": None,
         "page_fin": None, "question_source": None,
         "texte": "Reduction des Emissions de GES - annee 2025 : -11,11%",
         "sources": None, "score": 0.0305},
    ],
    "registre": [
        {"code": "GES-2023", "titre": "Bilan d'emissions de GES 2023 (Greenly)",
         "categorie": "bilan_carbone", "pages": 54,
         "url_partage": "http://localhost:8088/documents-preuve/GES-2023.pdf"},
        {"code": "KPI-RSE", "titre": "Liste des indicateurs de performance "
                                     "extra-financiere (GRI)",
         "categorie": "indicateurs", "pages": 5, "url_partage": None},
    ]}}

RECLASSEMENT = {"json": {"output": {"selection": [
    {"index": 1, "note": 10, "motif": "reponse directe et chiffree"},
    {"index": 2, "note": 8, "motif": "source primaire"},
    {"index": 3, "note": 5, "motif": "trajectoire"}]}}}

REDACTION = {"json": {"output": {
    "reponse": "Le dernier bilan carbone de Logiclever porte sur l'exercice 2023...",
    "reponse_courte": "Scope 1 < 0,1 tCO2e ; scope 2 < 0,1 tCO2e ; scope 3 : 183 tCO2e",
    "sources": [{"document": "GES-2023", "page": "12", "element": "resume executif"}],
    "confiance": 0.94, "elements_manquants": []}}}

CONTROLE = {"json": {"output": {
    "valide": True,
    "reponse_finale": "Le dernier bilan carbone de Logiclever porte sur 2023...",
    "anomalies": [], "confiance_finale": 0.92, "niveau": "haute", "alerte": ""}}}

PARAMETRES = {"json": {"fichiers_url": "http://localhost:8088",
                       "modele_embedding": "text-embedding-3-large",
                       "dimensions": 1536, "nb_candidats": 18,
                       "seuil_haute_confiance": 0.75, "seuil_a_verifier": 0.5}}


def main():
    code = noeuds_code()
    anomalies = []

    def verifier(condition, message):
        print("   %s %s" % ("OK      " if condition else "ANOMALIE", message))
        if not condition:
            anomalies.append(message)

    # ---------------------------------------------------- dossier de preuves
    print("1. Constituer le dossier de preuves")
    dossier = executer(code["Constituer le dossier de preuves"], [RECLASSEMENT], {
        "Recherche": [RECHERCHE], "Chaque question": [QUESTION],
        "Analyser la question": [ANALYSE]})[0]["json"]
    verifier(dossier["nb_preuves"] == 3, "3 preuves retenues")
    verifier(dossier["repli"] is False, "pas de repli quand le reclassement retient")
    cit = dossier["dossier"][0]["citation"]
    verifier("GES-2023" in cit and "FAIT/" not in cit,
             "la reponse validee cite ses documents (%s)" % cit)
    verifier(dossier["dossier"][1]["citation"] == "GES-2023, p. 12-13",
             "l'extrait documentaire cite sa page")

    # une source sans document en tete de liste ne doit pas devenir la reference
    rech_trou = json.loads(json.dumps(RECHERCHE))
    rech_trou["json"]["candidats"][0]["sources"].insert(
        0, {"document": "", "pages": "3", "option": "option sans document"})
    trou = executer(code["Constituer le dossier de preuves"], [RECLASSEMENT], {
        "Recherche": [rech_trou], "Chaque question": [QUESTION],
        "Analyser la question": [ANALYSE]})[0]["json"]["dossier"][0]
    verifier(trou["document"] == "GES-2023" and trou["page"] == "12, 14",
             "document et pages tires de la 1re source valide, pages fusionnees (%s p.%s)"
             % (trou["document"], trou["page"]))

    # ---------------------------------------------------- repli
    print("\n2. Repli quand le reclassement ne retient rien")
    vide = executer(code["Constituer le dossier de preuves"],
                    [{"json": {"output": {"selection": []}}}], {
                        "Recherche": [RECHERCHE], "Chaque question": [QUESTION],
                        "Analyser la question": [ANALYSE]})[0]["json"]
    verifier(vide["repli"] is True and vide["nb_preuves"] == 3,
             "repli sur les candidats deja en memoire (%d preuves)" % vide["nb_preuves"])

    # ---------------------------------------------------- consolidation
    print("\n3. Consolider la reponse")
    consolide = executer(code["Consolider la reponse"], [CONTROLE], {
        "Constituer le dossier de preuves": [{"json": dossier}],
        "Rediger la reponse": [REDACTION], "Parametres": [PARAMETRES],
        "Chaque question": [QUESTION]})[0]["json"]
    verifier(consolide["niveau"] == "haute", "niveau repris du controle")
    verifier(consolide["ligne"] == 7, "numero de ligne conserve")
    verifier("Bilan d'emissions de GES 2023" in consolide["citation"],
             "titre lisible resolu depuis le registre")
    verifier(consolide["client"] == "TotalEnergies", "client propage")
    verifier(consolide["sources"][0]["url"] and consolide["sources"][0]["url"].endswith("GES-2023.pdf"),
             "url_partage propagee dans sources[]")
    verifier(" - http://localhost:8088/documents-preuve/" in consolide["citation"],
             "url_partage ajoutee a la citation")

    # ---------------------------------------------------- niveau de confiance
    print("\n3 bis. Niveau : le plus prudent du controleur et des seuils")

    def consolider(controle=None, redaction=None, dossier_=None):
        ctrl = {"json": {"output": dict(CONTROLE["json"]["output"], **(controle or {}))}}
        red = {"json": {"output": dict(REDACTION["json"]["output"], **(redaction or {}))}}
        return executer(code["Consolider la reponse"], [ctrl], {
            "Constituer le dossier de preuves": [{"json": dossier_ or dossier}],
            "Rediger la reponse": [red], "Parametres": [PARAMETRES],
            "Chaque question": [QUESTION]})[0]["json"]

    r = consolider({"niveau": "haute", "confiance_finale": 0.6})
    verifier(r["niveau"] == "moyenne",
             "« haute » sous le seuil de 0,75 ramene a moyenne (%s)" % r["niveau"])
    r = consolider({"niveau": "haute"}, {"sources": [{"document": "DOC-INCONNU", "page": "1"}]})
    verifier(r["niveau"] == "a_verifier" and "absente(s) du registre" in r["alerte"],
             "sans source resolue : a_verifier + alerte (%s)" % r["niveau"])
    r = consolider({"niveau": "haute"}, dossier_=dict(dossier, repli=True))
    verifier(r["niveau"] == "a_verifier", "reponse batie sur le repli : a_verifier")
    r = consolider({"niveau": "sans_reponse", "confiance_finale": 0.1})
    verifier(r["niveau"] == "sans_reponse", "sans_reponse conserve")
    r = consolider(redaction={"sources": [
        {"document": "GES-2023", "page": "12"}, {"document": "GES-2023", "page": "p. 12"},
        {"document": "Bilan d'emissions de GES 2023 (Greenly)", "page": "14"}]})
    verifier(len(r["sources"]) == 2 and all(s["document"] == "GES-2023" for s in r["sources"]),
             "doublons fusionnes, titre recite resolu en code_doc (%s)"
             % [(s["document"], s["page"]) for s in r["sources"]])
    r = consolider({"reponse_courte_finale": "Non"}, {"reponse_courte": "Oui"})
    verifier(r["reponse_courte"] == "Non", "reponse courte corrigee par le controleur retenue")

    # ---------------------------------------------------- rassemblement
    print("\n4. Rassembler les reponses")
    deuxieme = dict(consolide, ligne=23, question="Vos salaries sont-ils en CDI ?",
                    reponse="100 % des effectifs sont en CDI.", niveau="haute")
    lot = [{"json": consolide}, {"json": deuxieme}]
    rassemble = executer(code["Rassembler les reponses"], lot, {})[0]["json"]
    verifier(rassemble["nombre"] == 2, "2 reponses agregees en 1 item")
    verifier("Ligne 7" in rassemble["resume"] and "Ligne 23" in rassemble["resume"],
             "resume exploitable par le controle de coherence")

    # ---------------------------------------------------- restitution
    print("\n5. Preparer la restitution")
    coherence = {"json": {"output": {"contradictions": [
        {"lignes": [7, 23], "nature": "Effectif CDI annonce a 100 % ligne 23",
         "ligne_a_corriger": 23,
         "reponse_corrigee": "100 % des salaries en CDI, hors alternants."}]}}}
    lignes = executer(code["Preparer la restitution"], [coherence],
                      {"Rassembler les reponses": [{"json": rassemble}]})
    l7 = [x["json"] for x in lignes if x["json"]["Ligne"] == 7][0]
    l23 = [x["json"] for x in lignes if x["json"]["Ligne"] == 23][0]
    verifier("hors alternants" in l23["Reponse"], "correction de coherence appliquee")
    verifier(l23["Niveau"] == "a_verifier", "la ligne corrigee repasse en a_verifier")
    verifier(l23["A relire"] == "OUI", "la ligne corrigee est marquee a relire")
    verifier(l23["Reponse courte"] == "" and "reponse courte a completer" in l23["Alerte"],
             "la reponse courte de la ligne corrigee est videe et signalee")
    verifier(l7["Reponse courte"] == consolide["reponse_courte"],
             "la reponse courte d'une ligne non corrigee est conservee")
    verifier("Coherence" in l7["Alerte"], "l'autre ligne du couple est signalee")
    colonnes = set(l7.keys())
    verifier(not any(c.startswith("_") for c in colonnes),
             "aucune colonne technique dans le classeur (%d colonnes)" % len(colonnes))

    # ---------------------------------------------------- sans contradiction
    print("\n6. Cas nominal, aucune contradiction")
    propre = executer(code["Preparer la restitution"],
                      [{"json": {"output": {"contradictions": []}}}],
                      {"Rassembler les reponses": [{"json": rassemble}]})
    verifier(all(x["json"]["A relire"] == "" for x in propre),
             "aucune ligne marquee a relire sans contradiction")

    print("\n%s" % ("Simulation complete : tous les controles passent."
                    if not anomalies else "%d anomalie(s)." % len(anomalies)))
    return len(anomalies)


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
