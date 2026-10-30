"""Production WSGI configuration. Never include OAuth query strings in access logs."""
import math
import os
from dotenv import load_dotenv

load_dotenv()
bind = os.getenv('GUNICORN_BIND', '0.0.0.0:8000')
workers = int(os.getenv('GUNICORN_WORKERS', '1'))
threads = int(os.getenv('GUNICORN_THREADS', '4'))
worker_class = 'gthread'
timeout = int(os.getenv('GUNICORN_TIMEOUT_SECONDS', str(math.ceil(float(os.getenv('AI_TOTAL_TIMEOUT_SECONDS', '45'))) + 60)))
accesslog = '-'
errorlog = '-'
access_log_format = '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s'
if min(workers, threads, timeout) < 1:
    raise ValueError('Gunicorn workers, threads and timeout must be positive.')
