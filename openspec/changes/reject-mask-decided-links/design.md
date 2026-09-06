## Decision

Compare the original and masked deciding spans using existing parser offsets.
For wikilinks use the target capture only; for markdown links use the href
span only. Skip a changed span rather than resolving it or recording an
invented dangling target. Masking outside these spans does not decide a target.
Do this before cap accounting so rejected candidates cannot hide valid links
or claim truncation. Keep the existing bounded scanner and its complexity.

Extraction version 3 shares version 2's cleaner. Existing stale-version scan
transactions replace links and stamp metadata atomically, without changing
content hashes or invalidating embeddings solely for this link-only change.
No new write-tool behavior is introduced. Deployment triggers one re-derivation
pass and temporarily refuses rewrite_links for scopes with stale markers.
