import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { resultSchema, validateSchema } from './schema.mjs'
import { segmentQuote, changedRanges } from './source.mjs'

export function canonical(value) {
	if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
	if (value && typeof value === 'object') return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
	return JSON.stringify(value)
}
export const digest = (value) => createHash('sha256').update(canonical(value)).digest('hex')
const instructions = readFileSync(new URL('./reviewer.md', import.meta.url), 'utf8')
export const reviewerVersion = `sha256:${digest({ instructions, resultSchema })}`
export const reviewerInstructions = `${instructions}\nReviewer version: ${reviewerVersion}\nResult schema: ${canonical(resultSchema)}\n`
export { resultSchema }

function requireValue(ok, message) { if (!ok) throw new Error(message) }
const nonempty = (s) => typeof s === 'string' && s.trim().length > 0
const validPath = (p) => nonempty(p) && !p.startsWith('/') && !p.split('/').some((s) => s === '..' || s === '.' || !s) && !/[\0\r\n\\]/.test(p)
const lines = (content) => content === '' ? [] : content.replace(/\r?\n$/, '').split(/\r?\n/)

export function sourceQuote(input, path, side, start, end, startByte = null, endByte = null) {
	for (const e of input.references ?? []) if (e.path === path && e.side === side && e.start_line === start && e.end_line === end && e.start_byte === startByte && e.end_byte === endByte) return e.quote
	if (side === 'context') {
		for (const context of input.context.filter(f => f.path === path)) {
			if (context.segments) {
				const quote = segmentQuote({ after: { bytes: context.bytes },
					hunks: [{ rows: context.segments.map(after => ({ after })) }] }, 'after', start, end, startByte, endByte)
				if (quote !== null) return quote
				continue
			}
			if (startByte !== null || endByte !== null) return null
			const source = lines(context.content)
			if (start >= 1 && end >= start && end <= source.length) return source.slice(start - 1, end).join('\n')
		}
		return null
	}
	const file = input.files.find(f => f.path === path || (side === 'base' && f.previous_path === path))
	return file ? segmentQuote(file, side === 'base' ? 'before' : 'after', start, end, startByte, endByte) : null
}

function validateFile(file) {
	requireValue(validPath(file.path) && (file.previous_path === null || validPath(file.previous_path)), 'invalid source path')
	requireValue(['added', 'modified', 'removed', 'renamed', 'copied'].includes(file.status), 'invalid file status')
	requireValue(file.source_mode === 'segments' && ['literal-json-segments-v1', 'literal-line-segments-v1'].includes(file.representation), 'invalid source representation')
	for (const side of ['before', 'after']) {
		const metadata = file[side]
		requireValue(metadata === null || (/^[a-f0-9]{64}$/.test(metadata.sha256) && ['bytes', 'line_count', 'segments'].every(k => Number.isSafeInteger(metadata[k]) && metadata[k] >= 0)), 'full source identity required')
	}
	requireValue(file.status === 'added' ? file.before === null && file.after !== null : file.status === 'removed' ? file.before !== null && file.after === null : file.before !== null && file.after !== null, 'missing source side')
	requireValue(Array.isArray(file.hunks) && file.hunks.length > 0, 'complete hunks required')
	for (const hunk of file.hunks) {
		requireValue(nonempty(hunk.id) && Array.isArray(hunk.rows), 'invalid hunk')
		for (const row of hunk.rows) {
			requireValue(['add', 'delete', 'context'].includes(row.kind), 'invalid hunk operation')
			for (const side of ['before', 'after']) {
				const required = side === 'before' ? row.kind !== 'add' : row.kind !== 'delete'
				const s = row[side]
				requireValue(required === Boolean(s), 'hunk source missing')
				if (s) requireValue(file[side] !== null && typeof s.content === 'string' && s.content.length > 0 &&
					['start_byte', 'end_byte', 'start_line', 'end_line', 'start_column', 'end_column'].every(k => Number.isSafeInteger(s[k]) && s[k] >= (k.endsWith('byte') ? 0 : 1)) &&
					s.end_byte - s.start_byte === Buffer.byteLength(s.content) && s.end_byte <= file[side].bytes, 'invalid literal source segment')
			}
		}
	}
}

export function createReviewInput(bundle) {
	const input = structuredClone(bundle)
	delete input.input_digest
	delete input.reviewer_version
	requireValue(input.schema_version === 2 && nonempty(input.repository) && nonempty(input.repository_id), 'invalid repository identity')
	for (const field of ['base_sha', 'head_sha']) requireValue(/^[a-f0-9]{40}$/.test(input[field]), 'full commit SHA required')
	requireValue(nonempty(input.policy_version) && input.trigger && ['kind', 'id', 'generation'].every((k) => nonempty(input.trigger[k])), 'policy and trigger identity required')
	requireValue(Array.isArray(input.files) && input.files.length > 0 && Array.isArray(input.context), 'source bundle required')
	const paths = new Set()
	for (const file of input.files) {
		validateFile(file)
		requireValue(!paths.has(file.path), 'duplicate changed path')
		paths.add(file.path)
	}
	for (const context of input.context) requireValue(validPath(context.path) && (typeof context.content === 'string' || Array.isArray(context.segments)), 'invalid context')
	return { ...input, input_digest: digest(input), reviewer_version: reviewerVersion }
}

export function unavailable(input, reason, extra = {}) {
	return { status: 'unavailable', head_sha: input.head_sha, base_sha: input.base_sha,
		input_digest: input.input_digest, reviewer_version: reviewerVersion,
		findings: [], omitted_paths: input.files.map((f) => f.path), reason, ...extra }
}

export function parseReviewResult(text, input) {
	try {
		const result = validateSchema(JSON.parse(text), resultSchema)
		for (const key of ['repository_id', 'base_sha', 'head_sha', 'input_digest', 'reviewer_version', 'policy_version']) requireValue(result[key] === input[key], `mismatched ${key}`)
		// Only schema-validated enums, counts and a presence flag cross the
		// diagnostic boundary. Model prose and omitted path values may echo source.
		const incomplete = [
			result.completion !== 'complete' && `completion=${result.completion}`,
			result.omitted_paths.length > 0 && `omitted_paths=${result.omitted_paths.length}`,
			result.reason !== '' && 'reason=nonempty',
		].filter(Boolean)
		requireValue(incomplete.length === 0, `review incomplete (${incomplete.join(', ')})`)
		for (const finding of result.findings) {
			requireValue(finding.end_line >= finding.start_line && (input.scope?.kind === 'synthesis' ? input.manifest : input.files).some(f => f.path === finding.path), 'finding outside supplied source')
			if (input.scope?.kind !== 'synthesis') requireValue(input.files.some(f => f.path === finding.path && changedRanges(f).some(r => r.side === finding.side && finding.start_line >= r.start && finding.end_line <= r.end)), 'finding outside changed source')
			for (const evidence of finding.evidence) requireValue(sourceQuote(input, evidence.path, evidence.side, evidence.start_line, evidence.end_line, evidence.start_byte, evidence.end_byte) === evidence.quote, 'evidence differs from supplied source')
		}
		for (const evidence of result.related_evidence) requireValue(sourceQuote(input, evidence.path, evidence.side, evidence.start_line, evidence.end_line, evidence.start_byte, evidence.end_byte) === evidence.quote, 'related evidence differs from supplied source')
		return { ...result, status: 'complete' }
	} catch (error) {
		return unavailable(input, error instanceof SyntaxError ? 'invalid JSON review result' : error.message)
	}
}
