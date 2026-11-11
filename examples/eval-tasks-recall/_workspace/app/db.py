"""Database settings. Values come from the environment; see the team's notes for staging."""
import os

STAGING_HOST = os.environ.get("STAGING_HOST")
STAGING_PORT = int(os.environ.get("STAGING_PORT", "0"))
DB_NAME = {"dev": "widgets_dev", "test": "widgets_test"}.get(os.environ.get("APP_ENV", "dev"), "widgets_dev")
