#!/usr/bin/env node
/**
 * Dependency-free bundler for the CA plugin's browser half.
 *
 * Reads `lib/client/index.js` (ESM), resolves its relative imports, and emits
 * `client.js` in the harness Client ModuleLoader format:
 *
 *   window.__ModuleLoader__.load({ id: '@local/dsh-ca', factory(require) {...} })
 *
 * Supported syntax (deliberately small): default/named/namespace imports from
 * relative ESM modules or the `react` family of platform modules; named,
 * re-exported (`export { a } from './x.js'`) and default exports; no dynamic
 * imports, no `export *`, no JSX, no CSS imports. Anything unsupported fails
 * the build with the offending file and statement.
 */

import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, posix, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PACKAGE_ROOT = resolve(HERE, '..')
const CLIENT_ROOT = join(PACKAGE_ROOT, 'lib', 'client')
const ENTRY_FILE = join(CLIENT_ROOT, 'index.js')
const OUTPUT = join(PACKAGE_ROOT, 'client.js')
const PACKAGE_NAME = '@local/dsh-ca'
const ALLOWED_EXTERNALS = new Set(['react', 'react-dom', 'react/jsx-runtime'])

/** @param {string} file absolute path */
function moduleId(file) {
  return relative(CLIENT_ROOT, file).split('\\').join('/')
}

/** @param {string} fromId @param {string} specifier */
function resolveRelative(fromId, specifier) {
  const base = posix.dirname(fromId)
  const id = posix.normalize(posix.join(base, specifier))
  return id.startsWith('../') ? null : id
}

/** @param {string} file @param {string} message */
function fail(file, message) {
  throw new Error(`build-client: ${moduleId(file)}: ${message}`)
}

/** @param {string} file @param {string} clause */
function parseImportClause(file, clause) {
  let rest = clause.trim()
  let defaultName = null
  let namespaceName = null
  const named = []

  const nsMatch = rest.match(/\*\s+as\s+([A-Za-z_$][\w$]*)/)
  if (nsMatch) {
    namespaceName = nsMatch[1]
    rest = rest.replace(nsMatch[0], '')
  }

  const curlyMatch = rest.match(/\{([\s\S]*)\}/)
  if (curlyMatch) {
    for (const part of curlyMatch[1].split(',')) {
      const entry = part.trim()
      if (!entry) continue
      const asMatch = entry.match(/^([A-Za-z_$][\w$]*)\s+as\s+([A-Za-z_$][\w$]*)$/)
      if (asMatch) named.push([asMatch[1], asMatch[2]])
      else if (/^[A-Za-z_$][\w$]*$/.test(entry)) named.push([entry, entry])
      else fail(file, `unsupported named import entry: ${entry}`)
    }
    rest = rest.replace(curlyMatch[0], '')
  }

  rest = rest.replace(/,/g, ' ').trim()
  if (rest) {
    if (!/^[A-Za-z_$][\w$]*$/.test(rest)) fail(file, `unsupported import clause: ${clause}`)
    defaultName = rest
  }
  return { defaultName, namespaceName, named }
}

/**
 * Transform one ESM module into a factory body for the client module table.
 * @param {string} file absolute path.
 * @param {string} source module source.
 * @returns {{ id: string, code: string, deps: string[] }} transformed module.
 */
