/**
 * 只读清单/资源检查工具，供 `dsh/test/*.test.mjs` 与
 * `dsh/scripts/verify/verify-install.mjs` 共用。
 *
 * 这里只做纯函数式读取与结构判定，不写文件、不启动进程、不访问网络。
 * 依赖仅 Node 内置模块。
 */

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'

/** bundle 包名；与 dsh/package.json、client.js 注册 id、profile bundles 一致。 */
export const PKG_NAME = '@local/dsh-ca'

/** icon 大小上限：256 KiB（DSH 展示资源限制）。 */
export const ICON_MAX_BYTES = 256 * 1024

/** 读取 JSON；失败时错误信息带文件路径，便于报告。 */
export function readJson(file) {
  let text
  try {
    text = readFileSync(file, 'utf8')
  } catch (err) {
    throw new Error(`读取失败 ${file}: ${err.message}`)
  }
  try {
    return JSON.parse(text)
  } catch (err) {
    throw new Error(`JSON 解析失败 ${file}: ${err.message}`)
  }
}

/** 文件存在且是普通文件。 */
export function isFile(file) {
  try {
    return statSync(file).isFile()
  } catch {
    return false
  }
}

/** 目录存在且是目录。 */
export function isDir(dir) {
  try {
    return statSync(dir).isDirectory()
  } catch {
    return false
  }
}

/**
 * 解析 package.json 的 exports 目标。
 * 支持字符串目标与条件对象（default/import/node/require 依序取第一个字符串），
 * 只用于本地静态检查，不实现完整 Node exports 语义。
 */
export function resolveExportTarget(value) {
  if (typeof value === 'string') return value
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  for (const key of ['default', 'import', 'node', 'require']) {
    const nested = resolveExportTarget(value[key])
    if (nested !== null) return nested
  }
  return null
}

/** 把相对 './x' 形式的 exports 目标解析到包内绝对路径并做越界检查。 */
export function resolveInsidePackage(pkgDir, target) {
  if (typeof target !== 'string' || target.length === 0) {
    throw new Error(`exports 目标不是非空字符串: ${JSON.stringify(target)}`)
  }
  if (path.isAbsolute(target)) {
    throw new Error(`exports 目标不得为绝对路径: ${target}`)
  }
  const absolute = path.resolve(pkgDir, target)
  const rel = path.relative(pkgDir, absolute)
  if (rel.startsWith('..') || path.isAbsolute(rel)) {
    throw new Error(`exports 目标越出包目录: ${target}`)
  }
  return absolute
}

/**
 * 极简 YAML 子集解析器：只覆盖 Cordis patch 用到的列表/映射/标量。
 *
 * 支持：
 * - 块列表 `- item`、`- key: value`
 * - 块映射 `key: value`、`key:`（下一行嵌套）
 * - 标量：单/双引号字符串、true/false、整数、其余原样字符串
 * - 注释行与空行；缩进必须为空格且是 2 的倍数
 *
 * 不支持：行内注释、多行标量、锚点、`!!js` 求值、流式集合求值
 * （`{}`/`[]` 会作为普通字符串保留，用于判定“可解析”已足够）。
 *
 * 返回容器树：`{ type: 'map', entries: {} }` 或 `{ type: 'list', items: [] }`。
 */
