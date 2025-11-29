# api/index.py
from app.invoice_api import app  # make sure import path matches
# Vercel will expose app as the function entry (the runtime expects a WSGI/ASGI app object named "app")
