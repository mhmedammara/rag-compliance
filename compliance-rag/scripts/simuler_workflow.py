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
    var $ = function (nom) {
      var items = __ctx[nom];
      if (!items) {
        // Noeud non execute dans cette branche (ex. "Preparer la question"
        // hors du chat) : reproduit isExecuted=false comme le ferait n8n
        // pour un noeud jamais atteint, plutot que de planter.
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
         "categorie": "bilan_carbone", "pages": 54},
        {"code": "KPI-RSE", "titre": "Liste des indicateurs de performance "
                                     "extra-financiere (GRI)",
         "categorie": "indicateurs", "pages": 5},
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

PARAMETRES = {"json": {"supabase_url": "https://x.supabase.co",
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