export function parseYamlLite(text, label = 'yaml') {
  const lines = String(text).replace(/\r\n?/g, '\n').split('\n')
  const significant = []
  for (let index = 0; index < lines.length; index++) {
    const raw = lines[index]
    const trimmed = raw.trim()
    if (trimmed === '' || trimmed.startsWith('#')) continue
    if (raw.includes('\t')) throw new Error(`${label}:${index + 1}: 缩进含 tab，YAML 只允许空格`)
    const indent = raw.length - raw.trimStart().length
    if (indent % 2 !== 0) throw new Error(`${label}:${index + 1}: 缩进不是 2 的倍数`)
    significant.push({ lineNo: index + 1, indent, content: trimmed })
  }
  if (significant.length === 0) throw new Error(`${label}: 内容为空`)

  const root = significant[0].content.startsWith('-')
    ? { type: 'list', items: [] }
    : { type: 'map', entries: {} }
  const stack = [{ indent: -1, container: root }]

  for (let index = 0; index < significant.length; index++) {
    const { lineNo, indent, content } = significant[index]
    while (stack.length > 1 && stack[stack.length - 1].indent >= indent) stack.pop()
    const parent = stack[stack.length - 1].container

    if (content.startsWith('-')) {
      if (parent.type !== 'list') throw new Error(`${label}:${lineNo}: 列表项出现在映射内部`)
      const item = { type: 'map', entries: {} }
      parent.items.push(item)
      stack.push({ indent, container: item })
      const rest = content.slice(1).trim()
      if (rest === '') continue
      const pair = splitKeyValue(rest, label, lineNo)
      if (pair.value === '') {
        const child = makeNestedContainer(significant, index, indent, label)
        item.entries[pair.key] = child
        stack.push({ indent, container: child })
      } else {
        item.entries[pair.key] = parseScalar(pair.value)
      }
      continue
    }

    if (parent.type !== 'map') throw new Error(`${label}:${lineNo}: 映射项出现在列表内部`)
    const pair = splitKeyValue(content, label, lineNo)
    if (pair.value === '') {
      const child = makeNestedContainer(significant, index, indent, label)
      parent.entries[pair.key] = child
      stack.push({ indent, container: child })
    } else {
      parent.entries[pair.key] = parseScalar(pair.value)
    }
  }
  return root
}

/** 找到 `key:` 后第一个更深缩进的有效行，决定嵌套容器是列表还是映射。 */
function makeNestedContainer(significant, index, indent, label) {
  for (let next = index + 1; next < significant.length; next++) {
    const candidate = significant[next]
    if (candidate.indent <= indent) break
    return candidate.content.startsWith('-')
      ? { type: 'list', items: [] }
      : { type: 'map', entries: {} }
  }
  // 空嵌套（`key:` 后无内容）按空映射处理，仍算可解析。
  return { type: 'map', entries: {} }
}

/** 在第一个未加引号的冒号处切分 `key: value`。 */
function splitKeyValue(text, label, lineNo) {
  let inSingle = false
  let inDouble = false
  for (let index = 0; index < text.length; index++) {
    const ch = text[index]
    if (ch === "'" && !inDouble) inSingle = !inSingle
    else if (ch === '"' && !inSingle) inDouble = !inDouble
    else if (ch === ':' && !inSingle && !inDouble) {
      const key = text.slice(0, index).trim()
      const value = text.slice(index + 1).trim()
      if (!/^[A-Za-z_][A-Za-z0-9_.-]*$/.test(key)) {
        throw new Error(`${label}:${lineNo}: 非法键名 ${JSON.stringify(key)}`)
      }
      return { key, value }
    }
  }
  throw new Error(`${label}:${lineNo}: 期望 key: value，实际 ${JSON.stringify(text)}`)
}

function parseScalar(value) {
  const text = value.trim()
  if (text.length >= 2) {
    const first = text[0]
    const last = text[text.length - 1]
    if ((first === "'" && last === "'") || (first === '"' && last === '"')) {
      return text.slice(1, -1)
    }
  }
  if (text === 'true') return true
  if (text === 'false') return false
  if (/^-?\d+$/.test(text)) return Number(text)
  return text
}

/**
 * 从 patch 文档里收集所有 insert 行（顶层 `- insert: [...]`）。
 * 返回 `{ rows, problems }`；rows 元素为行映射的 entries 对象。
 */
