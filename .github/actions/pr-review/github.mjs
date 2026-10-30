import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export const CHECK_NAME = 'Sylphx PR Review';
export const APP_ID = 1299545;
export const digest = value => createHash('sha256').update(value).digest('hex');
export const validSha = value => typeof value === 'string' && /^[a-f0-9]{40}$/.test(value);
export const validPath = value => typeof value === 'string' && value.length > 0 &&
  !value.startsWith('/') && !value.split('/').some(p => !p || p === '.' || p === '..') &&
  !/[\0\r\n\\]/.test(value);

export class Unavailable extends Error {}

export class GitHub {
  constructor({ repository, readToken, checkToken, apiOrigin = 'https://api.github.com', fetcher = fetch }) {
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) throw new Unavailable('Invalid repository.');
    this.repository = repository;
    this.readToken = readToken;
    this.checkToken = checkToken;
    this.apiOrigin = apiOrigin.replace(/\/$/, '');
    this.fetcher = fetcher;
  }

  async request(path, { method = 'GET', body, checks = false } = {}) {
    const token = checks ? this.checkToken : this.readToken;
    if (!token) throw new Unavailable(`Missing ${checks ? 'checks' : 'source'} credential.`);
    const response = await this.fetcher(`${this.apiOrigin}/repos/${this.repository}${path}`, {
      method, redirect: 'error', signal: AbortSignal.timeout(30_000),
      headers: { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json',
        'x-github-api-version': '2022-11-28', 'content-type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      // Never log a response body: a provider can echo a credential or source.
      throw new Unavailable(`GitHub ${method} ${path.split('?')[0]} returned HTTP ${response.status}.`);
    }
    return response.json();
  }

  async pages(path, field) {
    const rows = [];
    for (let page = 1; ; page++) {
      const body = await this.request(`${path}${path.includes('?') ? '&' : '?'}per_page=100&page=${page}`,
        { checks: field === 'check_runs' });
      const items = field ? body[field] : body;
      if (!Array.isArray(items)) throw new Unavailable('Malformed GitHub list response.');
      rows.push(...items);
      if (items.length < 100) return rows;
    }
  }

  async source(path, sha, optional = false) {
    if (!validSha(sha) || !validPath(path)) throw new Unavailable('Invalid pinned source reference.');
    let object;
    try {
      object = await this.request(`/contents/${path.split('/').map(encodeURIComponent).join('/')}?ref=${sha}`);
    } catch (error) {
      if (optional && error.message.endsWith('HTTP 404.')) return null;
      throw error;
    }
    if (object.type !== 'file' || !validSha(object.sha) || object.submodule_git_url || object.target) {
      throw new Unavailable(`Source ${path} is not an ordinary file.`);
    }
    const blobSha = object.sha;
    if (object.encoding !== 'base64') object = await this.request(`/git/blobs/${blobSha}`);
    if (object.encoding !== 'base64' || typeof object.content !== 'string') {
      throw new Unavailable(`Source ${path} has no complete content.`);
    }
    const bytes = Buffer.from(object.content, 'base64');
    const actualSha = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
    if (bytes.length !== object.size || actualSha !== blobSha) throw new Unavailable(`Source ${path} is incomplete.`);
    try {
      const content = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
      if (content.includes('\0')) throw new Error('binary');
      return content;
    } catch { throw new Unavailable(`Source ${path} is not UTF-8 text.`); }
  }

  async current(target) {
    if (target.kind === 'pull_request') {
      const pr = await this.request(`/pulls/${target.number}`);
      return pr.state === 'open' && pr.head.sha === target.head_sha && (!target.base_ref || pr.base.ref === target.base_ref);
    }
    const ref = await this.request(`/git/ref/heads/${target.ref.split('/').map(encodeURIComponent).join('/')}`);
    return ref.object?.sha === target.head_sha;
  }

  async checkRuns(sha) {
    return this.pages(`/commits/${sha}/check-runs?check_name=${encodeURIComponent(CHECK_NAME)}&filter=all`, 'check_runs');
  }

  async begin(target, externalId, detailsUrl) {
    const runs = await this.checkRuns(target.head_sha);
    const existing = runs.filter(r => r.app?.id === APP_ID && r.name === CHECK_NAME &&
      r.head_sha === target.head_sha && r.external_id === externalId).sort((a, b) => b.id - a.id)[0];
    if (existing) return existing;
    // POST has no retry. After an ambiguous transport result the next delivery
    // finds this stable external_id before attempting another creation.
    const created = await this.request('/check-runs', { method: 'POST', checks: true, body: {
      name: CHECK_NAME, head_sha: target.head_sha, external_id: externalId,
      details_url: detailsUrl, status: 'in_progress', started_at: new Date().toISOString(),
      output: { title: 'Review running', summary: `Reviewing exact head \`${target.head_sha}\`.` },
    } });
    return this.request(`/check-runs/${created.id}`, { checks: true });
  }

  async finish(run, conclusion, output) {
    await this.request(`/check-runs/${run.id}`, { method: 'PATCH', checks: true, body: {
      status: 'completed', conclusion, completed_at: new Date().toISOString(), output,
    } });
    const readback = await this.request(`/check-runs/${run.id}`, { checks: true });
    if (readback.app?.id !== APP_ID || readback.head_sha !== run.head_sha ||
        readback.conclusion !== conclusion || readback.status !== 'completed') {
      throw new Unavailable('Check Run readback did not match the published result.');
    }
    return readback;
  }
}

// Diff only two data files. External diff/text converters and caller Git
// configuration are disabled; repository programs are never checked out/run.
export function sourcePatch(before, after) {
  const dir = mkdtempSync(join(tmpdir(), 'pr-review-'));
  try {
    const a = join(dir, 'base'), b = join(dir, 'head');
    writeFileSync(a, before ?? '', { mode: 0o600 });
    writeFileSync(b, after ?? '', { mode: 0o600 });
    try {
      return execFileSync('git', ['diff', '--no-index', '--no-ext-diff', '--no-textconv', '--unified=3', '--', a, b], {
        encoding: 'utf8', maxBuffer: 100 * 1024 * 1024,
        env: { PATH: process.env.PATH, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null', LC_ALL: 'C' },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      if (error.status === 1 && typeof error.stdout === 'string') {
        return error.stdout.replaceAll(a, 'base').replaceAll(b, 'head');
      }
      throw new Unavailable('Could not compute the complete pinned source diff.');
    }
  } finally { rmSync(dir, { recursive: true }); }
}

export function hunkRanges(patch) {
  return [...patch.matchAll(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/gm)].map(m => ({
    base: { start: +m[1], count: m[2] === undefined ? 1 : +m[2] },
    head: { start: +m[3], count: m[4] === undefined ? 1 : +m[4] },
  }));
}

export function sourceWindows(content, ranges, side, radius = 40) {
  if (content === null) return [];
  const lines = content === '' ? [] : content.replace(/\n$/, '').split('\n');
  const merged = [];
  for (const range of ranges.map(h => h[side]).filter(r => r.count > 0)) {
    const start = Math.max(1, range.start - radius);
    const end = Math.min(lines.length, range.start + range.count - 1 + radius);
    const previous = merged.at(-1);
    if (previous && start <= previous.end + 1) previous.end = Math.max(previous.end, end);
    else merged.push({ start, end });
  }
  return merged.map(r => ({ start_line: r.start, content: lines.slice(r.start - 1, r.end).join('\n') }));
}

export function bundleFile(file, before, after, fullFileBytes = 80_000) {
  const patch = sourcePatch(before, after);
  const ranges = hunkRanges(patch);
  if ((before ?? '') !== (after ?? '') && !ranges.length) throw new Unavailable(`No text diff for ${file.filename}.`);
  const result = { path: file.filename, previous_path: file.previous_filename ?? null,
    status: file.status, before, after, patch, source_mode: 'full' };
  if (Buffer.byteLength(before ?? '') + Buffer.byteLength(after ?? '') > fullFileBytes) {
    Object.assign(result, { source_mode: 'windows', before: null, after: null,
      before_windows: sourceWindows(before, ranges, 'base'), after_windows: sourceWindows(after, ranges, 'head'),
      before_sha256: before === null ? null : digest(before), after_sha256: after === null ? null : digest(after),
      before_line_count: before === null || before === '' ? 0 : before.replace(/\n$/, '').split('\n').length,
      after_line_count: after === null || after === '' ? 0 : after.replace(/\n$/, '').split('\n').length });
  }
  return result;
}
