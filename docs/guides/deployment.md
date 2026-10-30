# Deployment boundary

Local operation is covered in [setup](setup.md). No production deployment or
frontend deployment is supplied by this repository's Compose file.

## Runtime

The Docker image starts Gunicorn using [config/gunicorn.py](../../config/gunicorn.py).
Compose overrides that with runserver for development; do not deploy that command.
Use a production PostgreSQL database and apply migrations before serving traffic.

Set `DEBUG=False`, a production `SECRET_KEY`, and the real `ALLOWED_HOSTS`.
`CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` accept comma-separated browser
origins. Set `FRONTEND_URL` to the fixed Calendar success-return URL if wanted.
Do not embed OAuth codes or tokens in that URL.

Outside debug, HTTPS redirects and secure cookies default on. Terminate TLS at a
controlled proxy. Enable `TRUST_PROXY_HTTPS` only when that proxy sets and sanitizes
`X-Forwarded-Proto`. Configure HSTS after HTTPS has been verified.

Gunicorn bind/workers/threads/timeout use `GUNICORN_*` variables. The timeout must
accommodate synchronous AI requests. Multiple workers need a shared cache if
throttling must be enforced globally; the repository does not provision one.

## Before public beta

- Use production Google credentials/redirects and verify account consent/access.
- Store secrets outside the image; preserve the Calendar encryption key with secure
  database backups. Losing it makes saved provider credentials unreadable.
- Omit query strings from proxy access logs, especially the Calendar callback.
  Application/Gunicorn logging does not replace proxy configuration.
- Configure static-file serving for admin assets, error reporting, database
  backup/restore and expired-token cleanup (`manage.py flushexpiredtokens`).
- Verify sign-in/refresh, user isolation, proposal approval, provider failure
  recovery and a real Calendar write on the deployed origin.

Remaining release work is tracked in the [MVP checklist](../product/individual-mvp.md).
