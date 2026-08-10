# Deployment Guide

This guide covers deploying the Yiyara application to production.

## Prerequisites

- Docker and Docker Compose
- Cloud hosting provider (AWS, GCP, DigitalOcean, etc.)
- Domain name (optional)
- SSL certificate (recommended)

## Environment Setup

### Production Environment Variables

Create a production `.env` file with:

```env
# Database (Production Neon instance)
PGHOST=your-prod-neon-host
PGDATABASE=your-prod-database
PGUSER=your-prod-username
PGPASSWORD=your-prod-password
PGPORT=5432

# AI Configuration
GEMINI_API_KEY=your-prod-gemini-key

# Django Configuration
DEBUG=False
SECRET_KEY=your-secure-production-secret-key
ALLOWED_HOSTS=your-domain.com,www.your-domain.com

# Security
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
```

### Generate Secure Secret Key

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

## Docker Production Setup

### Update Docker Compose

Create `docker-compose.prod.yml`:

```yaml
version: "3.8"

services:
  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile.prod
    environment:
      - DEBUG=False
    env_file:
      - .env
    volumes:
      - staticfiles:/app/staticfiles
    depends_on:
      - db

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile.prod
    environment:
      - NODE_ENV=production
    volumes:
      - nextjs-static:/app/out

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
      - nextjs-static:/var/www/html
      - staticfiles:/var/www/html/static
      - ./ssl:/etc/ssl/certs
    depends_on:
      - backend
      - frontend

volumes:
  staticfiles:
  nextjs-static:
```

### Production Dockerfiles

#### Backend Production Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Collect static files
RUN python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
```

#### Frontend Production Dockerfile

```dockerfile
FROM node:18-alpine AS builder

WORKDIR /app
COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/out /usr/share/nginx/html
EXPOSE 80
```

### Nginx Configuration

Create `nginx.conf`:

```nginx
events {
    worker_connections 1024;
}

http {
    upstream backend {
        server backend:8000;
    }

    server {
        listen 80;
        server_name your-domain.com;

        # Static files
        location /static/ {
            alias /var/www/html/static/;
        }

        location /api/ {
            proxy_pass http://backend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
        }

        # Frontend
        location / {
            try_files $uri $uri/ /index.html;
            root /var/www/html;
        }
    }
}
```

## Deployment Steps

### 1. Prepare Codebase

```bash
# Clone repository
git clone https://github.com/your-username/yiyara.git
cd yiyara

# Create production env file
cp .env.example .env.prod
# Edit .env.prod with production values
```

### 2. Build and Test Locally

```bash
# Test production build
docker-compose -f docker-compose.prod.yml build
docker-compose -f docker-compose.prod.yml up -d
```

### 3. Deploy to Server

#### Using Docker Compose on VPS

```bash
# On your server
scp docker-compose.prod.yml user@server:~/
scp .env.prod user@server:~/
scp nginx.conf user@server:~/

# On server
docker-compose -f docker-compose.prod.yml up -d
```

#### Using Cloud Platforms

**Railway:**

- Connect GitHub repository
- Set environment variables
- Deploy automatically

**Render:**

- Create web service for backend
- Create static site for frontend
- Configure environment variables

**AWS/GCP:**

- Use ECS/EKS for container orchestration
- Set up load balancer
- Configure auto-scaling

### 4. Database Migration

```bash
# Run migrations on production
docker-compose -f docker-compose.prod.yml exec backend python manage.py migrate
```

### 5. SSL Configuration

#### Using Let's Encrypt

```bash
# Install certbot
sudo apt install certbot

# Get certificate
sudo certbot certonly --standalone -d your-domain.com

# Update nginx.conf to use SSL
```

#### Using Cloudflare

- Set up Cloudflare DNS
- Enable SSL/TLS encryption
- Configure page rules

## Monitoring and Maintenance

### Health Checks

Add health check endpoint in Django:

```python
# views.py
from django.http import JsonResponse

def health_check(request):
    return JsonResponse({'status': 'healthy'})
```

### Logging

Configure logging to external service (Papertrail, LogDNA, etc.)

### Backups

- Database: Use Neon's built-in backups
- Files: Set up automated backups of volumes

### Updates

```bash
# Pull latest changes
git pull origin main

# Rebuild and restart
docker-compose -f docker-compose.prod.yml build
docker-compose -f docker-compose.prod.yml up -d

# Run migrations if needed
docker-compose -f docker-compose.prod.yml exec backend python manage.py migrate
```

## Troubleshooting

### Common Issues

1. **Static files not loading**: Check nginx configuration and volume mounts
2. **Database connection**: Verify production Neon credentials
3. **CORS issues**: Update ALLOWED_HOSTS and CORS settings
4. **Memory issues**: Monitor container resources and adjust limits

### Logs

```bash
# View all logs
docker-compose -f docker-compose.prod.yml logs

# Follow logs
docker-compose -f docker-compose.prod.yml logs -f backend
```

## Security Considerations

- Keep dependencies updated
- Use strong secret keys
- Enable HTTPS
- Configure firewall rules
- Regular security audits
- Monitor for vulnerabilities</content>
  <parameter name="filePath">/Users/jeolad/Documents/yiyara/docs/guides/deployment.md
