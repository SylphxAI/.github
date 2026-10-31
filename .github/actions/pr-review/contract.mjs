import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { resultSchema, validateSchema } from './schema.mjs'

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

export function sourceWindows(file, side) {
	if (file.source_mode === 'windows') return file[`${side}_windows`]
	const content = side === 'before' ? file.before : file.after
	return content === null ? [] : [{ start_line: 1, content }]
}
export function sourceQuote(input, path, side, start, end) {
	let windows
	if (side === 'context') {
		const context = input.context.find((f) => f.path === path)
		windows = context ? [{ start_line: 1, content: context.content }] : []
	} else {
		const file = input.files.find((f) => f.path === path || (side === 'base' && f.previous_path === path))
		windows = file ? sourceWindows(file, side === 'base' ? 'before' : 'after') : []
	}
	for (const window of windows) {
		const source = lines(window.content)
		if (start >= window.start_line && end >= start && end < window.start_line + source.length) {
			return source.slice(start - window.start_line, end - window.start_line + 1).join('\n')
		}
	}
	return null
}

function validateFile(file) {
	requireValue(validPath(file.path) && (file.previous_path === null || validPath(file.previous_path)), 'invalid source path')
	requireValue(['added', 'modified', 'removed', 'renamed', 'copied'].includes(file.status), 'invalid file status')
	requireValue(typeof file.patch === 'string', 'complete patch required')
	const mode = file.source_mode ?? 'full'
	requireValue(['full', 'windows'].includes(mode), 'invalid source_mode')
	file.source_mode = mode
	for (const side of ['before', 'after']) {
		if (mode === 'full') {
			requireValue(file[side] === null || typeof file[side] === 'string', 'full source required')
			continue
		}
		requireValue(file[side] === null && Array.isArray(file[`${side}_windows`]), 'windowed source required')
		requireValue(Number.isSafeInteger(file[`${side}_line_count`]) && file[`${side}_line_count`] >= 0, 'source line count required')
		const hash = file[`${side}_sha256`]
		requireValue(hash === null || /^[a-f0-9]{64}$/.test(hash), 'full source digest required')
		let previousEnd = 0
		for (const window of file[`${side}_windows`]) {
			requireValue(Number.isSafeInteger(window.start_line) && window.start_line > previousEnd && typeof window.content === 'string', 'invalid source window')
			previousEnd = window.start_line + lines(window.content).length - 1
			requireValue(previousEnd <= file[`${side}_line_count`], 'source window exceeds file')
		}
	}
	requireValue(mode === 'windows' || (file.status === 'added' ? file.before === null && file.after !== null : file.status === 'removed' ? file.before !== null && file.after === null : file.before !== null && file.after !== null), 'missing source side')
	// Each patch hunk, including its unchanged context, must be supplied verbatim.
	const probe = { files: [file], context: [] }
	for (const hunk of file.patch.matchAll(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/gm)) {
		for (const [side, start, count] of [['base', Number(hunk[1]), Number(hunk[2] ?? 1)], ['head', Number(hunk[3]), Number(hunk[4] ?? 1)]]) {
			if (count > 0) requireValue(sourceQuote(probe, file.path, side, start, start + count - 1) !== null, 'patch hunk missing source coverage')
		}
	}
}

export function createReviewInput(bundle) {
	const input = structuredClone(bundle)
	delete input.input_digest
	delete input.reviewer_version
	requireValue(input.schema_version === 1 && nonempty(input.repository) && nonempty(input.repository_id), 'invalid repository identity')
	for (const field of ['base_sha', 'head_sha']) requireValue(/^[a-f0-9]{40}$/.test(input[field]), 'full commit SHA required')
	requireValue(nonempty(input.policy_version) && input.trigger && ['kind', 'id', 'generation'].every((k) => nonempty(input.trigger[k])), 'policy and trigger identity required')
	requireValue(Array.isArray(input.files) && input.files.length > 0 && Array.isArray(input.context), 'source bundle required')
	const paths = new Set()
	for (const file of input.files) {
		validateFile(file)
		requireValue(!paths.has(file.path), 'duplicate changed path')
		paths.add(file.path)
	}
	for (const context of input.context) requireValue(validPath(context.path) && typeof context.content === 'string', 'invalid context')
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
			requireValue(finding.end_line >= finding.start_line && sourceQuote(input, finding.path, finding.side, finding.start_line, finding.end_line) !== null, 'finding outside supplied source')
			for (const evidence of finding.evidence) requireValue(sourceQuote(input, evidence.path, evidence.side, evidence.start_line, evidence.end_line) === evidence.quote, 'evidence differs from supplied source')
		}
		return { ...result, status: 'complete' }
	} catch (error) {
		return unavailable(input, error instanceof SyntaxError ? 'invalid JSON review result' : error.message)
	}
}
