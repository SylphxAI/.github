import { reviewerInstructions, resultSchema, parseReviewResult, unavailable } from './contract.mjs';
import { reviewPartitions } from './partition.mjs';

export function reviewRequest(input, { model, maxOutputTokens, reasoningEffort }) {
  return JSON.stringify({ model, stream: false, store: false,
    messages: [{ role: 'system', content: reviewerInstructions }, { role: 'user', content: JSON.stringify(input) }],
    response_format: { type: 'json_schema', json_schema: { name: 'pr_review', strict: true, schema: resultSchema } },
    ...(maxOutputTokens === undefined ? {} : { max_completion_tokens: maxOutputTokens }),
    ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
  });
}

export function contextBudget(metadata, model, maxOutputTokens) {
  if (metadata?.id !== model) throw new Error('Review model metadata does not match the configured model.');
  const context = metadata.context_window, maximum = metadata.max_output_tokens ?? metadata.limits?.max_output_tokens;
  const output = maxOutputTokens ?? maximum;
  if (![context, maximum, output].every(n => Number.isSafeInteger(n) && n > 0) || output > maximum) {
    throw new Error('Review model metadata has no usable context and output limits.');
  }
  // Byte-based text tokenization cannot emit more tokens than UTF-8 bytes.
  // Count the entire escaped wire body (including the schema) as a conservative
  // upper bound, not a usage estimate. Reserve output unchanged, plus 4096 for
  // message/schema framing that the serving tokenizer adds outside text.
  const bytes = context - output - 4096;
  if (bytes <= 0) throw new Error('Configured review output leaves no input context.');
  return { context_window: context, reserved_output: output, framing_reserve: 4096, request_byte_limit: bytes };
}

// The environment's ordinary AI policy chooses eligible paid-account capacity.
// This client never selects an area, changes model, retries, or falls back.
export function gatewayReviewer({ token, model, apiOrigin = 'https://api.sylphx.com',
  maxOutputTokens, reasoningEffort, deadlineMs, fetcher = fetch, current }) {
  const origin = new URL(apiOrigin);
  if (origin.protocol !== 'https:' || origin.username || origin.password || origin.search || origin.hash || origin.pathname !== '/') {
    throw new Error('Review gateway must be an ordinary HTTPS API origin.');
  }
  if (!token || typeof model !== 'string' || !model.trim() || ['auto', 'sylphx/auto'].includes(model)) {
    throw new Error('An approved review model and inference credential are required.');
  }
  if (maxOutputTokens !== undefined && (!Number.isSafeInteger(maxOutputTokens) || maxOutputTokens < 1)) {
    throw new Error('Invalid review output budget.');
  }
  const configuration = { model, maxOutputTokens, reasoningEffort };
  return async input => {
    let budget;
    try {
      const remaining = deadlineMs - Date.now();
      if (!(remaining > 0)) return unavailable(input, 'Review deadline expired before model metadata collection.');
      const metadataUrl = new URL(`/v1/public/models/${model.split('/').map(encodeURIComponent).join('/')}`, origin);
      const response = await fetcher(metadataUrl, {
        method: 'GET', redirect: 'error', signal: AbortSignal.timeout(Math.min(30_000, remaining)),
        headers: { accept: 'application/json' },
      });
      if (!response.ok) return unavailable(input, `Review model metadata returned HTTP ${response.status}.`);
      budget = contextBudget(await response.json(), model, maxOutputTokens);
      budget.metadata_url = String(metadataUrl); budget.metadata_read_at = new Date().toISOString();
    } catch { return unavailable(input, 'Review model context metadata is unavailable or invalid.'); }
    const requestBytes = value => Buffer.byteLength(reviewRequest(value, configuration));
    const sizes = [];
    const execute = async value => {
      const body = reviewRequest(value, configuration), bytes = Buffer.byteLength(body);
      if (bytes > budget.request_byte_limit) return unavailable(value, 'Review request exceeds the qualified model context.');
      sizes.push(bytes);
      return infer(value, body);
    };
    const result = await reviewPartitions(input, { execute, requestBytes, byteBudget: budget.request_byte_limit, current });
    return { ...result, budget, request_bytes: sizes };
  };

  async function infer(input, requestBody) {
    const remaining = deadlineMs - Date.now();
    if (!(remaining > 0)) return unavailable(input, 'Review deadline expired before inference.');
    let response;
    try {
      response = await fetcher(new URL('/v1/chat/completions', origin), {
        method: 'POST', redirect: 'error', signal: AbortSignal.timeout(remaining),
        headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        body: requestBody,
      });
    } catch {
      return unavailable(input, Date.now() >= deadlineMs ? 'Review inference exceeded its deadline.' : 'Review inference failed at the network.');
    }
    let body;
    try { body = await response.json(); } catch { return unavailable(input, `Review gateway returned invalid JSON (HTTP ${response.status}).`); }
    if (!response.ok) {
      // Only a bounded machine code is safe to publish. Provider prose may echo
      // source or credentials and therefore never crosses this boundary.
      const code = body.error?.code;
      const suffix = typeof code === 'string' && /^[a-z0-9_]{1,80}$/.test(code) ? ` (${code})` : '';
      return unavailable(input, `Review gateway refused HTTP ${response.status}${suffix}.`);
    }
    const choice = body.choices?.[0];
    if (body.object !== 'chat.completion' || body.model !== model || body.choices?.length !== 1 ||
        choice?.finish_reason !== 'stop' || choice.message?.role !== 'assistant' ||
        choice.message.refusal || choice.message.tool_calls?.length || typeof choice.message.content !== 'string') {
      return unavailable(input, 'Review inference was refused, interrupted, or did not return the configured model result.');
    }
    return parseReviewResult(choice.message.content, input);
  }
}
