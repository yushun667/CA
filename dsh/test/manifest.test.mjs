/**
 * dsh 包清单静态基线（node:test，零依赖）。
 *
 * 断言 DSH bundle 的安装契约：包身份、exports、bundle patch、Client 平台、
 * locale 展示元数据、icon 展示资源、files 覆盖。产品文件尚未落地时按
 * “渐进模式”skip；设置 `QA_STRICT=1` 时缺失直接失败（最终验收用）。
 *
 * 运行：`cd dsh && QA_STRICT=1 node --test`（或 `node --test 'test/**\/*.test.mjs'`）。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import path from 'node:path'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  ICON_MAX_BYTES,
  PKG_NAME,
  globToRegExp,
  inspectIcon,
  inspectLocales,
  inspectPatch,
  loadDshManifest,
  resolveExportTarget,
  resolveInsidePackage,
} from '../scripts/verify/lib/manifest-lib.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const dshDir = path.resolve(here, '..')
const repoRoot = path.resolve(dshDir, '..')
const manifestPath = path.join(dshDir, 'package.json')
const STRICT = process.env.QA_STRICT === '1'

function rel(file) {
  return path.relative(repoRoot, file) || file
}

/**
 * 产品文件尚未出现时：严格模式失败，否则 skip 并让调用方 return。
 * 返回 true 表示当前用例已结束（应直接 return）。
 */
function skipIfMissing(t, file, label) {
  if (existsSync(file)) return false
  if (STRICT) assert.fail(`${label} 缺失: ${rel(file)}（QA_STRICT=1）`)
  t.skip(`${label} 缺失: ${rel(file)}（渐进模式跳过；QA_STRICT=1 时失败）`)
  return true
}

const loaded = loadDshManifest(dshDir)

test('dsh/package.json 存在且为合法 JSON 对象', () => {
  assert.ok(loaded.present, `缺少 ${rel(manifestPath)}（等待 Lead 创建包骨架）`)
  assert.equal(typeof loaded.manifest, 'object')
  assert.ok(loaded.manifest !== null && !Array.isArray(loaded.manifest))
})

test('包身份字段：name/version/private/type/description', () => {
  const manifest = loaded.manifest ?? {}
  assert.equal(manifest.name, PKG_NAME)
  assert.match(String(manifest.version ?? ''), /^\d+\.\d+\.\d+(?:[-+].+)?$/, 'version 应为 semver')
  assert.equal(manifest.private, true)
  assert.equal(manifest.type, 'module', 'bundle 必须声明 type=module')
  assert.equal(typeof manifest.description, 'string')
  assert.ok(manifest.description.trim().length >= 20, 'description 应说明插件能力（>=20 字符）')
})

test('exports 提供 "." 与 "./client"，目标均在包内', () => {
  const exportsField = loaded.manifest?.exports
  assert.ok(exportsField && typeof exportsField === 'object' && !Array.isArray(exportsField), 'exports 缺失')
  const hostTarget = resolveExportTarget(exportsField['.'])
  const clientTarget = resolveExportTarget(exportsField['./client'])
  assert.equal(typeof hostTarget, 'string', 'exports["."] 必须是可解析的文件目标')
  assert.equal(typeof clientTarget, 'string', 'exports["./client"] 必须是可解析的文件目标')
  assert.ok(clientTarget.endsWith('.js'), `client 目标应为 .js 文件，实际 ${clientTarget}`)
  assert.doesNotThrow(() => resolveInsidePackage(dshDir, hostTarget))
  assert.doesNotThrow(() => resolveInsidePackage(dshDir, clientTarget))
})

test('exports 提供 locale 通配与 "./icon"', () => {
  const exportsField = loaded.manifest?.exports ?? {}
  assert.equal(typeof resolveExportTarget(exportsField['./locale/*.json']), 'string', '缺少 ./locale/*.json')
  assert.equal(typeof resolveExportTarget(exportsField['./icon']), 'string', '缺少 ./icon')
})

test('dsh.bundle.patch 声明且指向存在的补丁文件', () => {
  const declared = loaded.manifest?.dsh?.bundle?.patch
  assert.equal(typeof declared, 'string', 'dsh.bundle.patch 必须是字符串路径')
  const patchPath = resolveInsidePackage(dshDir, declared)
  assert.ok(existsSync(patchPath), `patch 文件不存在: ${rel(patchPath)}`)
})

test('cordis.patch.yml 可解析，且 insert 了 @local/dsh-ca Host 行', () => {
  const patch = inspectPatch(dshDir, loaded.manifest, PKG_NAME)
  assert.deepEqual(patch.problems, [], `patch 问题:\n- ${patch.problems.join('\n- ')}`)
  const rows = patch.rows.filter((row) => row.name === PKG_NAME)
  assert.equal(rows.length, 1, `期望恰好 1 个 name=${PKG_NAME} 的 insert 行，实际 ${rows.length}`)
  assert.ok(rows[0].id && String(rows[0].id).trim().length > 0, 'Host 行 id 必须非空')
})

