/* ============================================================
   Cursor Chat Browser — Download / Export JS
   Mirrors src/lib/download.ts (client-side operations)
   ============================================================ */

function convertChatToMarkdown(tab, includeMetadata) {
  if (includeMetadata === undefined) includeMetadata = true;
  let md = '';

  // YAML frontmatter
  if (includeMetadata) {
    const meta = {
      title: tab.title || 'Chat ' + tab.id,
      created: new Date(tab.timestamp).toISOString(),
      conversation_id: tab.id
    };
    const m = tab.metadata || {};
    if (m.modelsUsed && m.modelsUsed.length) meta.models_used = m.modelsUsed.join(', ');
    if (m.totalInputTokens) meta.total_input_tokens = m.totalInputTokens;
    if (m.totalOutputTokens) meta.total_output_tokens = m.totalOutputTokens;
    if (m.totalCachedTokens) meta.total_cached_tokens = m.totalCachedTokens;
    if (m.maxContextTokensUsed) meta.max_context_tokens_used = m.maxContextTokensUsed;
    if (m.contextTokenLimit) meta.context_token_limit = m.contextTokenLimit;
    if (m.totalResponseTimeMs) meta.total_response_time_sec = (m.totalResponseTimeMs / 1000).toFixed(1);
    if (m.totalThinkingDurationMs) meta.total_thinking_time_sec = (m.totalThinkingDurationMs / 1000).toFixed(1);
    if (m.totalToolCalls) meta.total_tool_calls = m.totalToolCalls;
    if (m.totalCost != null) meta.total_cost = m.totalCost;
    if (m.totalLinesAdded) meta.lines_added = m.totalLinesAdded;
    if (m.totalLinesRemoved) meta.lines_removed = m.totalLinesRemoved;
    if (m.totalFilesAdded) meta.files_added = m.totalFilesAdded;
    if (m.totalFilesRemoved) meta.files_removed = m.totalFilesRemoved;
    md += '---\n';
    for (const [k, v] of Object.entries(meta)) {
      md += k + ': ' + (Array.isArray(v) ? v.join(', ') : v) + '\n';
    }
    md += '---\n\n';
  }

  md += '# ' + (tab.title || 'Chat ' + tab.id) + '\n\n';
  md += '_Created: ' + new Date(tab.timestamp).toLocaleString() + '_\n\n';

  if (includeMetadata && tab.metadata) {
    const m = tab.metadata;
    const sumParts = [];
    if (m.modelsUsed && m.modelsUsed.length) sumParts.push('Models: ' + m.modelsUsed.join(', '));
    if (m.maxContextTokensUsed && m.contextTokenLimit) {
      sumParts.push('Context: ' + fmtNum(m.maxContextTokensUsed) + ' / ' + fmtNum(m.contextTokenLimit) + ' tokens');
    }
    if (m.totalResponseTimeMs) sumParts.push('Response time: ' + (m.totalResponseTimeMs / 1000).toFixed(1) + 's');
    if (m.totalThinkingDurationMs) sumParts.push('Thinking: ' + (m.totalThinkingDurationMs / 1000).toFixed(1) + 's');
    if (m.totalToolCalls) sumParts.push('Tool calls: ' + m.totalToolCalls);
    if (m.totalLinesAdded || m.totalLinesRemoved) {
      let lm = 'Lines:';
      if (m.totalLinesAdded) lm += ' +' + fmtNum(m.totalLinesAdded);
      if (m.totalLinesRemoved) lm += ' -' + fmtNum(m.totalLinesRemoved);
      sumParts.push(lm);
    }
    if (m.totalCost != null) sumParts.push('Cost: $' + Number(m.totalCost).toFixed(4));
    if (sumParts.length) md += '_' + sumParts.join(' | ') + '_\n\n';
  }
  md += '---\n\n';

  for (const bubble of (tab.bubbles || [])) {
    md += '### ' + (bubble.type === 'ai' ? 'Assistant' : 'User') + '\n\n';
    const bm = bubble.metadata || {};
    // Metadata line
    const metaParts = [];
    if (bm.modelName && bm.modelName !== 'default') metaParts.push('Model: ' + bm.modelName);
    if (bm.inputTokens > 0 || bm.outputTokens > 0) {
      let t = 'Tokens: in ' + (bm.inputTokens || 0) + ', out ' + (bm.outputTokens || 0);
      if (bm.cachedTokens > 0) t += ', cached ' + bm.cachedTokens;
      metaParts.push(t);
    }
    if (bm.responseTimeMs != null) metaParts.push('Response: ' + (bm.responseTimeMs / 1000).toFixed(1) + 's');
    if (bm.thinkingDurationMs) metaParts.push('Thinking: ' + (bm.thinkingDurationMs / 1000).toFixed(1) + 's');
    if (bm.cost != null) metaParts.push('Cost: $' + Number(bm.cost).toFixed(4));
    // Context window — show token counts + percentage
    if (bm.contextTokensUsed > 0 && bm.contextTokenLimit > 0) {
      const pct = ((bm.contextTokensUsed / bm.contextTokenLimit) * 100).toFixed(0);
      metaParts.push('Context: ' + fmtNum(bm.contextTokensUsed) + ' / ' + fmtNum(bm.contextTokenLimit) + ' (' + pct + '% used)');
    } else if (bm.contextWindowPercent != null) {
      metaParts.push('Context: ' + bm.contextWindowPercent.toFixed(1) + '% remaining');
    }
    if (metaParts.length) md += '_' + metaParts.join(' | ') + '_\n\n';

    if (bubble.timestamp) {
      md += '_' + new Date(bubble.timestamp).toLocaleString() + '_\n\n';
    }

    // Thinking
    if (bm.thinking) {
      const thinkText = typeof bm.thinking === 'string' ? bm.thinking : (bm.thinking.text || '');
      if (thinkText) {
        md += '<details><summary>Thinking' + (bm.thinkingDurationMs ? ' (' + (bm.thinkingDurationMs / 1000).toFixed(1) + 's)' : '') + '</summary>\n\n' + thinkText + '\n\n</details>\n\n';
      }
    }

    if (bubble.text) {
      md += bubble.text + '\n\n';
    } else if (bubble.type === 'ai') {
      md += '_[No text content]_\n\n';
    }

    // Tool calls with full input/output
    if (bm.toolCalls && bm.toolCalls.length) {
      for (const tc of bm.toolCalls) {
        const tcName = tc.name || 'unknown';
        const tcStatus = tc.status ? ' (' + tc.status + ')' : '';
        const tcSummary = tc.summary || tcName;
        md += '> **Tool: ' + tcSummary + '**' + tcStatus + '\n';
        if (tc.input) {
          md += '>\n> **INPUT:**\n> ```\n';
          String(tc.input).split('\n').forEach(function(line) { md += '> ' + line + '\n'; });
          md += '> ```\n';
        }
        if (tc.output) {
          md += '>\n> **OUTPUT:**\n> ```\n';
          String(tc.output).split('\n').forEach(function(line) { md += '> ' + line + '\n'; });
          md += '> ```\n';
        }
        md += '\n';
      }
    }
    md += '---\n\n';
  }

  // Code edit history
  if (includeMetadata && tab.codeBlockDiffs && tab.codeBlockDiffs.length) {
    md += '## Code edit history\n\n';
    tab.codeBlockDiffs.forEach(function(diff, i) {
      const file = diff.filePath || diff.file || ('diff-' + (diff.diffId || i));
      md += '- **' + file + '** (' + (diff.diffId || i) + ')\n';
      if (Array.isArray(diff.newModelDiffWrtV0) && diff.newModelDiffWrtV0.length) {
        const lines = diff.newModelDiffWrtV0.flatMap(function(d) { return d.modified || []; });
        if (lines.length) {
          md += '  ```\n  ' + lines.join('\n  ') + '\n  ```\n';
        }
      }
      md += '\n';
    });
    md += '---\n\n';
  }

  return md;
}

