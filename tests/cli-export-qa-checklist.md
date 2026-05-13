# CLI export + browse — manual QA checklist

Companion to the automated `tests/test_cli_export_e2e.py` suite. Run this once
per release branch (or whenever `scripts/export.py`, `app.py`, or the
workspace-scan service modules are touched) on each supported platform.

Mark each row pass/fail and attach short notes for any unexpected output.

## Environment

| Item               | Value                                          |
|--------------------|------------------------------------------------|
| OS / version       |                                                |
| Python version     | `python3 --version`                            |
| Repo SHA           | `git rev-parse HEAD`                           |
| `WORKSPACE_PATH`   | (env var, if overridden)                       |
| Cursor data layout | default `~/.config/Cursor/User/workspaceStorage` or override |

## 1. CLI: `python scripts/export.py --help`

- [ ] Exits 0
- [ ] Usage block lists `--since {all,last}`, `--out`, `--no-zip`, `--no-composer`, `--base-dir`, `--exclude-rules`
- [ ] No stack trace, no deprecation warning

## 2. CLI: default zip export

```bash
python scripts/export.py --out ./export
```

- [ ] Exits 0
- [ ] Final stdout line: `Exported N chat(s) to ./export/cursor-export-YYYY-MM-DD.zip`
- [ ] Archive opens with `unzip -l` and contains at least one `.md` entry
- [ ] Each `.md` inside the archive starts with a `---`-fenced YAML
      frontmatter containing `log_id`, `title`, `workspace`, `created_at`,
      `updated_at`

## 3. CLI: no-zip export

```bash
python scripts/export.py --out ./export --no-zip
```

- [ ] Exits 0
- [ ] `./export/manifest.jsonl` exists and is non-empty
- [ ] Each manifest line is valid JSON with `log_id`, `path`, `updated_at`
- [ ] At least one `.md` file is written under `./export/<date>/...`
- [ ] Frontmatter fields as in §2

## 4. CLI: incremental (`--since last`)

```bash
python scripts/export.py --out ./export --no-zip --since last
```

(run after §3)

- [ ] Exits 0
- [ ] If no new chats: stdout prints `No conversations found since last export.`
- [ ] Existing `.md` files in `./export` retain their previous mtimes (not rewritten)
- [ ] After producing a new chat in Cursor, re-running picks it up and writes
      only the new file

## 5. App server launch

```bash
python app.py
```

- [ ] Process stays running for at least 30 s without crash
- [ ] `curl -sI http://127.0.0.1:3000/` returns `HTTP/1.0 200 OK` (or 200 over 1.1)
- [ ] Home page lists at least one workspace card (assuming real Cursor data)
- [ ] No `Exception` / `Traceback` in server log

## 6. Browse flow (web UI)

Open `http://127.0.0.1:3000/` in a browser.

- [ ] Workspace list renders without console errors
- [ ] Clicking a workspace card opens the workspace view
- [ ] Within a workspace, opening a chat renders its bubbles
- [ ] Markdown export button on a chat downloads a `.md` whose frontmatter
      matches §2
- [ ] PDF export button on a chat downloads a `.pdf` that opens cleanly

## Sign-off

| Reviewer | Platform | Date | Result |
|----------|----------|------|--------|
|          |          |      |        |
