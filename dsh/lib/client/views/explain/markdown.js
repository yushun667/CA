/**
 * Bounded, dependency-free Markdown parser for AI explanations.
 *
 * The parser only produces plain data: blocks contain strings and inline
 * tokens contain strings. It never produces HTML strings and the view layer
 * builds React elements from the data, so every model-authored character is
 * escaped by React at render time. All parsing is bounded (input characters,
 * block count, inline token count, fenced-code characters) so a hostile or
 * runaway model answer cannot freeze the client.
 *
 * Supported subset (deliberately small):
 *   - ATX headings `#`..`######`
 *   - unordered / ordered lists (with simple continuation lines)
 *   - fenced code blocks (``` and ~~~)
 *   - blockquotes, horizontal rules, paragraphs
 *   - inline code, **bold**, `[E1]` evidence references and `file:line`
 *     source locations.
 */

/** Hard cap on the characters passed to the parser. */
export const MAX_MARKDOWN_CHARS = 24000
/** Hard cap on parsed blocks. */
export const MAX_MARKDOWN_BLOCKS = 400
/** Hard cap on inline tokens per text run. */
export const MAX_INLINE_TOKENS = 8000
/** Hard cap on characters of one fenced code block. */
export const MAX_CODE_BLOCK_CHARS = 8000

const FENCE_RE = /^ {0,3}(```|~~~)\s*([A-Za-z0-9_+.-]*)\s*$/
const HEADING_RE = /^ {0,3}(#{1,6})\s+(.*)$/
const UL_RE = /^ {0,3}[-*+]\s+(.*)$/
const OL_RE = /^ {0,3}(\d{1,3})[.)]\s+(.*)$/
const QUOTE_RE = /^ {0,3}>\s?(.*)$/
const HR_RE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/

/** `[E12]` evidence reference at the start of the remaining text. */
const EVIDENCE_RE = /^\[E(\d{1,4})\]/
/** `path/to/file.c:123` source location at the start of the remaining text. */
const LOCATION_RE =
  /^([A-Za-z0-9_@+./\\-]+\.(?:c|h|cc|hh|cpp|cxx|hpp|hxx|cu|py|rs|go|java|js|mjs|ts))[:：](\d+)/

/** True when a line starts a new non-paragraph block. */
function isBlockStart(line) {
  return (
    FENCE_RE.test(line) ||
    HEADING_RE.test(line) ||
    HR_RE.test(line) ||
    QUOTE_RE.test(line) ||
    UL_RE.test(line) ||
    OL_RE.test(line)
  )
}

/**
 * Split inline Markdown into plain tokens.
 *
 * @param {unknown} input
 * @param {number} [depth] recursion depth for bold spans (max 2)
 * @returns {Array<{type:'text',text:string}|{type:'code',text:string}|{type:'strong',tokens:Array<unknown>}|{type:'evidence',ref:string}|{type:'location',file:string,line:number,text:string}>}
 */
export function parseInline(input, depth = 0) {
  const text = typeof input === 'string' ? input : ''
  const tokens = []
  let buffer = ''
  let index = 0

  const flush = () => {
    if (buffer.length > 0) {
      tokens.push({ type: 'text', text: buffer })
      buffer = ''
    }
  }

  while (index < text.length && tokens.length < MAX_INLINE_TOKENS) {
    const char = text[index]

    if (char === '`') {
      const end = text.indexOf('`', index + 1)
      if (end > index + 1 && text.slice(index + 1, end).indexOf('\n') === -1) {
        flush()
        tokens.push({ type: 'code', text: text.slice(index + 1, end) })
        index = end + 1
        continue
      }
    }

    if (char === '*' && text[index + 1] === '*' && depth < 2) {
      const end = text.indexOf('**', index + 2)
      if (end > index + 2) {
        flush()
        tokens.push({ type: 'strong', tokens: parseInline(text.slice(index + 2, end), depth + 1) })
        index = end + 2
        continue
      }
    }

    if (char === '[') {
      const match = EVIDENCE_RE.exec(text.slice(index))
      if (match) {
        flush()
        tokens.push({ type: 'evidence', ref: `E${match[1]}` })
        index += match[0].length
        continue
      }
    }

    if (char !== ' ' && char !== '\n' && char !== '\t') {
      const location = LOCATION_RE.exec(text.slice(index))
      if (location) {
        flush()
        tokens.push({
          type: 'location',
          file: location[1],
          line: Number(location[2]),
          text: location[0],
        })
        index += location[0].length
        continue
      }
    }

    buffer += char
    index += 1
  }

  if (index < text.length) buffer += text.slice(index)
  flush()
  return tokens
}