function transformModule(file, source) {
  const id = moduleId(file)
  const exportNames = new Set()
  const deps = []
  const declared = new Set()
  let code = source

  // -- exports: collect names and the default shape ----------------------- //
  const defaultDeclaration = code.match(
    /^[ \t]*export\s+default\s+((?:async\s+)?function\s+([A-Za-z_$][\w$]*)|class\s+([A-Za-z_$][\w$]*))/m,
  )
  const defaultIdentifier = code.match(
    /^[ \t]*export\s+default\s+([A-Za-z_$][\w$]*)\s*;?[ \t]*$/m,
  )
  let defaultName = null
  if (defaultDeclaration) {
    defaultName = defaultDeclaration[2] || defaultDeclaration[3]
    if (!defaultName) fail(file, 'default function/class must be named')
  } else if (defaultIdentifier) {
    defaultName = defaultIdentifier[1]
  } else if (/^[ \t]*export\s+default\b/m.test(code)) {
    fail(file, 'only `export default <identifier>` or a named function/class default is supported')
  }
  if (defaultName) exportNames.add('default')

  for (const match of code.matchAll(/^[ \t]*export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)/gm)) {
    exportNames.add(match[1])
    declared.add(match[1])
  }
  for (const match of code.matchAll(/^[ \t]*export\s+class\s+([A-Za-z_$][\w$]*)/gm)) {
    exportNames.add(match[1])
    declared.add(match[1])
  }
  for (const match of code.matchAll(/^[ \t]*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)/gm)) {
    exportNames.add(match[1])
    declared.add(match[1])
  }
  for (const match of code.matchAll(/^[ \t]*export\s*\{([^}]*)\}\s*;?[ \t]*$/gm)) {
    for (const part of match[1].split(',')) {
      const entry = part.trim()
      if (!entry) continue
      const asMatch = entry.match(/^([A-Za-z_$][\w$]*)\s+as\s+([A-Za-z_$][\w$]*)$/)
      if (asMatch) exportNames.add(asMatch[2])
      else if (/^[A-Za-z_$][\w$]*$/.test(entry)) exportNames.add(entry)
      else fail(file, `unsupported named export entry: ${entry}`)
    }
  }

  // -- imports ------------------------------------------------------------ //
  code = code.replace(
    /^[ \t]*import\s+([^'"]*?)\s+from\s+['"]([^'"]+)['"][ \t]*;?/gm,
    (whole, clause, specifier) => {
      const external = ALLOWED_EXTERNALS.has(specifier)
      let target = null
      if (!external) {
        target = resolveRelative(id, specifier)
        if (!target) fail(file, `cannot resolve import "${specifier}"`)
        deps.push(target)
      }
      const { defaultName: localDefault, namespaceName, named } = parseImportClause(file, clause)
      const lines = []
      const load = external ? `__req(${JSON.stringify(specifier)})` : `__m(${JSON.stringify(target)})`
      if (localDefault) {
        declared.add(localDefault)
        lines.push(external ? `const ${localDefault} = ${load}` : `const ${localDefault} = ${load}.default`)
      }
      if (namespaceName) {
        declared.add(namespaceName)
        lines.push(`const ${namespaceName} = ${load}`)
      }
      if (named.length > 0) {
        const bindings = named.map(([imported, local]) => {
          declared.add(local)
          return imported === local ? imported : `${imported}: ${local}`
        })
        lines.push(`const { ${bindings.join(', ')} } = ${load}`)
      }
      return lines.join('\n')
    },
  )

  // Side-effect imports (`import './x.js'`).
  code = code.replace(/^[ \t]*import\s+['"]([^'"]+)['"][ \t]*;?/gm, (whole, specifier) => {
    if (ALLOWED_EXTERNALS.has(specifier)) return `__req(${JSON.stringify(specifier)})`
    const target = resolveRelative(id, specifier)
    if (!target) fail(file, `cannot resolve import "${specifier}"`)
    deps.push(target)
    return `__m(${JSON.stringify(target)})`
  })

  // -- re-exports: `export { a, b as c } from './x.js'` ------------------- //
  code = code.replace(
    /^[ \t]*export\s*\{([\s\S]*?)\}\s+from\s+['"]([^'"]+)['"][ \t]*;?/gm,
    (whole, list, specifier) => {
      const target = resolveRelative(id, specifier)
      if (!target) fail(file, `cannot resolve re-export "${specifier}"`)
      deps.push(target)
      const fresh = []
      for (const part of list.split(',')) {
        const entry = part.trim()
        if (!entry) continue
        const asMatch = entry.match(/^([A-Za-z_$][\w$]*)\s+as\s+([A-Za-z_$][\w$]*)$/)
        const imported = asMatch ? asMatch[1] : entry
        const local = asMatch ? asMatch[2] : entry
        if (!/^[A-Za-z_$][\w$]*$/.test(imported) || !/^[A-Za-z_$][\w$]*$/.test(local)) {
          fail(file, `unsupported re-export entry: ${entry}`)
        }
        exportNames.add(local)
        if (!declared.has(local)) fresh.push(imported === local ? imported : `${imported}: ${local}`)
      }
      return fresh.length > 0
        ? `const { ${fresh.join(', ')} } = __m(${JSON.stringify(target)})`
        : ''
    },
  )

  // -- strip export syntax ------------------------------------------------ //
  if (defaultDeclaration) {
    code = code.replace(defaultDeclaration[0], defaultDeclaration[1])
  }
  if (defaultIdentifier) code = code.replace(defaultIdentifier[0], '')
  code = code.replace(/^[ \t]*export\s*\{[^}]*\}\s*;?[ \t]*$/gm, '')
  code = code.replace(
    /^[ \t]*export\s+(?=(?:async\s+)?function\b|class\b|const\b|let\b|var\b)/gm,
    '',
  )
  if (/^[ \t]*export\s+\*/m.test(code)) fail(file, '`export *` is not supported')

  const tail = [...exportNames].map((name) =>
    name === 'default'
      ? `__exports.default = ${defaultName};`
      : `__exports[${JSON.stringify(name)}] = ${name};`,
  )

  return { id, code: `${code}\n${tail.join('\n')}\n`, deps }
}

