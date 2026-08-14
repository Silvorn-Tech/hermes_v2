# Authentication and authorization schema

## `users`

Internal Hermes user identity. `id` is a UUID primary key. `email` is required
(`varchar(320)`) and unique. `display_name` (`varchar(255)`), `avatar_url`
(`varchar(2048)`), and `last_login_at` (`timestamptz`) are nullable. `status`
is required PostgreSQL enum `user_status` (`ACTIVE` or `DISABLED`).
`created_at` and `updated_at` are required `timestamptz` columns with database
defaults. Identities and roles relate to this table.

## `identities`

External provider identities. `id` is a UUID primary key; `user_id` is a
required foreign key to `users.id` with `ON DELETE CASCADE` and an index.
`provider` (`varchar(50)`) and `provider_subject` (`varchar(255)`) are
required. The pair is unique. `created_at` is required `timestamptz` with a
database default. A user has zero or more identities.

## `roles`

Authorization roles. `id` is a UUID primary key. `name` is required,
`varchar(100)`, and unique. `description` is nullable text. `system_role` is a
required boolean that defaults to false. System roles are protected application
records; `SUPER_ADMIN` is the initial one. Roles relate to users through
`user_roles` and permissions through `role_permissions`.

## `permissions`

Application-defined capabilities. `id` is a UUID primary key. `name` is
required, `varchar(100)`, and unique. `description` is nullable text. A
permission can be granted by many roles through `role_permissions`.

## `user_roles`

User-to-role association. `user_id` and `role_id` are required foreign keys to
`users.id` and `roles.id`, each with `ON DELETE CASCADE`. Together they form
the composite primary key and uniqueness constraint.

## `role_permissions`

Role-to-permission association. `role_id` and `permission_id` are required
foreign keys to `roles.id` and `permissions.id`, each with `ON DELETE CASCADE`.
Together they form the composite primary key and uniqueness constraint.

## Initial seed data

The initial Alembic migration creates the protected `SUPER_ADMIN` role and
grants it the complete application-defined permission catalog. The same catalog
is available through an idempotent application seeder for controlled recovery
or test setup. No personal user or external identity is seeded.
