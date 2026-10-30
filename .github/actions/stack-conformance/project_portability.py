"""Recognize literal private destinations and explicit in-cluster runtime calls.

This is a source regression check, not a deployment or authorization census.
Offsets refer to the original source; findings never expose source values.
"""
from __future__ import annotations

import pathlib
import re

SOURCE = {'.rs', '.py', '.go', '.ts', '.tsx', '.jsx', '.js', '.mjs', '.cjs',
          '.sh', '.bash', '.java', '.lua', '.cs', '.kt', '.kts', '.swift', '.dart',
          '.c', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.m', '.mm', '.php', '.rb'}
CONFIG = {'.yaml', '.yml', '.toml', '.json', '.xml'}
MANIFEST_DIRS = {'deploy', 'deployment', 'deployments', 'infra', 'infrastructure',
                 'k8s', 'kubernetes', 'helm', 'charts', 'gitops', '.github'}
SUFFIX = re.compile(r'\.(?:svc(?:\.cluster\.local)?|cluster\.local|sylphx\.internal)(?![\w.-])', re.I)
PRIVATE_URL = re.compile(r'https?://([^/?#\s\"\x27`]+)', re.I)
URL = re.compile(r'\b[a-z][a-z0-9+.-]*://([^/?#\s\"\x27`]+)[^\s\"\x27`]*', re.I)
LITERAL = re.compile(r'\br(\#*)"[\s\S]*?"\1|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|`(?:\\.|[^`\\])*`')
IN_CLUSTER = re.compile(r'\b(?:load_incluster_config|loadInClusterConfig|InClusterConfig|'
                        r'inClusterConfig|loadFromCluster)\s*\(|'
                        r'\b(?:Config|config)::incluster(?:_env|_dns)?\s*\(')
DIAGNOSTIC = re.compile(r'\b(?:error|warn|info|debug|trace|log|print|println|eprintln|'
                        r'anyhow|bail|expect|ok_or|Err)\s*!?\s*\([^()]*$', re.I)
MOUNT = re.compile(r'/var/run/secrets/kubernetes\.io/serviceaccount(?:/[^\s\"\x27`]*)?')


def manifest(path):
    """Only manifest formats are allowed by deployment directory names."""
    p = pathlib.PurePosixPath(path)
    return (p.suffix in CONFIG and
            (bool({part.casefold() for part in p.parts[:-1]} & MANIFEST_DIRS)
             or p.name == 'sylphx.toml' or p.name.endswith('.deployment.profile.toml')))


def supported(path):
    return pathlib.PurePosixPath(path).suffix in SOURCE | CONFIG


def short_service_url(value):
    for match in PRIVATE_URL.finditer(value):
        # Authentication userinfo is not the destination hostname. This also
        # handles Git's configured credential URL without exposing its value.
        host = match.group(1).rsplit('@', 1)[-1].split(':', 1)[0]
        if re.fullmatch(r'[a-z][a-z0-9_-]*', host, re.I) and host.lower() != 'localhost':
            return True
    return False


def destination_host_span(value, start, end):
    """URL paths, queries and userinfo do not name the destination host."""
    for url in URL.finditer(value):
        if url.start() <= start < url.end():
            authority = url.group(1)
            host_start = url.start(1) + authority.rfind('@') + 1
            host = value[host_start:url.end(1)].split(':', 1)[0]
            return host_start <= start and end <= host_start + len(host)
    return True


def views(text, suffix, source_views):
    # Shell/Ruby/PHP/Lua comments differ from C-family // comments. Preserve
    # quoted and unquoted shell URLs while masking actual comments in place.
    if suffix in {'.sh', '.bash', '.rb', '.php', '.lua', '.yaml', '.yml', '.toml'}:
        comment = r'--\[\[[\s\S]*?\]\]|--[^\n]*' if suffix == '.lua' else r'\#[^\n]*'
        if suffix == '.php':
            comment += r'|//[^\n]*|/\*[\s\S]*?\*/'
        tokens = re.compile(LITERAL.pattern + '|' + comment)
        clean, code = list(text), list(text)
        for match in tokens.finditer(text):
            blank = ''.join('\n' if c == '\n' else ' ' for c in match.group())
            code[match.start():match.end()] = blank
            if not LITERAL.fullmatch(match.group()):
                clean[match.start():match.end()] = blank
        return ''.join(clean), ''.join(code)
    clean, code, _ = source_views(text, suffix)
    return clean, code


def scan(path, text, source_views):
    if not supported(path) or manifest(path):
        return []
    lower = text.lower()
    if not any(token in lower for token in ('.svc', '.cluster.local', '.sylphx.internal',
                                             '://', 'incluster', 'loadfromcluster',
                                             'kubernetes_service_', 'kubernetes.io/serviceaccount')):
        return []
    suffix = pathlib.PurePosixPath(path).suffix
    clean, code = views(text, suffix, source_views)
    out = []
    for literal in LITERAL.finditer(clean):
        prefix = clean[max(0, literal.start() - 120):literal.start()]
        if DIAGNOSTIC.search(prefix):
            continue
        value = literal.group()
        for match in SUFFIX.finditer(value):
            if not destination_host_span(value, match.start(), match.end()):
                continue
            before = value[:match.start()]
            # A suffix vocabulary or ends_with(".svc") refusal is not a host.
            # A concatenated suffix or template host still constructs a target.
            if not re.search(r'[\w}$.-]+$', before) and not re.search(r'\+\s*$', prefix):
                continue
            out.append((literal.start(), literal.end(), 'Runtime source embeds cluster-private DNS; configure a reachable public API origin'))
            break
        else:
            if short_service_url(value) or any(destination_host_span(value, m.start(), m.end())
                    for m in re.finditer(r'\bkubernetes\.default\b', value)):
                out.append((literal.start(), literal.end(), 'Runtime source embeds a single-label service or Kubernetes API hostname'))
            elif MOUNT.search(value):
                out.append((literal.start(), literal.end(), 'Runtime source assumes the Kubernetes service-account secret mount'))
            elif re.search(r'KUBERNETES_SERVICE_(?:HOST|PORT)', value) and re.search(r'\b(?:var|getenv|env|environ|get_env|GetEnvironmentVariable)\b', prefix, re.I):
                out.append((literal.start(), literal.end(), 'Runtime source reads an in-cluster Kubernetes endpoint variable'))
    for match in IN_CLUSTER.finditer(code):
        out.append((match.start(), match.end(), 'Runtime source explicitly constructs an in-cluster Kubernetes client'))
    for match in re.finditer(r'\bprocess\.env\.KUBERNETES_SERVICE_(?:HOST|PORT)\b', code):
        out.append((match.start(), match.end(), 'Runtime source reads an in-cluster Kubernetes endpoint variable'))
    if suffix in {'.sh', '.bash'}:
        # Shell destinations need not be quoted. Quoted ones are already masked.
        for match in re.finditer(r'https?://[^\s;|&]+', code):
            value = match.group()
            if any(destination_host_span(value, m.start(), m.end()) for m in SUFFIX.finditer(value)) or short_service_url(value):
                out.append((match.start(), match.end(), 'Runtime shell command embeds a cluster-private service destination'))
    return sorted(set(out))
