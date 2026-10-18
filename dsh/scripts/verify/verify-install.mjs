#!/usr/bin/env node
/**
 * `@local/dsh-ca` 安装核验（只读）。
 *
 * 检查 DSH profile 中 bundle 是否已登记、node_modules 链接是否指向本仓库
 * `dsh/`，以及安装后可读的 manifest / Host 入口 / client 产物 / patch /
 * locale / icon 是否完整可用。脚本不写任何文件、不启动/停止进程、不调用
 * plugin_manager；运行期可见性（GUI 页面、工具是否注入 Agent）输出为人工
 * 检查清单。
 *
 * 用法：
 *   node dsh/scripts/verify/verify-install.mjs
 *   node dsh/scripts/verify/verify-install.mjs --profile-dir /Users/yu/.dsh/profiles/desktop
 *   node dsh/scripts/verify/verify-install.mjs --json
 *
 * 退出码：0 = 无 FAIL（可有 WARN/SKIP）；1 = 至少一项 FAIL；2 = 用法错误。
 */

import { lstatSync, readFileSync, realpathSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  PKG_NAME,
  inspectClientBundle,
  inspectIcon,
  inspectLocales,
  inspectPatch,
  isDir,
  isFile,
  listFiles,
  readJson,
  resolveExportTarget,
  resolveInsidePackage,
} from './lib/manifest-lib.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
/** 本仓库 dsh/ 目录（脚本上一级再上一级）。 */
const LOCAL_DSH_DIR = path.resolve(here, '..', '..')
const DEFAULT_PROFILE_DIR = '/Users/yu/.dsh/profiles/desktop'

const checks = []
const manualChecklist = [
  '刷新 DSH Web GUI；Settings → Plugins 中出现 “CA Analyzer / CA 源码分析器”，标题、描述、图标正确且无 metadata 诊断/回退。',
  '左侧 sidebar 出现 CA 图标（order 20）；点击后在 main 插槽渲染 CA 整页（无白屏、无 Console 未捕获异常、无 “duplicate module id” 报错）。',
  'Agent 工具列表出现 12 个 ca_*：ca_status / ca_search / ca_symbol / ca_graph / ca_facts / ca_source / ca_stats / ca_callbacks / ca_registry / ca_explain / ca_task / ca_project；ca_project action=list 应返回项目列表与 active 项目；对 Agent 说“查看 CA 项目状态”应调用 ca_status 或 ca_project。',
  '对 Agent 说“用 ca_search 搜索 xv6 的 main 函数”，确认 POST /dsh-ca/ca.request → CA /api 查询链路返回真实数据（需已激活含索引的项目；未鉴权的 curl 预期 401，属正常鉴权，不计为失败）。',
  '项目页（#/projects）：手动新建/编辑/删除/激活项目（name、backend local/remote、sourceRoot、db 或 dbDir 二选一、compdb、python、caRepoPath、SSH host/user/port/keyPath）——项目=源码+数据库+后端位置，不自动扫描；顶栏项目选择器切换项目=激活并重启；旧配置应自动迁移为默认项目；激活后状态 running 且 /api/health 通过；DSH 重启后 projects/activeProjectId 仍在（$DSH_HOME/plugin-data/dsh-ca/settings.json）。',
  'UI 页面：#/projects 项目管理页、顶栏项目选择器、搜索下拉、函数详情（源码/分支/返回/资源）、调用图、CFG、stats、callbacks、tasks、backend（仅低层配置/状态）均可渲染；缩放/平移/折叠等交互正常。陈旧认知：旧的“扫描数据库选择器”主流程已废弃，backend.databases 仅保留为低层辅助端点。',
  '长任务：启动 index/update 任务，日志滚动、状态机、取消均正常；并发限制生效。',
  'AI 解释：在有可用模型配置时点击解释，返回带证据编号的中/英文 markdown；失败路径有可读错误与重试。',
  '远程 Linux 后端：probe → install → start → SSH 隧道健康检查（本机 macOS 未验证，需真实 Linux 主机）。',
  '禁用/卸载插件后：UI 行与工具消失，后台子进程/SSH 隧道/定时器被清理（不阻塞本次验收，但需记录）。',
]
const unverified = [
  '项目页 #/projects 与顶栏项目选择器的视觉/交互（需浏览器内人工确认；Client 产物已重建，需刷新页面加载）。',
  '项目新建/编辑/删除/激活的 UI 实测（Host manager/RPC 层已有单测；页面内实际操作需人工完成）。',
  'UI 视觉与交互（需要浏览器内实际打开页面；本脚本只能验证资源与注册产物可读）。',
  '运行期工具注入与 HTTP 路由端到端（Client↔Host 走同源鉴权 POST /dsh-ca/<endpoint>；未鉴权 curl 预期 401/403，需页面内 fetch 或真实 Agent 工具调用验证；本脚本按约束不调用 plugin_manager）。',
  '远程 Linux 主机安装/启动/隧道/远程项目激活切换（需要 SSH 可达的 Linux 机器；本验收只读观测了已激活 remote 项目状态，未做跨主机写操作）。',
  'AI 解释质量与真实模型调用（需要可用 provider；只验证接口存在，不评估输出质量）。',
]

