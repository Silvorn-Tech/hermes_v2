# Authentication and authorization ERD

```mermaid
erDiagram
    USERS ||--o{ IDENTITIES : owns
    USERS ||--o{ USER_ROLES : receives
    ROLES ||--o{ USER_ROLES : assigns
    ROLES ||--o{ ROLE_PERMISSIONS : grants
    PERMISSIONS ||--o{ ROLE_PERMISSIONS : contains

    USERS {
        uuid id PK
        varchar email UK
        varchar display_name
        varchar avatar_url
        user_status status
        timestamptz created_at
        timestamptz updated_at
        timestamptz last_login_at
    }
    IDENTITIES {
        uuid id PK
        uuid user_id FK
        varchar provider
        varchar provider_subject
        timestamptz created_at
    }
    ROLES {
        uuid id PK
        varchar name UK
        text description
        boolean system_role
    }
    PERMISSIONS {
        uuid id PK
        varchar name UK
        text description
    }
    USER_ROLES {
        uuid user_id PK, FK
        uuid role_id PK, FK
    }
    ROLE_PERMISSIONS {
        uuid role_id PK, FK
        uuid permission_id PK, FK
    }
```

A user may own zero or more external identities and may receive zero or more
roles. A role may belong to many users and grants zero or more permissions.
Both association tables use composite primary keys, so duplicate assignments are
not possible.
