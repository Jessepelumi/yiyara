# Start with a Python image
FROM python:3.12-slim

# Set working directory inside the container
WORKDIR /app

# Prevent Python from writing .pyc files and buffer output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies (needed for Postgres/Django)
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the Yiyara source code
COPY . .

# Start the Django server
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]