// ---------------------------------------------------------------------------
// 参数
// ---------------------------------------------------------------------------

function usage(exitCode) {
  const text = [
    '用法: node dsh/scripts/verify/verify-install.mjs [--profile-dir DIR] [--json] [--help]',
    '',
    '只读核验 @local/dsh-ca 是否已安装进 DSH profile。',
    '默认 profile 目录: $DSH_PROFILE_DIR 或 ' + DEFAULT_PROFILE_DIR,
  ].join('\n')
  if (exitCode === 0) {
    console.log(text)
    process.exit(0)
  }
  console.error(text)
  process.exit(exitCode)
}

function parseArgs(argv) {
  const options = { profileDir: process.env.DSH_PROFILE_DIR || DEFAULT_PROFILE_DIR, json: false }
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index]
    if (arg === '--help' || arg === '-h') usage(0)
    else if (arg === '--json') options.json = true
    else if (arg === '--profile-dir') {
      const value = argv[++index]
      if (!value) usage(2)
      options.profileDir = value
    } else if (arg.startsWith('--profile-dir=')) options.profileDir = arg.slice('--profile-dir='.length)
    else usage(2)
  }
  options.profileDir = path.resolve(options.profileDir)
  return options
}

// ---------------------------------------------------------------------------
// 检查记录
// ---------------------------------------------------------------------------

function add(level, id, title, detail = '') {
  checks.push({ level, id, title, detail })
}

const pass = (id, title, detail) => add('PASS', id, title, detail)
const fail = (id, title, detail) => add('FAIL', id, title, detail)
const warn = (id, title, detail) => add('WARN', id, title, detail)
const skip = (id, title, detail) => add('SKIP', id, title, detail)

// ---------------------------------------------------------------------------
// 各项检查
// ---------------------------------------------------------------------------

