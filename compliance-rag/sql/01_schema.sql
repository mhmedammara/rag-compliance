-- =====================================================================
--  LOGICLEVER - Base de connaissance conformite (EcoVadis / CyberVadis)
--  Schema Supabase : RAG hybride francais (vectoriel + plein texte) + SQL
--  A executer une seule fois. Idempotent.
-- =====================================================================

-- Supabase installe ses extensions dans le schema `extensions`, un Postgres
-- nu les met dans `public`. On accepte les deux cas.
do $$
declare ext text;
begin
  foreach ext in array array['vector', 'unaccent', 'pg_trgm'] loop
    if not exists (select 1 from pg_extension where extname = ext) then
      begin
        execute format('create extension %I with schema extensions', ext);
      exception when others then
        execute format('create extension %I', ext);
      end;
    end if;
  end loop;
end $$;

-- ---------------------------------------------------------------------
-- 1. Configuration de recherche plein texte FRANCAISE, insensible aux accents
--    "securite" doit trouver "sécurité", "emissions" doit trouver "émissions",
--    et la racinisation francaise doit rapprocher "former" et "formation".
-- ---------------------------------------------------------------------
do $$
declare schema_unaccent text;
begin
  select n.nspname into schema_unaccent
  from pg_extension e join pg_namespace n on n.oid = e.extnamespace
  where e.extname = 'unaccent';

  if not exists (select 1 from pg_ts_config where cfgname = 'fr_unaccent') then
    create text search configuration public.fr_unaccent (copy = french);
    execute format(
      'alter text search configuration public.fr_unaccent '
      'alter mapping for hword, hword_part, word, asciiword, asciihword, '
      'hword_asciipart with %I.unaccent, french_stem', schema_unaccent);
  end if;
end $$;


-- ---------------------------------------------------------------------
-- 1 bis. Enveloppe d'acces a unaccent(), pour ne pas dependre du
--        search_path effectif au moment de l'appel PostgREST.
-- ---------------------------------------------------------------------
create or replace function public.sans_accent(t text)
returns text
language sql
stable
parallel safe
set search_path = public, extensions, pg_catalog
as $$ select unaccent(coalesce(t, '')) $$;


-- ---------------------------------------------------------------------
-- 2. Registre des documents de preuve (source de la citation client)
-- ---------------------------------------------------------------------
create table if not exists public.documents (
  code_doc        text primary key,
  titre           text not null,
  categorie       text not null,
  fichier_source  text,
  date_reference  date,
  statut          text not null default 'actif',       -- actif | historique | obsolete
  langue_source   text not null default 'fr',          -- langue du document d'origine
  nb_pages        int,
  md5             text,
  url_partage     text,                                -- lien Drive a fournir au client
  cree_le         timestamptz not null default now()
);

comment on table public.documents is
  'Registre canonique des documents de preuve. Les doublons et versions anterieures sont exclus.';


-- ---------------------------------------------------------------------
-- 3. Chunks documentaires
--    Table compatible avec le noeud "Supabase Vector Store" de n8n :
--    colonnes id / content / metadata / embedding.
--    Colonne tsv generee en plus pour la recherche plein texte francaise.
-- ---------------------------------------------------------------------
create table if not exists public.documents_rag (
  id          bigserial primary key,
  content     text not null,
  metadata    jsonb not null default '{}'::jsonb,
  embedding   vector(1536),
  tsv         tsvector generated always as
                (to_tsvector('public.fr_unaccent', coalesce(content, ''))) stored,
  cree_le     timestamptz not null default now()
);

create index if not exists documents_rag_embedding_idx
  on public.documents_rag using hnsw (embedding vector_cosine_ops)
  with (m = 16, ef_construction = 64);

create index if not exists documents_rag_tsv_idx
  on public.documents_rag using gin (tsv);

create index if not exists documents_rag_metadata_idx
  on public.documents_rag using gin (metadata jsonb_path_ops);

create index if not exists documents_rag_code_doc_idx
  on public.documents_rag ((metadata->>'code_doc'));


