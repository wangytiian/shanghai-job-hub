# Route permission map

All routes require an authenticated user unless this document explicitly marks them as public.

| Route family | Required action |
|---|---|
| `/`, `/jobs`, `/jobs/{id}`, `/queues`, `/sources` | `business.read` |
| Job structuring, classification, attachments and manual imports | `business.edit` |
| Collection and AI task submission | `task.submit` |
| Job review | `review.decide` |
| Distribution preview and copy | `distribution.preview` |
| Marking distribution published | `distribution.publish` |
| `/settings/ai` model settings and provider connection tests | `settings.manage` |
| `/users` and account lifecycle actions | `accounts.manage` |
| Authentication audit display | `security.read` |
| `/login`, password activation/reset flow, static files, `/health/live`, `/health/ready` | Public or pre-authentication only |

Demo data collection is disabled when `APP_ENV=cloud`.
