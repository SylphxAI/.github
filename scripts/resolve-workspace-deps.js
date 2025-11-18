#!/usr/bin/env node

/**
 * Resolve workspace:* dependencies to actual versions
 * This script reads all package.json files in packages/* and replaces
 * workspace:* or workspace:^ with the actual version from the referenced package
 */

import { readFileSync, writeFileSync, readdirSync, statSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

function findPackages(dir) {
  const packages = new Map();
  const packagesDir = join(dir, 'packages');

  try {
    const entries = readdirSync(packagesDir);

    for (const entry of entries) {
      const pkgPath = join(packagesDir, entry, 'package.json');
      try {
        const pkgData = JSON.parse(readFileSync(pkgPath, 'utf8'));
        if (pkgData.name) {
          packages.set(pkgData.name, pkgData.version);
        }
      } catch (e) {
        // Skip if not a package directory
      }
    }
  } catch (e) {
    console.error('Error reading packages directory:', e.message);
  }

  return packages;
}

function resolveWorkspaceDeps(packagePath, versionMap) {
  const pkgData = JSON.parse(readFileSync(packagePath, 'utf8'));
  let modified = false;

  const depTypes = ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'];

  for (const depType of depTypes) {
    if (!pkgData[depType]) continue;

    for (const [dep, version] of Object.entries(pkgData[depType])) {
      // Check if it's a workspace dependency
      if (version.startsWith('workspace:')) {
        const targetVersion = versionMap.get(dep);

        if (!targetVersion) {
          console.warn(`Warning: Could not find version for workspace dependency: ${dep}`);
          continue;
        }

        // Extract the range operator from workspace protocol
        let newVersion;
        if (version === 'workspace:*') {
          newVersion = targetVersion;
        } else if (version === 'workspace:^') {
          newVersion = `^${targetVersion}`;
        } else if (version === 'workspace:~') {
          newVersion = `~${targetVersion}`;
        } else {
          // workspace:^1.0.0 -> ^1.0.0 (preserve the range)
          newVersion = version.replace('workspace:', '');
        }

        console.log(`Resolving ${dep}: ${version} -> ${newVersion}`);
        pkgData[depType][dep] = newVersion;
        modified = true;
      }
    }
  }

  if (modified) {
    writeFileSync(packagePath, JSON.stringify(pkgData, null, 2) + '\n');
    console.log(`✓ Updated ${packagePath}`);
  }

  return modified;
}

function main() {
  const cwd = process.cwd();
  console.log('Resolving workspace dependencies...\n');

  // Find all packages and their versions
  const versionMap = findPackages(cwd);
  console.log(`Found ${versionMap.size} packages:`);
  for (const [name, version] of versionMap) {
    console.log(`  ${name}@${version}`);
  }
  console.log('');

  // Resolve workspace deps in all packages
  const packagesDir = join(cwd, 'packages');
  const entries = readdirSync(packagesDir);
  let totalModified = 0;

  for (const entry of entries) {
    const pkgPath = join(packagesDir, entry, 'package.json');
    try {
      if (resolveWorkspaceDeps(pkgPath, versionMap)) {
        totalModified++;
      }
    } catch (e) {
      // Skip if not a package directory
    }
  }

  console.log(`\n✓ Resolved workspace dependencies in ${totalModified} package(s)`);
}

main();
