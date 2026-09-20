# -*- coding: utf-8 -*-
"""
Genere le workflow n8n :
    workflows/logiclever_reponses_questionnaires.json

ATTENTION - lire avant de modifier ce fichier :
  Ce script n'est PAS la source de verite du workflow en production. Le
  workflow reellement deploye est
  "workflows/Logiclever - Reponses aux questionnaires de conformite VF.json",
  maintenu a la main dans n8n (branche chat comprise). Ce generateur est
  adapte pour reproduire fidelement VF.json (memes modeles OpenAI, memes
  prompts, meme branche chat) afin de rester une reference exploitable,
  mais VF.json reste le fichier a editer pour tout changement destine a la
  production. Executer ce script n'affecte jamais ce qui tourne dans n8n :
  il faut reimporter son resultat, ou reporter le changement a la main dans
  le workflow live.

Chaine deterministe : aucun agent ne choisit ses outils, toutes les sources sont
interrogees pour chaque question. Une meme question donne toujours la meme reponse.

  1. Analyse de la question   sortie structuree (sous-requetes)
  2. Vectorisation            OpenAI embeddings, 1 appel
  3. Recherche                1 appel a recherche_globale (Supabase) : banque +
                              documents + indicateurs + registre, fusion RRF en SQL
  4. Reclassement             note de pertinence par candidat
  5. Redaction                sortie structuree + citations
  6. Controle de veracite     verification contre les preuves
  --- fin de boucle (formulaire) / sortie chat ---
  7. Controle de coherence    1 appel pour tout le formulaire (formulaire uniquement)
  8. Restitution Excel + journal Supabase (formulaire) / reponse chat (chat)

Une branche "chat" (Question du chat -> Preparer la question) rejoint la chaine
d'analyse directement, en contournant la boucle du formulaire ; en sortie,
"Aiguiller vers chat" separe les deux usages.
"""
import json
import os

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(os.path.dirname(ICI), "workflows",
                      "logiclever_reponses_questionnaires.json")

OPENAI_CHAT = "@n8n/n8n-nodes-langchain.lmChatOpenAi"
CHAIN = "@n8n/n8n-nodes-langchain.chainLlm"
PARSER = "@n8n/n8n-nodes-langchain.outputParserStructured"

MODELE = "gpt-5.6-luna"

noeuds, liens = [], {}


def n(nom, type_, params, x, y, tv=1, extra=None):
    d = {"parameters": params, "id": nom, "name": nom, "type": type_,
         "typeVersion": tv, "position": [x, y]}
    if extra:
        d.update(extra)
    noeuds.append(d)
    return nom


def lien(source, cible, sortie=0, type_="main", entree=0):
    liens.setdefault(source, {}).setdefault(type_, [])
    while len(liens[source][type_]) <= sortie:
        liens[source][type_].append([])
    liens[source][type_][sortie].append({"node": cible, "type": type_, "index": entree})


def note(titre, contenu, x, y, w, h, couleur=7):
    noeuds.append({
        "parameters": {"content": "## %s\n%s" % (titre, contenu),
                       "height": h, "width": w, "color": couleur},
        "id": "note_%d_%d" % (x, y), "name": "Note %d_%d" % (x, y),
        "type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": [x, y]})


def chaine(nom, prompt, schema, x, y, max_tokens=4000, reasoning_effort=None):
    """Une chaine LLM = 3 noeuds : la chaine, son modele OpenAI, son schema de sortie."""
    n(nom, CHAIN, {"promptType": "define", "text": prompt, "hasOutputParser": True,
                   "messages": {"messageValues": []}}, x, y, 1.5)
    options = {"maxTokens": max_tokens}
    if reasoning_effort:
        options["reasoningEffort"] = reasoning_effort
    m = n("Modele : " + nom, OPENAI_CHAT, {
        "model": {"__rl": True, "mode": "list", "value": MODELE,
                  "cachedResultName": MODELE},
        "builtInTools": {},
        "options": options,
    }, x - 40, y + 200, 1.3)
    p = n("Schema : " + nom, PARSER, {
        "schemaType": "manual",
        "inputSchema": json.dumps(schema, ensure_ascii=False, indent=2),
    }, x + 110, y + 200, 1.2)
    lien(m, nom, type_="ai_languageModel")
    lien(p, nom, type_="ai_outputParser")
    return nom


# =====================================================================
#  1. Entrees (formulaire + chat)
# =====================================================================
note("1 - Entree",
     "Le formulaire est la seule porte d'entree pour un questionnaire complet : on "
     "y depose le fichier (.xlsx/.csv) recu du service achats.\n\n"
     "La colonne des questions est detectee automatiquement : chaque client a "
     "son propre gabarit, il n'y a rien a normaliser en amont.\n\n"
     "Pour une question isolee, le chat (\"Question du chat\") rejoint la chaine "
     "d'analyse directement, sans passer par le formulaire.",
     -10100, -300, 440, 320)

n("Depot d'un questionnaire", "n8n-nodes-base.formTrigger", {
    "formTitle": "Questionnaire de conformite client",
    "formDescription": "Deposez le questionnaire RSE / securite recu du service achats. "
                       "Les reponses sont redigees a partir des documents de preuve "
                       "Logiclever et systematiquement sourcees.",
    "formFields": {"values": [
        {"fieldLabel": "Client", "requiredField": True},
        {"fieldLabel": "Intitule du questionnaire"},
        {"fieldLabel": "Fichier", "fieldType": "file",
         "acceptFileTypes": ".xlsx,.xls,.csv", "requiredField": True},
        {"fieldLabel": "Consignes particulieres", "fieldType": "textarea"},
    ]},
    "options": {"appendAttribution": False},
}, -10016, 80, 2.2)

n("Lire le fichier", "n8n-nodes-base.extractFromFile", {
    "operation": "xlsx", "binaryPropertyName": "Fichier",
    "options": {"headerRow": True, "includeEmptyCells": False},
}, -9776, 80, 1)
lien("Depot d'un questionnaire", "Lire le fichier")

