-- The search-eval usage export (#356, design D1). Run only by
-- `make search-eval-export`, through docker/db-exec.sh:
--
--   psql -X -q -A -t -v ON_ERROR_STOP=1 -v days=N -v user_id=<id or empty>
--
-- It emits one JSON object per line for the keyword_search, semantic_search,
-- hybrid_search (#354) and read_note rows of the last N days, limited to the fields the miner in
-- scripts/search_eval/mining.py consumes. `grant_id` is resolved from
-- oauth_tokens because the credential principal is the OAuth grant, never the
-- client id in actor_ref (D2). `user_filter` and `deployment_users` are the
-- provenance the report's attachability rule reads (D10).
--
-- Why a plain SELECT under -A -t and not COPY ... TO STDOUT: COPY's text
-- format backslash-escapes its output, which corrupts JSON. -A (unaligned)
-- and -t (tuples only) print each value verbatim, one row per line, and
-- json_build_object never emits a raw newline, so one row is one line.
--
-- The shape is pinned by tests/test_search_eval.py: exactly the read-only
-- BEGIN, this one SELECT and COMMIT. PostgreSQL itself refuses any write
-- inside a READ ONLY transaction. Do not add a statement here.
BEGIN TRANSACTION READ ONLY;
SELECT json_build_object(
    'id', ul.id,
    'created_at', (extract(epoch FROM ul.created_at) * 1000000)::bigint,
    'tool', ul.tool,
    'user_id', ul.user_id,
    'actor_kind', ul.actor_kind,
    'actor_ref', ul.actor_ref,
    'key_id', ul.key_id,
    'oauth_token_id', ul.oauth_token_id,
    'grant_id', ot.grant_id,
    'user_filter', NULLIF(:'user_id', ''),
    'deployment_users', (SELECT count(*) FROM users),
    'params', CASE
        WHEN jsonb_typeof(ul.params) = 'object' THEN (
            SELECT COALESCE(jsonb_object_agg(e.key, e.value), '{}'::jsonb)
            FROM jsonb_each(ul.params) AS e
            WHERE e.key IN (
                'query', 'folder', 'tags', 'frontmatter', 'limit',
                'result_paths', 'result_count', 'path', 'section',
                'error', 'over_quota'
            )
        )
    END
)::text
FROM usage_logs ul
LEFT JOIN oauth_tokens ot ON ot.id = ul.oauth_token_id
WHERE ul.tool IN ('keyword_search', 'semantic_search', 'hybrid_search', 'read_note')
  AND ul.created_at >= now() - make_interval(days => :'days'::int)
  AND (:'user_id' = '' OR ul.user_id = NULLIF(:'user_id', '')::int)
ORDER BY ul.created_at, ul.id;
COMMIT;
