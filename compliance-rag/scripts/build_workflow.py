# -*- coding: utf-8 -*-
"""
Genere le workflow n8n :
    workflows/logiclever_reponses_questionnaires.json

Chaine deterministe : aucun agent ne choisit ses outils, toutes les sources sont
interrogees pour chaque question. Une meme question donne toujours la meme reponse.

  1. Analyse de la question   Haiku, sortie structuree (sous-requetes)
  2. Vectorisation            OpenAI embeddings, 1 appel
  3. Recherche                1 appel a recherche_globale (Supabase) : banque +
                              documents + indicateurs + registre, fusion RRF en SQL
  4. Reclassement             Sonnet, note de pertinence par candidat
  5. Redaction                Opus, sortie structuree + citations
  6. Controle de veracite     Sonnet, verification contre les preuves
  --- fin de boucle ---
  7. Controle de coherence    Sonnet, 1 appel pour tout le formulaire
  8. Restitution Excel + journal Supabase (un seul insert, apres correction)

Opus est reserve a la redaction, seul texte lu par le client.
Detail des couts : GUIDE-DEPLOIEMENT.md.
"""
import json
import os

ICI = os.path.dirname(os.path.abspath(__file__))
SORTIE = os.path.join(os.path.dirname(ICI), "workflows",
                      "logiclever_reponses_questionnaires.json")

ANTHROPIC = "@n8n/n8n-nodes-langchain.lmChatAnthropic"
CHAIN = "@n8n/n8n-nodes-langchain.chainLlm"
PARSER = "@n8n/n8n-nodes-langchain.outputParserStructured"

OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"

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


def chaine(nom, prompt, schema, modele_id, x, y, max_tokens=4000, temperature=0.0):
    """Une chaine LLM = 3 noeuds : la chaine, son modele, son schema de sortie."""
    n(nom, CHAIN, {"promptType": "define", "text": prompt, "hasOutputParser": True,
                   "messages": {"messageValues": []}}, x, y, 1.5)
    m = n("Modele : " + nom, ANTHROPIC, {
        "model": {"__rl": True, "mode": "list", "value": modele_id,
                  "cachedResultName": modele_id},
        "options": {"temperature": temperature, "maxTokensToSample": max_tokens},
    }, x - 40, y + 200, 1.3)
    p = n("Schema : " + nom, PARSER, {
        "schemaType": "manual",
        "inputSchema": json.dumps(schema, ensure_ascii=False, indent=2),
    }, x + 110, y + 200, 1.2)
    lien(m, nom, type_="ai_languageModel")
    lien(p, nom, type_="ai_outputParser")
    return nom


# =====================================================================
#  1. Entrees
# =====================================================================
note("1 - Entree",
     "Le formulaire est la seule porte d'entree : on y depose le questionnaire "
     "client (.xlsx/.csv) recu du service achats.\n\n"
     "La colonne des questions est detectee automatiquement : chaque client a "
     "son propre gabarit, il n'y a rien a normaliser en amont.\n\n"
     "Pour une question isolee, deposer un fichier d'une seule ligne.",
     -700, -300, 440, 320)

n("Depot d'un questionnaire", "n8n-nodes-base.formTrigger", {
    "formTitle": "Questionnaire de conformite client",
    "formDescription": "Deposez le questionnaire RSE / securite recu du service achats. "
                       "Les reponses sont redigees a partir des documents de preuve "
                       "Logiclever et systematiquement sourcees.",
    "formFields": {"values": [
        {"fieldLabel": "Client", "requiredField": True},
        {"fieldLabel": "Intitule du questionnaire", "requiredField": False},
        {"fieldLabel": "Fichier", "fieldType": "file",
         "acceptFileTypes": ".xlsx,.xls,.csv", "requiredField": True},
        {"fieldLabel": "Consignes particulieres", "fieldType": "textarea",
         "requiredField": False},
    ]},
    "options": {"appendAttribution": False},
}, -660, 60, 2.2)

