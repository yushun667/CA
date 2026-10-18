/**
 * Local directory browsing for the project picker.
 *
 * The DSH host runs on the user's machine, so it can enumerate real directories
 * for the "choose a local source directory" flow without relying on a native
 * dialog service (which is unavailable in some host combinations). The list is
 * bounded and errors are normalized to the plugin's stable error codes.
 */

import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { BackendError } from './errors.js'

/** Maximum number of directory entries returned per call. */
export const MAX_LOCAL_BROWSE_ENTRIES = 500

/** Maximum accepted path length. */
export const MAX_LOCAL_PATH_LENGTH = 4096

/**
 * Expand a leading `~` against a home directory.
 * @param {string} input
 * @param {string} home
 * @returns {string}
 */
export function expandHomePath(input, home = os.homedir()) {
  const text = String(input == null ? '' : input).trim()
  if (text === '~') return home
  if (text.startsWith('~/') || text.startsWith('~\\')) return path.join(home, text.slice(2))
  return text
}

function normalizeError(error, target) {
  if (error instanceof BackendError) return error
  const code = error && error.code
  if (code === 'ENOENT' || code === 'ENOTDIR') {
    return new BackendError('DIR_NOT_FOUND', `目录不存在: ${target}`, { path: target })
  }
  if (code === 'EACCES' || code === 'EPERM') {
    return new BackendError('DIR_NOT_READABLE', `没有权限读取目录: ${target}`, { path: target })
  }
  return new BackendError('LOCAL_BROWSE_FAILED', `无法读取目录 ${target}: ${error && error.message ? error.message : error}`, {
    path: target,
  })
}

/**
 * List the child directories of a local directory.
 *
 * @param {object} [options]
 * @param {string} [options.path] absolute path or `~` path; defaults to home.
 * @param {string} [options.home] home directory override (tests).
 * @param {number} [options.limit] entry cap.
 * @returns {Promise<{path:string,parent:string|null,home:string,isHome:boolean,entries:Array<{name:string,path:string}>,truncated:boolean}>}
 */
export async function browseLocalDirs(options = {}) {
  const home = options.home || os.homedir()
  const rawPath = options.path === undefined || options.path === null ? '' : String(options.path)
  if (rawPath.length > MAX_LOCAL_PATH_LENGTH) {
    throw new BackendError('INVALID_ARGUMENT', `路径过长（最多 ${MAX_LOCAL_PATH_LENGTH} 字符）`, { path: rawPath })
  }
  if (/[\0\r\n]/.test(rawPath)) {
    throw new BackendError('INVALID_ARGUMENT', '路径不能包含 NUL 或换行符', { path: rawPath })
  }
  const expanded = expandHomePath(rawPath, home) || home
  if (!path.isAbsolute(expanded)) {
    throw new BackendError('INVALID_ARGUMENT', `路径必须是绝对路径: ${expanded}`, { path: expanded })
  }
  const target = path.resolve(expanded)
  let stat
  try {
    stat = await fs.stat(target)
  } catch (error) {
    throw normalizeError(error, target)
  }
  if (!stat.isDirectory()) {
    throw new BackendError('DIR_NOT_A_DIRECTORY', `不是目录: ${target}`, { path: target })
  }
  let dirents
  try {
    dirents = await fs.readdir(target, { withFileTypes: true })
  } catch (error) {
    throw normalizeError(error, target)
  }
  const limit = Math.max(1, Math.min(Number(options.limit) || MAX_LOCAL_BROWSE_ENTRIES, MAX_LOCAL_BROWSE_ENTRIES))
  const entries = []
  for (const entry of dirents) {
    if (entry.name.startsWith('.')) continue
    let isDirectory = entry.isDirectory()
    if (!isDirectory && entry.isSymbolicLink()) {
      try {
        isDirectory = (await fs.stat(path.join(target, entry.name))).isDirectory()
      } catch {
        isDirectory = false
      }
    }
    if (!isDirectory) continue
    entries.push({ name: entry.name, path: path.join(target, entry.name) })
  }
  entries.sort((left, right) => left.name.localeCompare(right.name))
  const truncated = entries.length > limit
  const parentPath = path.dirname(target)
  const parent = parentPath === target ? null : parentPath
  return {
    path: target,
    parent,
    home,
    isHome: path.resolve(home) === target,
    entries: truncated ? entries.slice(0, limit) : entries,
    truncated,
  }
}