test('dsh.client.platform === "web"，inject 为字符串数组', () => {
  const client = loaded.manifest?.dsh?.client
  assert.ok(client && typeof client === 'object', 'dsh.client 缺失')
  assert.equal(client.platform, 'web', 'Client 平台必须是 web')
  assert.ok(Array.isArray(client.inject), 'dsh.client.inject 必须是数组')
  for (const item of client.inject) assert.equal(typeof item, 'string', `inject 项应为字符串: ${item}`)
})

test('locale 元数据：每个 locale 都有非空 meta.title/description（含 en/zh）', (t) => {
  const exportsField = loaded.manifest?.exports ?? {}
  const localeWildcard = resolveExportTarget(exportsField['./locale/*.json'])
  assert.equal(typeof localeWildcard, 'string', '缺少 ./locale/*.json 导出')
  const localeDir = path.dirname(resolveInsidePackage(dshDir, localeWildcard))
  if (skipIfMissing(t, localeDir, 'locale 目录')) return

  // 用共享库逐文件校验，同时断言 en/zh 双语齐全。
  const result = inspectLocales(dshDir, loaded.manifest)
  assert.deepEqual(result.problems, [], `locale 问题:\n- ${result.problems.join('\n- ')}`)
  const langs = result.locales.map((locale) => locale.lang)
  assert.ok(langs.includes('en'), `缺少 en locale，实际 ${langs.join(',')}`)
  assert.ok(langs.includes('zh'), `缺少 zh locale，实际 ${langs.join(',')}`)
  for (const locale of result.locales) {
    assert.ok(locale.title.trim().length > 0, `${locale.lang}: meta.title 为空`)
    assert.ok(locale.description.trim().length > 0, `${locale.lang}: meta.description 为空`)
  }
})

test('icon 有效、<=256KiB、格式与扩展名一致', (t) => {
  const exportsField = loaded.manifest?.exports ?? {}
  const iconTarget = resolveExportTarget(exportsField['./icon']) ?? loaded.manifest?.icon
  assert.equal(typeof iconTarget, 'string', '缺少 ./icon 或 package.json icon')
  const iconPath = resolveInsidePackage(dshDir, iconTarget)
  if (skipIfMissing(t, iconPath, 'icon')) return
  const result = inspectIcon(iconPath)
  assert.deepEqual(result.problems, [], `icon 问题:\n- ${result.problems.join('\n- ')}`)
  assert.ok(result.size > 0 && result.size <= ICON_MAX_BYTES, `icon 大小异常: ${result.size}`)
})

test('files 覆盖运行时与展示资源', () => {
  const files = loaded.manifest?.files
  assert.ok(Array.isArray(files) && files.length > 0, 'files 缺失：目录安装虽不裁剪，但发布/打包必须有完整 files')
  const patterns = files.map((pattern) => globToRegExp(String(pattern)))
  // Host 入口随 exports["."] 变化（当前为 lib/host/bootstrap.js），动态纳入覆盖检查。
  const hostTarget = resolveExportTarget(loaded.manifest?.exports?.['.'])
  const hostRelative =
    typeof hostTarget === 'string' ? path.posix.normalize(hostTarget.replace(/^\.\//, '')) : 'lib/host/index.js'
  const required = [
    'client.js',
    'cordis.patch.yml',
    'icon.svg',
    'locale/en.json',
    'locale/zh.json',
    hostRelative,
  ]
  const missing = required.filter((file) => !patterns.some((pattern) => pattern.test(file)))
  assert.deepEqual(missing, [], `files 未覆盖: ${missing.join(', ')}；实际 files=${JSON.stringify(files)}`)
})

test('Host 入口文件存在（渐进；最终验收必须存在）', (t) => {
  const target = resolveExportTarget(loaded.manifest?.exports?.['.'])
  assert.equal(typeof target, 'string', 'exports["."] 缺失')
  const hostPath = resolveInsidePackage(dshDir, target)
  if (skipIfMissing(t, hostPath, 'Host 入口')) return
  t.diagnostic(`Host 入口: ${rel(hostPath)}`)
})

test('Client 产物文件存在（渐进；最终验收必须存在）', (t) => {
  const target = resolveExportTarget(loaded.manifest?.exports?.['./client'])
  assert.equal(typeof target, 'string', 'exports["./client"] 缺失')
  const clientPath = resolveInsidePackage(dshDir, target)
  if (skipIfMissing(t, clientPath, 'Client 产物')) return
  t.diagnostic(`Client 产物: ${rel(clientPath)}`)
})
