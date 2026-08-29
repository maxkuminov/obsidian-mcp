## 1. Telemetry

- [x] 1.1 timing.record result_count + result_paths (≤10 paths, ≤2048 bytes, enforced at record site) in keyword_search/semantic_search/find_related service returns, plus find_related's `source_path` key (full path ≤1024 bytes, else sha256 hex); verify params in usage_logs rows incl. a long-paths case

## 2. Panel

- [x] 2.1 Search-analytics page: top queries + zero-result queries for keyword/semantic search; find_related tables grouped by source path; window selector; error/refusal rows excluded
- [x] 2.2 Coverage section: top-logged retrievals (labeled as first-10-logged appearances) / never-retrieved, cap caveat beside both
- [x] 2.3 Nav entry; theme token partial

## 3. Docs and verification

- [x] 3.1 docs/architecture/search.md + usage-attribution.md notes on the new param keys
- [x] 3.2 End-to-end (live, post-deploy): real semantic_search via /mcp with a throwaway key recorded result_count 3 + 3 result_paths + embed_ms in params; /admin/search-analytics returns 200; alembic check clean
