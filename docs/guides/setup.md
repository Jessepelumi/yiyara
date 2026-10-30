# Local setup

Requires Docker/Compose and a PostgreSQL database. The image uses Python 3.12;
Compose starts only the backend and does not provision a database or frontend.

## Configure

From the repository root, copy `.env.example` to `.env` if you do not already have
one. Preserve existing secrets. The template is a starting point; supported optional
settings and defaults live in [config/settings.py](../../config/settings.py).

Set `SECRET_KEY`, `DEBUG=True`, `ALLOWED_HOSTS`, and the database connection:
`PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGPORT`. `PGSSLMODE` defaults to
`require`, suitable for the current hosted PostgreSQL setup. Local databases without
TLS need an explicit appropriate setting.

Generate an application secret if needed:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

Configure [Google sign-in](authentication.md#google-configuration),
[Calendar credentials](calendar.md#google-configuration), and
[AI](ai-configuration.md#provider-configuration) before testing the complete flow.
Never commit `.env` or print its contents in logs.

## Run

```sh
docker compose up -d --build backend
docker compose exec backend python manage.py migrate
docker compose logs -f backend
```

The API is at `http://localhost:8000`. Code is bind-mounted and runserver reloads
changes. After editing `.env`, recreate the service to reload container variables:

```sh
docker compose up -d --force-recreate backend
```

Rebuild after dependency changes. Use `docker compose down` to stop the service.

## Verify

```sh
docker compose exec backend python manage.py check
docker compose exec backend python manage.py makemigrations --check --dry-run
docker compose exec backend python manage.py test --settings=config.test_settings
```

The test settings use temporary SQLite and simulated providers. For PostgreSQL
coverage, run `python manage.py test --noinput --keepdb` inside the service against
a dedicated test database/account with test-database permissions. Do not point test
settings at production data. Live acceptance uses the [Postman guide](postman.md).