n("Lire le fichier", "n8n-nodes-base.extractFromFile", {
    "operation": "xlsx", "binaryPropertyName": "Fichier",
    "options": {"headerRow": False, "includeEmptyCells": True},
}, -420, 60, 1)
lien("Depot d'un questionnaire", "Lire le fichier")

NORMALISER = r"""
// Transforme la feuille deposee en une liste homogene de questions.
//
// La colonne des questions n'est jamais au meme endroit d'un client a l'autre :
// on retient celle qui contient le plus de cellules ressemblant a une question.

const sansAccent = (s) => (s || '').toString()
  .normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

const lignes = $input.all().map(i => i.json);
if (!lignes.length) throw new Error("Aucune ligne exploitable dans le fichier.");

const colonnes = [];
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
  throw new Error("Impossible d'identifier la colonne des questions. Colonnes vues : "
                  + colonnes.join(', '));
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
  {"mode": "runOnceForAllItems", "jsCode": NORMALISER}, -180, 60, 2)
lien("Lire le fichier", "Normaliser les questions")

n("Parametres", "n8n-nodes-base.set", {
    "mode": "manual", "duplicateItem": False,
    "fields": {"values": [
        {"name": "supabase_url", "stringValue": "https://VOTRE-PROJET.supabase.co"},
        {"name": "modele_embedding", "stringValue": "text-embedding-3-large"},
        {"name": "dimensions", "type": "numberValue", "numberValue": 1536},
        {"name": "nb_candidats", "type": "numberValue", "numberValue": 18},
        {"name": "seuil_haute_confiance", "type": "numberValue", "numberValue": 0.75},
        {"name": "seuil_a_verifier", "type": "numberValue", "numberValue": 0.5},
    ]},
    "include": "all", "options": {},
}, 60, 60, 3.4)
lien("Normaliser les questions", "Parametres")

n("Chaque question", "n8n-nodes-base.splitInBatches",
  {"batchSize": 1, "options": {"reset": False}}, 300, 60, 3)
lien("Parametres", "Chaque question")


# =====================================================================
#  2. Analyse
# =====================================================================
note("2 - Analyse de la question",
     "Reformule la question et produit 2 a 4 **sous-requetes** exprimees dans le "
     "vocabulaire des documents internes.\n\n"
     "C'est ce qui separe une recherche mot-a-mot d'une recherche qui trouve la "
     "bonne preuve : « controle de vos fournisseurs » devient « politique achats "
     "responsables clauses RSE contrats fournisseurs ».\n\n"
     "Modele : Haiku - tache cadree, la qualite de redaction n'intervient pas.",
     540, -300, 420, 340)

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
}, HAIKU, 580, 60, max_tokens=1200)

lien("Chaque question", "Analyser la question", sortie=1)


# =====================================================================
#  3. Vectorisation + recherche
# =====================================================================
note("3 - Recherche",
     "**Un seul appel HTTP par question.**\n\n"
     "`recherche_globale` interroge cote SQL les quatre sources - banque de "
     "reponses validees, extraits documentaires, indicateurs chiffres, registre - "
     "pour chacune des sous-requetes, puis fusionne les rangs (RRF).\n\n"
     "Fusion et dedoublonnage sont faits en SQL : le workflow n'a plus de code "
     "de tri a maintenir, et la logique se teste directement en base.",
     980, -300, 460, 340)

n("Vectoriser les requetes", "n8n-nodes-base.httpRequest", {
    "method": "POST", "url": "https://api.openai.com/v1/embeddings",
    "authentication": "predefinedCredentialType", "nodeCredentialType": "openAiApi",
    "sendBody": True, "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify({ "
                "model: $('Parametres').first().json.modele_embedding, "
                "dimensions: $('Parametres').first().json.dimensions, "
                "input: [$json.output.question_normalisee]"
                ".concat($json.output.sous_requetes).slice(0, 5) }) }}",
    "options": {},
}, 1020, 60, 4.2)
lien("Analyser la question", "Vectoriser les requetes")

n("Recherche", "n8n-nodes-base.httpRequest", {
    "method": "POST",
    "url": "={{ $('Parametres').first().json.supabase_url }}/rest/v1/rpc/recherche_globale",
    "authentication": "predefinedCredentialType", "nodeCredentialType": "supabaseApi",
    "sendBody": True, "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify({ "
                "requetes: [$('Analyser la question').first().json.output.question_normalisee]"
                ".concat($('Analyser la question').first().json.output.sous_requetes).slice(0, 5), "
                "requete_embeddings: $json.data.sort((a, b) => a.index - b.index)"
                ".map(d => d.embedding), "
                "annee_cible: $('Analyser la question').first().json.output.annee_cible ?? null, "
                "nb_candidats: $('Parametres').first().json.nb_candidats }) }}",
    "options": {"response": {"response": {"neverError": True}}},
}, 1260, 60, 4.2)
lien("Vectoriser les requetes", "Recherche")


# =====================================================================
#  4. Reclassement
# =====================================================================
note("4 - Reclassement",
     "Le RRF classe par **convergence** entre sous-requetes, pas par pertinence "
     "reelle. Sonnet note ici chaque candidat de 0 a 10 pour cette question "
     "precise.\n\n"
     "C'est l'etape qui evite de citer la bonne politique a la mauvaise page.",
     1500, -300, 400, 300)

RECLASSEMENT = r"""=Tu selectionnes les preuves qui serviront a repondre a une question de conformite.

