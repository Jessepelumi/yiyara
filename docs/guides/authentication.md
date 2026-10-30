# Authentication

All examples use `http://localhost:8000`, JSON bodies and trailing slashes.
Protected routes require `Authorization: Bearer <Yiyara access token>` and scope
objects to the signed-in user. Public exceptions are sign-in, refresh and the
Google Calendar callback. Missing/expired access returns 401.

## Google configuration

Set `GOOGLE_CLIENT_IDS` to the comma-separated allowed Google OAuth client IDs.
One web client is enough for the current flow. Tokens must identify a verified
Google email and match an allowed audience. Calendar consent is configured
[separately](calendar.md#google-configuration); the same web client may be used.

When obtaining a Google ID token in Postman, use the web client's authorization-code
flow, scope `openid email profile`, and the callback URL shown by Postman. Register
that exact callback in Google; do not substitute the backend Calendar callback.

## Application tokens

| Request | Body / result |
| --- | --- |
| `POST /api/users/auth/google/` | `{"id_token":"<Google ID token>"}` → access, refresh and user |
| `GET /api/users/me/` | Current user's public account fields |
| `POST /api/users/auth/refresh/` | `{"refresh":"<Yiyara refresh token>"}` → new access and refresh |
| `POST /api/users/auth/logout/` | Bearer access plus `{"refresh":"<Yiyara refresh token>"}` → 204 |

A Google ID token proves identity. Google's access token is not the Yiyara access
token and must not be used to authorize application requests.

Access defaults to **30 minutes**, configurable with `JWT_ACCESS_TOKEN_MINUTES`.
Refresh tokens last **seven days**. Successful refresh rotates and blacklists the
used refresh token; save both replacements. Each rotated token receives another
seven days, so there is no fixed seven-day maximum signed-in session. Previously
issued tokens retain their original expiry when settings change.

Refresh is a separate request, not an automatic backend action. The frontend must
implement renewal and handle failed refresh by asking for sign-in. Logout revokes
the supplied refresh token; already issued access tokens remain valid until expiry.
Inactive accounts cannot sign in or refresh.

## Existing accounts

An unlinked account with the same email returns 409; email matching alone does not
link identities. An administrator can link it using a verified Google ID token:

```sh
docker compose exec backend python manage.py link_google_account --email user@example.com
```

The command prompts for the token without displaying it. Do not put it in command
arguments. The verified Google email must match the existing active account.
