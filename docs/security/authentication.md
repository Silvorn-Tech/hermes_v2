# Authentication and authorization security

Hermes distinguishes identity from authorization. An external provider proves
control of an identity; Hermes assigns roles and permissions to its internal
user. This role-based access control supports least privilege because a role
receives only the application-defined permissions it needs.

`provider_subject` is used for external identity uniqueness because it is a
stable, provider-scoped identifier. An email address is neither a stable
provider subject nor an authorization decision.

`SUPER_ADMIN` is a protected system role (`system_role=true`), so it can be
handled differently from future user-created roles. Permissions remain
application-defined rather than UI-created.

`HERMES_ADMIN_EMAIL` is installation configuration, not application source
code. The bootstrap uses it only to create or reuse the initial internal user
and assigns `SUPER_ADMIN`; it does not create a Google identity. This prevents
an email address from being mistaken for a provider's stable subject and keeps
future OAuth linkage dependent on Google's verified `sub`.

Secrets must never be stored in a Chrome extension: browser clients are under
user control and their contents can be inspected. PostgreSQL must not be
exposed publicly; application services should be the controlled access point
to database credentials and authorization checks.
