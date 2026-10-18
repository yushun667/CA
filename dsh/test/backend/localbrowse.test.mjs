import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import {
  browseLocalDirs,
  expandHomePath,
  MAX_LOCAL_BROWSE_ENTRIES,
} from '../../lib/host/backend/localbrowse.js'
import { cleanupDir, makeTempDir } from './helpers/fakes.mjs'

function makeTree() {
  const root = makeTempDir('dshca-browse-')
  fs.mkdirSync(path.join(root, 'alpha'))
  fs.mkdirSync(path.join(root, 'beta'))
  fs.mkdirSync(path.join(root, '.hidden'))
  fs.writeFileSync(path.join(root, 'file.txt'), 'x')
  return root
}

test('browseLocalDirs lists directories, not files or dot dirs', async () => {
  const root = makeTree()
  try {
    const result = await browseLocalDirs({ path: root })
    assert.equal(result.path, path.resolve(root))
    assert.equal(result.isHome, false)
    assert.deepEqual(result.entries.map((entry) => entry.name), ['alpha', 'beta'])
    assert.equal(result.parent, path.dirname(path.resolve(root)))
  } finally {
    cleanupDir(root)
  }
})

test('browseLocalDirs defaults to home and expands ~ paths', async () => {
  const root = makeTree()
  try {
    const home = path.join(root, 'alpha')
    const viaHome = await browseLocalDirs({ home })
    assert.equal(viaHome.path, path.resolve(home))
    assert.equal(viaHome.isHome, true)
    const viaTilde = await browseLocalDirs({ home, path: '~' })
    assert.equal(viaTilde.path, path.resolve(home))
    const viaTildeChild = await browseLocalDirs({ home: root, path: '~/beta' })
    assert.equal(viaTildeChild.path, path.resolve(root, 'beta'))
  } finally {
    cleanupDir(root)
  }
})

test('browseLocalDirs normalizes path errors', async () => {
  const root = makeTree()
  try {
    await assert.rejects(() => browseLocalDirs({ path: path.join(root, 'missing') }), /DIR_NOT_FOUND|目录不存在/)
    await assert.rejects(() => browseLocalDirs({ path: path.join(root, 'file.txt') }), /DIR_NOT_A_DIRECTORY|不是目录/)
    await assert.rejects(() => browseLocalDirs({ path: 'relative/dir' }), /INVALID_ARGUMENT|绝对路径/)
  } finally {
    cleanupDir(root)
  }
})

test('browseLocalDirs caps the entry list', async () => {
  const root = makeTempDir('dshca-browse-many-')
  try {
    for (let index = 0; index < 5; index += 1) fs.mkdirSync(path.join(root, `dir-${index}`))
    const result = await browseLocalDirs({ path: root, limit: 2 })
    assert.equal(result.entries.length, 2)
    assert.equal(result.truncated, true)
    assert.ok(MAX_LOCAL_BROWSE_ENTRIES >= result.entries.length)
  } finally {
    cleanupDir(root)
  }
})

test('expandHomePath handles ~ and plain paths', () => {
  assert.equal(expandHomePath('~', '/home/u'), '/home/u')
  assert.equal(expandHomePath('~/src', '/home/u'), path.join('/home/u', 'src'))
  assert.equal(expandHomePath('/abs/path', '/home/u'), '/abs/path')
  assert.equal(expandHomePath('', '/home/u'), '')
})
