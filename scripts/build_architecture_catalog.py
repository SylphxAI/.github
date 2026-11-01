#!/usr/bin/env python3
"""Generate C3 repository identities from a complete dated GitHub snapshot.

Input is metadata only: version, sources (queries/page sizes/times), and
repositories (numeric id, full_name, archived). No source bodies or credentials.
The generated policy is pinned with the shared action; CI performs no census.
"""
import argparse
import json
import pathlib

ORGANIZATIONS = {'SylphxAI', 'Cubeage', 'EpiowAI', 'OzyrixLtd'}

# Architectural layers are decisions; consumer names come only from the census.
LAYERS = {
    **{name: sorted(ORGANIZATIONS)
       for name in ['SylphxAI/spiron', 'SylphxAI/cloud', 'SylphxAI/sylphx', 'SylphxAI/keel', 'SylphxAI/keel-engine']},
    **{'Cubeage/' + name: ['Cubeage']
       for name in ['cubeage-platform', 'cubeage-kit', 'tycoon-engine']},
}


def build(snapshot, products=None):
    if snapshot.get('version') != 1:
        raise ValueError('unsupported repository snapshot')
    sources = [source for source in snapshot['sources'] if source['organization'] in ORGANIZATIONS]
    repos = [row for row in snapshot['repositories'] if row['full_name'].split('/')[0] in ORGANIZATIONS]
    organizations = {s['organization'] for s in sources}
    if not ORGANIZATIONS <= organizations:
        raise ValueError('all declared organizations are required')
    if len({r['id'] for r in repos}) != len(repos):
        raise ValueError('repository IDs must be unique across the snapshot')
    if len({r['full_name'].casefold() for r in repos}) != len(repos):
        raise ValueError('repository names must be unique')
    for source in sources:
        rows = [r for r in repos if r['full_name'].split('/')[0] == source['organization']]
        if len(rows) != source['repository_count'] or sum(source['page_sizes']) != len(rows):
            raise ValueError('repository population does not match paginated source counts')
        if not source['read_at_utc'] or not source['query'] or source['page_sizes'][-1] >= 100:
            raise ValueError('a dated, complete final page is required')
    # Archived identities remain visible for history; they do not generate import
    # rules. Numeric IDs retain canonical identity across names and organizations.
    products = products or {'source': snapshot['product_source'], 'products': snapshot['products']}
    product_rows = [row for row in products['products'] if row['repository'].split('/')[0] in ORGANIZATIONS]
    if any(row['repository_id'] not in {r['id'] for r in repos} for row in product_rows):
        raise ValueError('product identity is outside the repository census')
    return {'version': 1, 'sources': sources, 'shared_layers': LAYERS,
            'product_source': products['source'], 'products': product_rows,
            'repositories': sorted(repos, key=lambda r: r['id'])}


def render(data):
    metadata = {k: v for k, v in data.items() if k != 'repositories'}
    prefix = json.dumps(metadata, indent=2, sort_keys=True).rstrip()[:-1].rstrip()
    rows = ',\n'.join('    ' + json.dumps(row, sort_keys=True) for row in data['repositories'])
    return prefix + ',\n  \"repositories\": [\n' + rows + '\n  ]\n}\n'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot', required=True)
    p.add_argument('--products', required=True, help='Dated product rows from the architecture map, resolved by repository ID')
    p.add_argument('--output', default='policy/architecture-repositories.json')
    args = p.parse_args()
    data = build(json.loads(pathlib.Path(args.snapshot).read_text()), json.loads(pathlib.Path(args.products).read_text()))
    pathlib.Path(args.output).write_text(render(data))


if __name__ == '__main__':
    main()
