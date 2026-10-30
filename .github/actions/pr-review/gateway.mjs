import { reviewerInstructions, resultSchema, parseReviewResult, unavailable } from './contract.mjs';

// The environment's ordinary AI policy chooses eligible paid-account capacity.
// This client never selects an area, changes model, retries, or falls back.
export function gatewayReviewer({ token, model, apiOrigin = 'https://api.sylphx.com',
  maxOutputTokens, reasoningEffort, deadlineMs, fetcher = fetch }) {
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
  return async input => {
    const remaining = deadlineMs - Date.now();
    if (!(remaining > 0)) return unavailable(input, 'Review deadline expired before inference.');
    let response;
    try {
      response = await fetcher(new URL('/v1/chat/completions', origin), {
        method: 'POST', redirect: 'error', signal: AbortSignal.timeout(remaining),
        headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        body: JSON.stringify({ model, stream: false, store: false,
          messages: [{ role: 'system', content: reviewerInstructions }, { role: 'user', content: JSON.stringify(input) }],
          response_format: { type: 'json_schema', json_schema: { name: 'pr_review', strict: true, schema: resultSchema } },
          ...(maxOutputTokens === undefined ? {} : { max_completion_tokens: maxOutputTokens }),
          ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
        }),
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
  };
}
