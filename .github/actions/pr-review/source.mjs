import { digest, hunkRanges, sourcePatch, Unavailable } from './github.mjs';

// A view is a sequence of literal source slices, never parsed JSON values.
// Inserting view separators cannot round numbers, collapse duplicate keys,
// normalize escapes, or repair invalid JSON. Concatenation recovers the source.
export function literalSegments(content, json = false, maxCharacters = 2048) {
  if (content === null || content === '') return [];
  const result = [];
  let start = 0, byte = 0, line = 1, column = 1, quoted = false, escaped = false;
  const emit = end => {
    const text = content.slice(start, end), bytes = Buffer.byteLength(text);
    const breaks = [...text.matchAll(/\n/g)];
    const endLine = line + breaks.length;
    const endColumn = breaks.length ? text.length - breaks.at(-1).index : column + text.length;
    result.push({ start_byte: byte, end_byte: byte + bytes, start_line: line, start_column: column,
      end_line: endLine, end_column: endColumn, content: text });
    byte += bytes; line = endLine; column = endColumn; start = end;
  };
  for (let i = 0; i < content.length; i++) {
    const c = content[i];
    if (json) {
      if (quoted) {
        if (escaped) escaped = false;
        else if (c === '\\') escaped = true;
        else if (c === '"') quoted = false;
      } else if (c === '"') quoted = true;
    }
    // Never bisect a surrogate pair or CRLF. State carries across long literals.
    const boundary = c === '\n' || (json && !quoted && '{}[],'.includes(c)) || i + 1 - start >= maxCharacters;
    const pair = /[\uD800-\uDBFF]/.test(c) && /[\uDC00-\uDFFF]/.test(content[i + 1] ?? '');
    if (boundary && !pair && !(c === '\r' && content[i + 1] === '\n')) emit(i + 1);
  }
  if (start < content.length) emit(content.length);
  return result;
}

const metadata = (content, segments) => content === null ? null : {
  sha256: digest(content), bytes: Buffer.byteLength(content),
  line_count: content === '' ? 0 : content.replace(/\n$/, '').split('\n').length,
  segments: segments.length,
};

export function segmentBundleFile(file, before, after) {
  const json = file.filename.endsWith('.json');
  const sides = { before: literalSegments(before, json), after: literalSegments(after, json) };
  const view = segments => segments.map(s => JSON.stringify(s.content)).join('\n') + (segments.length ? '\n' : '');
  const patch = sourcePatch(view(sides.before), view(sides.after),
    Buffer.byteLength(before ?? '') + Buffer.byteLength(after ?? '') <= 80_000 ? Math.max(sides.before.length, sides.after.length) : 40);
  const hunks = [];
  let current, old, next;
  for (const text of patch.split('\n')) {
    const range = hunkRanges(text)[0];
    if (range) {
      current = { id: String(hunks.length + 1), base: range.base, head: range.head, rows: [] };
      hunks.push(current); old = range.base.start - 1; next = range.head.start - 1;
    } else if (current && [' ', '-', '+'].includes(text[0])) {
      const kind = { ' ': 'context', '-': 'delete', '+': 'add' }[text[0]], row = { kind, view_count: 1 };
      if (kind !== 'add') row.before = sides.before[old++];
      if (kind !== 'delete') row.after = sides.after[next++];
      if ((kind !== 'add' && !row.before) || (kind !== 'delete' && !row.after)) throw new Unavailable('Literal diff exceeds pinned source.');
      const previous = current.rows.at(-1);
      if (previous?.kind === kind && ['before', 'after'].every(side => !row[side] ||
          (previous[side].end_byte === row[side].start_byte && previous[side].content.length + row[side].content.length <= 2048))) {
        previous.view_count++;
        for (const side of ['before', 'after']) if (row[side]) previous[side] = { ...previous[side],
          end_byte: row[side].end_byte, end_line: row[side].end_line, end_column: row[side].end_column,
          content: previous[side].content + row[side].content };
      } else current.rows.push(row);
    }
  }
  // Empty files and pure renames still have a review unit. A rename supplies
  // all its source, partitioned later if necessary, rather than inventing edits.
  if (!hunks.length) hunks.push({ id: '1', base: { start: sides.before.length ? 1 : 0, count: sides.before.length },
    head: { start: sides.after.length ? 1 : 0, count: sides.after.length }, rows: sides.after.map((s, i) => ({ kind: 'context', view_count: 1, before: sides.before[i], after: s })) });
  for (const hunk of hunks) {
    for (const [side, key] of [['base', 'before'], ['head', 'after']]) {
      if (hunk.rows.filter(r => r[key]).reduce((n, r) => n + r.view_count, 0) !== hunk[side].count) throw new Unavailable('Literal diff has incomplete hunk coverage.');
    }
  }
  return { path: file.filename, previous_path: file.previous_filename ?? null, status: file.status,
    source_mode: 'segments', representation: json ? 'literal-json-segments-v1' : 'literal-line-segments-v1',
    before: metadata(before, sides.before), after: metadata(after, sides.after), hunks };
}

