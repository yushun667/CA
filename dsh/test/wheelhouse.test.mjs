import assert from 'node:assert/strict'
import { readdirSync } from 'node:fs'
import test from 'node:test'

import {
  DEFAULT_CA_WHEEL_DIR,
  PLATFORM_SPECS,
  buildDownloadArgs,
  detectHostPlatform,
  findBundledCaWheel,
} from '../scripts/build-wheelhouse.mjs'

test('every supported platform maps to pip download tags', () => {
  for (const platform of Object.keys(PLATFORM_SPECS)) {
    const { pipArgs } = buildDownloadArgs({
      platform,
      requirements: 'requirements.txt',
      outDir: 'out',
      pythonVersion: '3.11',
    })
    assert.equal(pipArgs[0], 'download')
    assert.ok(pipArgs.includes('--only-binary=:all:'))
    assert.ok(pipArgs.includes('--platform'))
    assert.ok(pipArgs.includes('cp311'), `${platform} pins the CPython 3.11 ABI`)
  }
})

test('linux-x64 accepts modern and legacy manylinux tags', () => {
  const { pipArgs } = buildDownloadArgs({
    platform: 'linux-x64',
    requirements: 'requirements.txt',
    outDir: 'out',
  })
  for (const tag of PLATFORM_SPECS['linux-x64'].platforms) assert.ok(pipArgs.includes(tag))
})

test('an unknown platform is rejected with the supported list', () => {
  assert.throws(
    () => buildDownloadArgs({ platform: 'solaris-sparc', requirements: 'x', outDir: 'out' }),
    /unknown platform/,
  )
})

test('the host platform is one of the supported ids', () => {
  assert.ok(Object.keys(PLATFORM_SPECS).includes(detectHostPlatform()))
})

test('the CA wheel is baked into the plugin bundle', () => {
  const wheel = findBundledCaWheel(DEFAULT_CA_WHEEL_DIR)
  assert.ok(wheel, 'expected dsh/backend/wheels/ca_analyzer-*.whl')
  assert.match(wheel, /ca_analyzer-.*\.whl$/)
  assert.ok(readdirSync(DEFAULT_CA_WHEEL_DIR).includes(wheel.split('/').pop()))
})
