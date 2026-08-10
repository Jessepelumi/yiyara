# Local Development Setup

This guide provides advanced setup instructions beyond the basic Quick Start in the main README. Ensure you've completed the [Quick Start](../README.md#quick-start) before proceeding.

## Environment Configuration Details

### Database Setup

Follow the [Neon Postgres Connection Guide](neon.md) to set up your database connection.

### AI Configuration

1. Obtain a Google Gemini API key from [Google AI Studio](https://makersuite.google.com/app/apikey)
2. Add the API key to your `.env` file:

```env
GEMINI_API_KEY=your-gemini-api-key-here
```

### Django Configuration

Add the following to your `.env` file:

```env
DEBUG=True
SECRET_KEY=your-secure-django-secret-key-here
```

Generate a secure secret key using Python:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

## Development Workflow

### Running Commands

- View logs: `make logs`
- Restart services: `make restart`
- Stop services: `make down`
- Django shell: `make shell`

### Code Changes

- Backend changes are automatically reloaded
- Frontend uses hot reloading with Next.js
- Database schema changes require new migrations

## Troubleshooting

### Running Commands

- View logs: `make logs`
- Restart services: `make restart`
- Stop services: `make down`
- Django shell: `make shell`

### Code Changes

- Backend changes are automatically reloaded
- Frontend uses hot reloading with Next.js
- Database schema changes require new migrations

## Troubleshooting

### Common Issues

1. **Port conflicts**: Ensure ports 3000 and 8000 are available
2. **Database connection**: Verify Neon credentials in `.env`
3. **AI features**: Check Gemini API key validity
4. **Dependencies**: Ensure Docker has sufficient resources

### Logs

Check service logs:

```bash
make logs
```

Or specific service:

```bash
docker-compose logs backend
docker-compose logs frontend
```

## Next Steps

- Review the [API Documentation](api.md) for available endpoints
- Set up [AI Configuration](ai-configuration.md) for advanced features
- Check the [Deployment Guide](deployment.md) for production setup</content>
  <parameter name="filePath">/Users/jeolad/Documents/yiyara/docs/guides/setup.md
