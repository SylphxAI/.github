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

function discoverPackages(rootDir) {
  const rootPackageJson = readJson(path.join(rootDir, 'package.json'));
  const patterns = workspacePatterns(rootPackageJson);
  const packageDirs = new Set();

  if (isPublishable(rootPackageJson)) packageDirs.add(rootDir);

  for (const pattern of patterns) {
    for (const dir of expandWorkspacePattern(rootDir, pattern)) {
      packageDirs.add(path.resolve(dir));
    }
  }

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

function localDependencyNames(pkg, localNames) {
  const deps = new Set();
  for (const section of DEPENDENCY_SECTIONS) {
    const values = pkg.packageJson[section];
    if (!values || typeof values !== 'object' || Array.isArray(values)) continue;
    for (const name of Object.keys(values)) {
      if (localNames.has(name)) deps.add(name);
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

function auditPackage(manager, rootDir, pkg) {
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

function publishPackage(manager, rootDir, pkg) {
  const commonArgs = [...accessArgs(pkg), ...tagArgs()];
  switch (manager.type) {
    case 'bun':
      run('bun', ['publish', ...commonArgs], { cwd: pkg.dir });
      break;
    case 'pnpm': {
      const isRoot = path.resolve(pkg.dir) === path.resolve(rootDir);
      if (isRoot) {
        run('pnpm', ['publish', '--no-git-checks', ...commonArgs], { cwd: pkg.dir });
      } else {
        run('pnpm', ['--filter', pkg.name, 'publish', '--no-git-checks', ...commonArgs], { cwd: rootDir });
      }
      break;
    }
    case 'yarn':
      run('yarn', ['npm', 'publish', ...commonArgs], { cwd: pkg.dir });
      break;
    case 'npm':
    default:
      run('npm', ['publish', ...commonArgs], { cwd: pkg.dir });
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
      version: '1.0.0',
    }, null, 2));
    fs.writeFileSync(path.join(tempRoot, 'packages', 'b', 'package.json'), JSON.stringify({
      name: '@self-check/b',
      version: '1.0.0',
      dependencies: { '@self-check/a': 'workspace:*' },
    }, null, 2));

    const rootPackageJson = readJson(path.join(tempRoot, 'package.json'));
    const manager = detectPackageManager(tempRoot, rootPackageJson);
    const packages = discoverPackages(tempRoot);
    if (manager.type !== 'bun') fail(`self-check expected bun manager, got ${manager.type}`);
    if (packages.map((pkg) => pkg.name).join(',') !== '@self-check/a,@self-check/b') {
      fail(`self-check publish order was ${packages.map((pkg) => pkg.name).join(',')}`);
    }
    const leaks = workspaceProtocolEntries(readJson(path.join(tempRoot, 'packages', 'b', 'package.json')));
    if (leaks.length !== 1) fail('self-check expected one workspace protocol entry');
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
}

try {
  main();
} catch (error) {
  console.error(`::error::${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
