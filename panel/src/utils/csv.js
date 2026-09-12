// Client-side CSV writing. No dependency — builds an RFC-4180 string from
// plain objects and triggers a browser download. Backs both every tab's
// Export CSV action and the downloadable import samples in sampleCsv.js.

// YYYY-MM-DD, for stamping export filenames.
export function today() {
  return new Date().toISOString().slice(0, 10)
}

// Quote a single field only when it contains a comma, quote, or newline; inner
// quotes are doubled per RFC 4180.
function escapeField(value) {
  if (value == null) return ''
  const s = String(value)
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

// columns: [{ key, label, format? }]  — `format(row)` overrides `row[key]`.
// rows:    array of plain objects.
export function toCSV(columns, rows) {
  const header = columns.map((c) => escapeField(c.label)).join(',')
  const body = rows
    .map((row) =>
      columns
        .map((c) => escapeField(c.format ? c.format(row) : row[c.key]))
        .join(','),
    )
    .join('\r\n')
  return `${header}\r\n${body}`
}

// Build the CSV and save it as `filename`. A leading BOM makes Excel open
// UTF-8 (₹, accented names) correctly.
export function downloadCSV(filename, columns, rows) {
  const csv = toCSV(columns, rows)
  const blob = new Blob(['﻿', csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

/** Split CSV text into a list of smaller CSV documents, each repeating the
 *  header row. Used by the existing-QR import, where a single file can carry
 *  six figures of codes — far past what one request should hold.
 *
 *  Newlines inside a quoted cell are NOT row separators, so the scan tracks
 *  quote state rather than splitting on \n. Getting that wrong would cut a
 *  row in half and silently corrupt two records instead of importing one.
 */
export function chunkCsv(text, rowsPerChunk) {
  const lines = []
  let cur = ''
  let quoted = false
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (ch === '"') quoted = !quoted
    if ((ch === '\n' || ch === '\r') && !quoted) {
      if (ch === '\r' && text[i + 1] === '\n') i++
      lines.push(cur)
      cur = ''
    } else {
      cur += ch
    }
  }
  if (cur.trim()) lines.push(cur)
  const header = lines.shift()
  if (header === undefined) return []
  const rows = lines.filter((l) => l.trim())
  const out = []
  for (let i = 0; i < rows.length; i += rowsPerChunk) {
    out.push([header, ...rows.slice(i, i + rowsPerChunk)].join('\n'))
  }
  return out
}