export function fileSegments(file, side) {
  return file.hunks.flatMap(h => h.rows.flatMap(r => r[side] ? [r[side]] : []));
}

// Recover contiguous supplied windows using absolute byte offsets. Overlap from
// nearby hunks is deduplicated; gaps are never silently joined.
export function segmentWindows(segments) {
  const unique = new Map();
  for (const segment of segments) {
    const key = segment.start_byte, previous = unique.get(key);
    if (previous && JSON.stringify(previous) !== JSON.stringify(segment)) throw new Error('conflicting source fragments');
    unique.set(key, segment);
  }
  const windows = [];
  for (const segment of [...unique.values()].sort((a, b) => a.start_byte - b.start_byte)) {
    const previous = windows.at(-1);
    if (previous && previous.end_byte === segment.start_byte) {
      previous.content += segment.content; previous.end_byte = segment.end_byte;
      previous.end_line = segment.end_line; previous.end_column = segment.end_column;
    } else windows.push({ ...segment });
  }
  return windows;
}

export function segmentQuote(file, side, start, end, startByte, endByte) {
  for (const window of segmentWindows(fileSegments(file, side))) {
    if (startByte !== null && startByte !== undefined) {
      if (!Number.isSafeInteger(startByte) || !Number.isSafeInteger(endByte) || endByte <= startByte ||
          startByte < window.start_byte || endByte > window.end_byte) continue;
      const bytes = Buffer.from(window.content), prefix = bytes.subarray(0, startByte - window.start_byte).toString('utf8');
      const quote = bytes.subarray(startByte - window.start_byte, endByte - window.start_byte).toString('utf8');
      if (Buffer.byteLength(prefix) !== startByte - window.start_byte || Buffer.byteLength(quote) !== endByte - startByte) continue;
      const first = window.start_line + (prefix.match(/\n/g) ?? []).length;
      const last = first + (quote.replace(/\r?\n$/, '').match(/\n/g) ?? []).length;
      if (first === start && last === end) return quote;
    } else if (endByte === null || endByte === undefined) {
      const lines = window.content.replace(/\r?\n$/, '').split(/\r?\n/);
      if (start < window.start_line || end < start || end >= window.start_line + lines.length ||
          (start === window.start_line && window.start_column !== 1) ||
          (end === window.end_line && window.end_column !== 1 && window.end_byte < file[side].bytes)) continue;
      return lines.slice(start - window.start_line, end - window.start_line + 1).join('\n');
    }
  }
  return null;
}

export function changedRanges(file) {
  const ranges = [];
  for (const hunk of file.hunks) for (const row of hunk.rows) {
    if (row.kind === 'context') continue;
    const side = row.kind === 'add' ? 'head' : 'base', segment = row[side === 'head' ? 'after' : 'before'];
    ranges.push({ side, start: segment.start_line,
      end: segment.end_line - (segment.content.endsWith('\n') ? 1 : 0) });
  }
  if (!ranges.length && ['renamed', 'copied'].includes(file.status)) for (const s of fileSegments(file, 'after')) {
    ranges.push({ side: 'head', start: s.start_line, end: s.end_line - (s.content.endsWith('\n') ? 1 : 0) });
  }
  const merged = [];
  for (const range of ranges.sort((a, b) => a.side.localeCompare(b.side) || a.start - b.start)) {
    const previous = merged.at(-1);
    if (previous?.side === range.side && range.start <= previous.end + 1) previous.end = Math.max(previous.end, range.end);
    else merged.push({ ...range });
  }
  return merged;
}
