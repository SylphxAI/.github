import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { contextRequestLimit, resultSchema, validateSchema } from './schema.mjs'
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
	const file = input.files.find(f => (side === 'base' ? f.previous_path ?? f.path : f.path) === path)
	if (side !== 'context' && file) {
		const quote = segmentQuote(file, side === 'base' ? 'before' : 'after', start, end, startByte, endByte)
		if (quote !== null) return quote
	}
	{
		for (const context of input.context.filter(f => f.path === path && (f.side ?? 'context') === side)) {
			if (context.absent === true) continue
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
}

function validateFile(file) {
	requireValue(validPath(file.path) && (file.previous_path === null || validPath(file.previous_path)), 'invalid source path')
	requireValue(['added', 'modified', 'removed', 'renamed', 'copied'].includes(file.status), 'invalid file status')
	const binary = file.source_mode === 'binary-metadata'
	requireValue(binary ? file.representation === 'binary-metadata-v1' && nonempty(file.limitation) :
		file.source_mode === 'segments' && ['literal-json-segments-v1', 'literal-line-segments-v1'].includes(file.representation), 'invalid source representation')
	if (binary) requireValue(['before', 'after'].some(side => file[side]?.encoding === 'binary'), 'binary side required')
	for (const side of ['before', 'after']) {
		const metadata = file[side]
		requireValue(metadata === null || (/^[a-f0-9]{64}$/.test(metadata.sha256) && Number.isSafeInteger(metadata.bytes) && metadata.bytes >= 0 &&
			(metadata.encoding === 'binary' ? binary && metadata.line_count === null && metadata.segments === 0 :
				['line_count', 'segments'].every(k => Number.isSafeInteger(metadata[k]) && metadata[k] >= 0))), 'full source identity required')
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
				if (s) requireValue(file[side] !== null && file[side].encoding !== 'binary' && typeof s.content === 'string' && s.content.length > 0 &&
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
	const contexts = new Set()
	for (const context of input.context) {
		const absent = context.absent === true
		requireValue(context.absent === undefined || absent, 'invalid absence evidence')
		requireValue(validPath(context.path) && (absent
			? ['base', 'head'].includes(context.side) && Object.keys(context).every(k => ['path', 'side', 'commit_sha', 'absent'].includes(k))
			: typeof context.content === 'string' || Array.isArray(context.segments)), 'invalid context')
		requireValue(context.side === undefined || (['base', 'head'].includes(context.side) && context.commit_sha === input[`${context.side}_sha`]), 'invalid pinned context identity')
		if (absent) requireValue(!input.files.some(f => (context.side === 'base' ? f.previous_path ?? f.path : f.path) === context.path && f[context.side === 'base' ? 'before' : 'after'] !== null), 'absence contradicts supplied source')
		const key = canonical([context.path, context.side ?? 'context'])
		requireValue(!contexts.has(key), 'duplicate context identity')
		contexts.add(key)
	}
	return { ...input, input_digest: digest(input), reviewer_version: reviewerVersion }
}

export function unavailable(input, reason, extra = {}) {
	return { status: 'unavailable', head_sha: input.head_sha, base_sha: input.base_sha,
		input_digest: input.input_digest, reviewer_version: reviewerVersion,
		findings: [], omitted_paths: input.files.map((f) => f.path), reason, ...extra }
}

// Retained only in the mode-0600 runner result, never the public reason/receipt.
const diagnostic = (result, input) => ({ input_digest: input.input_digest, scope: input.scope ?? null,
	completion: result.completion, omitted_paths: result.omitted_paths.filter(p => p.length <= 1024 && (input.manifest ?? input.files).some(f => f.path === p)).slice(0, 32),
	reason: result.reason.slice(0, 4096), context_notes: result.context_notes.slice(0, 16).map(n => n.slice(0, 1024)),
	context_requests: result.context_requests.filter(r => validPath(r.path) && r.path.length <= 1024).slice(0, contextRequestLimit).map(r => ({ path: r.path, side: r.side })),
})

// Only machine-authored metadata crosses the public receipt boundary. Unknown
// paths and all source/model prose stay out, even when the quote is a secret.
function evidenceFailure(input, evidence, expected, kind, index, findingIndex) {
	const known = (input.files ?? []).some(f => (evidence.side === 'base' ? f.previous_path ?? f.path : f.path) === evidence.path) ||
		(input.context ?? []).some(c => c.absent !== true && c.path === evidence.path && (c.side ?? 'context') === evidence.side) ||
		(input.references ?? []).some(r => r.path === evidence.path && r.side === evidence.side)
	const fingerprint = quote => quote === null ? null : ({ bytes: Buffer.byteLength(quote),
		sha256: createHash('sha256').update(quote).digest('hex') })
	return { version: 1, code: expected === null ? 'source_unavailable' : 'quote_mismatch',
		input_digest: input.input_digest, kind, evidence_index: index, finding_index: findingIndex,
		path: known && validPath(evidence.path) && /^[a-zA-Z0-9._/ @+-]{1,1024}$/.test(evidence.path) ? evidence.path : null,
		side: evidence.side, start_line: evidence.start_line, end_line: evidence.end_line,
		start_byte: evidence.start_byte, end_byte: evidence.end_byte,
		expected: fingerprint(expected), returned: fingerprint(evidence.quote) }
}

export function parseReviewResult(text, input) {
	let diagnostics, validationFailure
	try {
		const result = validateSchema(JSON.parse(text), resultSchema)
		for (const key of ['repository_id', 'base_sha', 'head_sha', 'input_digest', 'reviewer_version', 'policy_version']) requireValue(result[key] === input[key], `mismatched ${key}`)
		const incomplete = [
			result.completion !== 'complete' && `completion=${result.completion}`,
			result.omitted_paths.length > 0 && `omitted_paths=${result.omitted_paths.length}`,
			result.reason !== '' && 'reason=nonempty',
		].filter(Boolean)
		if (incomplete.length || result.context_requests.length) diagnostics = diagnostic(result, input)
		const paths = new Set((input.manifest ?? input.files).map(f => f.path))
		requireValue(result.omitted_paths.every(p => paths.has(p)) && new Set(result.omitted_paths).size === result.omitted_paths.length, 'invalid omitted changed paths')
		requireValue(result.context_requests.length <= contextRequestLimit && result.context_requests.every(r => validPath(r.path) && r.path.length <= 1024), 'invalid context request')
		requireValue(result.completion !== 'complete' || (incomplete.length === 0 && result.context_requests.length === 0), 'inconsistent complete review')
		const requireEvidence = (evidence, kind, index, findingIndex = null) => {
			const expected = sourceQuote(input, evidence.path, evidence.side, evidence.start_line, evidence.end_line, evidence.start_byte, evidence.end_byte)
			if (expected !== evidence.quote) {
				validationFailure = evidenceFailure(input, evidence, expected, kind, index, findingIndex)
				throw new Error(kind === 'related_evidence' ? 'related evidence differs from supplied source' : 'evidence differs from supplied source')
			}
		}
		const binaryFiles = (input.scope?.kind === 'synthesis' ? input.manifest : input.files).filter(f => f.source_mode === 'binary-metadata')
		const binaryPaths = new Set(binaryFiles.map(f => f.path))
		requireValue(result.binary_reviews.every(r => binaryPaths.has(r.path)) && new Set(result.binary_reviews.map(r => r.path)).size === result.binary_reviews.length, 'invalid binary review paths')
		for (const review of result.binary_reviews) {
			for (const [kind, evidence] of [['source', review.source_evidence], ['derivation', review.derivation_evidence]]) {
				for (const [index, quote] of evidence.entries()) requireEvidence(quote, `binary_${kind}_evidence`, index)
			}
			if (review.status === 'source-reviewed') requireValue(review.source_evidence.length > 0 && review.derivation_evidence.length > 0, 'binary provenance requires source and derivation evidence')
		}
		if (result.completion === 'complete') requireValue(result.binary_reviews.length === binaryFiles.length && result.binary_reviews.every(r => r.status === 'source-reviewed'), 'binary provenance review incomplete')
		for (const [findingIndex, finding] of result.findings.entries()) {
			requireValue(finding.end_line >= finding.start_line && (input.scope?.kind === 'synthesis' ? input.manifest : input.files).some(f => f.path === finding.path), 'finding outside supplied source')
			if (input.scope?.kind !== 'synthesis') requireValue(input.files.some(f => f.path === finding.path && changedRanges(f).some(r => r.side === finding.side && finding.start_line >= r.start && finding.end_line <= r.end)), 'finding outside changed source')
			for (const [index, evidence] of finding.evidence.entries()) requireEvidence(evidence, 'finding_evidence', index, findingIndex)
		}
		for (const [index, evidence] of result.related_evidence.entries()) requireEvidence(evidence, 'related_evidence', index)
		if (incomplete.length) return { ...unavailable(input, `review incomplete (${incomplete.join(', ')})`),
			status: result.completion, omitted_paths: result.omitted_paths.length ? result.omitted_paths : [...paths],
			context_requests: result.context_requests, diagnostics }
		return { ...result, status: 'complete' }
	} catch (error) {
		return unavailable(input, error instanceof SyntaxError ? 'invalid JSON review result' : error.message,
			{ ...(diagnostics ? { diagnostics } : {}), ...(validationFailure ? { validation_failure: validationFailure } : {}) })
	}
}