/** 1. profile manifest 是否登记 bundle。返回 profile manifest 或 null。 */
function checkProfileManifest(profileDir) {
  const profileManifestPath = path.join(profileDir, 'package.json')
  if (!isFile(profileManifestPath)) {
    fail('profile.manifest', 'profile package.json 不可读', profileManifestPath)
    return null
  }
  let profile
  try {
    profile = readJson(profileManifestPath)
  } catch (err) {
    fail('profile.manifest', 'profile package.json 解析失败', err.message)
    return null
  }
  const bundles = profile?.dsh?.profile?.bundles
  if (!Array.isArray(bundles)) {
    fail('profile.bundles', 'profile package.json 缺少 dsh.profile.bundles 数组', profileManifestPath)
  } else if (!bundles.includes(PKG_NAME)) {
    fail(
      'profile.bundles',
      `dsh.profile.bundles 未包含 ${PKG_NAME}`,
      `当前 bundles=${JSON.stringify(bundles)}（Lead 尚未安装或安装失败）`,
    )
  } else {
    pass('profile.bundles', `dsh.profile.bundles 包含 ${PKG_NAME}`, `bundles 共 ${bundles.length} 项`)
  }

  const dependency = profile?.dependencies?.[PKG_NAME] ?? profile?.devDependencies?.[PKG_NAME]
  if (dependency) {
    pass('profile.dependency', `profile dependencies 已登记 ${PKG_NAME}`, String(dependency))
  } else {
    warn('profile.dependency', `profile dependencies 未登记 ${PKG_NAME}`, '若为符号链接+本地目录安装可忽略；请核对安装方式')
  }

  const lockPath = path.join(profileDir, 'pnpm-lock.yaml')
  if (isFile(lockPath)) {
    const lockText = readFileSync(lockPath, 'utf8')
    if (lockText.includes(PKG_NAME)) {
      pass('profile.lock', 'pnpm-lock.yaml 记录 @local/dsh-ca', lockPath)
    } else {
      warn('profile.lock', 'pnpm-lock.yaml 未记录 @local/dsh-ca', '可能是本地链接安装，建议核对')
    }
  } else {
    skip('profile.lock', 'pnpm-lock.yaml 不存在', lockPath)
  }
  return profile
}

/** 2. node_modules 链接与安装目录。返回安装目录或 null。 */
function checkInstalledLink(profileDir) {
  const linkPath = path.join(profileDir, 'node_modules', ...PKG_NAME.split('/'))
  let stat
  try {
    stat = lstatSync(linkPath)
  } catch {
    fail('installed.link', `node_modules 中不存在 ${PKG_NAME} 链接/目录`, linkPath)
    return null
  }
  const kind = stat.isSymbolicLink() ? 'symlink' : stat.isDirectory() ? 'directory' : 'other'
  if (kind === 'other') {
    fail('installed.link', `${PKG_NAME} 不是链接或目录`, `${linkPath} (${kind})`)
    return null
  }
  let resolved
  try {
    resolved = realpathSync(linkPath)
  } catch (err) {
    fail('installed.link', `${PKG_NAME} 链接目标不可解析（悬空链接？）`, `${linkPath}: ${err.message}`)
    return null
  }
  if (!isDir(resolved)) {
    fail('installed.link', `${PKG_NAME} 链接目标不是目录`, resolved)
    return null
  }
  if (resolved !== LOCAL_DSH_DIR) {
    warn(
      'installed.link',
      `${PKG_NAME} 链接指向的目录与工作区 dsh/ 不一致`,
      `链接目标=${resolved}；工作区=${LOCAL_DSH_DIR}（可能是另一份 checkout，核对是否要验证的对象）`,
    )
  } else {
    pass('installed.link', `${PKG_NAME} 链接存在且指向工作区 dsh/`, `${kind} → ${resolved}`)
  }
  return resolved
}

