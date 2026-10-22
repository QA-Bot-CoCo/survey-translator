"""Local static file and API server; Vercel uses public/ and api/ directly."""
from survey.web import create_app

app = create_app(serve_static=True)
