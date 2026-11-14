import { digest } from './identity.mjs';

export const adjudicationVersion = 'candidate-adjudication-v1';
export const adjudicationInstruction = 'Adjudicate each candidate exactly once. Retain only a genuine blocker grounded in the supplied pinned source. Discard false claims. Use insufficient-context when required causal source cannot establish either verdict. A quote alone is not semantic proof. Return only the strict schema. Cite exact supplied lines and bytes. Do not infer absent or binary content.';
const identityKeys = ['repository_id', 'base_sha', 'head_sha', 'input_digest', 'reviewer_version', 'policy_version'];
export const candidateId = (finding, inputDigest, index = 0) => digest({ finding, input_digest: inputDigest, candidate_index: index });
export const adjudicationSchema = {
  type: 'object', properties: {
    schema_version: { type: 'integer', const: 1 }, ...Object.fromEntries(identityKeys.map(k => [k, { type: 'string', minLength: 1 }])),
    completion: { type: 'string', enum: ['complete', 'incomplete'] },
    decisions: { type: 'array', items: { type: 'object', properties: {
      candidate_id: { type: 'string', minLength: 1 }, verdict: { type: 'string', enum: ['retain', 'discard', 'insufficient-context'] },
      contradiction: { type: 'string' }, proof_evidence: { type: 'array', items: { type: 'object', properties: {
        path: { type: 'string', minLength: 1 }, side: { type: 'string', enum: ['base', 'head', 'context'] },
        start_line: { type: 'integer', minimum: 1 }, end_line: { type: 'integer', minimum: 1 }, quote: { type: 'string', minLength: 1 },
        start_byte: { type: ['integer', 'null'], minimum: 0 }, end_byte: { type: ['integer', 'null'], minimum: 0 },
      }, required: ['path','side','start_line','end_line','quote','start_byte','end_byte'], additionalProperties: false } },
    }, required: ['candidate_id','verdict','contradiction','proof_evidence'], additionalProperties: false } },
    reason: { type: 'string' },
  }, required: ['schema_version', ...identityKeys, 'completion', 'decisions', 'reason'], additionalProperties: false,
};
export function buildCandidates(result, input) {
  return (result.findings ?? []).map((finding, index) => ({ candidate_id: candidateId(finding, input.input_digest, index), index, finding }));
}
export function adjudicationRequest(input, candidates, source) {
  return { schema_version: 1, ...Object.fromEntries(identityKeys.map(k => [k, input[k]])),
    candidates: candidates.map(({ candidate_id, finding }) => ({ candidate_id, finding })),
    source_files: source.files, source_context: source.context ?? [],
    instruction: adjudicationInstruction };
}
const byteLength = value => Buffer.byteLength(value, 'utf8');
function sourceLineProof(source, e) {
  if (!source || typeof source.content !== 'string' || e.path !== source.path || e.side !== source.side ||
      !Number.isInteger(e.start_line) || !Number.isInteger(e.end_line) || e.start_line < 1 || e.end_line < e.start_line ||
      e.end_line > source.content.split('\n').length || typeof e.quote !== 'string' || !e.quote.trim()) return false;
  const lines = source.content.split('\n');
  const quote = lines.slice(e.start_line - 1, e.end_line).join('\n');
  if (quote !== e.quote) return false;
  const offsets = []; let at = 0;
  for (const line of lines) { offsets.push(at); at += byteLength(line) + 1; }
  const start = offsets[e.start_line - 1];
  const end = start + byteLength(quote);
  const bytes = byteLength(source.content);
  if ((e.start_byte === null) !== (e.end_byte === null)) return false;
  if (e.start_byte === null) return true;
  return Number.isInteger(e.start_byte) && Number.isInteger(e.end_byte) && e.start_byte === start && e.end_byte === end &&
    e.start_byte >= 0 && e.end_byte >= e.start_byte && e.end_byte <= bytes;
}
const proofSource = (source, e) => (source.files ?? []).concat(source.context ?? []).find(x => x.path === e.path && x.side === e.side);
function strictShape(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || value.schema_version !== 1 ||
      !['complete', 'incomplete'].includes(value.completion) || typeof value.reason !== 'string' || !Array.isArray(value.decisions)) return false;
  const allowed = new Set(['schema_version', ...identityKeys, 'completion', 'decisions', 'reason']);
  if (Object.keys(value).some(k => !allowed.has(k)) || identityKeys.some(k => typeof value[k] !== 'string' || !value[k])) return false;
  for (const d of value.decisions) {
    if (!d || typeof d !== 'object' || Array.isArray(d) || Object.keys(d).some(k => !['candidate_id','verdict','contradiction','proof_evidence'].includes(k)) ||
        typeof d.candidate_id !== 'string' || !d.candidate_id || !['retain','discard','insufficient-context'].includes(d.verdict) ||
        typeof d.contradiction !== 'string' || !Array.isArray(d.proof_evidence)) return false;
    for (const e of d.proof_evidence) {
      if (!e || typeof e !== 'object' || Array.isArray(e) || Object.keys(e).some(k => !['path','side','start_line','end_line','quote','start_byte','end_byte'].includes(k)) ||
          typeof e.path !== 'string' || !e.path || !['base','head','context'].includes(e.side) || !Number.isInteger(e.start_line) || !Number.isInteger(e.end_line) ||
          e.start_line < 1 || e.end_line < e.start_line || typeof e.quote !== 'string' || !e.quote.trim() ||
          !((e.start_byte === null && e.end_byte === null) || (Number.isInteger(e.start_byte) && Number.isInteger(e.end_byte) && e.start_byte >= 0 && e.end_byte >= e.start_byte))) return false;
    }
  }
  return true;
}
export function validateAdjudication(value, input, candidates, source) {
  if (!strictShape(value) || !identityKeys.every(k => value[k] === input[k])) return { ok: false, reason: 'Adjudication schema or identity does not match pinned input.' };
  if (value.completion !== 'complete' || !value.reason.trim()) return { ok: false, reason: 'Adjudication did not complete with a reason.' };
  if (!Array.isArray(value.decisions) || value.decisions.length !== candidates.length) return { ok: false, reason: 'Adjudication does not cover each candidate exactly once.' };
  const ids = new Set(candidates.map(c => c.candidate_id)), seen = new Set();
  if (ids.size !== candidates.length) return { ok: false, reason: 'Candidate IDs are not unique.' };
  for (const d of value.decisions) {
    if (seen.has(d.candidate_id) || !ids.has(d.candidate_id)) return { ok: false, reason: 'Adjudication contains duplicate or unknown candidate IDs.' };
    seen.add(d.candidate_id);
    if (d.verdict === 'retain' && d.proof_evidence.length === 0) return { ok: false, reason: 'Retained candidate has no proof evidence.' };
    for (const e of d.proof_evidence) if (!sourceLineProof(proofSource(source, e), e)) return { ok: false, reason: 'Adjudication proof evidence is not exact supplied source.' };
  }
  return { ok: seen.size === ids.size, reason: seen.size === ids.size ? '' : 'Adjudication omitted a candidate.' };
}
export function applyAdjudication(result, value, candidates) {
  const byId = new Map(candidates.map(c => [c.candidate_id, c.finding]));
  const incomplete = value.completion !== 'complete' || value.decisions.some(d => d.verdict === 'insufficient-context');
  const retained = incomplete ? [] : value.decisions.filter(d => d.verdict === 'retain').map(d => byId.get(d.candidate_id));
  const discarded = value.decisions.filter(d => d.verdict !== 'retain').map(d => ({ candidate_id: d.candidate_id, verdict: d.verdict, reason: d.contradiction }));
  return { ...result, status: incomplete ? 'incomplete' : result.status, findings: retained,
    discarded_items: [...(result.discarded_items ?? []), ...discarded],
    causal_validation: { version: adjudicationVersion, status: incomplete ? 'incomplete' : 'complete', candidates: candidates.length, decisions: value.decisions.length, reason: value.reason },
    ...(incomplete ? { reason: 'Final candidate adjudication reported insufficient context.' } : {}) };
}