export function collectPatchInsertRows(patchRoot) {
  const problems = []
  const rows = []
  if (!patchRoot || patchRoot.type !== 'list') {
    return { rows, problems: ['patch 顶层必须是列表（以 - insert: 开头）'] }
  }
  for (const [index, item] of patchRoot.items.entries()) {
    if (!item || item.type !== 'map') {
      problems.push(`patch 列表第 ${index + 1} 项不是映射`)
      continue
    }
    const insert = item.entries.insert
    if (insert === undefined) continue
    if (!insert || insert.type !== 'list') {
      problems.push(`patch 第 ${index + 1} 项的 insert 不是列表`)
      continue
    }
    for (const [rowIndex, row] of insert.items.entries()) {
      if (!row || row.type !== 'map') {
        problems.push(`patch 第 ${index + 1} 项 insert[${rowIndex}] 不是映射`)
        continue
      }
      rows.push(row.entries)
    }
  }
  if (rows.length === 0) problems.push('patch 中没有找到任何 insert 行')
  return { rows, problems }
}

/**
 * 检查图标：存在、非空、<=256KiB、格式签名与扩展名一致、SVG 结构安全。
 * 返回 `{ ok, problems, format, size }`。
 */
export function inspectIcon(file) {
  const problems = []
  if (!isFile(file)) {
    return { ok: false, problems: [`图标文件不存在: ${file}`], format: null, size: 0 }
  }
  const buffer = readFileSync(file)
  const size = buffer.length
  const ext = path.extname(file).toLowerCase()

  if (size === 0) problems.push('图标文件为空')
  if (size > ICON_MAX_BYTES) {
    problems.push(`图标 ${size} 字节超过 256KiB 上限（${ICON_MAX_BYTES}）`)
  }

  let format = null
  if (buffer.length >= 8 && buffer.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    format = 'png'
  } else if (buffer.length >= 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) {
    format = 'jpeg'
  } else if (
    buffer.length >= 12 &&
    buffer.toString('ascii', 0, 4) === 'RIFF' &&
    buffer.toString('ascii', 8, 12) === 'WEBP'
  ) {
    format = 'webp'
  } else {
    const head = buffer.toString('utf8', 0, Math.min(size, 4096)).trimStart()
    if (head.startsWith('<') && /<svg[\s>]/i.test(head)) format = 'svg'
  }
  if (format === null) problems.push('未识别图标格式（期望 SVG/PNG/JPEG/WebP 有效签名）')

  const expectedByExt = { '.svg': 'svg', '.png': 'png', '.jpg': 'jpeg', '.jpeg': 'jpeg', '.webp': 'webp' }
  if (expectedByExt[ext] && format && expectedByExt[ext] !== format) {
    problems.push(`扩展名 ${ext} 与实际格式 ${format} 不一致`)
  }

  if (format === 'svg') {
    const text = buffer.toString('utf8')
    if (!/<\/svg\s*>/i.test(text)) problems.push('SVG 缺少闭合标签 </svg>')
    if (/<script[\s>]/i.test(text)) problems.push('SVG 含 <script>，不允许')
    if (/\son[a-z]+\s*=/i.test(text)) problems.push('SVG 含内联事件属性，不允许')
    if (/<image[\s>]/i.test(text)) problems.push('SVG 引用外部 <image>，安装校验不保证可分发')
  }

  return { ok: problems.length === 0, problems, format, size }
}

/** 把简单 glob（单星、双星、双星加斜杠、问号）转成正则，用于检查 package.json files 覆盖。 */
export function globToRegExp(glob) {
  let pattern = ''
  for (let index = 0; index < glob.length; index++) {
    const ch = glob[index]
    if (ch === '*') {
      if (glob[index + 1] === '*') {
        index++
        if (glob[index + 1] === '/') {
          index++
          pattern += '(?:.*/)?'
        } else {
          pattern += '.*'
        }
      } else {
        pattern += '[^/]*'
      }
    } else if (ch === '?') {
      pattern += '[^/]'
    } else {
      pattern += ch.replace(/[.+^${}()|[\]\\]/g, '\\$&')
    }
  }
  return new RegExp(`^${pattern}$`)
}

/**
 * 递归列出目录下匹配扩展名的文件，跳过 node_modules/.git 与符号链接目录。
 * 返回按路径排序的相对路径数组。
 */
