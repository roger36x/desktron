import http from 'node:http';

export class AppControlClientError extends Error {
  constructor(code, message, options = {}) {
    super(message || code);
    this.name = 'AppControlClientError';
    this.code = code;
    this.status = Number(options.status || 0);
    this.retryable = Boolean(options.retryable);
  }
}

const TERMINAL = new Set(['succeeded', 'failed', 'rejected', 'cancelled']);

export class AppControlClient {
  constructor(options = {}) {
    this.socketPath = String(options.socketPath || '');
    this.sessionId = String(options.sessionId || '');
    this.capability = String(options.capability || '');
    this.timeoutMs = Math.max(1000, Number(options.timeoutMs || 30_000));
    if (!this.socketPath || !this.sessionId || !this.capability) {
      throw new AppControlClientError('browser_unavailable', 'Desk 会话未提供后台浏览器控制通道', { retryable: true });
    }
  }

  async request(path, body, options = {}) {
    const data = body === undefined ? null : Buffer.from(JSON.stringify(body));
    const timeoutMs = Math.max(1, Number(options.timeoutMs || this.timeoutMs));
    return await new Promise((resolve, reject) => {
      let settled = false;
      let deadlineTimer;
      const finish = (error, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(deadlineTimer);
        options.signal?.removeEventListener('abort', abort);
        error ? reject(error) : resolve(value);
      };
      const request = http.request({
        ...channelTarget(this.socketPath), path, method: data ? 'POST' : 'GET',
        headers: {
          'Content-Type': 'application/json',
          'X-Desk-App-Control-Session': this.capability,
          ...(data ? { 'Content-Length': data.length } : {}),
        }, timeout: timeoutMs,
      }, response => {
        const chunks = [];
        let size = 0;
        response.on('error', () => finish(new AppControlClientError('browser_protocol_error', '后台浏览器响应中断')));
        response.on('data', chunk => {
          size += chunk.length;
          if (size > 1024 * 1024) request.destroy(new Error('response_too_large'));
          else chunks.push(chunk);
        });
        response.on('end', () => {
          let value = {};
          try { value = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'); }
          catch { return finish(new AppControlClientError('browser_protocol_error', '后台浏览器返回了无效 JSON')); }
          if ((response.statusCode || 500) >= 400) {
            return finish(new AppControlClientError('browser_operation_failed', String(value.error || `HTTP ${response.statusCode}`),
              { status: response.statusCode, retryable: (response.statusCode || 0) >= 500 }));
          }
          finish(null, value);
        });
      });
      const abort = () => request.destroy(Object.assign(new Error('aborted'), { name: 'AbortError' }));
      deadlineTimer = setTimeout(() => request.destroy(new Error('timeout')), timeoutMs);
      options.signal?.addEventListener('abort', abort, { once: true });
      request.on('timeout', () => request.destroy(new Error('timeout')));
      request.on('error', error => {
        if (error?.name === 'AbortError') return finish(new AppControlClientError('aborted', '请求已取消'));
        finish(new AppControlClientError('browser_unavailable', `Desk 后台浏览器不可用：${String(error?.message || error)}`,
          { retryable: true }));
      });
      if (data) request.write(data);
      request.end();
      if (options.signal?.aborted) abort();
    });
  }

  async wait(operation, options = {}) {
    let current = operation;
    const deadline = performance.now() + Math.max(1, Number(options.timeoutMs || this.timeoutMs));
    if (options.signal?.aborted) throw new AppControlClientError('aborted', '请求已取消');
    while (!TERMINAL.has(current?.status)) {
      if (typeof current?.id !== 'string' || !current.id) {
        throw new AppControlClientError('browser_protocol_error', '后台浏览器缺少操作标识');
      }
      const left = deadline - performance.now();
      if (left <= 0) throw new AppControlClientError('browser_timeout', '后台浏览器操作超时', { retryable: true });
      const seconds = Math.max(1, Math.min(25, Math.ceil(left / 1000)));
      const query = new URLSearchParams({ timeout: String(seconds), session_id: this.sessionId });
      current = await this.request(`/workbench/app-control/operations/${encodeURIComponent(current.id)}/wait?${query}`,
        undefined, { ...options, timeoutMs: Math.min(left, (seconds + 3) * 1000) });
    }
    if (performance.now() > deadline) throw new AppControlClientError('browser_timeout', '后台浏览器操作超时', { retryable: true });
    return current;
  }

  async open(url, options = {}) {
    const started = performance.now();
    if (options.signal?.aborted) throw new AppControlClientError('aborted', '请求已取消');
    const operation = await this.request('/workbench/app-control/open', {
      session_id: this.sessionId, app_id: 'browser', url, isolated: true,
      allowed_main_frame_hosts: Array.isArray(options.allowedMainFrameHosts)
        ? options.allowedMainFrameHosts : [],
      intent: options.intent || '后台匿名读取公开网页',
    // Do not sever the creation receipt on caller cancellation. Finish the bounded
    // submission so onLease can hand the lease back for cleanup, then cancel wait.
    }, { ...options, signal: undefined });
    options.onLease?.(operation?.lease_id);
    return await this.wait(operation, { ...options,
      timeoutMs: Math.max(1, (options.timeoutMs || this.timeoutMs) - (performance.now() - started)) });
  }

  async inspect(leaseId, input, options = {}) {
    const started = performance.now();
    const operation = await this.request('/workbench/app-control/inspect', {
      lease_id: leaseId, session_id: this.sessionId, observation: 'document', input,
      settle_ms: Math.max(0, Math.min(5000, Number(options.settleMs ?? 5000))), intent: '读取公开搜索结果',
    }, options);
    return await this.wait(operation, { ...options,
      timeoutMs: Math.max(1, (options.timeoutMs || this.timeoutMs) - (performance.now() - started)) });
  }

  async close(leaseId, idempotencyKey, options = {}) {
    const started = performance.now();
    const operation = await this.request('/workbench/app-control/act', {
      lease_id: leaseId, session_id: this.sessionId, action: 'close', input: {},
      idempotency_key: idempotencyKey, intent: '关闭本次匿名搜索标签',
    }, options);
    return await this.wait(operation, { ...options,
      timeoutMs: Math.max(1, (options.timeoutMs || this.timeoutMs) - (performance.now() - started)) });
  }

  async yield(leaseId, options = {}) {
    return await this.request('/workbench/app-control/yield', {
      lease_id: leaseId, session_id: this.sessionId,
    }, options);
  }
}

// POSIX 为 Unix Socket 路径；Windows 为 `tcp:127.0.0.1:<端口>` 的专用回环通道。
export function channelTarget(address) {
  const value = String(address || '');
  const match = /^tcp:127\.0\.0\.1:(\d{1,5})$/.exec(value);
  if (match) return { host: '127.0.0.1', port: Number(match[1]) };
  if (value.startsWith('tcp:')) throw new AppControlClientError('browser_unavailable', '本地控制通道地址无效');
  return { socketPath: value };
}

export function createAppControlClientFromEnv(env = process.env, options = {}) {
  if (!env.DESKTRON_APP_CONTROL_SOCKET || !env.DESKTRON_SESSION_ID ||
      !env.DESKTRON_APP_CONTROL_SESSION_CAPABILITY) return null;
  return new AppControlClient({
    socketPath: env.DESKTRON_APP_CONTROL_SOCKET,
    sessionId: env.DESKTRON_SESSION_ID,
    capability: env.DESKTRON_APP_CONTROL_SESSION_CAPABILITY,
    timeoutMs: options.timeoutMs,
  });
}