/** 3. 安装目录内 manifest 与 Host/Client 产物。 */
function checkInstalledPackage(installedDir) {
  if (!installedDir) {
    skip('installed.manifest', '未安装，跳过安装目录内容检查')
    return
  }
  const manifestPath = path.join(installedDir, 'package.json')
  let manifest
  try {
    manifest = readJson(manifestPath)
  } catch (err) {
    fail('installed.manifest', '安装目录 package.json 不可读', err.message)
    return
  }
  if (manifest.name !== PKG_NAME) {
    fail('installed.manifest', `安装目录 name 不是 ${PKG_NAME}`, String(manifest.name))
    return
  }
  if (manifest.type !== 'module') {
    fail('installed.manifest', '安装目录 package.json 缺少 type=module', JSON.stringify(manifest.type))
  } else {
    pass('installed.manifest', '安装目录 manifest 可读且身份正确', `name=${manifest.name} version=${manifest.version}`)
  }

  // Host 入口
  const hostTarget = resolveExportTarget(manifest.exports?.['.'])
  if (typeof hostTarget !== 'string') {
    fail('installed.host-entry', 'manifest 缺少 exports["."] Host 入口', '')
  } else {
    const hostPath = resolveInsidePackage(installedDir, hostTarget)
    let text = null
    try {
      text = readFileSync(hostPath, 'utf8')
    } catch (err) {
      fail('installed.host-entry', 'Host 入口不可读', `${hostPath}: ${err.message}`)
    }
    if (text !== null) {
      const injectMatch = text.match(/export\s+const\s+inject\s*=\s*\[([^\]]*)\]/)
      const services = injectMatch ? [...injectMatch[1].matchAll(/['"]([^'"]+)['"]/g)].map((item) => item[1]) : []
      const requiredServices = ['tools', 'connection', 'webServer']
      if (!injectMatch) {
        fail('installed.host-entry', 'Host 入口缺少 export const inject', hostPath)
      } else if (!requiredServices.every((required) => services.includes(required))) {
        fail(
          'installed.host-entry',
          'Host 入口 inject 未包含 tools/connection/webServer（connection.rpc 需要 webServer）',
          JSON.stringify(services),
        )
      } else {
        pass('installed.host-entry', 'Host 入口可读且 inject 含 tools/connection/webServer', hostPath)
      }
    }
  }

  // Client 产物
  const clientTarget = resolveExportTarget(manifest.exports?.['./client'])
  if (typeof clientTarget !== 'string') {
    fail('installed.client', 'manifest 缺少 exports["./client"]', '')
  } else {
    const clientPath = resolveInsidePackage(installedDir, clientTarget)
    const result = inspectClientBundle(clientPath, PKG_NAME)
    if (result.problems.length > 0) {
      fail('installed.client', 'client 产物校验失败', result.problems.join('；'))
    } else {
      pass('installed.client', 'client 产物可读、id 正确、可编译', `${clientPath} (${result.size} 字节)`)
    }
  }

  // client 产物新鲜度：源模块比 client.js 新说明打包产物可能过期（Lead 负责重建）。
  const clientDir = path.join(installedDir, 'lib', 'client')
  const freshClientTarget = resolveExportTarget(manifest.exports?.['./client'])
  if (typeof freshClientTarget === 'string' && isDir(clientDir)) {
    const clientFilePath = resolveInsidePackage(installedDir, freshClientTarget)
    try {
      const bundleMtime = statSync(clientFilePath).mtimeMs
      const sources = listFiles(clientDir, ['.js', '.mjs'])
      const stale = sources
        .map((relative) => ({ relative, mtime: statSync(path.join(clientDir, relative)).mtimeMs }))
        .filter((entry) => entry.mtime > bundleMtime)
        .map((entry) => entry.relative)
      if (stale.length > 0) {
        warn(
          'installed.bundle-freshness',
          `client.js 可能过期：${stale.length} 个源模块比产物新`,
          `需运行 node scripts/build-client.mjs 重新打包；最新源=${stale.slice(0, 3).join(', ')}`,
        )
      } else {
        pass('installed.bundle-freshness', 'client.js 不早于任何 Client 源模块', `${sources.length} 个源模块`)
      }
    } catch (err) {
      warn('installed.bundle-freshness', '无法比较 client.js 与源模块时间戳', err.message)
    }
  }

  // bundle patch
  const patch = inspectPatch(installedDir, manifest, PKG_NAME)
  if (patch.problems.length > 0) {
    fail('installed.patch', 'cordis.patch.yml 校验失败', patch.problems.join('；'))
  } else {
    pass('installed.patch', 'cordis.patch.yml 可解析且 insert 了 Host 行', `${patch.path} (${patch.rows.length} 行)`)
  }

  // locale 展示元数据
  const locales = inspectLocales(installedDir, manifest, PKG_NAME)
  if (locales.problems.length > 0) {
    fail('installed.locale', 'locale 展示元数据校验失败', locales.problems.join('；'))
  } else {
    const langs = locales.locales.map((locale) => `${locale.lang}:${locale.title}`).join(', ')
    pass('installed.locale', 'locale meta.title/description 完整', langs)
  }

  // icon 展示资源
  const iconTarget = resolveExportTarget(manifest.exports?.['./icon']) ?? manifest.icon
  if (typeof iconTarget !== 'string') {
    fail('installed.icon', 'manifest 未声明 ./icon 或 icon', '')
  } else {
    const iconPath = resolveInsidePackage(installedDir, iconTarget)
    const icon = inspectIcon(iconPath)
    if (!icon.ok) {
      fail('installed.icon', 'icon 校验失败', icon.problems.join('；'))
    } else {
      pass('installed.icon', 'icon 可读、格式有效、体积合规', `${iconPath} (${icon.format}, ${icon.size} 字节)`)
    }
  }
}