NORMALISER = r"""
// Transforme la feuille deposee en une liste homogene de questions.
//
// La colonne des questions n'est jamais au meme endroit d'un client a l'autre :
// on retient celle qui contient le plus de cellules ressemblant a une question.

const sansAccent = (s) => (s || '').toString()
  .normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

// Le nœud de lecture (sans ligne d'en-tete) sort chaque ligne comme un
// tableau brut sous la cle "row" : { row: [valA, valB, ...] }. On re-ecrit
// chaque ligne en objet indexe (c0, c1, ...) pour que la detection de
// colonnes ci-dessous fonctionne quel que soit le gabarit client.
const lignes = $input.all().map(i => {
  const j = i.json;
  if (Array.isArray(j.row)) {
    const o = {};
    j.row.forEach((v, k) => { o['c' + k] = v; });
    return o;
  }
  return j;
});
if (!lignes.length) throw new Error("Aucune ligne exploitable dans le fichier.");

// L'ordre de "colonnes" doit refleter l'ordre reel des colonnes de la feuille
// pour que la detection de la section reste correcte. "Include Empty Cells"
// desactive fait qu'une ligne partielle donnerait un Object.keys() dans le
// mauvais ordre : on part donc de la ligne la plus complete.
const ligneReference = lignes.reduce((a, b) =>
  Object.keys(b).length > Object.keys(a).length ? b : a, lignes[0]);
const colonnes = Object.keys(ligneReference);
for (const l of lignes) {
  for (const k of Object.keys(l)) if (!colonnes.includes(k)) colonnes.push(k);
}

const estQuestion = (v) => {
  const s = sansAccent(v).trim();
  if (s.length < 12) return 0;
  if (v.includes('?')) return 1;
  if (/^(merci de|veuillez|precisez|decrivez|indiquez|detaillez|listez|fournissez|expliquez|donnez|joignez)/.test(s)) return 1;
  if (/^(quel|quelle|quels|quelles|comment|combien|pourquoi|quand|qui |ou )/.test(s)) return 1;
  if (/^(avez-vous|disposez-vous|etes-vous|pouvez-vous|existe-t-il|y a-t-il|est-ce|si oui|si non)/.test(s)) return 1;
  if (/^(votre|vos) (entreprise|organisation|societe|groupe|structure)/.test(s)) return 1;
  if (/(-vous|-elle|-il)\b/.test(s)) return 0.7;
  return 0;
};
// Un simple comptage suffit : estQuestion est assez strict pour ne pas
// confondre une colonne de reponses avec une colonne de questions. Verifie par
// ablation sur les 3 classeurs reels (voir scripts/verifier.py).
const scoreColonne = (col) => {
  const vals = lignes.map(l => String(l[col] ?? '').trim()).filter(v => v.length > 0);
  if (vals.length < 2) return 0;
  return vals.reduce((a, v) => a + estQuestion(v), 0);
};

const classement = colonnes.map(c => [c, scoreColonne(c)]).sort((a, b) => b[1] - a[1]);
if (!classement.length || classement[0][1] <= 0) {
  // Aucune cellule ne ressemble a une question : le plus souvent, le fichier
  // depose n'est pas la feuille du questionnaire (onglet de listes de choix,
  // extrait partiel...). On montre un apercu pour rendre l'erreur parlante.
  const apercu = lignes.map(l => colonnes.map(c => String(l[c] ?? '').trim()).join(' | '))
    .filter(Boolean).slice(0, 8).join(' / ').slice(0, 300);
  throw new Error("Aucune question detectee dans le fichier : verifiez que la feuille deposee "
                  + "est bien celle du questionnaire (colonnes : " + colonnes.join(', ')
                  + (apercu ? " ; apercu : " + apercu : " ; fichier vide ?") + ")");
}
const col = classement[0][0];

// colonne de section : celle qui precede, si elle est courte et peu interrogative
const idx = colonnes.indexOf(col);
let colSection = null;
for (const cand of [colonnes[idx - 1], colonnes[idx - 2]]) {
  if (!cand) continue;
  const vals = lignes.map(l => String(l[cand] ?? '').trim()).filter(Boolean);
  if (vals.length < 2) continue;
  const moy = vals.reduce((a, v) => a + v.length, 0) / vals.length;
  const interro = vals.reduce((a, v) => a + estQuestion(v), 0) / vals.length;
  if (moy < 60 && interro < 0.3) { colSection = cand; break; }
}

const meta = $('Depot d\'un questionnaire').isExecuted
  ? $('Depot d\'un questionnaire').first().json : {};

const sorties = [];
lignes.forEach((l, i) => {
  const q = String(l[col] ?? '').trim();
  if (estQuestion(q) < 0.7) return;
  sorties.push({ json: {
    ligne: i + 1,
    section: colSection ? String(l[colSection] ?? '').trim() : '',
    question: q,
    client: meta['Client'] || 'Client',
    intitule: meta['Intitule du questionnaire'] || 'Questionnaire de conformite',
    consignes: meta['Consignes particulieres'] || '',
  }});
});
if (!sorties.length) throw new Error("Aucune question detectee dans la colonne " + col + ".");
sorties.forEach(s => s.json.total = sorties.length);
return sorties;
"""

n("Normaliser les questions", "n8n-nodes-base.code",
  {"mode": "runOnceForAllItems", "jsCode": NORMALISER}, -9536, 80, 2)
lien("Lire le fichier", "Normaliser les questions")

n("Parametres", "n8n-nodes-base.set", {
    "assignments": {"assignments": [
        {"id": "p1", "name": "modele_embedding", "value": "text-embedding-3-large", "type": "string"},
        {"id": "p2", "name": "dimensions", "value": 1536, "type": "number"},
        {"id": "p3", "name": "nb_candidats", "value": 40, "type": "number"},
        {"id": "p4", "name": "seuil_haute_confiance", "value": 0.75, "type": "number"},
        {"id": "p5", "name": "seuil_a_verifier", "value": 0.5, "type": "number"},
        {"id": "p6", "name": "supabase_url", "value": "https://VOTRE-PROJET.supabase.co", "type": "string"},
    ]},
    "includeOtherFields": True, "options": {},
}, -9296, 80, 3.4)
lien("Normaliser les questions", "Parametres")

n("Chaque question", "n8n-nodes-base.splitInBatches",
  {"options": {"reset": False}}, -9056, 80, 3)
lien("Parametres", "Chaque question")

# --- branche chat : rejoint "Analyser la question" directement -------
note("1 bis - Chat (question isolee)",
     "Une question posee dans le chat contourne le formulaire : \"Preparer la "
     "question\" reproduit le format de sortie de \"Normaliser les questions\" "
     "et rejoue les parametres de la branche formulaire (nb_candidats, seuils, "
     "URL Supabase), sans toucher au nœud \"Parametres\".\n\n"
     "En sortie, \"Aiguiller vers chat\" (apres Consolider la reponse) separe "
     "les deux usages.",
     -10100, 250, 440, 300)

n("Question du chat", "@n8n/n8n-nodes-langchain.chatTrigger", {
    "public": True,
    "initialMessages": "Posez une question d'un questionnaire de conformite : la "
                       "reponse est redigee a partir des documents de preuve "
                       "Logiclever et sourcee.",
    "options": {"inputPlaceholder": "Votre question...",
                "subtitle": "Une question, une reponse sourcee.",
                "title": "Assistant questionnaires Logiclever",
                "responseMode": "lastNode"},
}, -10032, 336, 1.5)

