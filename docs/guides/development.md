# Development Guide

This guide provides information for developers contributing to the Yiyara project.

## Development Setup

Follow the [Local Development Setup](setup.md) guide.

## Project Structure

See the [backend README](../backend/README.md#project-structure) for detailed project structure.

## Coding Standards

### Python (Backend)

- Follow PEP 8 style guide
- Use type hints where possible
- Write docstrings for classes and functions
- Use meaningful variable names
- Keep functions small and focused

### TypeScript/JavaScript (Frontend)

- Use TypeScript for all new code
- Follow the existing component patterns
- Use meaningful variable names
- Keep components small and reusable

### Git Workflow

1. Create feature branch: `git checkout -b feature/your-feature`
2. Make commits: `git commit -m "Add: brief description"`
3. Push branch: `git push origin feature/your-feature`
4. Create pull request

### Commit Messages

Format: `Type: Brief description`

Types:

- `Add:` - New features
- `Fix:` - Bug fixes
- `Update:` - Changes to existing features
- `Refactor:` - Code restructuring
- `Docs:` - Documentation changes

## Testing

### Backend Tests

```bash
# Run Django tests
make shell
python manage.py test
```

### Frontend Tests

```bash
cd frontend
npm test
```

### Manual Testing

- Test all user flows in the frontend
- Verify API responses
- Check database operations
- Test AI processing

## API Development

### Adding New Endpoints

1. Create/update models in appropriate app
2. Create serializers
3. Create views with proper permissions
4. Add URL patterns
5. Update API documentation

### Authentication

All API endpoints require authentication. Use `permissions.IsAuthenticated` for views.

## AI Integration

### Adding New Providers

1. Create provider class in `backend/ai/providers/`
2. Implement required methods
3. Update `ai_engine.py` to support new provider
4. Add environment configuration

### Modifying AI Behavior

- Prompts are in `backend/ai/prompts/`
- Workflow logic in `backend/workflow/ai_engine.py`
- Test AI responses thoroughly

## Frontend Development

### Component Structure

- Use functional components with hooks
- Keep components in `src/components/`
- Use TypeScript interfaces for props
- Follow existing styling patterns

### State Management

Currently using React Query for server state. Consider Redux for complex client state.

### API Integration

Use the API client in `src/lib/api/`. Add new endpoints there.

## Database

### Migrations

```bash
# Create migration
python manage.py makemigrations

# Apply migrations
make migrate
```

### Schema Changes

- Update models first
- Create and apply migrations
- Update serializers if needed
- Test thoroughly

## Docker Development

### Rebuilding

```bash
# Rebuild specific service
docker-compose build backend

# Rebuild all
make build
```

### Debugging

```bash
# Access container shell
docker-compose exec backend bash

# View logs
make logs
```

## Deployment

See the [Deployment Guide](deployment.md) for production setup.

## Contributing

1. Read all documentation
2. Follow coding standards
3. Write tests for new features
4. Update documentation as needed
5. Test thoroughly before submitting PR

## Support

- Check existing issues on GitHub
- Create detailed bug reports
- Ask questions in discussions
- Review pull requests constructively</content>
  <parameter name="filePath">/Users/jeolad/Documents/yiyara/docs/guides/development.md
