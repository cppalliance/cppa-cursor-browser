# API Deprecation Policy

How the HTTP API (`/api/*`), CLI flags (`cursor-chat-export`), and shared JSON response fields are deprecated and removed. Complements the [Versioning](../README.md#versioning) section and [CHANGELOG.md](../CHANGELOG.md).

## Pre-1.0 posture

While the project is at `0.x.y`, **breaking changes may land in any minor release** without a prior deprecation cycle. Deprecations are still recorded in the changelog when practicable, but there is no guarantee of advance notice before removal. After `1.0.0`, the workflow below applies in full.

## Deprecation workflow

When an endpoint, parameter, response field, or CLI flag is scheduled for removal:

1. **CHANGELOG** — Add an entry under `### Deprecated` naming the surface, its replacement (if any), and the planned removal version.
2. **Response headers** — Deprecated HTTP endpoints and parameters emit a `Deprecation` header on every affected response (see [Header format](#header-format)). When the first endpoint is deprecated, implement this via a small shared Flask helper so handlers stay consistent with the policy.
3. **Server log** — Route handlers log `logging.warning()` with the deprecated symbol and recommended replacement.
4. **Removal** — Remove no earlier than **one minor version** after the deprecation was announced (e.g. deprecated in `1.2.0`, removable from `1.3.0`). Document under `### Removed` in the changelog.

## Header format

Deprecated HTTP routes and query parameters set:

```http
Deprecation: true; sunset=2026-09-01
```

- `sunset` — ISO 8601 calendar date (UTC) when removal is scheduled.
- Optionally add `link="<url>"` pointing to the changelog entry or migration notes.

Clients should treat any `Deprecation: true` response as a signal to migrate before the sunset date.

## Surfaces covered

| Surface | Signals |
|---------|---------|
| HTTP endpoint or parameter | `Deprecation` header, changelog entry, server log |
| JSON response field | Changelog entry; field remains until removal |
| CLI flag | `(deprecated)` in `--help`, changelog entry |

## Removal documentation

Removals go under `### Removed` in [CHANGELOG.md](../CHANGELOG.md): what was removed, which version deprecated it, and the migration path.
