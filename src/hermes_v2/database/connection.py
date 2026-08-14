"""Database configuration sourced from the environment."""

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for Hermes v2 ORM models."""


def database_url_from_environment() -> str:
    """Return the required SQLAlchemy database URL."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        message = "DATABASE_URL must be configured"
        raise RuntimeError(message)
    return database_url


def create_engine_from_environment() -> Engine:
    """Create a PostgreSQL engine using DATABASE_URL."""
    return create_engine(database_url_from_environment(), pool_pre_ping=True)