export function listFiles(dir, extensions, root = dir) {
  const found = []
  if (!isDir(dir)) return found
  for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    if (entry.name === 'node_modules' || entry.name === '.git') continue
    const absolute = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      found.push(...listFiles(absolute, extensions, root))
    } else if (entry.isFile() && extensions.includes(path.extname(entry.name))) {
      found.push(path.relative(root, absolute))
    }
  }
  return found
}

/**
 * 去掉 JS 注释，保留字符串与正则字面量内容。仅用于静态扫描，不改变语义。
 *
 * 状态机：code / line / block / single / double / template / regex。
 * 正则字面量必须识别（例如 `/^```/` 里的反引号会打乱模板字符串状态）；
 * 是否进入 regex 用前一个有效 token 的启发式判定：运算符/关键字之后是正则，
 * 标识符、`)`、`]`、字面量之后是除号。
 */
export function stripJsComments(text) {
  let out = ''
  let state = 'code' // code | line | block | single | double | template | regex
  let regexClass = false
  for (let index = 0; index < text.length; index++) {
    const ch = text[index]
    const next = text[index + 1]
    if (state === 'code') {
      if (ch === '/' && next === '/') {
        state = 'line'
        out += '  '
        index++
      } else if (ch === '/' && next === '*') {
        state = 'block'
        out += '  '
        index++
      } else if (ch === '/' && regexCanStart(out)) {
        state = 'regex'
        regexClass = false
        out += ch
      } else {
        if (ch === "'") state = 'single'
        else if (ch === '"') state = 'double'
        else if (ch === '`') state = 'template'
        out += ch
      }
      continue
    }
    if (state === 'line') {
      if (ch === '\n') {
        state = 'code'
        out += '\n'
      } else out += ' '
      continue
    }
    if (state === 'block') {
      if (ch === '*' && next === '/') {
        state = 'code'
        out += '  '
        index++
      } else out += ch === '\n' ? '\n' : ' '
      continue
    }
    if (state === 'regex') {
      out += ch
      if (ch === '\\') {
        out += next ?? ''
        index++
      } else if (ch === '\n') {
        // 正则字面量不会跨行；按除号处理，回到代码状态。
        state = 'code'
      } else if (ch === '[') {
        regexClass = true
      } else if (ch === ']') {
        regexClass = false
      } else if (ch === '/' && !regexClass) {
        state = 'code'
        while (/[a-z]/i.test(text[index + 1] ?? '')) out += text[++index]
      }
      continue
    }
    out += ch
    if (ch === '\\') {
      out += next ?? ''
      index++
      continue
    }
    if (
      (state === 'single' && ch === "'") ||
      (state === 'double' && ch === '"') ||
      (state === 'template' && ch === '`')
    ) {
      state = 'code'
    }
  }
  return out
}

/** 启发式判断 `/` 是否开始正则字面量（基于已扫描代码的最后一个有效 token）。 */
function regexCanStart(out) {
  const tail = out.slice(-80).replace(/\s+$/, '')
  if (tail === '') return true
  const last = tail[tail.length - 1]
  if ('([{,;:=!&|?+-*%^~<>'.includes(last)) return true
  return /\b(?:return|typeof|instanceof|in|of|new|delete|void|throw|case|do|else|yield|await)\s*$/.test(tail)
}

/** 提取静态 import/export-from 与动态 import 的模块说明符。输入应已去注释。 */
export function collectImportSpecifiers(text) {
  const specifiers = []
  const patterns = [
    /\bimport\s+(?:[^'"()]*?\s+from\s+)?(['"])([^'"]+)\1/g,
    /\bexport\s+[^'"]*?\s+from\s+(['"])([^'"]+)\1/g,
    /\bimport\s*\(\s*(['"])([^'"]+)\1\s*\)/g,
  ]
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern)) specifiers.push(match[2])
  }
  return specifiers
}

/**
 * 采集 dsh 包清单的静态事实；不存在 manifest 时返回 `{ present: false }`。
 * 只读；供测试与安装核验复用，避免两处解析逻辑漂移。
 */