PREPARER_QUESTION = r"""
// Remet la question du chat au format attendu par la chaine d'analyse
// (memes champs qu'en sortie de "Normaliser les questions").
const q = String($json.chatInput ?? $json.question ?? '').trim();
if (!q) throw new Error("Aucune question recue du chat.");
return [{ json: {
  ligne: 1,
  section: '',
  question: q,
  client: 'Chat',
  intitule: 'Question isolee (chat)',
  consignes: '',
  // Parametres de la branche formulaire, rejoues ici pour que la chaine
  // d'analyse (Vectoriser / Recherche / Consolider) puisse les lire sans
  // toucher au nœud "Parametres".
  modele_embedding: 'text-embedding-3-large',
  dimensions: 1536,
  nb_candidats: 40,
  seuil_haute_confiance: 0.75,
  seuil_a_verifier: 0.5,
  supabase_url: 'https://VOTRE-PROJET.supabase.co',
}}];
"""

n("Preparer la question", "n8n-nodes-base.code",
  {"mode": "runOnceForAllItems", "jsCode": PREPARER_QUESTION}, -9536, 320, 2)
lien("Question du chat", "Preparer la question")


# =====================================================================
#  2. Analyse
# =====================================================================
note("2 - Analyse de la question",
     "Reformule la question et produit 2 a 4 **sous-requetes** exprimees dans le "
     "vocabulaire des documents internes.\n\n"
     "C'est ce qui separe une recherche mot-a-mot d'une recherche qui trouve la "
     "bonne preuve : « controle de vos fournisseurs » devient « politique achats "
     "responsables clauses RSE contrats fournisseurs ».",
     -8860, -300, 420, 340)

ANALYSE = r"""=Tu prepares la recherche documentaire qui permettra de repondre a une question
d'un service achats sur la conformite RSE / securite de LOGICLEVER.

LOGICLEVER : ESN francaise de conseil en systemes d'information, 50 salaries
(48 Paris, 2 Lyon), entreprise liberee, SASU sans groupe. Evaluee EcoVadis
(79/100, top 2%) et CyberVadis (860/1000, niveau Mature).

QUESTION DU CLIENT
Section : {{ $json.section || 'non precisee' }}
Question : {{ $json.question }}
{{ $json.consignes ? 'Consignes du client : ' + $json.consignes : '' }}

TRAVAIL DEMANDE
1. Reformule la question en francais clair et autonome (question_normalisee).
2. Produis 2 a 4 sous-requetes de recherche complementaires, dans le
   **vocabulaire des documents internes**, pas celui de la question.
   Exemples :
     "controle de vos fournisseurs" -> "politique achats responsables clauses RSE
      contrats fournisseurs", "evaluation SSI fournisseurs dependance",
      "code de conduite fournisseurs KYC"
     "bilan carbone" -> "bilan emissions gaz a effet de serre scope 1 2 3 tCO2e",
      "objectif reduction emissions trajectoire"
3. Determine le type de reponse attendu :
   oui_non | chiffre | description | date | liste | mixte
4. Si la question porte sur une annee precise, donne annee_cible (sinon null).
   Sans annee explicite, vise l'exercice le plus recent disponible.
5. Signale les points de vigilance : toute formulation qui pousserait a
   sur-declarer. En particulier, LOGICLEVER **n'est certifiee ni ISO 9001,
   ni ISO 14001, ni ISO 27001**, n'a **pas** d'objectif valide SBTi, et n'a
   **pas** de CSE (PV de carence)."""

chaine("Analyser la question", ANALYSE, {
    "type": "object",
    "required": ["question_normalisee", "sous_requetes", "type_reponse",
                 "points_de_vigilance"],
    "properties": {
        "question_normalisee": {"type": "string"},
        "sous_requetes": {"type": "array", "items": {"type": "string"},
                          "minItems": 2, "maxItems": 4},
        "type_reponse": {"type": "string",
                         "enum": ["oui_non", "chiffre", "description", "date",
                                  "liste", "mixte"]},
        "annee_cible": {"type": ["integer", "null"]},
        "points_de_vigilance": {"type": "array", "items": {"type": "string"}},
    },
}, -8768, 80, max_tokens=4000, reasoning_effort="low")

lien("Chaque question", "Analyser la question", sortie=1)
lien("Preparer la question", "Analyser la question")


# =====================================================================
#  3. Vectorisation + recherche
# =====================================================================
note("3 - Recherche",
     "**Un seul appel HTTP par question.**\n\n"
     "`recherche_globale` interroge cote SQL les quatre sources - banque de "
     "reponses validees, extraits documentaires, indicateurs chiffres, registre - "
     "pour chacune des sous-requetes, puis fusionne les rangs (RRF).\n\n"
     "Chat et formulaire lisent leurs parametres (nb_candidats, URL Supabase) "
     "depuis \"Preparer la question\" si elle a ete executee, sinon depuis "
     "\"Parametres\".",
     -8420, -300, 460, 340)

PARAMS_TERNAIRE = (
    "($('Preparer la question').isExecuted ? $('Preparer la question').first().json "
    ": $('Parametres').first().json)"
)

n("Vectoriser les requetes", "n8n-nodes-base.httpRequest", {
    "method": "POST", "url": "https://api.openai.com/v1/embeddings",
    "authentication": "predefinedCredentialType", "nodeCredentialType": "openAiApi",
    "sendBody": True, "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify({ "
                "model: %s.modele_embedding, "
                "dimensions: %s.dimensions, "
                "input: [$json.output.question_normalisee]"
                ".concat($json.output.sous_requetes).slice(0, 5) }) }}"
                % (PARAMS_TERNAIRE, PARAMS_TERNAIRE),
    "options": {},
}, -8336, 80, 4.2)
lien("Analyser la question", "Vectoriser les requetes")

n("Recherche", "n8n-nodes-base.httpRequest", {
    "method": "POST",
    "url": "={{ %s.supabase_url }}/rest/v1/rpc/recherche_globale" % PARAMS_TERNAIRE,
    "authentication": "predefinedCredentialType", "nodeCredentialType": "supabaseApi",
    "sendBody": True, "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify({ "
                "requetes: [$('Analyser la question').item.json.output.question_normalisee]"
                ".concat($('Analyser la question').item.json.output.sous_requetes).slice(0, 5), "
                "requete_embeddings: $json.data.sort((a, b) => a.index - b.index)"
                ".map(d => d.embedding), "
                "annee_cible: $('Analyser la question').item.json.output.annee_cible ?? null, "
                "nb_candidats: %s.nb_candidats }) }}" % PARAMS_TERNAIRE,
    "options": {"response": {"response": {"neverError": True}}},
}, -8096, 80, 4.2)
lien("Vectoriser les requetes", "Recherche")


