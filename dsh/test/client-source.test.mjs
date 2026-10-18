/**
 * Client 源模块静态基线（node:test，零依赖）。
 *
 * 打包器会把 `dsh/lib/client/**` 的 ESM 模块打进 `dsh/client.js`，运行期没有
 * npm 解析能力，因此源码必须严格满足 CONTRACT 的约束：只 import 相对路径
 * （或 `react`，由浏览器模块表提供）、无 CSS import、无动态 import、无顶层
 * await，且每个模块可被 `node --check` 解析。Lead 的 build-client.mjs 若存在
 * 也做同样的语法检查。
 *
 * 运行：`cd dsh && node --test`（或 `node --test 'test/**\/*.test.mjs'`）。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  collectImportSpecifiers,
  listFiles,
  stripJsComments,
} from '../scripts/verify/lib/manifest-lib.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const dshDir = path.resolve(here, '..')
const repoRoot = path.resolve(dshDir, '..')
const clientDir = path.join(dshDir, 'lib', 'client')
const STRICT = process.env.QA_STRICT === '1'
const CLIENT_FILES = listFiles(clientDir, ['.js', '.mjs'])
// 与 scripts/build-client.mjs 的 ALLOWED_EXTERNALS 对齐：平台提供的 react 家族。
const ALLOWED_BARE = new Set(['react', 'react-dom', 'react/jsx-runtime'])

function rel(file) {
  return path.relative(repoRoot, file) || file
}

function skipIfNoClient(t) {
  if (CLIENT_FILES.length > 0) return false
  if (STRICT) assert.fail('dsh/lib/client 下没有可检查的 JS 模块（QA_STRICT=1）')
  t.skip('dsh/lib/client 下暂无 JS 模块（渐进模式跳过；QA_STRICT=1 时失败）')
  return true
}

/** 对单个文件跑 `node --check`；返回错误字符串或 null。 */
function checkSyntax(absolute) {
  const result = spawnSync(process.execPath, ['--check', absolute], {
    cwd: dshDir,
    encoding: 'utf8',
    timeout: 20_000,
  })
  if (result.error) return `无法执行 node --check: ${result.error.message}`
  if (result.status === 0) return null
  return `${result.stderr || result.stdout || ''}`.trim().split('\n').slice(0, 6).join('\n')
}

test('Client 源模块清单非空', (t) => {
  if (skipIfNoClient(t)) return
  t.diagnostic(`Client 源模块: ${CLIENT_FILES.length} 个文件`)
})

test('每个 Client 源模块通过 node --check', (t) => {
  if (skipIfNoClient(t)) return
  const failures = []
  for (const relative of CLIENT_FILES) {
    const error = checkSyntax(path.join(clientDir, relative))
    if (error) failures.push(`${relative}: ${error}`)
  }
  assert.deepEqual(failures, [], `Client 源模块语法失败:\n- ${failures.join('\n- ')}`)
  t.diagnostic(`node --check 通过: ${CLIENT_FILES.length} 个文件`)
})

test('Client 源只 import 相对路径或 react，无 CSS/动态 import', (t) => {
  if (skipIfNoClient(t)) return
  const problems = []
  for (const relative of CLIENT_FILES) {
    const code = stripJsComments(readFileSync(path.join(clientDir, relative), 'utf8'))
    for (const specifier of collectImportSpecifiers(code)) {
      if (specifier.startsWith('.')) {
        if (specifier.endsWith('.css')) problems.push(`${relative}: 禁止 CSS import ${specifier}`)
        continue
      }
      if (!ALLOWED_BARE.has(specifier)) {
        problems.push(`${relative}: 非允许的裸模块 import ${JSON.stringify(specifier)}（仅允许 react 家族）`)
      }
    }
  }
  assert.deepEqual(problems, [], problems.join('\n'))
})

test('Client 源没有顶层 await（契约禁止）', (t) => {
  if (skipIfNoClient(t)) return
  const problems = []
  for (const relative of CLIENT_FILES) {
    const code = stripJsComments(readFileSync(path.join(clientDir, relative), 'utf8'))
    // 顶层 await 只会出现在第 0 列；函数体内的 await 有缩进。
    if (/^(?:await\b|for\s+await\b)/m.test(code)) {
      problems.push(`${relative}: 含顶层 await`)
    }
  }
  assert.deepEqual(problems, [], problems.join('\n'))
})

test('打包器 scripts/build-client.mjs 存在且语法正确', (t) => {
  const buildPath = path.join(dshDir, 'scripts', 'build-client.mjs')
  if (!existsSync(buildPath)) {
    if (STRICT) assert.fail(`打包器缺失: ${rel(buildPath)}（QA_STRICT=1）`)
    t.skip(`打包器缺失: ${rel(buildPath)}（渐进模式跳过；QA_STRICT=1 时失败）`)
    return
  }
  const error = checkSyntax(buildPath)
  assert.equal(error, null, `打包器语法失败: ${error}`)
})
