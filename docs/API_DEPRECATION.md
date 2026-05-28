# API Deprecation Policy

How the HTTP API (`/api/*`), CLI flags (`cursor-chat-export`), and shared JSON response fields are deprecated and removed. Complements the [Versioning](../README.md#versioning) section and [CHANGELOG.md](../CHANGELOG.md).

## Pre-1.0 posture

While the project is at `0.x.y`, **breaking changes may land in any minor release** without a prior deprecation cycle. Deprecations are still recorded in the changelog when practicable, but there is no guarantee of advance notice before removal. Pre-1.0, workflow steps 2–4 (CLI help text, response headers, server logs) are **encouraged but optional**; step 1 (changelog) applies when practicable. After `1.0.0`, the workflow below applies in full.

## Deprecation workflow

When an endpoint, parameter, response field, or CLI flag is scheduled for removal:

1. **CHANGELOG** — Add an entry under `### Deprecated` naming the surface, its replacement (if any), and the planned removal version.
2. **CLI help** (flags only) — Add `(deprecated, use <replacement>)` to the flag's argparse help string.
3. **Response headers** — Deprecated HTTP endpoints and parameters emit deprecation headers on every affected response (see [Header format](#header-format)). When the first endpoint is deprecated, implement via a small shared Flask helper so handlers stay consistent with the policy.
4. **Server log** — Route handlers log `logging.warning()` with the deprecated symbol and recommended replacement.
5. **Removal** — Remove no earlier than **one minor version** after the deprecation was announced (e.g. deprecated in `1.2.0`, removable from `1.3.0`). Document under `### Removed` in the changelog.

## Header format

This project currently documents a **simplified custom format** for pre-1.0. It does not match [IETF `Deprecation`](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-deprecation-header) (HTTP-date value, not `true`) or [RFC 8594](https://www.rfc-editor.org/rfc/rfc8594) (separate `Sunset` and `Link` headers). Adopt the standards-aligned form below when the shared Flask helper lands or at `1.0.0`.

**Current (custom, pre-1.0):**

```http
Deprecation: true; sunset=2026-09-01
```

- `sunset=` — ISO 8601 calendar date (UTC) when removal is scheduled, embedded in the `Deprecation` value.

**Target (standards-aligned)** — emit as **separate headers** from Flask:

```http
Deprecation: Tue, 01 Sep 2026 00:00:00 GMT
Sunset: Tue, 01 Sep 2026 00:00:00 GMT
Link: <https://github.com/cppalliance/cppa-cursor-browser/blob/HEAD/CHANGELOG.md>; rel="deprecation"
```

Migration notes use a separate **`Link`** header (RFC 8288), not a `link=` parameter on `Deprecation`.

Clients should treat any deprecation signal as a prompt to migrate before the sunset date.

## Surfaces covered

| Surface | Signals |
|---------|---------|
| HTTP endpoint or parameter | Deprecation headers, changelog entry, server log |
| JSON response field | Changelog entry; field remains until removal (no in-band signal today; future: `X-Deprecated-Fields` header or `_deprecated` envelope key) |
| CLI flag | `(deprecated)` in `--help`, changelog entry |

## Removal documentation

Removals go under `### Removed` in [CHANGELOG.md](../CHANGELOG.md): what was removed, which version deprecated it, and the migration path.