# =====================================================================
#  4. Reclassement
# =====================================================================
note("4 - Reclassement",
     "Le RRF classe par **convergence** entre sous-requetes, pas par pertinence "
     "reelle. Ce noeud note ici chaque candidat de 0 a 10 pour cette question "
     "precise.\n\n"
     "C'est l'etape qui evite de citer la bonne politique a la mauvaise page.",
     -7940, -300, 400, 300)

RECLASSEMENT = r"""=Tu selectionnes les preuves qui serviront a repondre a une question de conformite.

QUESTION : {{ $('Preparer la question').isExecuted ? $('Preparer la question').item.json.question : $('Chaque question').item.json.question }}
Reformulation : {{ $('Analyser la question').item.json.output.question_normalisee }}
Type de reponse attendu : {{ $('Analyser la question').item.json.output.type_reponse }}

CANDIDATS
{{ ($json.candidats || []).map(c => '#' + c.index + ' [' + c.type + ' | ' + c.reference
   + (c.page_debut ? ' p.' + c.page_debut + (c.page_fin && c.page_fin !== c.page_debut ? '-' + c.page_fin : '') : '')
   + '] ' + (c.titre || '') + '\n' + String(c.texte).slice(0, 600)).join('\n\n---\n\n') }}

Attribue a chaque candidat une note de pertinence de 0 a 10 :
  10  repond directement et integralement a la question
  7-9 apporte un element de reponse important (chiffre, mesure, engagement)
  4-6 contexte utile mais insuffisant seul
  0-3 hors sujet

Ne retiens que les candidats notes 4 et plus, au maximum 8, classes par note
decroissante. Si aucun candidat n'atteint 4, renvoie une liste vide : il vaut
mieux signaler l'absence de preuve que citer un document a cote du sujet."""

chaine("Reclasser les preuves", RECLASSEMENT, {
    "type": "object", "required": ["selection"],
    "properties": {"selection": {"type": "array", "items": {
        "type": "object", "required": ["index", "note"],
        "properties": {"index": {"type": "integer"}, "note": {"type": "number"},
                       "motif": {"type": "string"}}}}},
}, -7808, 80, max_tokens=6000)
lien("Recherche", "Reclasser les preuves")

DOSSIER = r"""
// Construit le dossier de preuves transmis au redacteur.
//
// Repli borne : si le reclassement n'a rien retenu, on reprend les 5 meilleurs
// candidats du RRF plutot que de renoncer. Ils sont deja en memoire : cela ne
// coute ni appel ni latence, et la confiance restera basse.

const recherche = $('Recherche').item.json;
const candidats = recherche.candidats || [];
const q = $('Preparer la question').isExecuted
  ? $('Preparer la question').item.json
  : $('Chaque question').item.json;
const analyse = $('Analyser la question').item.json.output;
const selection = ($json.output && $json.output.selection) || [];
const parIndex = new Map(candidats.map(c => [c.index, c]));

let retenus = selection
  .filter(s => s.note >= 4)
  .sort((a, b) => b.note - a.note)
  .slice(0, 8)
  .map(s => ({ candidat: parIndex.get(s.index), note: s.note }))
  .filter(x => x.candidat);

let repli = false;
if (!retenus.length && candidats.length) {
  repli = true;
  retenus = candidats.slice(0, 5).map(c => ({ candidat: c, note: 0 }));
}

const dossier = retenus.map((x, i) => {
  const c = x.candidat;

  // Une reponse validee porte ses propres documents justificatifs : c'est eux
  // qu'il faut citer au client, pas la reference interne de la banque
  // (FAIT/GES-SCOPES, ECOVADIS/LAB100...).
  if (c.type === 'reponse_validee' && Array.isArray(c.sources) && c.sources.length) {
    const refs = [...new Set(c.sources
      .filter(s => s && s.document)
      .map(s => s.document + (s.pages ? ', p. ' + s.pages : '')))].slice(0, 3);
    return {
      rang: i + 1, type: c.type, note: x.note,
      document: c.sources[0].document,
      titre: c.titre || c.sources[0].document,
      page: c.sources[0].pages || '',
      citation: refs.join(' ; ') || c.reference,
      texte: String(c.texte).slice(0, 1200),
    };
  }

  const page = c.page_debut
    ? (c.page_fin && c.page_fin !== c.page_debut
        ? 'p. ' + c.page_debut + '-' + c.page_fin : 'p. ' + c.page_debut)
    : '';
  return {
    rang: i + 1, type: c.type, note: x.note, document: c.reference,
    titre: c.titre || c.reference, page,
    citation: c.reference + (page ? ', ' + page : ''),
    texte: String(c.texte).slice(0, 1200),
  };
});

return [{ json: {
  ligne: q.ligne, section: q.section, question_client: q.question,
  client: q.client, intitule: q.intitule,
  consignes: q.consignes || '', analyse,
  dossier, nb_preuves: dossier.length, repli,
  registre: recherche.registre || [],
}}];
"""

n("Constituer le dossier de preuves", "n8n-nodes-base.code",
  {"mode": "runOnceForAllItems", "jsCode": DOSSIER}, -7552, 80, 2)
lien("Reclasser les preuves", "Constituer le dossier de preuves")


# =====================================================================
#  5. Redaction
# =====================================================================
note("5 - Redaction",
     "Le texte que l'acheteur lit.\n\n"
     "Regle centrale : chaque affirmation doit etre portee par une preuve du "
     "dossier. Une force se dit pleinement ; une faiblesse se dit avec la mesure "
     "compensatoire - jamais en la niant.",
     -7420, -340, 440, 380, 3)

