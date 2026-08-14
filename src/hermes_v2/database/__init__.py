"""Database configuration and declarative base for Hermes v2."""

from hermes_v2.database.connection import Base, create_engine_from_environment

__all__ = ["Base", "create_engine_from_environment"]
