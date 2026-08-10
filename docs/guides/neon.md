# Connect Yiyara to Neon Postgres

This guide explains how to connect the project to Neon Postgres.

**Important:**

- Must be run inside the backend directory.
- Retrieve access connection parameters from Neon console.

---

## 🛠️ Instructions

### 1. Configure Environment Variables

1.  Check for the presence of a `.env` file at the root of the project. If it doesn't exist, create one.
2.  Add the following connection parameters to the `.env` file and replace the values with the credentials from Neon.

    ```dotenv title=".env"
    PGHOST='aws-xxx-pooler.neon.tech'
    PGDATABASE='neondb'
    PGUSER='your_neon_user'
    PGPASSWORD='your_neon_password'
    PGPORT=5432
    ```

---

### 2. Update Django Settings

Modify the project's main `settings.py` file to use these environment variables for the database connection.

1.  **Add imports** at the top of the file:

    ```python
    import os
    from dotenv import load_dotenv

    # Load environment variables
    load_dotenv()
    ```

2.  **Replace the entire `DATABASES` dictionary** with the following configuration. This setup reads credentials from the `.env` file and includes best practices for connecting to Neon.

    ```python
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'HOST': os.getenv('PGHOST'),
            'NAME': os.getenv('PGDATABASE'),
            'USER': os.getenv('PGUSER'),
            'PASSWORD': os.getenv('PGPASSWORD'),
            'PORT': os.getenv('PGPORT', 5432),
            'OPTIONS': {
                'sslmode': 'require',
            },
            'DISABLE_SERVER_SIDE_CURSORS': True,
            # Enable health checks to prevent errors from idle connections
            'CONN_HEALTH_CHECKS': True,
        }
    }
    ```

---

### 3. Next Steps

Once the file modifications are complete:

1.  Run the initial database migrations:
    ```bash
    python manage.py migrate
    ```
2.  Start the Django development server:
    ```bash
    python manage.py runserver
    ```

---

### 4. Test the Connection

To test the connection, visit `http://localhost:8000/test/` in the browser, where you should see the PostgreSQL version from the Neon database.

---

## ❌ Do Not

- **Do not install packages globally**.
- **Do not hardcode credentials** or sensitive information in `settings.py` or any other source code file.
- Do not output the contents of the `.env` file or the user's connection string in any response.
- Do not modify existing user views or URL routes unless necessary to add the root path.