REDACTION = r"""=Tu rédiges la réponse officielle de LOGICLEVER au questionnaire du client.

Ta mission n’est PAS d’auditer, de commenter ou de résumer le dossier documentaire.
Ta mission est de **répondre directement au client au nom de Logiclever**, à partir des éléments de preuve fournis.

## 1. SOURCES ET PÉRIMÈTRE

Les preuves du dossier constituent la source de vérité pour les faits que tu affirmes.

Le contexte général de l’entreprise fourni dans ce prompt peut t’aider à comprendre Logiclever, mais **ne constitue pas une preuve**. Pour une affirmation factuelle, appuie-toi sur les preuves du dossier.

N’invente jamais :

* chiffre, date, certification, label ou classement ;
* politique, procédure, engagement ou objectif ;
* résultat, fréquence ou périmètre ;
* conformité réglementaire ;
* dispositif ou pratique non documenté.

Tu peux reformuler, synthétiser et organiser les informations présentes dans les preuves.

Tu peux également combiner plusieurs preuves lorsqu’elles portent clairement sur le même sujet et permettent de construire une réponse cohérente.

En revanche, ne combine jamais des éléments distincts pour créer une conclusion qui n’est pas explicitement étayée.

## 2. PRIORITÉ ABSOLUE : RÉPONDRE COMME LOGICLEVER

La réponse doit être formulée **comme une réponse de Logiclever au client**.

Ne parle pas du dossier, des documents, des preuves ou de ton analyse.

### À ÉVITER

* « Le dossier présente… »
* « Les éléments disponibles indiquent… »
* « Les documents transmis permettent de confirmer… »
* « D’après les preuves… »
* « La valeur doit être confirmée. »
* « Il existe des informations contradictoires… »
* « Les éléments disponibles ne permettent pas de confirmer… »

sauf si le client demande explicitement une analyse documentaire ou si une contradiction empêche réellement de fournir une réponse fiable.

### À PRIVILÉGIER

* « Oui, Logiclever… »
* « Logiclever met en œuvre… »
* « En 2025, Logiclever… »
* « Notre politique prévoit… »
* « Nous suivons… »
* « Nous avons mis en place… »
* « Le score obtenu par Logiclever est… »
* « L’objectif suivi est… »

Le ton doit être celui d’une **entreprise répondant à un questionnaire commercial, fournisseur ou conformité** : professionnel, direct, factuel, clair et assuré.

Ne transforme pas une réponse fournisseur en rapport d’audit.

## 3. RÈGLE DE COMPORTEMENT FACE AUX PREUVES

Utilise la meilleure information pertinente disponible pour répondre à la question.

Ne signale pas spontanément toutes les ambiguïtés ou divergences internes du dossier.

Si plusieurs preuves donnent des valeurs différentes :

1. privilégie la preuve la plus directement pertinente pour la question ;
2. privilégie une information explicitement présentée comme actuelle, récente ou applicable à la période demandée ;
3. privilégie une source clairement identifiable et spécifique au sujet ;
4. ne mentionne la divergence que si elle empêche raisonnablement de répondre de façon fiable.

**Ne transforme pas une divergence documentaire en réponse d’auditeur.**

Exemple :

Si une preuve pertinente indique :
« EcoVadis : 79/100, 98e percentile »

et qu'une autre preuve moins directement pertinente indique une autre valeur, réponds simplement avec la valeur retenue si elle est suffisamment étayée.

Ne réponds pas automatiquement :
« Le dossier présente des informations contradictoires… »

## 4. NIVEAU DE PRÉCISION DES AFFIRMATIONS

Respecte exactement la force de ce que les preuves permettent d’affirmer.

Ne transforme jamais :

* « certifié » en « conforme » ou inversement ;
* « évalué » en « certifié » ;
* « score obtenu » en « conformité réglementaire » ;
* « objectif » en « résultat atteint » ;
* « politique » en « pratique effectivement réalisée » ;
* « audit » en « conformité » ;
* « aligné avec » en « certifié selon » ;
* « inspiré de » en « conforme à » ;
* « engagement » en « résultat ».

Ne renforce jamais une affirmation pour la rendre plus commerciale.

Tu peux toutefois présenter une information de manière **positive et professionnelle** lorsqu’elle est réellement étayée.

Exemple :

Preuve :
« Logiclever suit la proportion de femmes dans l’effectif et par grade. »

Bonne réponse :
« Logiclever suit la représentation des femmes dans l’effectif global et par grade. »

Mauvaise réponse :
« Logiclever garantit une représentation équitable des femmes à tous les niveaux. »

## 5. CONTEXTE ENTREPRISE

Logiclever est une SASU française de conseil informatique.

Éléments de contexte disponibles :

* 50 collaborateurs ;
* 48 à Paris et 2 à Lyon ;
* aucun groupe ou maison mère ;
* fonctionnement en « entreprise libérée » depuis 2018 ;
* environ 90 % du chiffre d’affaires dans le secteur de l’énergie.

Ces informations servent uniquement à contextualiser la réponse.

Pour les sujets de conformité, RSE, sécurité, RH, certifications, engagements, résultats ou politiques, **les preuves du dossier priment**.

Évaluations externes connues :

* EcoVadis : 79/100, 98e percentile / top 2 % ;
* CyberVadis : 860/1000, niveau Mature, benchmark 654 ;
* Great Place To Work Trust Index : 96 %.

Ne présente jamais ces éléments comme des certifications.

## 6. CERTIFICATIONS ET ENGAGEMENTS

Ne prétends jamais que Logiclever possède une certification, un label, une adhésion ou un engagement qui n’est pas explicitement étayé.

En particulier, ne présente jamais Logiclever comme certifiée :

* ISO 9001 ;
* ISO 14001 ;
* ISO 27001 ;
* SBTi.

Ne prétends pas non plus à l’existence :

* d’un CSE ou d’une représentation syndicale ;
* d’un accord collectif ;
* d’un label ;
* d’une adhésion ;
* d’un objectif validé ;
* d’une certification réglementaire,

sans preuve explicite.

Si une absence doit être mentionnée parce que la question la demande, réponds simplement et professionnellement, sans posture défensive.

## 7. STYLE DE RÉPONSE

Commence par répondre directement à la question.

Pour une question fermée :

* généralement 1 à 3 phrases ;
* commence par « Oui », « Non » ou une formulation directe lorsque pertinent.

Pour une question ouverte :

* généralement 3 à 8 phrases ;
* développe suffisamment pour répondre à tous les aspects de la question ;
* privilégie les informations concrètes : pratiques, dispositifs, indicateurs, résultats, fréquence, périmètre.

Évite les listes à puces dans la réponse sauf si elles améliorent clairement la lisibilité.

Utilise les chiffres avec leur unité et leur année lorsqu'elles sont disponibles.

Mets en avant les résultats ou évaluations externes lorsqu’ils répondent directement à la question.

## 8. FAIBLESSES ET INFORMATIONS MANQUANTES

Ne fabrique jamais une information pour rendre la réponse plus favorable.

Si un élément demandé n’est pas documenté, ne l’invente pas.

Mais ne transforme pas automatiquement cette absence en commentaire d’audit.

### Mauvais :

« Les éléments disponibles ne permettent toutefois pas de confirmer l’existence d’un plan d’action formalisé. »

### Préférer, lorsque la question exige réellement cette information :

« Logiclever n’a pas documenté de plan d’action formalisé spécifiquement sur ce point dans les éléments disponibles. »

Et lorsque l’absence n’est pas nécessaire pour répondre à la question, **ne la mentionne pas**.

L’objectif est de donner au client la réponse la plus utile, complète et professionnelle possible, sans masquer ni inventer les faits.

## 9. DATES ET INFORMATIONS HISTORIQUES

Ne crée jamais de date.

Si une donnée est explicitement rattachée à une année, conserve cette année.

Une information ancienne ne doit pas être présentée comme actuelle si les preuves ne permettent pas de l'établir.

## 10. CONFIANCE

Attribue une confiance comprise entre 0 et 1 selon la solidité de la réponse :

* 0.90–1.00 : réponse directement étayée par une preuve claire, précise et actuelle ;
* 0.70–0.89 : réponse bien étayée, avec une légère incertitude de périmètre ou de formulation ;
* 0.50–0.69 : réponse construite à partir de plusieurs éléments ou comportant une incertitude significative ;
* 0.30–0.49 : réponse partielle, ancienne ou faiblement étayée ;
* < 0.30 : absence de preuve réellement exploitable.

Si `repli` est activé, la confiance doit être au maximum de 0.35.

La confiance mesure la **solidité de la réponse**, pas son caractère commercial.

## 11. SOURCES

Dans le champ `sources`, indique uniquement les références effectivement utilisées pour construire la réponse.

Ne cite pas systématiquement toutes les preuves disponibles.

## 12. VARIABLES

Question et contexte :

Section :
{{ $json.section || 'non precisee' }}

Question du client :
{{ $json.question_client }}

Type de réponse :
{{ $json.analyse.type_reponse }}

{{ $json.consignes ? 'Consignes du client : ' + $json.consignes : '' }}

{{ ($json.analyse.points_de_vigilance || []).length ? 'Points de vigilance : ' + $json.analyse.points_de_vigilance.join(' | ') : '' }}

## 13. DOSSIER DOCUMENTAIRE

{{ $json.dossier.length === 0 ? 'AUCUNE PREUVE PERTINENTE TROUVEE.' : $json.dossier.map(d => '### Preuve ' + d.rang + ' - ' + d.citation + ' (' + d.titre + ')\n' + d.texte).join('\n\n') }}

{{ $json.repli ? '\nATTENTION : aucune preuve n a passe le seuil de pertinence. Les elements ci-dessus sont approximatifs : reponds prudemment et mets confiance a 0.35 au maximum.' : '' }}

## 14. CONTRÔLE FINAL SILENCIEUX

Avant de répondre, vérifie silencieusement :

1. Ai-je répondu exactement à la question ?
2. La réponse parle-t-elle comme Logiclever ?
3. Chaque affirmation factuelle importante est-elle étayée ?
4. Ai-je évité toute invention ou extrapolation ?
5. Ai-je évité de transformer la réponse en analyse d’auditeur ?
6. Ai-je conservé le bon niveau de force des affirmations ?
7. Ai-je utilisé la meilleure preuve pertinente plutôt que de sur-exposer les divergences documentaires ?
8. La confiance correspond-elle réellement à la solidité de la réponse ?
9. Les sources correspondent-elles uniquement aux éléments utilisés ?

Ne révèle pas ce raisonnement.

Respecte strictement le format imposé par l’Output Parser. N’ajoute aucun contenu en dehors des champs attendus par celui-ci.
"""

