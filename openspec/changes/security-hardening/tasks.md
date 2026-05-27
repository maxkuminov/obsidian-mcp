## 1. CSRF Protection

- [x] 1.1 Create `src/csrf.py` module with `generate_csrf_token(session)` and `validate_csrf_token(session, token)` using `itsdangerous.URLSafeTimedSerializer` (1-hour max age). Both functions should no-op gracefully when session middleware is not active (single-user mode).
- [x] 1.2 Add `csrf_token` to `_panel_context()` in `src/control_panel/routes.py` so every template has access to it
- [x] 1.3 Add CSRF validation to all panel POST handlers in `src/control_panel/routes.py` — create a FastAPI dependency `verify_csrf` that reads the `csrf_token` form field and raises 403 on failure; attach it to the router
- [x] 1.4 Add CSRF validation to all POST handlers in `src/control_panel/users.py` (same dependency)
- [x] 1.5 Add CSRF validation to auth POST handlers in `src/auth/routes.py` (`login`, `register`, `logout`) — login and register forms need the token in the template context too
- [x] 1.6 Add `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">` to every POST form in templates: `keys.html`, `oauth.html`, `settings.html`, `reembed_confirm.html`, `user_edit.html`, `users.html`, `base.html` (logout form), `login.html`, `register.html`

## 2. REST API Auth

- [x] 2.1 Add `require_user_panel` dependency to the `/api` router in `src/api/routes.py` and scope `GET /api/keys`, `GET /api/usage` by user_id for non-admins
- [x] 2.2 Gate `POST /api/keys` and `DELETE /api/keys/{id}` behind user ownership check (non-admins can only manage their own keys); stamp `user_id` on created keys
- [x] 2.3 Gate `GET /api/stats` behind `require_admin_panel` (it reveals system-wide counts)

## 3. API Key URL Leak Fix

- [x] 3.1 Change `create_key_form` in `src/control_panel/routes.py` to store the raw key in `request.session["flash_new_key"]` instead of the URL query param
- [x] 3.2 Update `keys_page` to read and clear `request.session.pop("flash_new_key", None)` and pass it to the template
- [x] 3.3 Update `keys.html` template to read `new_key` from the template context (already does, just verify the source changed)

## 4. OAuth Timing-Safe Comparisons

- [x] 4.1 Replace `client.client_secret_hash != _hash(client_secret)` with `not secrets.compare_digest(client.client_secret_hash, _hash(client_secret))` in `_handle_auth_code` in `src/oauth/routes.py`
- [x] 4.2 Apply the same fix in `_handle_refresh` in `src/oauth/routes.py`

## 5. Request Trust & Input Validation

- [x] 5.1 Change `ProxyHeadersMiddleware` in `src/main.py` from `trusted_hosts="*"` to `trusted_hosts=["127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]`
- [x] 5.2 Add path traversal validation to the `folder` query param in `vault_page` in `src/control_panel/routes.py` — resolve against vault root and fall back to root on traversal

## 6. Auth Hardening

- [x] 6.1 Add `@limiter.limit("5/minute")` to `POST /admin/auth/login` in `src/auth/routes.py`
- [x] 6.2 Add session fixation prevention: call `request.session.clear()` before populating session data in `login_submit` and `register_submit` in `src/auth/routes.py`
- [x] 6.3 Change `_validate_multi_user_secret` in `src/config.py` to reject `secret_key="changeme"` in all modes (remove the `multi_user_mode` gate), update error message to suggest `secrets.token_hex(32)`

## 7. Cosmetic Security Fixes

- [x] 7.1 Update `_mask_openai_key` in `src/control_panel/routes.py` to show only `***...` + last 4 chars (instead of first 8 + last 4)
