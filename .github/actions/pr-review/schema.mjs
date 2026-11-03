export const contextRequestLimit = 16

const text = { type: 'string', minLength: 1 }
const line = { type: 'integer', minimum: 1 }
const object = (properties) => ({
	type: 'object', properties, required: Object.keys(properties), additionalProperties: false,
})
const evidence = object({
	path: text, side: { type: 'string', enum: ['base', 'head', 'context'] },
	start_line: line, end_line: line, quote: text,
	start_byte: { type: ['integer', 'null'], minimum: 0 },
	end_byte: { type: ['integer', 'null'], minimum: 0 },
})

export const resultSchema = object({
	schema_version: { type: 'integer', const: 1 },
	repository_id: text,
	base_sha: text,
	head_sha: text,
	input_digest: text,
	reviewer_version: text,
	policy_version: text,
	completion: { type: 'string', enum: ['complete', 'incomplete', 'unavailable'] },
	findings: { type: 'array', items: object({
		path: text, side: { type: 'string', enum: ['base', 'head'] }, start_line: line, end_line: line,
		severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
		title: text, explanation: text, trigger: text, consequence: text,
		evidence: { type: 'array', minItems: 1, items: evidence },
	}) },
	binary_reviews: { type: 'array', items: object({
		path: text, status: { type: 'string', enum: ['source-reviewed', 'unresolved'] },
		source_evidence: { type: 'array', items: evidence },
		derivation_evidence: { type: 'array', items: evidence }, limitation: text,
	}) },
	review_summary: text,
	related_evidence: { type: 'array', items: evidence },
	omitted_paths: { type: 'array', items: text },
	context_requests: { type: 'array', maxItems: contextRequestLimit, items: object({
		path: { type: 'string', minLength: 1, maxLength: 1024 },
		side: { type: 'string', enum: ['base', 'head'] },
	}) },
	context_notes: { type: 'array', items: text },
	reason: { type: 'string' },
})

// The supported schema vocabulary is deliberately small and checked recursively.
export function validateSchema(value, schema, at = 'result') {
	const fail = () => { throw new Error(`invalid ${at}`) }
	if (Array.isArray(schema.type)) {
		if (value === null && schema.type.includes('null')) return value
		return validateSchema(value, { ...schema, type: schema.type.find(t => t !== 'null') }, at)
	}
	if ('const' in schema && value !== schema.const) fail()
	if (schema.enum && !schema.enum.includes(value)) fail()
	if (schema.type === 'object') {
		if (!value || typeof value !== 'object' || Array.isArray(value)) fail()
		for (const key of schema.required) if (!Object.hasOwn(value, key)) fail()
		for (const key of Object.keys(value)) {
			if (!Object.hasOwn(schema.properties, key)) fail()
			validateSchema(value[key], schema.properties[key], `${at}.${key}`)
		}
	}
	if (schema.type === 'array') {
		if (!Array.isArray(value) || value.length < (schema.minItems ?? 0) || value.length > (schema.maxItems ?? Infinity)) fail()
		for (const item of value) validateSchema(item, schema.items, `${at}[]`)
	}
	if (schema.type === 'string' && (typeof value !== 'string' || value.trim().length < (schema.minLength ?? 0) || value.length > (schema.maxLength ?? Infinity))) fail()
	if (schema.type === 'integer' && (!Number.isSafeInteger(value) || value < schema.minimum)) fail()
	return value
}