chaine("Rediger la reponse", REDACTION, {
    "type": "object", "required": ["reponse", "sources", "confiance"],
    "properties": {
        "reponse": {"type": "string",
                    "description": "Reponse en francais, prete a coller dans le "
                                   "questionnaire client."},
        "reponse_courte": {"type": "string",
                           "description": "Oui / Non / la valeur seule, pour une "
                                          "case a cocher."},
        "sources": {"type": "array", "items": {
            "type": "object", "required": ["document", "page"],
            "properties": {"document": {"type": "string"}, "page": {"type": "string"},
                           "element": {"type": "string"}}}},
        "confiance": {"type": "number", "minimum": 0, "maximum": 1},
        "elements_manquants": {"type": "array", "items": {"type": "string"}},
    },
}, -7280, 80, max_tokens=8000)
lien("Constituer le dossier de preuves", "Rediger la reponse")


# =====================================================================
#  6. Controle de veracite
# =====================================================================
note("6 - Controle de veracite",
     "Second passage, avec un oeil neuf : chaque affirmation est confrontee au "
     "dossier de preuves.\n\n"
     "Detecte l'affirmation non etayee, la certification revendiquee a tort, le "
     "chiffre deforme, la source qui ne dit pas ce qu'on lui fait dire.",
     -6920, -340, 420, 340, 3)

CONTROLE = r"""=Tu es le relecteur conformite de LOGICLEVER. Tu verifies un projet de reponse
avant envoi a un client. Ton role n'est pas de reecrire par gout, mais
d'empecher qu'une affirmation non etayee parte chez le client.

## QUESTION POSEE
{{ $('Constituer le dossier de preuves').item.json.question_client }}

## PROJET DE REPONSE
{{ $json.output.reponse }}

## SOURCES CITEES
{{ JSON.stringify($json.output.sources) }}

## DOSSIER DE PREUVES DISPONIBLE
{{ $('Constituer le dossier de preuves').item.json.dossier.length === 0 ? 'VIDE' : $('Constituer le dossier de preuves').item.json.dossier.map(d => '### ' + d.citation + ' (' + d.titre + ')\n' + d.texte).join('\n\n') }}

## CONTROLES

1. **Affirmation non etayee** - chaque fait, chiffre, date ou nom de dispositif
   figure-t-il dans le dossier ? Tout element absent doit etre supprime ou
   requalifie.
2. **Certification revendiquee a tort** - la reponse laisse-t-elle croire a une
   certification ISO 9001 / 14001 / 27001, a un objectif valide SBTi, ou a
   l'existence d'un CSE ? Aucun des trois n'existe chez LOGICLEVER. Une
   formulation ambigue compte comme une erreur.
3. **Chiffre deforme** - valeur, unite, annee et perimetre exacts ? Un
   pourcentage donne pour une annee ne doit pas etre attribue a une autre.
4. **Source mal citee** - le document cite contient-il bien l'element avance ?
5. **Omission dommageable** - un element favorable et pertinent du dossier a-t-il
   ete laisse de cote ? Si oui, l'integrer.
6. **Registre** - la reponse est-elle affirmative et complete, ou trop prudente au
   point de desservir Logiclever a preuve egale ? Renforce la formulation **sans
   jamais ajouter de fait absent du dossier**.

## SORTIE
- valide : true si le projet part tel quel.
- reponse_finale : la version a envoyer (identique au projet si valide).
- anomalies : ce que tu as corrige, en francais, une ligne par point.
- confiance_finale : ton evaluation apres corrections.
- niveau : haute (>= 0.75 et aucune anomalie bloquante) | moyenne (>= 0.5) |
  a_verifier (< 0.5, anomalie non resolue, ou sujet sensible) |
  sans_reponse (dossier vide ou hors perimetre Logiclever).
- alerte : message court au relecteur humain, chaine vide si rien a signaler."""

