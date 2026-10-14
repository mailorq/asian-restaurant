# Identity signing keys & authorization

## Local development
```bash
scripts/dev_gen_jwt_key.sh   # writes ops/identity/jwt_private_key.pem (gitignored)
```
`compose.yaml` mounts that key into `backend` read-only. It is never committed.

## Production
The private key is a **read-only secret file** mounted at
`/run/secrets/identity_jwt_private_key`, pointed to by `IDENTITY_JWT_PRIVATE_KEY_FILE`
(`compose.prod.yaml` uses a Docker secret). The secret keeps the host file's ownership and
mode, and the backend reads it as uid/gid 10001: `root:10001`, mode `0440` (see DEPLOY.md).
Production **rejects** an inline
`IDENTITY_JWT_PRIVATE_KEY`: with `DJANGO_PRODUCTION=1` the settings fail closed if the
secret file is missing/unreadable, if the dev `kid` is used, if an inline key is passed,
or if `SECRET_KEY`/`DEBUG`/`ALLOWED_HOSTS` use dev defaults. Inline
`IDENTITY_JWT_PRIVATE_KEY` is a **dev-only** convenience and is never valid in production.

## Key rotation (no downtime)
Two keys may be published in the JWKS at once, distinguished by `kid`:
1. generate a new key file, point `IDENTITY_JWT_PRIVATE_KEY_FILE` at it with a new
   `IDENTITY_JWT_KID`;
2. keep the old public key as `IDENTITY_JWT_PREVIOUS_PUBLIC_KEY(_FILE)` +
   `IDENTITY_JWT_PREVIOUS_KID` so tokens signed before the switch still verify;
3. after the old TTL window elapses, drop the previous key.
New tokens sign with the current key; operations selects the verifying key by `kid`.

Each backend process reads and parses its keys once, so every step above takes effect only after the
backend is restarted (`dc up -d backend` recreates it); writing a new key into the same file changes nothing
until then. Operations keeps the fetched JWKS for 5 minutes and fetches again for a `kid` it does not know.
If identity's JWKS cannot be fetched, operations answers `503` with `Retry-After`, not `401`: an unreachable key
says nothing about the token, and the panel shows the service as unavailable rather than rights as changed.

## Published keys are refused
A signing key whose private part is public lets anyone sign a staff token. `config/published_keys.py` lists such
keys by the sha256 of their public part, and production refuses to start when the signing key or the previous
public key is one of them. A key that leaks is added to that list, and every environment that used it generates
a fresh key and rotates as above.

## Authorization lifecycle & revocation SLO
`employee.service.set_employee_role` is the only path that changes staff membership: it
atomically flips the group, bumps `User.authz_version`, appends `EmployeeRoleAudit`, and
publishes `identity.authz_changed.v1` through the outbox. Operations projects it into
`EmployeeAuthorization` and requires, for every request: `token.authz_version ==
projected authz_version`, `role_active`, and `user_active` — failing closed for an
unknown or stale subject.

**Revocation SLO:** a revocation is enforced once the `identity.authz_changed.v1` event
reaches operations (outbox relay + bridge + consumer). Target ≤ 5s under normal load;
until then the short token TTL (default 600s) is the outer bound. Sensitive command
endpoints must reject when the authorization projection is missing or older than the
token, never silently accept.
