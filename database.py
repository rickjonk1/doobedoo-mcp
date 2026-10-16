"""Eén gedeelde SQLAlchemy-instantie voor alle modellen."""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