-- ---------------------------------------------------------------------
-- 4. Banque de reponses validees
--    EcoVadis, CyberVadis, formulaires clients deja envoyes, faits socles.
--    C'est la source la plus fiable : une question client ressemble presque
--    toujours a une question deja repondue.
-- ---------------------------------------------------------------------
create table if not exists public.reponses_validees (
  id            bigserial primary key,
  source_ref    text not null,                 -- ECOVADIS/LAB100, CLIENT/CGI-SBTI, FAIT/GES-SCOPES...
  origine       text not null,                 -- fait_socle | ecovadis | cybervadis | formulaire_client
  theme         text,
  sous_theme    text,
  intitule      text,
  question      text not null,                 -- toujours en francais
  question_source text,                        -- libelle d'origine (CyberVadis, en anglais)
  reponse       text not null,
  sources       jsonb not null default '[]'::jsonb,   -- [{document, pages, option}]
  fiabilite     text not null default 'a_verifier',
  statut        text not null default 'actif',       -- actif | historique (fait remplace par une valeur plus recente)
  -- poids de confiance utilise pour departager deux reponses concurrentes
  poids         real generated always as (
                  case origine
                    when 'fait_socle'        then 1.00
                    when 'ecovadis'          then 0.92
                    when 'cybervadis'        then 0.88
                    when 'formulaire_client' then 0.85
                    else 0.60
                  end) stored,
  embedding     vector(1536),
  tsv           tsvector generated always as
                  (to_tsvector('public.fr_unaccent',
                     coalesce(question, '') || ' ' || coalesce(intitule, '') || ' ' ||
                     coalesce(theme, '')    || ' ' || coalesce(sous_theme, '') || ' ' ||
                     coalesce(reponse, ''))) stored,
  maj_le        timestamptz not null default now()
);

-- Migration : "create table if not exists" ne touche pas une table deja
-- existante, donc les colonnes ajoutees apres coup doivent etre migrees
-- explicitement pour rester idempotent sur un projet deja deploye.
alter table public.reponses_validees add column if not exists statut text not null default 'actif';

create index if not exists reponses_embedding_idx
  on public.reponses_validees using hnsw (embedding vector_cosine_ops);
create index if not exists reponses_tsv_idx
  on public.reponses_validees using gin (tsv);
create unique index if not exists reponses_source_question_idx
  on public.reponses_validees (source_ref, md5(question));


-- ---------------------------------------------------------------------
-- 5. Indicateurs extra-financiers (donnees chiffrees, interrogees en SQL)
--    Une ligne = un indicateur x une annee. Permet de repondre precisement
--    a "quelle est la valeur de X en 2025 ?".
-- ---------------------------------------------------------------------
create table if not exists public.indicateurs (
  id              bigserial primary key,
  theme           text,
  contribue_a     text,
  enjeu_materiel  text,
  indicateur      text not null,
  norme_gri       text,
  objectif        text,
  methode         text,
  annee           int not null,
  valeur_texte    text,                  -- valeur telle que saisie ("1,6 t/collaborateur")
  statut          text,
  code_doc        text references public.documents(code_doc),
  date_maj        date,
  tsv             tsvector generated always as
                    (to_tsvector('public.fr_unaccent',
                       coalesce(indicateur, '') || ' ' || coalesce(theme, '') || ' ' ||
                       coalesce(contribue_a, '') || ' ' || coalesce(enjeu_materiel, '') || ' ' ||
                       coalesce(norme_gri, ''))) stored
);

create index if not exists indicateurs_tsv_idx on public.indicateurs using gin (tsv);
create index if not exists indicateurs_annee_idx on public.indicateurs (annee);
create index if not exists indicateurs_trgm_idx
  on public.indicateurs using gin (indicateur gin_trgm_ops);
create unique index if not exists indicateurs_unique_idx
  on public.indicateurs (md5(indicateur), annee, coalesce(methode, ''), coalesce(objectif, ''));


