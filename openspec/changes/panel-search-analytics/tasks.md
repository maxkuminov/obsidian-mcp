## 1. Telemetry

- [ ] 1.1 timing.record result_count + result_paths (≤10 paths, ≤2048 bytes, enforced at record site) in keyword_search/semantic_search/find_related service returns; verify params in usage_logs rows incl. a long-paths case

## 2. Panel

- [ ] 2.1 Search-analytics page: top queries + zero-result queries for keyword/semantic search; find_related tables grouped by source path; window selector; error/refusal rows excluded
- [ ] 2.2 Coverage section: top-logged retrievals (labeled as first-10-logged appearances) / never-retrieved, cap caveat beside both
- [ ] 2.3 Nav entry; theme token partial

## 3. Docs and verification

- [ ] 3.1 docs/architecture/search.md + usage-attribution.md notes on the new param keys
- [ ] 3.2 End-to-end against live server: issue searches incl. a guaranteed zero-result query; verify all page sections