/**
 * Parse bounded Markdown into render blocks.
 *
 * @param {unknown} input
 * @returns {{blocks: Array<Record<string, unknown>>, truncated: boolean}}
 */
export function parseMarkdown(input) {
  let source = typeof input === 'string' ? input : ''
  let truncated = false
  if (source.length > MAX_MARKDOWN_CHARS) {
    source = source.slice(0, MAX_MARKDOWN_CHARS)
    truncated = true
  }
  source = source.replace(/\r\n?/g, '\n')
  const lines = source.split('\n')
  const blocks = []

  const push = (block) => {
    if (blocks.length < MAX_MARKDOWN_BLOCKS) blocks.push(block)
    else truncated = true
  }

  let index = 0
  while (index < lines.length) {
    const line = lines[index]

    if (line.trim().length === 0) {
      index += 1
      continue
    }

    const fence = FENCE_RE.exec(line)
    if (fence) {
      const marker = fence[1]
      const body = []
      let chars = 0
      index += 1
      while (index < lines.length) {
        const candidate = lines[index]
        const closing = FENCE_RE.exec(candidate)
        if (closing && closing[1] === marker) {
          index += 1
          break
        }
        chars += candidate.length + 1
        if (chars <= MAX_CODE_BLOCK_CHARS) body.push(candidate)
        else truncated = true
        index += 1
      }
      push({ type: 'code', lang: fence[2] || '', text: body.join('\n') })
      continue
    }

    const heading = HEADING_RE.exec(line)
    if (heading) {
      push({ type: 'heading', level: heading[1].length, text: heading[2].trim() })
      index += 1
      continue
    }

    if (HR_RE.test(line)) {
      push({ type: 'hr' })
      index += 1
      continue
    }

    const quote = QUOTE_RE.exec(line)
    if (quote) {
      const parts = []
      while (index < lines.length) {
        const item = QUOTE_RE.exec(lines[index])
        if (!item) break
        parts.push(item[1])
        index += 1
      }
      push({ type: 'quote', text: parts.join('\n') })
      continue
    }

    const unordered = UL_RE.exec(line)
    const ordered = unordered ? null : OL_RE.exec(line)
    if (unordered || ordered) {
      const isOrdered = Boolean(ordered)
      const items = []
      while (index < lines.length) {
        const nextUnordered = UL_RE.exec(lines[index])
        const nextOrdered = OL_RE.exec(lines[index])
        if (isOrdered && nextOrdered) {
          items.push(nextOrdered[2])
          index += 1
        } else if (!isOrdered && nextUnordered) {
          items.push(nextUnordered[1])
          index += 1
        } else if (items.length > 0 && /^ {1,6}\S/.test(lines[index])) {
          items[items.length - 1] = `${items[items.length - 1]} ${lines[index].trim()}`
          index += 1
        } else {
          break
        }
      }
      push({ type: 'list', ordered: isOrdered, items })
      continue
    }

    const paragraph = [line]
    index += 1
    while (index < lines.length && lines[index].trim().length > 0 && !isBlockStart(lines[index])) {
      paragraph.push(lines[index])
      index += 1
    }
    push({ type: 'paragraph', text: paragraph.join('\n') })
  }

  return { blocks, truncated }
}