chaine("Controler la veracite", CONTROLE, {
    "type": "object",
    "required": ["valide", "reponse_finale", "anomalies", "confiance_finale",
                 "niveau", "alerte"],
    "properties": {
        "valide": {"type": "boolean"},
        "reponse_finale": {"type": "string"},
        "anomalies": {"type": "array", "items": {"type": "string"}},
        "confiance_finale": {"type": "number", "minimum": 0, "maximum": 1},
        "niveau": {"type": "string",
                   "enum": ["haute", "moyenne", "a_verifier", "sans_reponse"]},
        "alerte": {"type": "string"},
    },
}, -6768, 80, max_tokens=8000)
lien("Rediger la reponse", "Controler la veracite")

CONSOLIDER = r"""
// Assemble le resultat de la question et le renvoie dans la boucle.
// Dans une boucle SplitInBatches, .first() relit toujours le 1er item du node :
// .item renvoie l'item du tour courant (l'appariement se fait par index/pairing).
const d = $('Constituer le dossier de preuves').item.json;
const redaction = $('Rediger la reponse').item.json.output;
const controle = $json.output;
const p = $('Preparer la question').isExecuted
  ? $('Preparer la question').first().json
  : $('Parametres').first().json;
const q = { client: d.client, intitule: d.intitule, question: d.question_client, ligne: d.ligne, section: d.section };

const confiance = controle.confiance_finale ?? redaction.confiance ?? 0;
const niveau = controle.niveau
  || (confiance >= p.seuil_haute_confiance ? 'haute'
      : confiance >= p.seuil_a_verifier ? 'moyenne' : 'a_verifier');

const sources = (redaction.sources || []).map(s => {
  const fiche = (d.registre || []).find(r => r.code === s.document);
  return { document: s.document, titre: fiche ? fiche.titre : s.document,
           page: s.page || '', url: fiche ? fiche.url_partage : null };
});

const alertes = [];
if (controle.alerte) alertes.push(controle.alerte);
if (!controle.valide && (controle.anomalies || []).length) {
  alertes.push('Corrections : ' + controle.anomalies.join(' / '));
}
if (!sources.length) alertes.push('Reponse sans source documentaire.');
if (d.repli) alertes.push('Aucune preuve au-dessus du seuil : recherche elargie.');
if ((redaction.elements_manquants || []).length) {
  alertes.push('Elements manquants : ' + redaction.elements_manquants.join(' / '));
}

return [{ json: {
  client: q.client, questionnaire: q.intitule,
  ligne: d.ligne, section: d.section, question: d.question_client,
  reponse: controle.reponse_finale || redaction.reponse,
  reponse_courte: redaction.reponse_courte || '',
  sources,
  citation: sources.map(s => s.titre + (s.page ? ' (p. ' + s.page + ')' : '')
                            + (s.url ? ' - ' + s.url : '')).join(' ; '),
  confiance: Number(confiance.toFixed(3)),
  niveau,
  alerte: alertes.join(' | '),
  nb_preuves: d.nb_preuves,
}}];
"""

n("Consolider la reponse", "n8n-nodes-base.code",
  {"mode": "runOnceForAllItems", "jsCode": CONSOLIDER}, -6512, 80, 2)
lien("Controler la veracite", "Consolider la reponse")


# =====================================================================
#  Aiguillage chat / formulaire
# =====================================================================
note("Aiguillage chat / formulaire",
     "Une reponse du chat (\"Question isolee (chat)\") part vers \"Formater la "
     "reponse chat\". Toute autre reponse (formulaire) repart dans la boucle "
     "\"Chaque question\".",
     -6420, 250, 380, 260)

n("Aiguiller vers chat", "n8n-nodes-base.switch", {
    "rules": {"values": [{
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "",
                        "typeValidation": "strict", "version": 3},
            "conditions": [{
                "leftValue": "={{ $json.questionnaire }}",
                "operator": {"type": "string", "operation": "equals"},
                "rightValue": "Question isolee (chat)",
                "id": "0ae42889-764f-4f92-93e5-1d11e349f466",
            }],
            "combinator": "and",
        },
        "renameOutput": True, "outputKey": "Chat",
    }]},
    "options": {"fallbackOutput": "extra",
                "renameFallbackOutput": "Formulaire (suite de boucle)"},
}, -6352, 352, 3.4)
lien("Consolider la reponse", "Aiguiller vers chat")
lien("Aiguiller vers chat", "Formater la reponse chat", sortie=0)
lien("Aiguiller vers chat", "Chaque question", sortie=1)

FORMATER_CHAT = r"""
// Forme la reponse affichee dans le chat (reponse + sources + confiance).
const r = $json;
const blocs = [r.reponse];
if (r.citation) blocs.push('Sources : ' + r.citation);
blocs.push('Confiance : ' + Math.round((r.confiance || 0) * 100) + ' % (' + r.niveau + ')');
if (r.alerte) blocs.push('A relire : ' + r.alerte);
return [{ json: { output: blocs.join(String.fromCharCode(10, 10)) } }];
"""

n("Formater la reponse chat", "n8n-nodes-base.code",
  {"mode": "runOnceForAllItems", "jsCode": FORMATER_CHAT}, -5696, 576, 2)


# =====================================================================
#  7. Coherence d'ensemble (formulaire uniquement)
# =====================================================================
note("7 - Coherence d'ensemble",
     "**Un seul appel pour tout le formulaire.**\n\n"
     "Une contradiction entre deux reponses du meme formulaire (« 100 % de CDI » "
     "ligne 7, « hors alternants » ligne 23) est reperee immediatement par un "
     "acheteur et discredite l'ensemble.\n\n"
     "Ne concerne pas le chat, qui ne traite qu'une question a la fois.",
     -6180, -340, 440, 360, 3)

RASSEMBLER = r"""
// Une chaine LLM s'execute une fois par item : on agrege les N reponses en un
// seul item pour n'avoir qu'un appel par formulaire.
const reponses = $input.all().map(i => i.json)
  .filter(r => r && r.question)
  .sort((a, b) => (a.ligne || 0) - (b.ligne || 0));
if (!reponses.length) throw new Error("Aucune reponse produite.");
return [{ json: {
  client: reponses[0].client,
  questionnaire: reponses[0].questionnaire,
  nombre: reponses.length,
  reponses,
  resume: reponses.map(r => 'Ligne ' + r.ligne + ' | ' + r.question
                          + '\nReponse : ' + String(r.reponse).slice(0, 400)).join('\n\n'),
}}];
"""

n("Rassembler les reponses", "n8n-nodes-base.code",
  {"mode": "runOnceForAllItems", "jsCode": RASSEMBLER}, -6208, 80, 2)