/** 4. 运行期可见性无法静态证明，输出 WARN 并附人工清单。 */
function reportRuntimeVisibility(profileDir) {
  warn(
    'runtime.gui',
    'GUI 渲染、工具注入与 RPC 需人工核对（本脚本按约束不调用 plugin_manager / inspect）',
    `profile=${profileDir}；见下方人工检查清单，或由 Lead 用 cordis_inspect_query 查询 Slots/Tool。`,
  )
  if (isDir(profileDir)) {
    pass('profile.dir', 'profile 目录存在', profileDir)
  } else {
    fail('profile.dir', 'profile 目录不存在', profileDir)
  }
}

// ---------------------------------------------------------------------------
// 输出
// ---------------------------------------------------------------------------

const LEVEL_ICON = { PASS: '✔ PASS', FAIL: '✖ FAIL', WARN: '! WARN', SKIP: '- SKIP' }

function printReport(options, extra) {
  console.log('CA plugin install verifier — @local/dsh-ca（只读）')
  console.log(`profile dir : ${options.profileDir}`)
  console.log(`workspace   : ${LOCAL_DSH_DIR}`)
  console.log(`date        : ${new Date().toISOString()}`)
  console.log('')
  for (const check of checks) {
    console.log(`${LEVEL_ICON[check.level].padEnd(7)} ${check.id.padEnd(24)} ${check.title}`)
    if (check.detail) console.log(`        └─ ${check.detail}`)
  }
  const summary = summarize()
  console.log('')
  console.log(
    `Summary: ${summary.PASS} pass, ${summary.FAIL} fail, ${summary.WARN} warn, ${summary.SKIP} skip` +
      (summary.ok ? '' : ' — 安装未通过核验'),
  )
  console.log('')
  console.log('人工检查清单（脚本不可自动验证）：')
  manualChecklist.forEach((item, index) => console.log(`  ${index + 1}. ${item}`))
  console.log('')
  console.log('未验证项（如实记录，不视为通过）：')
  unverified.forEach((item) => console.log(`  - ${item}`))
  if (extra?.notes) {
    console.log('')
    for (const note of extra.notes) console.log(`note: ${note}`)
  }
  console.log('')
  console.log(`RESULT: ${summary.ok ? 'PASS' : 'FAIL'}`)
}

function summarize() {
  const counts = { PASS: 0, FAIL: 0, WARN: 0, SKIP: 0 }
  for (const check of checks) counts[check.level]++
  return { ...counts, ok: counts.FAIL === 0 }
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------

const options = parseArgs(process.argv.slice(2))
const profile = checkProfileManifest(options.profileDir)
reportRuntimeVisibility(options.profileDir)
const installedDir = checkInstalledLink(options.profileDir)
checkInstalledPackage(installedDir)
if (profile) {
  const bundles = profile?.dsh?.profile?.bundles
  if (Array.isArray(bundles) && bundles.includes(PKG_NAME) && !installedDir) {
    fail('installed.consistency', 'bundles 已登记但 node_modules 链接缺失', '安装状态不一致，建议重新 install_bundle')
  }
}

const summary = summarize()
if (options.json) {
  console.log(
    JSON.stringify(
      {
        packageName: PKG_NAME,
        profileDir: options.profileDir,
        workspaceDshDir: LOCAL_DSH_DIR,
        checks,
        summary,
        manualChecklist,
        unverified,
      },
      null,
      2,
    ),
  )
} else {
  printReport(options)
}
process.exit(summary.ok ? 0 : 1)
