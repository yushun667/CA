#!/usr/bin/env node
/**
 * Build an offline wheelhouse for one target platform.
 *
 * The CA plugin installs the analyzer backend from a prebuilt CA wheel plus a
 * wheelhouse of its runtime dependencies (clang, libclang, duckdb, pytest and
 * their transitive wheels), so a machine without network access can still
 * install the backend. The CA wheel itself is baked into the plugin
 * (`dsh/backend/wheels/`); this script collects the matching dependency wheels.
 *
 * Usage:
 *   node dsh/scripts/build-wheelhouse.mjs                      # native platform
 *   node dsh/scripts/build-wheelhouse.mjs --platform linux-x64
 *   node dsh/scripts/build-wheelhouse.mjs --platform windows-x64 --out artifacts/wh-win
 *   node dsh/scripts/build-wheelhouse.mjs --tar                 # also write .tar.gz
 *
 * The output directory contains the dependency wheels, a copy of the CA wheel
 * and `manifest.json` (platform, pip arguments, file hashes). Cross-platform
 * downloads require network access and a pip whose version can resolve the
 * pinned requirements; run this on any machine with network, then copy the
 * directory (or tarball) to the offline host.
 */

import { createHash } from 'node:crypto'
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
export const PACKAGE_ROOT = resolve(HERE, '..')
export const REPO_ROOT = resolve(PACKAGE_ROOT, '..')
export const DEFAULT_REQUIREMENTS = join(REPO_ROOT, 'requirements.txt')
export const DEFAULT_CA_WHEEL_DIR = join(PACKAGE_ROOT, 'backend', 'wheels')

/** Target platform ids and the pip tags that accept wheels for them. */
export const PLATFORM_SPECS = Object.freeze({
  'linux-x64': {
    label: 'Linux x86_64',
    platforms: [
      'manylinux_2_28_x86_64',
      'manylinux2014_x86_64',
      'manylinux2010_x86_64',
      'manylinux1_x86_64',
    ],
  },
  'linux-arm64': {
    label: 'Linux aarch64',
    platforms: ['manylinux_2_28_aarch64', 'manylinux2014_aarch64', 'manylinux2010_aarch64'],
  },
  'macos-arm64': {
    label: 'macOS arm64',
    platforms: ['macosx_11_0_arm64', 'macosx_12_0_arm64', 'macosx_13_0_arm64', 'macosx_14_0_arm64'],
  },
  'macos-x64': {
    label: 'macOS x86_64',
    platforms: ['macosx_10_9_x86_64', 'macosx_10_15_x86_64', 'macosx_11_0_x86_64'],
  },
  'windows-x64': {
    label: 'Windows x86_64',
    platforms: ['win_amd64', 'win32'],
  },
})

/** @returns {string} the current host platform id. */
export function detectHostPlatform() {
  const platform = process.platform
  const arch = process.arch
  if (platform === 'darwin' && (arch === 'arm64' || arch === 'x64')) {
    return arch === 'arm64' ? 'macos-arm64' : 'macos-x64'
  }
  if (platform === 'linux' && (arch === 'x64' || arch === 'arm64')) {
    return arch === 'x64' ? 'linux-x64' : 'linux-arm64'
  }
  if (platform === 'win32' && arch === 'x64') return 'windows-x64'
  throw new Error(`unsupported host platform: ${platform}/${arch}`)
}

/**
 * Build the `pip download` argument list for one target platform.
 * @param {object} input
 * @param {string} input.platform target platform id.
 * @param {string} input.requirements requirements file.
 * @param {string} input.outDir output directory.
 * @param {string} [input.pythonVersion] e.g. `3.11`.
 * @returns {{argv: string[], pipArgs: string[]}}
 */
export function buildDownloadArgs({ platform, requirements, outDir, pythonVersion = '3.11' }) {
  const spec = PLATFORM_SPECS[platform]
  if (!spec) throw new Error(`unknown platform "${platform}" (expected one of: ${Object.keys(PLATFORM_SPECS).join(', ')})`)
  const pipArgs = ['download', '-r', requirements, '-d', outDir, '--only-binary=:all:']
  if (platform !== 'native') {
    for (const tag of spec.platforms) pipArgs.push('--platform', tag)
    pipArgs.push('--python-version', pythonVersion, '--implementation', 'cp', '--abi', `cp${pythonVersion.replace('.', '')}`)
  }
  return { argv: ['-m', 'pip', ...pipArgs], pipArgs }
}