lien("Chaque question", "Rassembler les reponses", sortie=0)

COHERENCE = r"""=Tu relis l'ensemble des reponses d'un meme questionnaire client avant envoi,
pour y traquer les **contradictions**.

Client : {{ $json.client }} - {{ $json.questionnaire }}
{{ $json.nombre }} reponses.

{{ $json.resume }}

## CE QUE TU CHERCHES
Une incompatibilite **factuelle** entre deux reponses : un chiffre, une date, un
perimetre, un oui/non qui ne peuvent pas etre vrais en meme temps.

Exemples de vraies contradictions :
  - « 100 % des salaries en CDI » et « hors alternants et stagiaires »
  - « bilan carbone annuel » et « bilan carbone tous les deux ans »
  - « pas de CSE » et « les representants du personnel sont consultes »

## CE QUE TU NE SIGNALES PAS
  - une difference de formulation, de longueur ou de ton ;
  - une reponse plus detaillee qu'une autre sur le meme sujet ;
  - une repetition.
Ces cas ne sont pas des contradictions. En l'absence d'incompatibilite factuelle,
renvoie une liste vide : c'est le resultat normal et attendu.

## CORRECTION
Pour chaque contradiction, corrige la ligne la **moins etayee** (la plus vague,
ou celle qui generalise). Tu ne vois pas les preuves : tu peux donc **nuancer ou
retirer** une affirmation, jamais en **ajouter** une. Si tu ne peux pas trancher
sans preuve, signale la contradiction sans proposer de correction et laisse le
relecteur humain arbitrer."""

chaine("Controle de coherence", COHERENCE, {
    "type": "object", "required": ["contradictions"],
    "properties": {"contradictions": {"type": "array", "items": {
        "type": "object", "required": ["lignes", "nature"],
        "properties": {
            "lignes": {"type": "array", "items": {"type": "integer"}},
            "nature": {"type": "string"},
            "ligne_a_corriger": {"type": ["integer", "null"]},
            "reponse_corrigee": {"type": ["string", "null"]},
        }}}},
}, -5968, 80, max_tokens=8000)
lien("Rassembler les reponses", "Controle de coherence")


# =====================================================================
#  8. Restitution (formulaire uniquement)
# =====================================================================
note("8 - Restitution",
     "Un classeur Excel par questionnaire : la reponse, la citation exacte "
     "(document + page), la confiance et l'alerte de relecture.\n\n"
     "Les lignes **A relire = OUI** sont a reprendre en priorite.\n\n"
     "Le journal Supabase est ecrit **apres** la passe de coherence : il contient "
     "donc la version finale corrigee, en un seul insert.",
     -5720, -340, 420, 340)

RESTITUTION = r"""
// Applique les corrections de coherence, puis construit le classeur.
const base = $('Rassembler les reponses').first().json;
const contradictions = ($json.output && $json.output.contradictions) || [];
const reponses = base.reponses.map(r => ({ ...r }));
const parLigne = new Map(reponses.map(r => [r.ligne, r]));

for (const c of contradictions) {
  for (const l of (c.lignes || [])) {
    const r = parLigne.get(l);
    if (!r) continue;
    r.alerte = [r.alerte, 'Coherence : ' + c.nature].filter(Boolean).join(' | ');
    if (r.niveau === 'haute') r.niveau = 'moyenne';
  }
  const cible = parLigne.get(c.ligne_a_corriger);
  if (cible && c.reponse_corrigee) {
    cible.reponse = c.reponse_corrigee;
    cible.alerte = [cible.alerte, 'Reponse ajustee pour coherence'].filter(Boolean).join(' | ');
    cible.niveau = 'a_verifier';
  }
}

return reponses.map(r => ({ json: {
  'Ligne': r.ligne,
  'Section': r.section || '',
  'Question': r.question,
  'Reponse': r.reponse,
  'Reponse courte': r.reponse_courte || '',
  'Sources (document et page)': r.citation,
  'Confiance': r.confiance,
  'Niveau': r.niveau,
  'A relire': r.niveau === 'haute' ? '' : 'OUI',
  'Alerte': r.alerte || '',
}}));
"""

n("Preparer la restitution", "n8n-nodes-base.code",
  {"mode": "runOnceForAllItems", "jsCode": RESTITUTION}, -5680, 80, 2)
lien("Controle de coherence", "Preparer la restitution")

n("Classeur de reponses", "n8n-nodes-base.convertToFile", {
    "operation": "xlsx",
    "options": {"fileName": "reponses-questionnaire.xlsx", "sheetName": "Reponses"},
}, -5440, -16, 1.1)
lien("Preparer la restitution", "Classeur de reponses")

n("Journaliser la campagne", "n8n-nodes-base.httpRequest", {
    "method": "POST",
    "url": "={{ $('Parametres').first().json.supabase_url }}/rest/v1/reponses_generees",
    "authentication": "predefinedCredentialType", "nodeCredentialType": "supabaseApi",
    "sendBody": True, "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify((() => { "
                "const base = $('Rassembler les reponses').first().json; "
                "const sources = new Map(base.reponses.map(r => [r.ligne, r.sources])); "
                "return $('Preparer la restitution').all().map(i => ({ "
                "client: base.client, questionnaire: base.questionnaire, "
                "ligne: i.json.Ligne, section: i.json.Section, question: i.json.Question, "
                "reponse: i.json.Reponse, sources: sources.get(i.json.Ligne) || [], "
                "confiance: i.json.Confiance, niveau: i.json.Niveau, "
                "alerte: i.json.Alerte })); })()) }}",
    "options": {"response": {"response": {"neverError": True}}},
}, -5440, 224, 4.2)
lien("Preparer la restitution", "Journaliser la campagne")


# =====================================================================
#  Assemblage
# =====================================================================
workflow = {
    "name": "Logiclever - Reponses aux questionnaires de conformite",
    "nodes": noeuds, "connections": liens, "active": False,
    "settings": {"executionOrder": "v1", "saveManualExecutions": True,
                 "saveExecutionProgress": True, "errorWorkflow": ""},
    "pinData": {},
    "tags": [{"name": "conformite"}, {"name": "RSE"}, {"name": "RAG"}],
    "meta": {"instanceId": "logiclever-conformite"},
}

os.makedirs(os.path.dirname(SORTIE), exist_ok=True)
with open(SORTIE, "w", encoding="utf8") as f:
    json.dump(workflow, f, ensure_ascii=False, indent=2)

reels = [x for x in noeuds if x["type"] != "n8n-nodes-base.stickyNote"]
print("Workflow ecrit : %s" % SORTIE)
print("  %d noeuds fonctionnels + %d notes" % (len(reels), len(noeuds) - len(reels)))
print("  modele utilise pour toutes les chaines LLM : %s" % MODELE)
