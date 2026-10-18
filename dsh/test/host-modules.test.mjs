/**
 * Host 模块静态基线（node:test，零依赖）。
 *
 * - 对 `dsh/lib/host/**\/*.js` 逐个做 `node --check` 等价语法检查
 *   （spawn 当前 Node 的 --check，不执行模块）。
 * - 静态扫描 import：只允许相对路径与 Node 内置模块（契约要求）；
 *   扫描前剔除注释与字符串，避免把 JSDoc 里的 `import('型')` 当运行时依赖。
 * - 校验 Host 入口（manifest exports["."] 指向的文件）的 `inject` 至少包含
 *   tools/connection/webServer：connection.rpc 要求调用方注入 webServer，
 *   缺了会在激活期失败。
 *
 * 运行：`cd dsh && node --test`（或 `node --test 'test/**\/*.test.mjs'`）。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { builtinModules } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  collectImportSpecifiers,
  listFiles,
  loadDshManifest,
  resolveExportTarget,
  resolveInsidePackage,
  stripJsComments,
} from '../scripts/verify/lib/manifest-lib.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const dshDir = path.resolve(here, '..')
const repoRoot = path.resolve(dshDir, '..')
const hostDir = path.join(dshDir, 'lib', 'host')
const STRICT = process.env.QA_STRICT === '1'
const HOST_FILES = listFiles(hostDir, ['.js', '.mjs'])
const BUILTINS = new Set(builtinModules)

function rel(file) {
  return path.relative(repoRoot, file) || file
}

function skipIfNoHost(t) {
  if (HOST_FILES.length > 0) return false
  if (STRICT) assert.fail('dsh/lib/host 下没有可检查的 JS 模块（QA_STRICT=1）')
  t.skip('dsh/lib/host 下暂无 JS 模块（渐进模式跳过；QA_STRICT=1 时失败）')
  return true
}

/** 从 manifest 的 exports["."] 解析 Host 入口；解析失败返回 null。 */
function resolveHostEntry() {
  const loaded = loadDshManifest(dshDir)
  if (!loaded.present) return null
  const target = resolveExportTarget(loaded.manifest?.exports?.['.'])
  if (typeof target !== 'string') return null
  return resolveInsidePackage(dshDir, target)
}

test('Host 模块清单非空', (t) => {
  if (skipIfNoHost(t)) return
  t.diagnostic(`Host 模块: ${HOST_FILES.length} 个文件`)
})

test('每个 Host 模块通过 node --check', (t) => {
  if (skipIfNoHost(t)) return
  const failures = []
  for (const relative of HOST_FILES) {
    const absolute = path.join(hostDir, relative)
    const result = spawnSync(process.execPath, ['--check', absolute], {
      cwd: dshDir,
      encoding: 'utf8',
      timeout: 20_000,
    })
    if (result.error) {
      failures.push(`${relative}: 无法执行 node --check: ${result.error.message}`)
    } else if (result.status !== 0) {
      const detail = `${result.stderr || result.stdout || ''}`.trim().split('\n').slice(0, 6).join('\n')
      failures.push(`${relative}: exit ${result.status}\n${detail}`)
    }
  }
  assert.deepEqual(failures, [], `Host 模块语法失败:\n- ${failures.join('\n- ')}`)
  t.diagnostic(`node --check 通过: ${HOST_FILES.length} 个文件`)
})

test('Host 模块只 import 相对路径与 Node 内置模块', (t) => {
  if (skipIfNoHost(t)) return
  const problems = []
  for (const relative of HOST_FILES) {
    const text = stripJsComments(readFileSync(path.join(hostDir, relative), 'utf8'))
    for (const specifier of collectImportSpecifiers(text)) {
      if (specifier.startsWith('.')) continue
      const bare = specifier.startsWith('node:') ? specifier.slice('node:'.length) : specifier
      if (BUILTINS.has(bare)) continue
      problems.push(`${relative}: 非 Node 内置依赖 import ${JSON.stringify(specifier)}`)
    }
  }
  assert.deepEqual(problems, [], problems.join('\n'))
})

test('Host 入口 inject 至少包含 tools、connection、webServer', (t) => {
  const entry = resolveHostEntry()
  if (!entry) {
    if (STRICT) assert.fail('无法从 manifest exports["."] 解析 Host 入口（QA_STRICT=1）')
    t.skip('无法从 manifest exports["."] 解析 Host 入口（渐进模式跳过）')
    return
  }
  let text
  try {
    text = readFileSync(entry, 'utf8')
  } catch {
    if (STRICT) assert.fail(`Host 入口缺失: ${rel(entry)}（QA_STRICT=1）`)
    t.skip(`Host 入口缺失: ${rel(entry)}（渐进模式跳过）`)
    return
  }
  const match = text.match(/export\s+const\s+inject\s*=\s*\[([^\]]*)\]/)
  assert.ok(match, `Host 入口 (${rel(entry)}) 缺少 \`export const inject = [...]\``)
  const services = [...match[1].matchAll(/['"]([^'"]+)['"]/g)].map((item) => item[1])
  for (const required of ['tools', 'connection', 'webServer']) {
    assert.ok(services.includes(required), `inject 缺少 ${required}，实际 ${JSON.stringify(services)}`)
  }
})