-- ---------------------------------------------------------------------
-- 6. Journal des questionnaires traites
--    Alimente la boucle d'amelioration : toute reponse relue et validee par
--    un humain peut etre reinjectee dans reponses_validees.
-- ---------------------------------------------------------------------
-- Une seule table : la campagne est identifiee par (client, questionnaire, cree_le).
-- Les reponses sont ecrites en un seul lot, apres le controle de coherence :
-- le journal contient donc toujours la version finale, corrigee.
create table if not exists public.reponses_generees (
  id                bigserial primary key,
  client            text,
  questionnaire     text,
  ligne             int,
  section           text,
  question          text not null,
  reponse           text,
  sources           jsonb default '[]'::jsonb,
  confiance         real,                            -- 0..1
  niveau            text,                            -- haute | moyenne | a_verifier | sans_reponse
  alerte            text,                            -- signalement au relecteur
  valide_par        text,
  reponse_finale    text,
  cree_le           timestamptz not null default now()
);

create index if not exists reponses_generees_campagne_idx
  on public.reponses_generees (client, cree_le desc);


-- =====================================================================
--  FONCTIONS DE RECHERCHE
-- =====================================================================

-- ---------------------------------------------------------------------
-- 6.2 recherche_hybride : fusion RRF entre recherche vectorielle et
--     recherche plein texte francaise. C'est la fonction principale.
--
--     RRF (Reciprocal Rank Fusion) : score = somme(1 / (k + rang))
--     Robuste car elle ne compare jamais deux echelles de score
--     heterogenes (cosinus vs ts_rank), seulement des rangs.
-- ---------------------------------------------------------------------
create or replace function public.recherche_hybride (
  requete            text,
  requete_embedding  vector(1536),
  nb_resultats       int     default 12,
  filtre_categories  text[]  default null,
  inclure_historique boolean default false,
  k                  int     default 50,
  poids_vectoriel    float   default 1.0,
  poids_texte        float   default 1.0,
  annee_cible        int     default null
) returns table (
  id              bigint,
  content         text,
  metadata        jsonb,
  code_doc        text,
  titre           text,
  page_debut      int,
  page_fin        int,
  score_rrf       float,
  rang_vectoriel  int,
  rang_texte      int
)
language sql
stable
set search_path = public, extensions, pg_catalog
as $$
with
requete_ts as (
  select websearch_to_tsquery('public.fr_unaccent', requete) as q
),
candidats as (
  select d.id, d.content, d.metadata, d.embedding, d.tsv
  from public.documents_rag d
  join public.documents doc on doc.code_doc = d.metadata->>'code_doc'
  where (filtre_categories is null or doc.categorie = any(filtre_categories))
    and (inclure_historique or doc.statut = 'actif')
),
vectoriel as (
  select c.id,
         row_number() over (order by c.embedding <=> requete_embedding) as rang
  from candidats c
  where c.embedding is not null and requete_embedding is not null
  order by c.embedding <=> requete_embedding
  limit greatest(nb_resultats * 5, 50)
),
plein_texte as (
  select c.id,
         row_number() over (order by ts_rank_cd(c.tsv, r.q, 32) desc) as rang
  from candidats c, requete_ts r
  where c.tsv @@ r.q
  order by ts_rank_cd(c.tsv, r.q, 32) desc
  limit greatest(nb_resultats * 5, 50)
),
fusion as (
  select coalesce(v.id, t.id) as id,
         coalesce(poids_vectoriel / (k + v.rang), 0.0) +
         coalesce(poids_texte     / (k + t.rang), 0.0) as score_rrf,
         v.rang as rang_v,
         t.rang as rang_t
  from vectoriel v
  full outer join plein_texte t on t.id = v.id
)
select d.id,
       d.content,
       d.metadata,
       d.metadata->>'code_doc'                        as code_doc,
       doc.titre                                      as titre,
       nullif(d.metadata->>'page_debut', '')::int     as page_debut,
       nullif(d.metadata->>'page_fin', '')::int       as page_fin,
       -- annee demandee : privilegie (+30%) le document de cette annee-la,
       -- penalise (-20%) les autres (utile surtout quand inclure_historique
       -- a ete active pour cette meme raison par l'appelant, sinon ce seul
       -- filtre actif/historique suffirait deja a ecarter les autres annees).
       -- L'annee vient du code_doc (ex. GES-2023, EV-FICHE-2026), pas de
       -- date_reference : celle-ci est la date de PUBLICATION du document,
       -- pas l'exercice qu'il couvre (GES-2023 a ete publie le 2025-01-13).
       (f.score_rrf * case
           when annee_cible is not null then
             case when substring(doc.code_doc from '\d{4}')::int = annee_cible
                  then 1.3 else 0.8 end
           else 1.0
         end)::float as score_rrf,
       f.rang_v::int,
       f.rang_t::int