QUESTION : {{ $('Chaque question').first().json.question }}
Reformulation : {{ $('Analyser la question').first().json.output.question_normalisee }}
Type de reponse attendu : {{ $('Analyser la question').first().json.output.type_reponse }}

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
}, SONNET, 1540, 60, max_tokens=2000)
lien("Recherche", "Reclasser les preuves")

DOSSIER = r"""
// Construit le dossier de preuves transmis au redacteur.
//
// Repli borne : si le reclassement n'a rien retenu, on reprend les 5 meilleurs
// candidats du RRF plutot que de renoncer. Ils sont deja en memoire : cela ne
// coute ni appel ni latence, et la confiance restera basse.

const recherche = $('Recherche').first().json;
const candidats = recherche.candidats || [];
const q = $('Chaque question').first().json;
const analyse = $('Analyser la question').first().json.output;
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
  consignes: q.consignes || '', analyse,
  dossier, nb_preuves: dossier.length, repli,
  registre: recherche.registre || [],
}}];
"""

n("Constituer le dossier de preuves", "n8n-nodes-base.code",
  {"mode": "runOnceForAllItems", "jsCode": DOSSIER}, 1800, 60, 2)
lien("Reclasser les preuves", "Constituer le dossier de preuves")


# =====================================================================
#  5. Redaction
# =====================================================================
note("5 - Redaction",
     "**Seule etape sur Opus** : c'est le texte que l'acheteur lit.\n\n"
     "Regle centrale : chaque affirmation doit etre portee par une preuve du "
     "dossier. Une force se dit pleinement ; une faiblesse se dit avec la mesure "
     "compensatoire et l'echeance - jamais en la niant.",
     2040, -340, 440, 380, 3)