async function triggerDownload(blob, filename) {
  // Use File System Access API if available — remembers last save location
  if (window.showSaveFilePicker) {
    try {
      const ext = filename.split('.').pop() || '';
      const types = [];
      if (ext === 'md') types.push({ description: 'Markdown', accept: { 'text/markdown': ['.md'] } });
      else if (ext === 'html') types.push({ description: 'HTML', accept: { 'text/html': ['.html'] } });
      else if (ext === 'pdf') types.push({ description: 'PDF', accept: { 'application/pdf': ['.pdf'] } });
      else if (ext === 'json') types.push({ description: 'JSON', accept: { 'application/json': ['.json'] } });
      else if (ext === 'csv') types.push({ description: 'CSV', accept: { 'text/csv': ['.csv'] } });
      else if (ext === 'zip') types.push({ description: 'ZIP Archive', accept: { 'application/zip': ['.zip'] } });
      const handle = await window.showSaveFilePicker({ suggestedName: filename, types: types });
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      return;
    } catch (e) {
      if (e.name === 'AbortError') return; // User cancelled
      // Fall through to legacy download
    }
  }
  // Fallback: programmatic download
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

async function downloadAs(format) {
  if (!selectedTab) return;
  const tab = selectedTab;
  const fname = sanitizeFilename(tab.title || 'chat-' + tab.id);

  if (format === 'md') {
    const md = convertChatToMarkdown(tab, true);
    await triggerDownload(new Blob([md], { type: 'text/markdown' }), fname + '.md');
  }
  else if (format === 'html') {
    const md = convertChatToMarkdown(tab, true);
    // Sanitise with DOMPurify before embedding in the download blob (issue #11).
    // The downloaded file is opened in a browser and any payload would execute
    // in the file:// origin, so XSS still applies.
    const htmlContent = renderMarkdownSafe(md);
    const html = `<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>${escapeHtml(tab.title || 'Chat')}</title>
<style>body{max-width:800px;margin:40px auto;padding:0 20px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;line-height:1.6;color:#333}pre{background:#f5f5f5;padding:1em;overflow-x:auto;border-radius:4px;border:1px solid #ddd}code{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:0.9em}hr{border:none;border-top:1px solid #ddd;margin:2em 0}h1,h2,h3{margin-top:2em;margin-bottom:1em}blockquote{border-left:4px solid #ddd;margin:0;padding-left:1em;color:#666}em{color:#666}@media(prefers-color-scheme:dark){body{background:#1a1a1a;color:#ddd}pre{background:#2d2d2d;border-color:#404040}blockquote{border-color:#404040;color:#999}em{color:#999}}</style>
</head><body>${htmlContent}</body></html>`;
    await triggerDownload(new Blob([html], { type: 'text/html' }), fname + '.html');
  }
  else if (format === 'pdf') {
    await downloadPDF(tab, fname);
  }
  else if (format === 'json') {
    await triggerDownload(new Blob([JSON.stringify(tab, null, 2)], { type: 'application/json' }), fname + '.json');
  }
  else if (format === 'csv') {
    await downloadCSV(tab, fname);
  }
  else if (format === 'csv-code') {
    await downloadCSVCodeEdits(tab, fname);
  }
}

async function downloadPDF(tab, fname) {
  try {
    const md = convertChatToMarkdown(tab, true);
    const res = await fetch('/api/generate-pdf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ markdown: md, title: tab.title || 'Chat ' + tab.id })
    });
    if (!res.ok) throw new Error('Failed to generate PDF');
    const blob = await res.blob();
    triggerDownload(blob, fname + '.pdf');
  } catch (e) {
    alert('Failed to generate PDF: ' + e.message);
  }
}