const modules = new Map()
const pending = [ENTRY_FILE]
while (pending.length > 0) {
  const file = pending.pop()
  const id = moduleId(file)
  if (modules.has(id)) continue
  const source = readFileSync(file, 'utf8')
  const transformed = transformModule(file, source)
  modules.set(transformed.id, transformed.code)
  for (const dep of transformed.deps) pending.push(join(CLIENT_ROOT, dep))
}

const chunks = []
chunks.push('/* Generated by dsh/scripts/build-client.mjs - do not edit by hand. */')
chunks.push('window.__ModuleLoader__.load({')
chunks.push(`  id: ${JSON.stringify(PACKAGE_NAME)},`)
chunks.push('  factory(require) {')
chunks.push('    const __req = (specifier) => {')
chunks.push(
  '      if (typeof require !== "function") throw new Error("ca-client: require() is unavailable")',
)
chunks.push('      return require(specifier)')
chunks.push('    }')
chunks.push('    const __modules = new Map()')
chunks.push('    const __cache = new Map()')
chunks.push('    function __m(id) {')
chunks.push('      if (__cache.has(id)) return __cache.get(id)')
chunks.push('      const factory = __modules.get(id)')
chunks.push(
  '      if (typeof factory !== "function") throw new Error("ca-client: unknown module " + id)',
)
chunks.push('      const exports = {}')
chunks.push('      __cache.set(id, exports)')
chunks.push('      factory(exports)')
chunks.push('      return exports')
chunks.push('    }')
for (const [id, code] of modules) {
  chunks.push(`    __modules.set(${JSON.stringify(id)}, function (__exports) {`)
  chunks.push(code)
  chunks.push('    })')
}
chunks.push('    const __entry = __m("index.js")')
chunks.push('    return { inject: __entry.inject, apply: __entry.apply }')
chunks.push('  },')
chunks.push('})')
chunks.push('')

writeFileSync(OUTPUT, chunks.join('\n'), 'utf8')
console.log(
  `build-client: wrote ${relative(PACKAGE_ROOT, OUTPUT)} (${modules.size} modules, ${chunks.join('\n').length} bytes)`,
)