export function loadDshManifest(dshDir) {
  const manifestPath = path.join(dshDir, 'package.json')
  if (!isFile(manifestPath)) return { present: false, manifestPath }
  const manifest = readJson(manifestPath)
  return { present: true, manifestPath, manifest }
}

/**
 * 解析并校验 manifest 里的 patch 文件。
 * 返回 `{ path, text, root, rows, problems }`；problems 非空即不通过。
 */
export function inspectPatch(dshDir, manifest, target = PKG_NAME) {
  const problems = []
  const declared = resolveExportTarget(manifest?.dsh?.bundle?.patch)
  if (typeof declared !== 'string' || declared.length === 0) {
    return { path: null, text: '', root: null, rows: [], problems: ['dsh.bundle.patch 未声明或不是字符串'] }
  }
  const patchPath = resolveInsidePackage(dshDir, declared)
  if (!isFile(patchPath)) {
    return { path: patchPath, text: '', root: null, rows: [], problems: [`patch 文件不存在: ${patchPath}`] }
  }
  const text = readFileSync(patchPath, 'utf8')
  if (text.trim() === '') problems.push('patch 文件为空')
  let root = null
  try {
    root = parseYamlLite(text, declared)
  } catch (err) {
    problems.push(`patch 解析失败: ${err.message}`)
  }
  const rows = []
  if (root) {
    const collected = collectPatchInsertRows(root)
    problems.push(...collected.problems)
    rows.push(...collected.rows)
    const named = rows.filter((row) => row.name === target)
    if (named.length === 0) problems.push(`patch 未 insert name=${target} 的行`)
    for (const [index, row] of named.entries()) {
      if (typeof row.id !== 'string' || row.id.trim() === '') {
        problems.push(`patch 第 ${index + 1} 个 ${target} 行缺少非空 id`)
      }
    }
  }
  return { path: patchPath, text, root, rows, problems }
}

/** 校验 locale 元数据：每个已导出的 locale JSON 都必须有非空 meta.title/description。 */
export function inspectLocales(dshDir, manifest, target = PKG_NAME) {
  const problems = []
  const locales = []
  const wildcard = resolveExportTarget(manifest?.exports?.['./locale/*.json'])
  const localeDir = path.join(dshDir, wildcard ? path.dirname(wildcard) : 'locale')
  if (!isDir(localeDir)) {
    return { locales, problems: [`locale 目录不存在: ${localeDir}`] }
  }
  const files = readdirSync(localeDir)
    .filter((name) => name.endsWith('.json'))
    .sort()
  if (files.length === 0) problems.push(`locale 目录没有 JSON 文件: ${localeDir}`)
  for (const name of files) {
    const file = path.join(localeDir, name)
    const locale = { lang: name.slice(0, -'.json'.length), file }
    try {
      const data = readJson(file)
      locale.title = data?.meta?.title
      locale.description = data?.meta?.description
      if (typeof locale.title !== 'string' || locale.title.trim() === '') {
        problems.push(`${name}: meta.title 缺失或为空`)
      }
      if (typeof locale.description !== 'string' || locale.description.trim() === '') {
        problems.push(`${name}: meta.description 缺失或为空`)
      }
    } catch (err) {
      problems.push(`${name}: ${err.message}`)
    }
    locales.push(locale)
  }
  for (const required of ['en.json', 'zh.json']) {
    if (!files.includes(required)) problems.push(`缺少必需 locale: ${required}`)
  }
  return { locales, problems }
}

/**
 * 汇总包内所有展示/运行时资源的可读性（不要求 profile 已安装），
 * 供 `verify-install.mjs` 与最终人工核对使用。
 */
