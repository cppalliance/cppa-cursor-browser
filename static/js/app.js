/* ============================================================
   Cursor Chat Browser — Main JS (vanilla, no build step)
   ============================================================ */

// ---------- Theme toggle ----------

function getStoredTheme() {
  return localStorage.getItem('theme') || 'dark';
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('theme', theme);

  const moonIcon = document.getElementById('icon-moon');
  const sunIcon = document.getElementById('icon-sun');
  if (moonIcon && sunIcon) {
    moonIcon.style.display = theme === 'dark' ? 'block' : 'none';
    sunIcon.style.display = theme === 'light' ? 'block' : 'none';
  }

  // Switch highlight.js theme
  const hljsLink = document.getElementById('hljs-theme');
  if (hljsLink) {
    hljsLink.href = theme === 'dark'
      ? 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/vs2015.min.css'
      : 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css';
  }
}

function toggleTheme() {
  const current = getStoredTheme();
  applyTheme(current === 'dark' ? 'light' : 'dark');
}

// Apply on load
document.addEventListener('DOMContentLoaded', () => {
  applyTheme(getStoredTheme());
  const themeToggle = document.getElementById('theme-toggle');
  if (themeToggle) {
    themeToggle.addEventListener('click', toggleTheme);
  }
});


// ---------- Utility functions ----------

function escapeHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

/**
 * Render Markdown to HTML, then sanitise with DOMPurify before returning.
 *
 * Marked.js does NOT sanitise. Without DOMPurify, `[link](javascript:...)`,
 * dangerous `data:` URIs, and inline event handlers all survive into the DOM
 * — that's the XSS class fixed in issue #11.
 *
 * Fallback: if either marked or DOMPurify is missing (CDN failure, ad blocker,
 * tests), return the plain-text-escaped string rather than ever emit raw or
 * unsanitised HTML. Never fall through to a bare Marked call without sanitising.
 */
function renderMarkdownSafe(text) {
  if (!text) return '';
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
    return escapeHtml(text);
  }
  try {
    const html = marked.parse(text, { breaks: true, gfm: true });
    return DOMPurify.sanitize(html);
  } catch (e) {
    return escapeHtml(text);
  }
}

function formatDate(timestamp) {
  if (!timestamp) return '';
  try {
    const d = new Date(typeof timestamp === 'number' ? timestamp : timestamp);
    if (isNaN(d.getTime())) return String(timestamp);
    return d.toLocaleString();
  } catch (e) {
    return String(timestamp);
  }
}

function sanitizeFilename(name) {
  return (name || '').replace(/[<>:"/\\|?*]/g, '_').replace(/\s+/g, '_').slice(0, 120);
}

/**
 * Normalize GET /api/workspaces body: { projects, warnings? } (issue #67).
 * Accepts a legacy plain array for backward compatibility.
 */
function normalizeWorkspacesResponse(body) {
  if (Array.isArray(body)) {
    return { projects: body, warnings: [] };
  }
  if (body && typeof body === 'object') {
    return {
      projects: Array.isArray(body.projects) ? body.projects : [],
      warnings: Array.isArray(body.warnings) ? body.warnings : [],
    };
  }
  return { projects: [], warnings: [] };
}

/** Human-readable text from API ``warnings`` entries. */
function formatParseWarnings(warnings) {
  if (!warnings || !warnings.length) return '';
  return warnings.map(w => w.detail || `${w.count || 0} items could not be loaded`).join(' ');
}

/**
 * Show or replace an incomplete-results banner (issue #67).
 * @param {string} containerId - element to prepend into
 * @param {Array} warnings - API warnings array
 */
function showIncompleteResultsBanner(containerId, warnings) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const existing = container.querySelector('.incomplete-results-banner');
  if (existing) existing.remove();
  if (!Array.isArray(warnings) || !warnings.length) return;

  const text = formatParseWarnings(warnings);
  const banner = document.createElement('div');
  banner.className = 'alert alert-warning incomplete-results-banner';
  banner.setAttribute('role', 'status');
  banner.textContent = text
    ? `Some results may be incomplete: ${text}`
    : 'Some results may be incomplete due to parse errors.';
  container.insertBefore(banner, container.firstChild);
}
