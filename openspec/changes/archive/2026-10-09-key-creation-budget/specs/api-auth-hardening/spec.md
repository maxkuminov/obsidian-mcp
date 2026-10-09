## MODIFIED Requirements

### Requirement: REST API application-level auth
All `/api/*` endpoints SHALL require an authenticated session via the same `require_user_panel` dependency used by panel routes. Key creation (`POST /api/keys`) SHALL be available to every authenticated account and SHALL be owner-scoped: the created key is stamped with the authenticated account's id, and creation is bounded by the key-creation budget, the active-key cap and the admin-only unlimited rule of the `api-key-issuance` capability rather than by an admin gate. Key revocation (`DELETE /api/keys/{id}`) and limit edits (`PUT /api/keys/{id}/limit`) SHALL be permitted to the key's owner or an administrator and SHALL be refused with 403 for anyone else. Administrative endpoints (`GET /api/stats`) SHALL additionally require `require_admin_panel`. Unauthenticated requests MUST receive HTTP 401 with a JSON body in multi-user mode (a programmatic client must never be redirected to an HTML login page, #130) or rely on Traefik SSO in single-user mode.

#### Scenario: Unauthenticated API request in multi-user mode
- **WHEN** a request to `GET /api/keys` has no valid session cookie and multi-user mode is enabled
- **THEN** the server responds with HTTP 401 and a JSON body, not a redirect to the login page

#### Scenario: Authenticated non-admin API request
- **WHEN** a non-admin user with a valid session requests `GET /api/keys`
- **THEN** the server returns only the user's own keys (scoped by user_id)

#### Scenario: Key creation stamped with user_id
- **WHEN** a user creates a key via `POST /api/keys`
- **THEN** the created key has `user_id` set to the authenticated user's id

#### Scenario: A non-admin may create their own key
- **WHEN** an authenticated non-admin sends a valid `POST /api/keys` within the key-creation budget and under the active-key cap
- **THEN** the key is created and owned by that non-admin

#### Scenario: A non-admin cannot revoke another account's key
- **WHEN** an authenticated non-admin sends `DELETE /api/keys/{id}` for a key owned by a different account
- **THEN** the server responds with HTTP 403 and the key is unchanged