export function inspectPackageResources(dshDir, options = {}) {
  const target = options.packageName ?? PKG_NAME
  const loaded = loadDshManifest(dshDir)
  if (!loaded.present) {
    return { ok: false, problems: [`manifest 不存在: ${loaded.manifestPath}`], manifest: null }
  }
  const manifest = loaded.manifest
  const problems = []

  if (manifest.name !== target) problems.push(`manifest.name=${manifest.name}，期望 ${target}`)
  if (manifest.type !== 'module') problems.push(`manifest.type=${manifest.type}，期望 module`)
  if (manifest.private !== true) problems.push('manifest.private 应为 true')

  const clientTarget = resolveExportTarget(manifest.exports?.['./client'])
  let clientPath = null
  if (typeof clientTarget !== 'string') {
    problems.push('exports["./client"] 未声明或不是字符串目标')
  } else {
    clientPath = resolveInsidePackage(dshDir, clientTarget)
    if (!isFile(clientPath)) problems.push(`client 产物不存在: ${clientPath}`)
    else if (readFileSync(clientPath, 'utf8').trim() === '') problems.push(`client 产物为空: ${clientPath}`)
  }

  const hostTarget = resolveExportTarget(manifest.exports?.['.'])
  let hostPath = null
  if (typeof hostTarget !== 'string') {
    problems.push('exports["."] 未声明或不是字符串目标')
  } else {
    hostPath = resolveInsidePackage(dshDir, hostTarget)
    if (!isFile(hostPath)) problems.push(`Host 入口不存在: ${hostPath}`)
  }

  const iconTarget = resolveExportTarget(manifest.exports?.['./icon']) ?? manifest.icon ?? null
  const iconPath = typeof iconTarget === 'string' ? resolveInsidePackage(dshDir, iconTarget) : null
  const icon = iconPath ? inspectIcon(iconPath) : { ok: false, problems: ['未声明 ./icon 或 package.json icon'], format: null, size: 0 }
  problems.push(...icon.problems)

  const patch = inspectPatch(dshDir, manifest, target)
  problems.push(...patch.problems)

  const locales = inspectLocales(dshDir, manifest, target)
  problems.push(...locales.problems)

  const client = inspectClientBundle(clientPath, target)

  return {
    ok: problems.length === 0,
    problems,
    packageDir: dshDir,
    manifestPath: loaded.manifestPath,
    manifest,
    hostPath,
    clientPath,
    iconPath,
    icon,
    locales: locales.locales,
    patch,
    client,
  }
}

/**
 * 校验 Client 产物：ModuleLoader 注册、id 与包名、无 ESM 顶层语法、可编译。
 * 只做静态/编译检查，不执行 `window` 依赖。
 */
export function inspectClientBundle(clientPath, target = PKG_NAME) {
  const problems = []
  if (!clientPath || !isFile(clientPath)) {
    return { present: false, problems: [`client 产物不存在: ${clientPath}`] }
  }
  const text = readFileSync(clientPath, 'utf8')
  if (text.trim() === '') problems.push('client 产物为空')
  if (!text.includes('window.__ModuleLoader__.load(')) {
    problems.push('client 产物缺少 window.__ModuleLoader__.load( 注册调用')
  }
  const idMatch = text.match(/__ModuleLoader__\.load\(\s*\{[\s\S]{0,2000}?\bid\s*:\s*(['"])([^'"]+)\1/)
  if (!idMatch) {
    problems.push('无法从 ModuleLoader.load({...}) 中提取注册 id')
  } else if (idMatch[2] !== target) {
    problems.push(`client 注册 id=${idMatch[2]}，期望 ${target}`)
  }
  // 注释里可能出现 `import('react')` 之类的类型引用；先剥离注释再做格式判定。
  const code = stripJsComments(text)
  if (/^\s*(?:import|export)\b/m.test(code)) {
    problems.push('client 产物含顶层 import/export，浏览器脚本格式不合法')
  }
  if (/\bimport\s*\(/.test(code)) {
    problems.push('client 产物含动态 import（契约禁止）')
  }
  try {
    // 只编译、不执行；不要传参调用，避免触碰 window。
    new Function(text)
  } catch (err) {
    problems.push(`client 产物语法错误: ${err.message}`)
  }
  const size = Buffer.byteLength(text, 'utf8')
  return { present: true, problems, id: idMatch ? idMatch[2] : null, size }
}
