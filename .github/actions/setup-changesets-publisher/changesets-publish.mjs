#!/usr/bin/env node
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const DEPENDENCY_SECTIONS = [
  'dependencies',
  'devDependencies',
  'peerDependencies',
  'optionalDependencies',
  'bundleDependencies',
  'bundledDependencies',
];

const args = new Set(process.argv.slice(2));
const isDryRun = args.has('--dry-run');
const isSelfCheck = args.has('--self-check');

function log(message) {
  console.log(`[sylphx-publish] ${message}`);
}

function fail(message) {
  throw new Error(message);
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function fileExists(filePath) {
  return fs.existsSync(filePath);
}

function sanitizeForFileName(value) {
  return value.replace(/^@/, '').replace(/[\/]/g, '-').replace(/[^a-zA-Z0-9._-]/g, '-');
}

function run(command, commandArgs, options = {}) {
  const printable = [command, ...commandArgs].join(' ');
  log(`$ ${printable}`);
  const result = spawnSync(command, commandArgs, {
    cwd: options.cwd,
    env: { ...process.env, ...options.env },
    encoding: 'utf8',
    stdio: options.capture ? ['ignore', 'pipe', 'pipe'] : 'inherit',
  });

  if (result.error) {
    fail(`Failed to start ${printable}: ${result.error.message}`);
  }

  if (result.status !== 0 && !options.allowFailure) {
    const output = [result.stdout, result.stderr].filter(Boolean).join('\n');
    fail(`Command failed (${result.status}): ${printable}${output ? `\n${output}` : ''}`);
  }

  return result;
}

function detectPackageManager(rootDir, rootPackageJson) {
  const packageManager = rootPackageJson.packageManager;
  if (typeof packageManager === 'string') {
    if (packageManager.startsWith('bun@')) return { type: 'bun', packageManager };
    if (packageManager.startsWith('pnpm@')) return { type: 'pnpm', packageManager };
    if (packageManager.startsWith('yarn@')) return { type: 'yarn', packageManager };
    if (packageManager.startsWith('npm@')) return { type: 'npm', packageManager };
  }

  if (fileExists(path.join(rootDir, 'bun.lock')) || fileExists(path.join(rootDir, 'bun.lockb'))) {
    return { type: 'bun', packageManager };
  }
  if (fileExists(path.join(rootDir, 'pnpm-lock.yaml'))) return { type: 'pnpm', packageManager };
  if (fileExists(path.join(rootDir, 'yarn.lock'))) return { type: 'yarn', packageManager };
  if (fileExists(path.join(rootDir, 'package-lock.json'))) return { type: 'npm', packageManager };

  return { type: 'npm', packageManager };
}

function workspacePatterns(rootPackageJson) {
  const workspaces = rootPackageJson.workspaces;
  if (Array.isArray(workspaces)) return workspaces;
  if (workspaces && Array.isArray(workspaces.packages)) return workspaces.packages;
  return [];
}

function walkDirectories(startDir, visitor) {
  if (!fileExists(startDir)) return;
  const entries = fs.readdirSync(startDir, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    if (entry.name === 'node_modules' || entry.name === '.git') continue;
    const fullPath = path.join(startDir, entry.name);
    visitor(fullPath);
    walkDirectories(fullPath, visitor);
  }
}

function expandWorkspacePattern(rootDir, pattern) {
  const normalized = pattern.replace(/^\.\//, '').replace(/\/package\.json$/, '');
  const packageDirs = [];

  if (normalized.includes('**')) {
    const base = normalized.split('**')[0].replace(/[/*]+$/, '');
    const baseDir = path.join(rootDir, base);
    walkDirectories(baseDir, (dir) => {
      if (fileExists(path.join(dir, 'package.json'))) packageDirs.push(dir);
    });
    return packageDirs;
  }

  if (normalized.endsWith('/*')) {
    const baseDir = path.join(rootDir, normalized.slice(0, -2));
    if (!fileExists(baseDir)) return packageDirs;
    for (const entry of fs.readdirSync(baseDir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const dir = path.join(baseDir, entry.name);
      if (fileExists(path.join(dir, 'package.json'))) packageDirs.push(dir);
    }
    return packageDirs;
  }

  const explicitDir = path.join(rootDir, normalized);
  if (fileExists(path.join(explicitDir, 'package.json'))) packageDirs.push(explicitDir);
  return packageDirs;
}

function isPublishable(packageJson) {
  if (!packageJson.name || !packageJson.version) return false;
  if (packageJson.private === true) return false;
  if (packageJson.publishConfig?.private === true) return false;
  return true;
}

function collectWorkspacePackageDirs(rootDir, rootPackageJson) {
  const patterns = workspacePatterns(rootPackageJson);
  const packageDirs = new Set();

  packageDirs.add(path.resolve(rootDir));

  for (const pattern of patterns) {
    for (const dir of expandWorkspacePattern(rootDir, pattern)) {
      packageDirs.add(path.resolve(dir));
    }
  }

  return packageDirs;
}

function discoverPackages(rootDir) {
  const rootPackageJson = readJson(path.join(rootDir, 'package.json'));
  const packageDirs = collectWorkspacePackageDirs(rootDir, rootPackageJson);
  const packages = [];
  for (const dir of packageDirs) {
    const packageJson = readJson(path.join(dir, 'package.json'));
    if (!isPublishable(packageJson)) continue;
    packages.push({
      dir,
      relativeDir: path.relative(rootDir, dir) || '.',
      name: packageJson.name,
      version: packageJson.version,
      packageJson,
    });
  }

  return topologicalSort(packages);
}

function localPackageVersionMap(rootDir) {
  const rootPackageJson = readJson(path.join(rootDir, 'package.json'));
  const versions = new Map();

  for (const dir of collectWorkspacePackageDirs(rootDir, rootPackageJson)) {
    const packageJsonPath = path.join(dir, 'package.json');
    if (!fileExists(packageJsonPath)) continue;
    const packageJson = readJson(packageJsonPath);
    if (!packageJson.name || !packageJson.version) continue;
    if (versions.has(packageJson.name)) {
      fail(`Duplicate workspace package name ${packageJson.name}; cannot materialize workspace: ranges safely.`);
    }
    versions.set(packageJson.name, packageJson.version);
  }

  return versions;
}

function localDependencyNames(pkg, localNames) {
  const deps = new Set();
  for (const section of DEPENDENCY_SECTIONS) {
    const values = pkg.packageJson[section];
    if (!values || typeof values !== 'object' || Array.isArray(values)) continue;
    for (const [name, spec] of Object.entries(values)) {
      if (localNames.has(name)) deps.add(name);
      if (typeof spec === 'string' && spec.startsWith('workspace:')) {
        const alias = parseWorkspaceAlias(spec.slice('workspace:'.length));
        if (alias && localNames.has(alias.packageName)) deps.add(alias.packageName);
      }
    }
  }
  return deps;
}

function topologicalSort(packages) {
  const byName = new Map(packages.map((pkg) => [pkg.name, pkg]));
  const localNames = new Set(byName.keys());
  const permanent = new Set();
  const temporary = new Set();
  const sorted = [];

  function visit(pkg) {
    if (permanent.has(pkg.name)) return;
    if (temporary.has(pkg.name)) {
      fail(`Workspace dependency cycle includes ${pkg.name}; cannot produce deterministic publish order.`);
    }
    temporary.add(pkg.name);
    for (const dependencyName of localDependencyNames(pkg, localNames)) {
      visit(byName.get(dependencyName));
    }
    temporary.delete(pkg.name);
    permanent.add(pkg.name);
    sorted.push(pkg);
  }

  for (const pkg of [...packages].sort((a, b) => a.name.localeCompare(b.name))) visit(pkg);
  return sorted;
}

function registryArgs() {
  const registry = process.env.NPM_CONFIG_REGISTRY || process.env.npm_config_registry;
  return registry ? ['--registry', registry] : [];
}

function packageVersionExists(name, version) {
  const result = run('npm', ['view', `${name}@${version}`, 'version', '--json', ...registryArgs()], {
    capture: true,
    allowFailure: true,
  });
  if (result.status === 0) return true;

  const output = `${result.stdout || ''}\n${result.stderr || ''}`;
  if (/E404|404 Not Found|No match found|not found/i.test(output)) return false;
  fail(`Could not check npm registry for ${name}@${version}:\n${output}`);
}


function tagNameForPackage(pkg) {
  return `${pkg.name}@${pkg.version}`;
}

function remoteTagExists(tagName) {
  const result = run('git', ['ls-remote', '--exit-code', '--tags', 'origin', `refs/tags/${tagName}`], {
    capture: true,
    allowFailure: true,
  });
  if (result.status === 0) return true;
  if (result.status === 2) return false;
  const output = `${result.stdout || ''}\n${result.stderr || ''}`;
  fail(`Could not check remote tag ${tagName}:\n${output}`);
}

function ensureLocalTag(tagName) {
  const existing = run('git', ['rev-parse', '--quiet', '--verify', `refs/tags/${tagName}`], {
    capture: true,
    allowFailure: true,
  });
  if (existing.status === 0) return;
  run('git', ['tag', tagName]);
}

function emitNewTag(pkg) {
  const tagName = tagNameForPackage(pkg);
  ensureLocalTag(tagName);
  // changesets/action parses this exact line to decide which Git tags and
  // GitHub releases to create for a custom publish command.
  console.log(`New tag: ${tagName}`);
}

function findPackedTarball(packDir, packageName) {
  const files = fs.readdirSync(packDir)
    .filter((file) => file.endsWith('.tgz'))
    .map((file) => path.join(packDir, file))
    .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
  if (files.length === 0) fail(`Pack command for ${packageName} did not create a .tgz in ${packDir}`);
  return files[0];
}

function packPackage(manager, rootDir, pkg, packDir) {
  switch (manager.type) {
    case 'bun':
      run('bun', ['pm', 'pack', '--destination', packDir], { cwd: pkg.dir });
      return findPackedTarball(packDir, pkg.name);
    case 'pnpm': {
      const isRoot = path.resolve(pkg.dir) === path.resolve(rootDir);
      if (isRoot) {
        run('pnpm', ['pack', '--pack-destination', packDir], { cwd: pkg.dir });
      } else {
        run('pnpm', ['--filter', pkg.name, 'pack', '--pack-destination', packDir], { cwd: rootDir });
      }
      return findPackedTarball(packDir, pkg.name);
    }
    case 'yarn': {
      const outFile = path.join(packDir, `${sanitizeForFileName(pkg.name)}-${pkg.version}.tgz`);
      run('yarn', ['pack', '--out', outFile], { cwd: pkg.dir });
      return outFile;
    }
    case 'npm':
    default:
      run('npm', ['pack', '--pack-destination', packDir], { cwd: pkg.dir });
      return findPackedTarball(packDir, pkg.name);
  }
}

function readPackedManifest(tarballPath) {
  const result = run('tar', ['-xOf', tarballPath, 'package/package.json'], { capture: true });
  return JSON.parse(result.stdout);
}

function workspaceProtocolEntries(packageJson) {
  const entries = [];
  for (const section of DEPENDENCY_SECTIONS) {
    const values = packageJson[section];
    if (!values || typeof values !== 'object' || Array.isArray(values)) continue;
    for (const [name, spec] of Object.entries(values)) {
      if (typeof spec === 'string' && spec.startsWith('workspace:')) {
        entries.push(`${section}.${name}=${spec}`);
      }
    }
  }
  return entries;
}

function materializeWorkspaceRange(range, version) {
  if (range === '' || range === '*') return version;
  if (range === '^') return `^${version}`;
  if (range === '~') return `~${version}`;
  return range;
}

function parseWorkspaceAlias(range) {
  const match = range.match(/^(@[^/\s]+\/[^@\s]+|[^@/\s][^@\s]*)@(.+)$/);
  if (!match) return null;
  return { packageName: match[1], range: match[2] };
}

function materializeWorkspaceSpec(dependencyName, spec, localVersions) {
  const protocol = 'workspace:';
  const range = spec.slice(protocol.length);

  if (range.startsWith('.') || range.startsWith('/')) {
    fail(`${dependencyName} uses path-based ${spec}; publish metadata needs a named workspace package range.`);
  }

  if (localVersions.has(dependencyName)) {
    return materializeWorkspaceRange(range, localVersions.get(dependencyName));
  }

  const alias = parseWorkspaceAlias(range);
  if (alias && localVersions.has(alias.packageName)) {
    const materializedRange = materializeWorkspaceRange(alias.range, localVersions.get(alias.packageName));
    return `npm:${alias.packageName}@${materializedRange}`;
  }

  fail(`${dependencyName} uses ${spec}, but no matching local workspace package was found.`);
}

function materializePackageManifest(pkg, localVersions) {
  const packageJsonPath = path.join(pkg.dir, 'package.json');
  const originalText = fs.readFileSync(packageJsonPath, 'utf8');
  const packageJson = JSON.parse(originalText);
  const materialized = [];

  for (const section of DEPENDENCY_SECTIONS) {
    const values = packageJson[section];
    if (!values || typeof values !== 'object' || Array.isArray(values)) continue;
    for (const [name, spec] of Object.entries(values)) {
      if (typeof spec !== 'string' || !spec.startsWith('workspace:')) continue;
      const replacement = materializeWorkspaceSpec(name, spec, localVersions);
      values[name] = replacement;
      materialized.push(`${section}.${name}: ${spec} -> ${replacement}`);
    }
  }

  if (materialized.length === 0) return null;

  fs.writeFileSync(packageJsonPath, `${JSON.stringify(packageJson, null, 2)}\n`);
  log(`Materialized ${pkg.name}@${pkg.version}: ${materialized.join(', ')}`);

  return () => {
    fs.writeFileSync(packageJsonPath, originalText);
  };
}

function materializeWorkspaceManifests(packages, localVersions) {
  const restorers = [];
  for (const pkg of packages) {
    const restore = materializePackageManifest(pkg, localVersions);
    if (restore) restorers.push(restore);
  }
  return () => {
    for (const restore of restorers.reverse()) restore();
  };
}

function assertMaterializedManifest(pkg) {
  const leaks = workspaceProtocolEntries(readJson(path.join(pkg.dir, 'package.json')));
  if (leaks.length > 0) {
    fail(`${pkg.name}@${pkg.version} source manifest still contains workspace: dependencies after materialization: ${leaks.join(', ')}`);
  }
}

function auditPackage(manager, rootDir, pkg) {
  assertMaterializedManifest(pkg);
  const packDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sylphx-pack-'));
  try {
    const tarballPath = packPackage(manager, rootDir, pkg, packDir);
    const packedManifest = readPackedManifest(tarballPath);
    const leaks = workspaceProtocolEntries(packedManifest);
    if (leaks.length > 0) {
      fail(`${pkg.name}@${pkg.version} packed artifact still contains workspace: dependencies: ${leaks.join(', ')}`);
    }
    log(`Packed artifact OK: ${pkg.name}@${pkg.version}`);
  } finally {
    fs.rmSync(packDir, { recursive: true, force: true });
  }
}

function accessArgs(pkg) {
  const access = pkg.packageJson.publishConfig?.access || (pkg.name.startsWith('@') ? 'public' : undefined);
  return access ? ['--access', access] : [];
}

function tagArgs() {
  const tag = process.env.NPM_CONFIG_TAG || process.env.npm_config_tag;
  return tag ? ['--tag', tag] : [];
}

function publishEnv() {
  const env = {};
  if (process.env.NODE_AUTH_TOKEN && !process.env.NPM_CONFIG_TOKEN && !process.env.npm_config_token) {
    // GitHub's setup-node writes NODE_AUTH_TOKEN/.npmrc for npm-compatible
    // clients. Bun publish reads NPM_CONFIG_TOKEN instead, so bridge the same
    // workflow-scoped token without introducing another secret surface.
    env.NPM_CONFIG_TOKEN = process.env.NODE_AUTH_TOKEN;
    env.npm_config_token = process.env.NODE_AUTH_TOKEN;
    log('Using NODE_AUTH_TOKEN as NPM_CONFIG_TOKEN for publish command authentication.');
  }
  return env;
}

function publishPackage(manager, rootDir, pkg) {
  const commonArgs = [...accessArgs(pkg), ...tagArgs()];
  const env = publishEnv();
  switch (manager.type) {
    case 'bun':
      run('bun', ['publish', ...commonArgs], { cwd: pkg.dir, env });
      break;
    case 'pnpm': {
      const isRoot = path.resolve(pkg.dir) === path.resolve(rootDir);
      if (isRoot) {
        run('pnpm', ['publish', '--no-git-checks', ...commonArgs], { cwd: pkg.dir, env });
      } else {
        run('pnpm', ['--filter', pkg.name, 'publish', '--no-git-checks', ...commonArgs], { cwd: rootDir, env });
      }
      break;
    }
    case 'yarn':
      run('yarn', ['npm', 'publish', ...commonArgs], { cwd: pkg.dir, env });
      break;
    case 'npm':
    default:
      run('npm', ['publish', ...commonArgs], { cwd: pkg.dir, env });
      break;
  }
}

function selfCheck() {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sylphx-publish-self-check-'));
  try {
    fs.mkdirSync(path.join(tempRoot, 'packages', 'a'), { recursive: true });
    fs.mkdirSync(path.join(tempRoot, 'packages', 'b'), { recursive: true });
    fs.writeFileSync(path.join(tempRoot, 'package.json'), JSON.stringify({
      private: true,
      packageManager: 'bun@1.2.0',
      workspaces: ['packages/*'],
    }, null, 2));
    fs.writeFileSync(path.join(tempRoot, 'packages', 'a', 'package.json'), JSON.stringify({
      name: '@self-check/a',
      version: '1.2.3',
    }, null, 2));
    fs.writeFileSync(path.join(tempRoot, 'packages', 'b', 'package.json'), JSON.stringify({
      name: '@self-check/b',
      version: '1.0.0',
      dependencies: { '@self-check/a': 'workspace:*' },
      peerDependencies: { '@self-check/a': 'workspace:^' },
      optionalDependencies: { '@self-check/a': 'workspace:~' },
      devDependencies: { '@self-check/a': 'workspace:^1.0.0' },
    }, null, 2));

    const rootPackageJson = readJson(path.join(tempRoot, 'package.json'));
    const manager = detectPackageManager(tempRoot, rootPackageJson);
    const packages = discoverPackages(tempRoot);
    const localVersions = localPackageVersionMap(tempRoot);
    if (manager.type !== 'bun') fail(`self-check expected bun manager, got ${manager.type}`);
    if (packages.map((pkg) => pkg.name).join(',') !== '@self-check/a,@self-check/b') {
      fail(`self-check publish order was ${packages.map((pkg) => pkg.name).join(',')}`);
    }
    const leaks = workspaceProtocolEntries(readJson(path.join(tempRoot, 'packages', 'b', 'package.json')));
    if (leaks.length !== 4) fail(`self-check expected four workspace protocol entries, got ${leaks.length}`);

    const restore = materializeWorkspaceManifests(packages, localVersions);
    const materialized = readJson(path.join(tempRoot, 'packages', 'b', 'package.json'));
    if (materialized.dependencies['@self-check/a'] !== '1.2.3') fail('self-check did not materialize workspace:* to the local version');
    if (materialized.peerDependencies['@self-check/a'] !== '^1.2.3') fail('self-check did not materialize workspace:^ to the local version');
    if (materialized.optionalDependencies['@self-check/a'] !== '~1.2.3') fail('self-check did not materialize workspace:~ to the local version');
    if (materialized.devDependencies['@self-check/a'] !== '^1.0.0') fail('self-check did not strip explicit workspace ranges');
    if (workspaceProtocolEntries(materialized).length !== 0) fail('self-check materialized manifest still contains workspace: ranges');
    restore();
    if (workspaceProtocolEntries(readJson(path.join(tempRoot, 'packages', 'b', 'package.json'))).length !== 4) {
      fail('self-check did not restore source manifests after materialization');
    }

    const previousNodeAuthToken = process.env.NODE_AUTH_TOKEN;
    const previousNpmConfigToken = process.env.NPM_CONFIG_TOKEN;
    const previousLowercaseNpmConfigToken = process.env.npm_config_token;
    try {
      process.env.NODE_AUTH_TOKEN = 'self-check-token';
      delete process.env.NPM_CONFIG_TOKEN;
      delete process.env.npm_config_token;
      const bridgedEnv = publishEnv();
      if (bridgedEnv.NPM_CONFIG_TOKEN !== 'self-check-token' || bridgedEnv.npm_config_token !== 'self-check-token') {
        fail('self-check did not bridge NODE_AUTH_TOKEN to NPM_CONFIG_TOKEN');
      }
      process.env.NPM_CONFIG_TOKEN = 'explicit-token';
      if (publishEnv().NPM_CONFIG_TOKEN) {
        fail('self-check should not override an explicit NPM_CONFIG_TOKEN');
      }
    } finally {
      if (previousNodeAuthToken === undefined) delete process.env.NODE_AUTH_TOKEN;
      else process.env.NODE_AUTH_TOKEN = previousNodeAuthToken;
      if (previousNpmConfigToken === undefined) delete process.env.NPM_CONFIG_TOKEN;
      else process.env.NPM_CONFIG_TOKEN = previousNpmConfigToken;
      if (previousLowercaseNpmConfigToken === undefined) delete process.env.npm_config_token;
      else process.env.npm_config_token = previousLowercaseNpmConfigToken;
    }

    log('self-check passed');
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
}

function main() {
  if (isSelfCheck) {
    selfCheck();
    return;
  }

  const rootDir = process.cwd();
  const rootPackagePath = path.join(rootDir, 'package.json');
  if (!fileExists(rootPackagePath)) fail(`No package.json found in ${rootDir}`);

  const rootPackageJson = readJson(rootPackagePath);
  const manager = detectPackageManager(rootDir, rootPackageJson);
  const packages = discoverPackages(rootDir);
  const localVersions = localPackageVersionMap(rootDir);

  log(`Detected package manager: ${manager.packageManager || manager.type}`);
  log(`Publishable packages: ${packages.map((pkg) => `${pkg.name}@${pkg.version} (${pkg.relativeDir})`).join(', ') || 'none'}`);

  const unpublished = [];
  const tagRecovery = [];

  for (const pkg of packages) {
    const exists = packageVersionExists(pkg.name, pkg.version);
    if (!exists) {
      unpublished.push(pkg);
      continue;
    }

    const tagName = tagNameForPackage(pkg);
    if (!remoteTagExists(tagName)) {
      tagRecovery.push(pkg);
    }
  }

  if (unpublished.length === 0 && tagRecovery.length === 0) {
    log('No unpublished package versions or missing release tags found. Nothing to publish.');
    return;
  }

  const restoreManifests = materializeWorkspaceManifests(unpublished, localVersions);
  try {
    if (unpublished.length > 0) {
      log(`Unpublished packages: ${unpublished.map((pkg) => `${pkg.name}@${pkg.version}`).join(', ')}`);
      for (const pkg of unpublished) auditPackage(manager, rootDir, pkg);
    }

    if (tagRecovery.length > 0) {
      log(`Published packages missing remote release tags: ${tagRecovery.map((pkg) => tagNameForPackage(pkg)).join(', ')}`);
    }

    if (isDryRun) {
      log('--dry-run set; not publishing or creating tags.');
      return;
    }

    for (const pkg of unpublished) {
      publishPackage(manager, rootDir, pkg);
      emitNewTag(pkg);
    }

    for (const pkg of tagRecovery) {
      emitNewTag(pkg);
    }
  } finally {
    restoreManifests();
  }
}

try {
  main();
} catch (error) {
  console.error(`::error::${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
