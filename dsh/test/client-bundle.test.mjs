/**
 * Client 打包产物静态基线（node:test，零依赖）。
 *
 * 断言 `dsh/client.js` 是合法的浏览器模块表注册脚本：
 * `window.__ModuleLoader__.load({ id: '@local/dsh-ca', factory(require) {...} })`，
 * 可被 `new Function` 编译（只编译不执行，不触碰 window）。
 *
 * 运行：`cd dsh && node --test`（或 `node --test 'test/**\/*.test.mjs'`）。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  PKG_NAME,
  inspectClientBundle,
  loadDshManifest,
  resolveExportTarget,
  resolveInsidePackage,
  stripJsComments,
} from '../scripts/verify/lib/manifest-lib.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const dshDir = path.resolve(here, '..')
const repoRoot = path.resolve(dshDir, '..')
const STRICT = process.env.QA_STRICT === '1'
const loaded = loadDshManifest(dshDir)

function rel(file) {
  return path.relative(repoRoot, file) || file
}

/** 渐进模式：产物尚未生成时 skip；QA_STRICT=1 时失败。 */
function skipIfMissing(t, file, label) {
  try {
    readFileSync(file, 'utf8')
    return false
  } catch {
    if (STRICT) assert.fail(`${label} 缺失或不可读: ${rel(file)}（QA_STRICT=1）`)
    t.skip(`${label} 缺失或不可读: ${rel(file)}（渐进模式跳过；QA_STRICT=1 时失败）`)
    return true
  }
}

const clientPath = (() => {
  const target = resolveExportTarget(loaded.manifest?.exports?.['./client'])
  if (typeof target !== 'string') return path.join(dshDir, 'client.js')
  try {
    return resolveInsidePackage(dshDir, target)
  } catch {
    return path.join(dshDir, 'client.js')
  }
})()

test('client 产物存在且非空', (t) => {
  if (skipIfMissing(t, clientPath, 'Client 产物')) return
  const text = readFileSync(clientPath, 'utf8')
  assert.ok(text.trim().length > 0, 'client 产物为空')
})

test('包含 window.__ModuleLoader__.load( 注册调用', (t) => {
  if (skipIfMissing(t, clientPath, 'Client 产物')) return
  const text = readFileSync(clientPath, 'utf8')
  assert.ok(
    text.includes('window.__ModuleLoader__.load('),
    '缺少 window.__ModuleLoader__.load( 调用：Client 产物必须是浏览器模块表可加载脚本',
  )
})

test('注册 id 与包名/清单一致', (t) => {
  if (skipIfMissing(t, clientPath, 'Client 产物')) return
  const expected = loaded.manifest?.name ?? PKG_NAME
  const text = readFileSync(clientPath, 'utf8')
  const match = text.match(/__ModuleLoader__\.load\(\s*\{[\s\S]{0,2000}?\bid\s*:\s*(['"])([^'"]+)\1/)
  assert.ok(match, '无法从 ModuleLoader.load({...}) 中提取 id')
  assert.equal(match[2], expected, `client 注册 id=${match[2]}，清单 name=${expected}`)
})

test('语法可编译：new Function 通过，无顶层 import/export 与动态 import', (t) => {
  if (skipIfMissing(t, clientPath, 'Client 产物')) return
  const text = readFileSync(clientPath, 'utf8')
  // 产物内联了源码注释（可能有 JSDoc 类型引用 import('react')），先剥离注释再判定。
  const code = stripJsComments(text)
  assert.doesNotMatch(code, /^\s*(?:import|export)\b/m, 'client 产物含顶层 import/export，浏览器脚本格式不合法')
  assert.doesNotMatch(code, /\bimport\s*\(/, 'client 产物含动态 import（契约禁止）')
  // new Function 只编译函数体，不会执行 body，因此不会访问 window。
  assert.doesNotThrow(() => new Function(text), 'new Function 编译失败（语法错误）')
  const result = inspectClientBundle(clientPath, expectedName())
  assert.deepEqual(result.problems, [], `client 产物问题:\n- ${result.problems.join('\n- ')}`)
})

test('factory 从浏览器模块表 require("react")，不内嵌 React', (t) => {
  if (skipIfMissing(t, clientPath, 'Client 产物')) return
  const text = readFileSync(clientPath, 'utf8')
  assert.match(text, /factory\(\s*require\s*\)/, '缺少 factory(require) 签名')
  // 打包器可能用 __req("react") 包装浏览器模块表的 require；两者都接受。
  assert.match(text, /(?:__req|require)\(\s*(['"])react\1\s*\)/, 'factory 未通过 require("react")/__req("react") 获取 React')
})

function expectedName() {
  return loaded.manifest?.name ?? PKG_NAME
}
