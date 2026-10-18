/**
 * Thin executor around local `ssh`/`rsync`/`pip` invocations for the remote
 * backend. All argv construction is delegated to ./commands.js; this module
 * only runs processes and logs them.
 *
 * Tests inject `deps.runProcess` (one-shot) and `deps.spawn` (long-lived
 * serve/tunnel processes) so no real SSH connection is required.
 */

import { spawn as nodeSpawn } from 'node:child_process';

import { buildChildEnv, runProcess } from '../process.js';
import { buildSshArgv } from './commands.js';

/**
 * @param {object} [options]
 * @param {object} [options.deps] injectables: runProcess, spawn, sshBinary
 * @param {any} [options.logger]
 */
export function createRemoteExecutor(options = {}) {
  const deps = options.deps || {};
  const logger = options.logger || null;
  const runProcessFn = typeof deps.runProcess === 'function' ? deps.runProcess : runProcess;
  // Default to the real child_process spawn so a manager created without any
  // injected deps still works in production.
  const spawnFn = typeof deps.spawn === 'function' ? deps.spawn : nodeSpawn;
  const sshBinary = deps.sshBinary || 'ssh';
  /** @type {Array<{kind: string, target?: string, command: string, args: string[], script?: string}>} */
  const calls = [];

  async function run(command, args, runOptions = {}) {
    const logLine = runOptions.label ? `[remote] ${runOptions.label}: ${command}` : `[remote] ${command}`;
    logger?.info?.(logLine);
    return runProcessFn({
      spawn: spawnFn,
      command,
      args,
      cwd: runOptions.cwd,
      env: runOptions.env,
      timeoutMs: runOptions.timeoutMs,
      signal: runOptions.signal,
      logger,
      maxOutputBytes: runOptions.maxOutputBytes || 512 * 1024,
      graceMs: runOptions.graceMs,
      onStdoutLine: runOptions.onStdoutLine,
      onStderrLine: runOptions.onStderrLine,
    });
  }

  /**
   * Execute a shell script on the remote host through one ssh invocation.
   * @param {string|object} sshInput
   * @param {string} script
   * @param {{timeoutMs?: number, signal?: AbortSignal, onStdoutLine?: Function,
   *          onStderrLine?: Function, fallbackSsh?: object|null, label?: string, maxOutputBytes?: number}} [runOptions]
   */
  async function runSsh(sshInput, script, runOptions = {}) {
    // Optional password auth: the manager injects a resolver that creates a
    // short-lived SSH_ASKPASS session (password never logged here).
    let session = null;
    if (typeof deps.resolveSshAuth === 'function') {
      try {
        session = await deps.resolveSshAuth(sshInput);
      } catch (authError) {
        throw authError;
      }
    }
    try {
      const built = buildSshArgv(session ? session.ssh : sshInput, script, {
        sshBinary,
        fallback: runOptions.fallbackSsh || null,
        batchMode: session ? session.batchMode : true,
        extraOptions: session?.extraOptions || [],
      });
      calls.push({
        kind: 'ssh',
        target: built.ssh.target,
        command: built.command,
        args: built.args,
        script: String(script),
      });
      return await run(built.command, built.args, {
        ...runOptions,
        env: buildChildEnv(session?.env || {}, deps.env || process.env),
        label: runOptions.label || `ssh ${built.ssh.target}`,
      });
    } finally {
      try {
        session?.cleanup?.();
      } catch {
        // helper cleanup is best effort
      }
    }
  }

  /**
   * Run a local command (rsync/ssh-free helpers).
   * @param {string} command
   * @param {string[]} args
   * @param {object} [runOptions]
   */
  async function runLocal(command, args, runOptions = {}) {
    calls.push({ kind: 'local', command, args: args.map(String), script: undefined });
    return run(command, args, runOptions);
  }

  return {
    runSsh,
    runLocal,
    calls,
    sshBinary,
    /** Snapshot of every command issued (tests/diagnostics). */
    history() {
      return calls.map((call) => ({ ...call, args: call.args.slice() }));
    },
  };
}