REDACTION = r"""=Tu rediges la reponse officielle de LOGICLEVER a une question d'un service
achats client. Cette reponse sera lue par un acheteur ou un auditeur.

## L'ENTREPRISE
LOGICLEVER, SASU francaise de conseil en systemes d'information (ESN), 50 salaries
(48 Paris, 2 Lyon), sans societe mere. Entreprise liberee depuis 2018. ~90 % du CA
avec le secteur de l'energie.
Evaluations externes : EcoVadis 79/100 (98e percentile, top 2 %), CyberVadis
860/1000 (niveau Mature, benchmark 654), Great Place To Work (Trust Index 96 %).

## LA QUESTION
Section : {{ $json.section || 'non precisee' }}
Question : {{ $json.question_client }}
Type de reponse attendu : {{ $json.analyse.type_reponse }}
{{ $json.consignes ? 'Consignes du client : ' + $json.consignes : '' }}
{{ ($json.analyse.points_de_vigilance || []).length ? 'Points de vigilance : ' + $json.analyse.points_de_vigilance.join(' | ') : '' }}

## DOSSIER DE PREUVES
{{ $json.dossier.length === 0 ? 'AUCUNE PREUVE PERTINENTE TROUVEE.' : $json.dossier.map(d => '### Preuve ' + d.rang + ' - ' + d.citation + ' (' + d.titre + ')\n' + d.texte).join('\n\n') }}
{{ $json.repli ? '\nATTENTION : aucune preuve n a passe le seuil de pertinence. Les elements ci-dessus sont approximatifs : reponds prudemment et mets confiance a 0.35 au maximum.' : '' }}

## REGLES DE REDACTION

**1. Ancrage factuel - non negociable.**
Chaque affirmation doit provenir du dossier ci-dessus. N'invente aucun chiffre,
aucune date, aucun nom de certification, aucun engagement. Si le dossier ne
permet pas de repondre, dis-le : confiance a 0.3 ou moins, et redige la meilleure
reponse partielle possible en signalant ce qui manque.

**2. Ne jamais revendiquer ce que LOGICLEVER ne detient pas.**
Interdits absolus, sauf preuve explicite au dossier :
  - certification ISO 9001, ISO 14001, ISO 27001 (aucune n'est detenue) ;
  - objectif valide par la SBTi (statut : non applique) ;
  - CSE ou representation syndicale (PV de carence) ;
  - toute certification, label ou adhesion absent du dossier.
Sur ces sujets, dire ce qui existe reellement a la place. Exemple attendu :
« Logiclever n'est pas certifiee ISO 27001. Sa Politique de Securite des Systemes
d'Information est toutefois construite sur le referentiel ISO 27001, un objectif
de certification y est inscrit, et le dispositif a ete evalue par un tiers
independant : CyberVadis attribue 860/1000, niveau Mature. »

**3. Registre : affirmatif, precis, complet.**
Tu ecris pour un acheteur qui compare des fournisseurs. Une reponse exacte mais
timide dessert autant qu'une reponse fausse.
  - Commence par la reponse directe (Oui / Non / le chiffre), puis justifie.
  - Cite les chiffres avec leur annee et leur unite, en privilegiant le dernier
    exercice.
  - Mets en avant les evaluations externes : elles valent plus qu'une declaration.
  - Nomme les dispositifs (politique, procedure, registre, frequence de revue) :
    la maturite se prouve par le processus, pas par l'intention.

**4. Traiter les points faibles sans les masquer.**
Quand un indicateur est en retrait de l'objectif : donner la valeur reelle, puis
la trajectoire, la mesure en place et l'echeance. Ne pas l'omettre - un acheteur
qui decouvre l'ecart ensuite perd confiance dans tout le reste.

**5. Format.**
Francais professionnel, phrases completes. 3 a 8 phrases pour une question
ouverte, 1 a 3 pour une question fermee. Aucun marqueur interne (pas de
« Preuve 3 », pas de « d'apres le dossier ») : les references vont dans le champ
sources.

**6. Confiance (0 a 1).**
  0.9-1.0  preuve directe, chiffree, datee, issue de la banque de reponses validees
  0.7-0.89 preuve documentaire claire et a jour
  0.5-0.69 reponse construite par recoupement de plusieurs documents
  0.3-0.49 preuve partielle ou perimetre incertain
  < 0.3    pas de preuve exploitable"""

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
}, OPUS, 2080, 60, max_tokens=3000, temperature=0.15)
lien("Constituer le dossier de preuves", "Rediger la reponse")


# =====================================================================
#  6. Controle de veracite
# =====================================================================
note("6 - Controle de veracite",
     "Second passage, avec un oeil neuf : chaque affirmation est confrontee au "
     "dossier de preuves.\n\n"
     "Detecte l'affirmation non etayee, la certification revendiquee a tort, le "
     "chiffre deforme, la source qui ne dit pas ce qu'on lui fait dire.",
     2540, -340, 420, 340, 3)