from fusion f
join public.documents_rag d on d.id = f.id
left join public.documents doc on doc.code_doc = d.metadata->>'code_doc'
order by score_rrf desc
limit nb_resultats;
$$;


-- ---------------------------------------------------------------------
-- 6.3 recherche_reponses : banque de reponses deja validees (hybride + poids
--     de fiabilite selon l'origine).
-- ---------------------------------------------------------------------
create or replace function public.recherche_reponses (
  requete           text,
  requete_embedding vector(1536),
  nb_resultats      int    default 6,
  filtre_origines   text[] default null,
  k                 int    default 50,
  inclure_historique boolean default false,
  annee_cible       int default null
) returns table (
  id         bigint,
  source_ref text,
  origine    text,
  theme      text,
  sous_theme text,
  question   text,
  reponse    text,
  sources    jsonb,
  fiabilite  text,
  score      float
)
language sql
stable
set search_path = public, extensions, pg_catalog
as $$
with
requete_ts as (
  select websearch_to_tsquery('public.fr_unaccent', requete) as q
),
candidats as (
  select r.* from public.reponses_validees r
  where (filtre_origines is null or r.origine = any(filtre_origines))
    and (inclure_historique or r.statut = 'actif')
),
-- Une reponse n'est veritablement etayee que si au moins une de ses sources
-- pointe vers un document actif du registre ; parmi celles qui resolvent, la
-- plus recente donne la fraicheur du fait. Jointure faite ici (pas de colonne
-- stockee) pour rester a jour automatiquement si le registre change.
piece_doc as (
  select c.id,
         bool_or(d.code_doc is not null) as document_valide,
         max(d.date_reference) as date_ref,
         -- annee tiree du code_doc (ex. GES-2023), pas de date_reference qui
         -- est la date de PUBLICATION du document, pas l'exercice couvert.
         bool_or(annee_cible is not null
                 and substring(d.code_doc from '\d{4}')::int = annee_cible) as annee_correspond
  from candidats c
  left join lateral jsonb_array_elements(c.sources) s on true
  left join public.documents d
    on d.code_doc = s->>'document' and d.statut = 'actif'
  group by c.id
),
vectoriel as (
  select c.id, row_number() over (order by c.embedding <=> requete_embedding) as rang
  from candidats c
  where c.embedding is not null and requete_embedding is not null
  order by c.embedding <=> requete_embedding
  limit greatest(nb_resultats * 5, 40)
),
plein_texte as (
  select c.id,
         row_number() over (order by ts_rank_cd(c.tsv, r.q, 32) desc) as rang
  from candidats c, requete_ts r
  where c.tsv @@ r.q
  order by ts_rank_cd(c.tsv, r.q, 32) desc
  limit greatest(nb_resultats * 5, 40)
),
fusion as (
  select coalesce(v.id, t.id) as id,
         coalesce(1.0 / (k + v.rang), 0.0) + coalesce(1.0 / (k + t.rang), 0.0) as brut
  from vectoriel v
  full outer join plein_texte t on t.id = v.id
)
-- Score final : RRF x poids (fiabilite de la source) x preference document
-- (penalise -30% une reponse sans document actif resolu, sans l'exclure) x
-- correspondance temporelle :
--   - annee_cible precisee (la question vise une annee) : privilegie (+30%)
--     une reponse dont le document resolu porte cette annee-la, penalise
--     (-20%) sinon. La fraicheur generale ne s'applique pas ici : demander
--     "2023" ne doit jamais faire gagner la valeur la plus recente.
--   - annee_cible absente (la question ne precise pas d'annee) : bonus de
--     fraicheur jusqu'a +30 %, decroissance exponentielle sur 3 ans - une
--     reponse sans document resolu n'a pas de date et ne recoit aucun bonus.
select r.id, r.source_ref, r.origine, r.theme, r.sous_theme,
       r.question, r.reponse, r.sources, r.fiabilite,
       (f.brut * r.poids
         * case when coalesce(pd.document_valide, false) then 1.0 else 0.7 end
         * case
             when annee_cible is not null then
               case when coalesce(pd.annee_correspond, false) then 1.3 else 0.8 end
             else
               (1 + 0.3 * coalesce(
                   exp(-(current_date - pd.date_ref)::float / (365.0 * 3)), 0))
           end
       )::float as score
from fusion f
join public.reponses_validees r on r.id = f.id
left join piece_doc pd on pd.id = f.id
order by score desc
limit nb_resultats;
$$;


-- ---------------------------------------------------------------------
-- 6.4 recherche_indicateurs : valeurs chiffrees par indicateur et par annee.
--     Combine plein texte francais et similarite trigramme (tolerante aux
--     formulations approximatives : "turnover", "taux de rotation"...).
-- ---------------------------------------------------------------------
create or replace function public.recherche_indicateurs (
  requete      text,
  annee_cible  int default null,
  nb_resultats int default 12
) returns table (
  indicateur     text,
  theme          text,
  enjeu_materiel text,
  norme_gri      text,
  objectif       text,
  methode        text,
  annee          int,
  valeur         text,
  statut         text,
  code_doc       text,
  date_maj       date,
  score          float
)
language sql
stable
set search_path = public, extensions, pg_catalog
as $$
with q as (
  select websearch_to_tsquery('public.fr_unaccent', requete) as tq,
         public.sans_accent(lower(requete))                            as brut
)
select i.indicateur, i.theme, i.enjeu_materiel, i.norme_gri, i.objectif, i.methode,
       i.annee, i.valeur_texte, i.statut, i.code_doc, i.date_maj,
       ( case when i.tsv @@ q.tq
              then ts_rank_cd(i.tsv, q.tq, 32) * 2.0 else 0.0 end
         + similarity(public.sans_accent(lower(i.indicateur)), q.brut) )::float as score
from public.indicateurs i, q
where (annee_cible is null or i.annee = annee_cible)
  and ( i.tsv @@ q.tq
        or similarity(public.sans_accent(lower(i.indicateur)), q.brut) > 0.15 )
order by score desc, i.annee desc
limit nb_resultats;
$$;


-- ---------------------------------------------------------------------
-- 6.4 recherche_globale : LE point d'entree utilise par n8n.
--
--     Compose les trois recherches ci-dessus pour toutes les sous-requetes
--     d'une meme question, puis fusionne les rangs (RRF) entre sous-requetes.
--     Un extrait remonte par plusieurs sous-requetes gagne donc en score.
--
--     Interet : n8n n'a plus qu'un seul appel HTTP par question, et le
--     dedoublonnage n'est plus a la charge du workflow.
--
--     requetes           : ["question reformulee", "sous-requete 1", ...]
--     requete_embeddings : [[0.1, ...], [0.2, ...]]  meme ordre, meme longueur
-- ---------------------------------------------------------------------
create or replace function public.recherche_globale (
  requetes            jsonb,
  requete_embeddings  jsonb,
  annee_cible         int default null,
  nb_candidats        int default 18,
  k                   int default 20
) returns jsonb
language sql
stable
set search_path = public, extensions, pg_catalog
as $$
with req as (
  select r.ord::int                                   as rang,
         r.val #>> '{}'                               as texte,
         replace(e.val::text, ' ', '')::vector(1536)  as emb
  from jsonb_array_elements(requetes) with ordinality as r(val, ord)
  join jsonb_array_elements(requete_embeddings) with ordinality as e(val, ord)
    on e.ord = r.ord
),
-- --- banque de reponses validees (source la plus fiable : bonus de 60 %)
rep as (
  select x.id, x.source_ref, x.sous_theme, x.question, x.reponse, x.sources,
         row_number() over (partition by q.rang order by x.score desc) as rg
  from req q, lateral public.recherche_reponses(q.texte, q.emb, 6, annee_cible => annee_cible) x
),
rep_f as (
  select id, min(source_ref) as reference, min(sous_theme) as titre,
         min(question) as question, min(reponse) as texte,
         -- jsonb n'a pas d'agregat min() : chaque id n'ayant qu'une valeur,
         -- on prend la premiere. Doit rester du jsonb pour que les citations
         -- des reponses validees soient exploitables cote workflow.
         (array_agg(sources order by rg))[1] as sources,
         sum(1.6 / (k + rg)) as score
  from rep group by id
),
-- --- extraits documentaires
doc as (
  select x.id, x.code_doc, x.titre, x.page_debut, x.page_fin, x.content,
         row_number() over (partition by q.rang order by x.score_rrf desc) as rg
  from req q, lateral public.recherche_hybride(q.texte, q.emb, 10,
         inclure_historique => (annee_cible is not null), annee_cible => annee_cible) x
),
doc_f as (
  select id, min(code_doc) as reference, min(titre) as titre,
         min(page_debut) as page_debut, min(page_fin) as page_fin,
         min(content) as texte, sum(1.0 / (k + rg)) as score
  from doc group by id
),
-- --- indicateurs chiffres (bonus de 30 % : une valeur datee vaut une preuve)
ind as (
  select x.indicateur, x.annee, x.valeur, x.objectif, x.norme_gri,
         x.methode, x.code_doc,
         row_number() over (partition by q.rang order by x.score desc) as rg
  from req q, lateral public.recherche_indicateurs(q.texte, annee_cible, 8) x
),
ind_f as (
  select indicateur, annee, min(coalesce(code_doc, 'KPI-RSE')) as reference,
         min(indicateur) as titre,
         min(indicateur) || ' - annee ' || annee || ' : ' || coalesce(min(valeur), 'non mesure')
           || coalesce(' (objectif : ' || min(objectif) || ')', '')
           || coalesce(' [' || min(norme_gri) || ']', '')
           || coalesce(' - methode : ' || min(methode), '') as texte,
         sum(1.3 / (k + rg)) as score
  from ind group by indicateur, annee
),
tout as (
  select 'reponse_validee' as type, reference, titre, null::int as page_debut,
         null::int as page_fin, question, texte, sources, score from rep_f
  union all
  select 'extrait_document', reference, titre, page_debut, page_fin,
         null, texte, null::jsonb, score from doc_f
  union all
  select 'indicateur', reference, titre, null, null, null, texte, null::jsonb, score
  from ind_f
),
classes as (
  select row_number() over (order by score desc) as index, *
  from tout order by score desc limit nb_candidats
)
select jsonb_build_object(
  'candidats', coalesce((
      select jsonb_agg(jsonb_build_object(
               'index', index, 'type', type, 'reference', reference, 'titre', titre,
               'page_debut', page_debut, 'page_fin', page_fin,
               'question_source', question, 'texte', texte,
               'sources', sources, 'score', round(score::numeric, 6))
             order by score desc)
      from classes), '[]'::jsonb),
  'registre', coalesce((
      select jsonb_agg(jsonb_build_object('code', code_doc, 'titre', titre,
                                          'categorie', categorie, 'pages', nb_pages,
                                          'url_partage', url_partage)
             order by code_doc)
      from public.documents where statut = 'actif'), '[]'::jsonb)
);
$$;


-- ---------------------------------------------------------------------
-- 7. Securite : ces tables ne contiennent que des donnees d'entreprise
--    destinees a etre partagees avec les clients. RLS activee, lecture
--    ouverte au role de service utilise par n8n.
-- ---------------------------------------------------------------------
alter table public.documents           enable row level security;
alter table public.documents_rag       enable row level security;
alter table public.reponses_validees   enable row level security;
alter table public.indicateurs         enable row level security;
alter table public.reponses_generees   enable row level security;

do $$
declare t text;
begin
  foreach t in array array['documents', 'documents_rag', 'reponses_validees',
                           'indicateurs', 'reponses_generees']
  loop
    execute format(
      'drop policy if exists %I on public.%I', 'service_role_all_' || t, t);
    execute format(
      'create policy %I on public.%I for all to service_role using (true) with check (true)',
      'service_role_all_' || t, t);
  end loop;
end $$;