/** @returns {string|null} the newest CA wheel baked into the plugin. */
export function findBundledCaWheel(dir = DEFAULT_CA_WHEEL_DIR) {
  if (!existsSync(dir)) return null
  const wheels = readdirSync(dir)
    .filter((name) => /^ca_analyzer-.*\.whl$/.test(name))
    .map((name) => join(dir, name))
    .filter((file) => statSync(file).isFile())
  wheels.sort()
  return wheels.length > 0 ? wheels[wheels.length - 1] : null
}

function sha256(file) {
  return createHash('sha256').update(readFileSync(file)).digest('hex')
}

/**
 * Build one wheelhouse directory (and optionally a tarball).
 * @param {object} options
 * @param {string} options.platform
 * @param {string} [options.outDir]
 * @param {string} [options.python] python interpreter used for pip.
 * @param {string} [options.requirements]
 * @param {string} [options.caWheel]
 * @param {string} [options.pythonVersion]
 * @param {boolean} [options.tar]
 * @param {Function} [options.run] injectable spawnSync-compatible function.
 * @returns {object} manifest
 */
export function buildWheelhouse(options) {
  const platform = options.platform || detectHostPlatform()
  const python = options.python || resolvePython(options)
  const requirements = options.requirements || DEFAULT_REQUIREMENTS
  const pythonVersion = options.pythonVersion || '3.11'
  const outDir = resolve(options.outDir || join(REPO_ROOT, 'artifacts', 'wheelhouse', platform))
  const run = options.run || ((command, args) => spawnSync(command, args, { stdio: 'inherit' }))

  if (!existsSync(requirements)) throw new Error(`requirements file not found: ${requirements}`)
  const caWheel = options.caWheel || findBundledCaWheel()
  if (!caWheel || !existsSync(caWheel)) throw new Error('bundled CA wheel not found under dsh/backend/wheels/')
  mkdirSync(outDir, { recursive: true })

  const { argv } = buildDownloadArgs({ platform, requirements, outDir, pythonVersion })
  const result = run(python, argv)
  if (result && result.error) throw result.error
  if (result && typeof result.status === 'number' && result.status !== 0) {
    throw new Error(`pip download failed with exit code ${result.status}`)
  }

  cpSync(caWheel, join(outDir, caWheel.split('/').pop()), { force: true })
  const files = readdirSync(outDir)
    .filter((name) => name.endsWith('.whl'))
    .sort()
    .map((name) => ({ name, sha256: sha256(join(outDir, name)), bytes: statSync(join(outDir, name)).size }))
  const manifest = {
    schema: 1,
    platform,
    label: PLATFORM_SPECS[platform]?.label || platform,
    python: pythonVersion,
    caWheel: caWheel.split('/').pop(),
    createdAt: new Date().toISOString(),
    files,
  }
  writeFileSync(join(outDir, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`)
  if (options.tar === true && options.writeTar !== false) {
    const tarball = `${outDir.replace(/[\\/]$/, '')}.tar.gz`
    const tarResult = run('tar', ['-czf', tarball, '-C', outDir, '.'])
    if (!tarResult || tarResult.status === 0) manifest.tarball = tarball
  }
  return manifest
}

function resolvePython(options) {
  if (options.python) return options.python
  const venv = join(REPO_ROOT, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
  return existsSync(venv) ? venv : 'python3'
}

function parseArgs(argv) {
  const out = { tar: false }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--tar') out.tar = true
    else if (arg === '--platform') out.platform = argv[++index]
    else if (arg === '--out') out.outDir = argv[++index]
    else if (arg === '--python') out.python = argv[++index]
    else if (arg === '--requirements') out.requirements = argv[++index]
    else if (arg === '--python-version') out.pythonVersion = argv[++index]
    else if (arg === '--list-platforms') out.listPlatforms = true
    else throw new Error(`unknown argument: ${arg}`)
  }
  return out
}

function main() {
  const options = parseArgs(process.argv.slice(2))
  if (options.listPlatforms) {
    for (const [id, spec] of Object.entries(PLATFORM_SPECS)) console.log(`${id}\t${spec.label}`)
    return
  }
  const manifest = buildWheelhouse(options)
  console.log(
    `wheelhouse: ${manifest.platform} · ${manifest.files.length} wheels · ca wheel ${manifest.caWheel}` +
      (manifest.tarball ? ` · tarball ${manifest.tarball}` : ''),
  )
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main()
  } catch (error) {
    console.error(`build-wheelhouse: ${error.message}`)
    process.exit(1)
  }
}
