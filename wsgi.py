# wsgi.py
from app import app

# Gunicorn looks for a module-level variable named "app" by default when you run:
#   gunicorn wsgi:app