async function downloadCSV(tab, fname) {
  const headers = [
    'conversation_id','message_index','role','model',
    'input_tokens','output_tokens','cached_tokens',
    'context_tokens_used','context_token_limit','context_pct_remaining',
    'response_time_ms','thinking_duration_ms','cost',
    'tool_name','tool_status','tool_summary','tool_input','tool_output',
    'thinking_text',
    'timestamp','text'
  ];
  const rows = [headers];
  (tab.bubbles || []).forEach(function(b, i) {
    const bm = b.metadata || {};
    const fullText = (b.text || '').replace(/\r?\n/g, ' ');
    const tc = (bm.toolCalls && bm.toolCalls[0]) || {};
    const thinkText = bm.thinking ? (typeof bm.thinking === 'string' ? bm.thinking : (bm.thinking.text || '')) : '';
    rows.push([
      tab.id, String(i), b.type === 'user' ? 'user' : 'assistant',
      bm.modelName || '',
      bm.inputTokens > 0 ? String(bm.inputTokens) : '',
      bm.outputTokens > 0 ? String(bm.outputTokens) : '',
      bm.cachedTokens > 0 ? String(bm.cachedTokens) : '',
      bm.contextTokensUsed > 0 ? String(bm.contextTokensUsed) : '',
      bm.contextTokenLimit > 0 ? String(bm.contextTokenLimit) : '',
      bm.contextWindowPercent != null ? String(bm.contextWindowPercent) : '',
      bm.responseTimeMs != null ? String(bm.responseTimeMs) : '',
      bm.thinkingDurationMs ? String(bm.thinkingDurationMs) : '',
      bm.cost != null ? String(bm.cost) : '',
      tc.name || '', tc.status || '', tc.summary || '',
      (tc.input || '').replace(/\r?\n/g, ' '),
      (tc.output || '').replace(/\r?\n/g, ' '),
      thinkText.replace(/\r?\n/g, ' '),
      b.timestamp ? new Date(b.timestamp).toISOString() : '',
      fullText.replace(/"/g, '""')
    ]);
  });
  const csv = rows.map(function(r) { return r.map(function(c) { return '"' + String(c).replace(/"/g, '""') + '"'; }).join(','); }).join('\n');
  await triggerDownload(new Blob([csv], { type: 'text/csv;charset=utf-8' }), fname + '.csv');
}

async function downloadCSVCodeEdits(tab, fname) {
  const headers = ['conversation_id','diff_index','diff_id','file_path','timestamp','summary'];
  const rows = [headers];
  (tab.codeBlockDiffs || []).forEach(function(diff, i) {
    const diffId = String(diff.diffId || i);
    const filePath = String(diff.filePath || diff.file || '');
    const ts = diff.timestamp ? new Date(diff.timestamp).toISOString() : '';
    const modified = (diff.newModelDiffWrtV0 || []).flatMap(function(d) { return d.modified || []; });
    const summary = modified.slice(0, 5).join('; ').replace(/"/g, '""');
    rows.push([tab.id, String(i), diffId, filePath.replace(/"/g, '""'), ts, summary]);
  });
  const csv = rows.map(function(r) { return r.map(function(c) { return '"' + String(c).replace(/"/g, '""') + '"'; }).join(','); }).join('\n');
  await triggerDownload(new Blob([csv], { type: 'text/csv;charset=utf-8' }), fname + '-code-edits.csv');
}

function copyAllMarkdown() {
  if (!selectedTab) return;
  const md = convertChatToMarkdown(selectedTab, true);
  navigator.clipboard.writeText(md).then(function() {
    // Brief feedback (you could show a toast)
    const btn = document.getElementById('btn-copy-all');
    if (btn) {
      const orig = btn.innerHTML;
      btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Copied!';
      setTimeout(function() { btn.innerHTML = orig; }, 1500);
    }
  });
}