CONTROLE = r"""=Tu es le relecteur conformite de LOGICLEVER. Tu verifies un projet de reponse
avant envoi a un client. Ton role n'est pas de reecrire par gout, mais
d'empecher qu'une affirmation non etayee parte chez le client.

## QUESTION POSEE
{{ $('Constituer le dossier de preuves').first().json.question_client }}

## PROJET DE REPONSE
{{ $json.output.reponse }}

## SOURCES CITEES
{{ JSON.stringify($json.output.sources) }}

## DOSSIER DE PREUVES DISPONIBLE
{{ $('Constituer le dossier de preuves').first().json.dossier.length === 0 ? 'VIDE' : $('Constituer le dossier de preuves').first().json.dossier.map(d => '### ' + d.citation + ' (' + d.titre + ')\n' + d.texte).join('\n\n') }}

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
}, SONNET, 2580, 60, max_tokens=3000)
lien("Rediger la reponse", "Controler la veracite")

CONSOLIDER = r"""
// Assemble le resultat de la question et le renvoie dans la boucle.
const d = $('Constituer le dossier de preuves').first().json;
const redaction = $('Rediger la reponse').first().json.output;
const controle = $json.output;
const p = $('Parametres').first().json;
const q = $('Chaque question').first().json;

const confiance = controle.confiance_finale ?? redaction.confiance ?? 0;
const niveau = controle.niveau
  || (confiance >= p.seuil_haute_confiance ? 'haute'
      : confiance >= p.seuil_a_verifier ? 'moyenne' : 'a_verifier');

const sources = (redaction.sources || []).map(s => {
  const fiche = (d.registre || []).find(r => r.code === s.document);
  return { document: s.document, titre: fiche ? fiche.titre : s.document,
           page: s.page || '' };
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
  citation: sources.map(s => s.titre + (s.page ? ' (p. ' + s.page + ')' : '')).join(' ; '),
  confiance: Number(confiance.toFixed(3)),
  niveau,
  alerte: alertes.join(' | '),
  nb_preuves: d.nb_preuves,
}}];
"""

n("Consolider la reponse", "n8n-nodes-base.code",
  {"mode": "runOnceForAllItems", "jsCode": CONSOLIDER}, 2840, 60, 2)
lien("Controler la veracite", "Consolider la reponse")
lien("Consolider la reponse", "Chaque question")


# =====================================================================
#  7. Coherence d'ensemble
# =====================================================================
note("7 - Coherence d'ensemble",
     "**Un seul appel pour tout le formulaire.**\n\n"
     "Une contradiction entre deux reponses du meme formulaire (« 100 % de CDI » "
     "ligne 7, « hors alternants » ligne 23) est reperee immediatement par un "
     "acheteur et discredite l'ensemble.\n\n"
     "Le traitement question par question ne peut structurellement pas la voir : "
     "il faut cette passe sur l'ensemble.",
     3100, -340, 440, 360, 3)

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
  {"mode": "runOnceForAllItems", "jsCode": RASSEMBLER}, 3140, 60, 2)
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
}, SONNET, 3380, 60, max_tokens=4000)
lien("Rassembler les reponses", "Controle de coherence")


# =====================================================================
#  8. Restitution
# =====================================================================
note("8 - Restitution",
     "Un classeur Excel par questionnaire : la reponse, la citation exacte "
     "(document + page), la confiance et l'alerte de relecture.\n\n"
     "Les lignes **A relire = OUI** sont a reprendre en priorite.\n\n"
     "Le journal Supabase est ecrit **apres** la passe de coherence : il contient "
     "donc la version finale corrigee, en un seul insert.",
     3640, -340, 420, 340)

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
  {"mode": "runOnceForAllItems", "jsCode": RESTITUTION}, 3680, 60, 2)
lien("Controle de coherence", "Preparer la restitution")

n("Classeur de reponses", "n8n-nodes-base.convertToFile", {
    "operation": "xlsx",
    "options": {"fileName": "reponses-questionnaire.xlsx", "sheetName": "Reponses"},
}, 3920, -40, 1.1)
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
    "executeOnce": True,
    "options": {"response": {"response": {"neverError": True}}},
}, 3920, 200, 4.2)
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
print("  modeles :")
for x in noeuds:
    if x["type"] == ANTHROPIC:
        print("     %-44s %s" % (x["name"][:44], x["parameters"]["model"]["value"]))
