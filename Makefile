# Variables
COMPOSE=docker-compose
BACKEND_SVC=backend
FRONTEND_SVC=frontend

# Help System
help:
	@echo "Yiyara AI Development Commands:"
	@echo "  make up         - Start all containers"
	@echo "  make down       - Stop all containers"
	@echo "  make build      - Rebuild containers (no cache)"
	@echo "  make restart    - Restart all services"
	@echo "  make logs       - Follow logs"
	@echo "  make shell      - Open Django shell inside container"
	@echo "  make migrate    - Run Django database migrations"
	@echo "  make superuser  - Create a new Django admin user"
	@echo "  make clean      - Remove containers, images, and volumes (Nuclear)"

# Docker Commands
up:
	$(COMPOSE) up

down:
	$(COMPOSE) down

build:
	$(COMPOSE) build --no-cache

restart:
	$(COMPOSE) restart

logs:
	$(COMPOSE) logs -f

# Django Commands
migrate:
	$(COMPOSE) exec $(BACKEND_SVC) python manage.py migrate

superuser:
	$(COMPOSE) exec $(BACKEND_SVC) python manage.py createsuperuser

shell:
	$(COMPOSE) exec $(BACKEND_SVC) python manage.py shell

collectstatic:
	$(COMPOSE) exec $(BACKEND_SVC) python manage.py collectstatic --noinput

# Safety/Cleanup
clean:
	@echo "WARNING: This will delete volumes and local images."
	$(COMPOSE) down -v --rmi local