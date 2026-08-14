# Authentication architecture

Hermes keeps its own internal user identity and separates authentication from
authorization:

```text
Google -> Identity -> User -> Role -> Permission
```

An `Identity` records an external provider and its stable provider subject.
Google will later provide its `sub`; Telegram will later provide its user ID.
Email is contact data on `User`, not an external provider identifier, because
provider emails may change and are not provider-scoped stable subjects.

Roles are assigned to Hermes users, never inferred from an authentication
provider. This lets Google and a future Telegram identity link to the same user
without changing roles or the `users` model. The initial protected system role
is `SUPER_ADMIN`; future custom roles can use the same role-permission model.

OAuth and provider integrations are intentionally not implemented here.

## Initial administrator bootstrap

Google OAuth is not required to create the initial Hermes administrator record.
The `bootstrap-admin` command reads `HERMES_ADMIN_EMAIL` from configuration,
creates or reuses that internal user, and assigns the protected `SUPER_ADMIN`
role. It is idempotent: repeated executions do not create another user or role
assignment.

The bootstrap does not create a Google identity because it does not know
Google's stable `sub` identifier. A future successful OAuth flow will create
`identities(provider="google", provider_subject=<sub>)` and link it to this
existing user.

The email is configuration rather than source code so each installation can
choose its own initial administrator. It is normalized only by trimming outer
whitespace and lowercasing; provider-specific transformations such as Gmail
dot or plus-alias removal are intentionally not applied.
