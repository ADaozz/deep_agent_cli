/**
 * A deliberately small Markdown renderer for assistant output.
 *
 * It emits HTML for a fixed, hand-written subset (headings, paragraphs, lists,
 * quotes, fenced code, tables, rules, bold/italic/inline code/links). Every
 * piece of source text is HTML-escaped before it reaches the output, and link
 * targets are restricted to http(s)/mailto/relative URLs, so the result is safe
 * to hand to `v-html` — no raw user or model text is ever injected verbatim.
 */

const ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ESCAPES[char] ?? char)
}

function safeHref(raw: string): string {
  const href = raw.trim()
  if (/^(https?:\/\/|mailto:)/i.test(href)) return href
  if (/^(\/|#)/.test(href)) return href
  return ''
}

/** Inline spans: code → links → bold → italic, with protected slots. */
function renderInline(source: string): string {
  const slots: string[] = []
  const keep = (html: string): string => `\u0000${slots.push(html) - 1}\u0000`

  let text = escapeHtml(source)

  // Inline code wins over every other inline rule.
  text = text.replace(/`([^`]+)`/g, (_match, code: string) => keep(`<code>${code}</code>`))

  text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, label: string, href: string) => {
    const url = safeHref(href)
    if (!url) return match
    return keep(
      `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer noopener">${label}</a>`,
    )
  })

  text = text.replace(/\*\*([^*]+)\*\*/g, (_match, body: string) => keep(`<strong>${body}</strong>`))
  text = text.replace(/(^|[\s(])\*([^*\n]+)\*/g, (_match, lead: string, body: string) =>
    keep(`${lead}<em>${body}</em>`),
  )
  text = text.replace(/(^|[\s(])_([^_\n]+)_/g, (_match, lead: string, body: string) =>
    keep(`${lead}<em>${body}</em>`),
  )

  // A slot kept inside bold/italic still contains its own placeholder. Restore
  // until the text is stable so nested inline code is not left as an index.
  let restored = text
  for (let pass = 0; pass <= slots.length; pass += 1) {
    const next = restored.replace(/\u0000(\d+)\u0000/g, (_match, index: string) => slots[Number(index)] ?? '')
    if (next === restored) break
    restored = next
  }
  return restored
}

const HEADING = /^(#{1,6})\s+(.*)$/
const FENCE = /^\s*```(.*)$/
const RULE = /^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$/
const UL = /^(\s*)([-*+])\s+(.*)$/
const OL = /^(\s*)(\d{1,9})[.)]\s+(.*)$/
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/
const QUOTE = /^\s*>\s?(.*)$/

function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '')
  return trimmed.split('|').map((cell) => cell.trim())
}

function renderTable(rows: string[][]): string {
  if (!rows.length) return ''
  const [head, ...body] = rows
  const headHtml = head.map((cell) => `<th>${renderInline(cell)}</th>`).join('')
  const bodyHtml = body
    .map((row) => `<tr>${row.map((cell) => `<td>${renderInline(cell)}</td>`).join('')}</tr>`)
    .join('')
  return `<div class="md-table"><table><thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`
}

export function renderMarkdown(source: string): string {
  const lines = (source ?? '').replace(/\r\n?/g, '\n').split('\n')
  const out: string[] = []
  let paragraph: string[] = []

  const flushParagraph = (): void => {
    if (!paragraph.length) return
    out.push(`<p>${paragraph.map((line) => renderInline(line)).join('<br />')}</p>`)
    paragraph = []
  }

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]

    // fenced code -------------------------------------------------------
    const fence = FENCE.exec(line)
    if (fence) {
      flushParagraph()
      const body: string[] = []
      index += 1
      while (index < lines.length && !/^\s*```/.test(lines[index])) {
        body.push(lines[index])
        index += 1
      }
      out.push(`<pre><code>${escapeHtml(body.join('\n'))}</code></pre>`)
      continue
    }

    if (!line.trim()) {
      flushParagraph()
      continue
    }

    if (RULE.test(line)) {
      flushParagraph()
      out.push('<hr />')
      continue
    }

    const heading = HEADING.exec(line)
    if (heading) {
      flushParagraph()
      const level = Math.min(heading[1].length, 6)
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`)
      continue
    }

    // table -------------------------------------------------------------
    if (line.includes('|') && index + 1 < lines.length && TABLE_SEP.test(lines[index + 1])) {
      flushParagraph()
      const rows: string[][] = [splitRow(line)]
      index += 2
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        rows.push(splitRow(lines[index]))
        index += 1
      }
      index -= 1
      out.push(renderTable(rows))
      continue
    }

    // blockquote --------------------------------------------------------
    const quote = QUOTE.exec(line)
    if (quote) {
      flushParagraph()
      const body: string[] = [quote[1]]
      while (index + 1 < lines.length && QUOTE.test(lines[index + 1])) {
        index += 1
        body.push(QUOTE.exec(lines[index])?.[1] ?? '')
      }
      out.push(`<blockquote>${body.map((item) => renderInline(item)).join('<br />')}</blockquote>`)
      continue
    }

    // lists -------------------------------------------------------------
    const ordered = OL.exec(line)
    const unordered = ordered ? null : UL.exec(line)
    if (ordered || unordered) {
      flushParagraph()
      const tag = ordered ? 'ol' : 'ul'
      const items: string[] = []
      let expect = ordered ? Number(ordered[2]) : 0
      while (index < lines.length) {
        const current = lines[index]
        const nextOrdered = OL.exec(current)
        const nextUnordered = nextOrdered ? null : UL.exec(current)
        if (nextOrdered) {
          if (!ordered || Number(nextOrdered[2]) !== expect) break
          items.push(renderInline(nextOrdered[3]))
          expect += 1
        } else if (nextUnordered) {
          if (ordered) break
          items.push(renderInline(nextUnordered[3]))
        } else if (/^\s{2,}\S/.test(current) && items.length) {
          // continuation line of the previous item
          items[items.length - 1] += ` ${renderInline(current.trim())}`
        } else {
          break
        }
        index += 1
      }
      index -= 1
      out.push(`<${tag}>${items.map((item) => `<li>${item}</li>`).join('')}</${tag}>`)
      continue
    }

    paragraph.push(line.trim())
  }

  flushParagraph()
  return out.join('\n')
}
