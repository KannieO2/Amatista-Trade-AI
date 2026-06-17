# Database Package

This package will contain shared SQLAlchemy models, Alembic migrations, and seed data.

The initial schema is loaded through `infrastructure/postgres/init.sql` during local Docker startup. Move table ownership here once migrations are introduced.

