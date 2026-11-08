-- Look inside the RDS store: what Bedrock keeps in Aurora, in the local Postgres.
-- The Postgres version of the Dashboards "Bedrock KB — internals" view.
--
--   make local-pg-inspect                              (this file, against bedrock_kb)
--   podman exec -i local-kb_postgres_1 psql -U postgres -d bedrock -v table=bedrock_integration.other_kb < local-kb/pg/inspect.sql
--
-- Bedrock's own fields share the x-amz-bedrock-kb- prefix, inside custom_metadata beside the
-- sidecar attributes; filtering on that prefix is the Dashboards demonstration.

\if :{?table}
\else
  \set table bedrock_integration.bedrock_kb
\endif
\pset pager off
\pset null '—'

\echo '\n== Chunks per source file (Dashboards: chunks-per-file table)'
SELECT metadata->>'sourceUrl'                                  AS source,
       custom_metadata->>'x-amz-bedrock-kb-data-source-id'     AS data_source,
       count(*)                                                AS chunks,
       sum(length(chunks))                                     AS characters
FROM :table GROUP BY 1, 2 ORDER BY 1;

\echo '\n== Bedrock''s internal fields: the x-amz-bedrock-kb- prefix, one row per chunk and field'
SELECT left(id::text, 8) AS id, field.key, field.value
FROM :table, jsonb_each(custom_metadata) AS field
WHERE field.key LIKE 'x-amz-bedrock-kb-%'
ORDER BY metadata->>'sourceUrl', id, field.key;

\echo '\n== Sidecar attributes (everything else in custom_metadata), with their JSON types'
SELECT field.key, jsonb_typeof(field.value) AS type, field.value, count(*) AS chunks
FROM :table, jsonb_each(custom_metadata) AS field
WHERE field.key NOT LIKE 'x-amz-bedrock-kb-%'
GROUP BY 1, 2, 3 ORDER BY 1, 3::text;

\echo '\n== The metadata column (json): what Bedrock keeps beside the text'
SELECT DISTINCT jsonb_object_keys(metadata::jsonb) AS key FROM :table;

\echo '\n== Vectors: dimensions and norms (Qwen3 and Titan both emit unit vectors)'
SELECT vector_dims(embedding) AS dims, count(*) AS chunks,
       round(min(vector_norm(embedding))::numeric, 4) AS min_norm,
       round(max(vector_norm(embedding))::numeric, 4) AS max_norm
FROM :table GROUP BY 1;

\echo '\n== Indexes and sizes (Bedrock checks for the hnsw and both gin indexes at every sync)'
SELECT i.indexrelid::regclass AS index, am.amname AS method,
       pg_size_pretty(pg_relation_size(i.indexrelid)) AS size,
       pg_get_indexdef(i.indexrelid) AS definition
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am am ON am.oid = c.relam
WHERE i.indrelid = :'table'::regclass ORDER BY 1;

SELECT pg_size_pretty(pg_relation_size(:'table'::regclass))       AS table_size,
       pg_size_pretty(pg_total_relation_size(:'table'::regclass)) AS with_indexes_and_toast,
       n_live_tup AS live_rows, n_dead_tup AS dead_rows
FROM pg_stat_user_tables WHERE relid = :'table'::regclass;

\echo '\n== Search settings for new sessions (ALTER DATABASE … SET, as AWS''s guide recommends)'
SELECT name, current_setting(name) AS setting FROM pg_settings
WHERE name IN ('hnsw.ef_search', 'hnsw.iterative_scan', 'hnsw.max_scan_tuples',
               'default_text_search_config', 'shared_buffers', 'maintenance_work_mem')
ORDER BY name;
