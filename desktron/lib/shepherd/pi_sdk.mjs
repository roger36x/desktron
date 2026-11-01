/** Resolve Pi's two-phase factory by capabilities, not release labels. */
import {existsSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {CapabilityMismatch, requireMethods} from './pi_runtime_contract.mjs';

const PROXY_ENV = ['https_proxy', 'http_proxy', 'all_proxy'];

function proxyRequired(settingsManager) {
  // Desk policy (manual/system) or an inherited proxy variable means a direct
  // connection would bypass the user's network route.
  if (['system', 'manual'].includes(process.env.DESKTRON_NETWORK_MODE)) return true;
  if (Object.keys(process.env).some(k => PROXY_ENV.includes(k.toLowerCase()) && String(process.env[k]).trim())) return true;
  const settings = typeof settingsManager?.getGlobalSettings === 'function' ? settingsManager.getGlobalSettings() : {};
  return Boolean(String(settings?.httpProxy || '').trim());
}

export async function configurePiHttp(root, settingsManager) {
  // SDK imports do not run Pi's CLI setup. Install its native proxy-aware
  // dispatcher before initialization, then apply the loaded global settings.
  // An SDK without the dispatcher is accepted only when no proxy is required.
  const path = join(root, 'dist/core/http-dispatcher.js');
  const http = existsSync(path) ? await import(pathToFileURL(path).href) : null;
  if (typeof http?.configureHttpDispatcher !== 'function' || typeof http?.applyHttpProxySettings !== 'function') {
    if (proxyRequired(settingsManager)) {
      throw new CapabilityMismatch('Pi httpProxy is configured but this Pi SDK cannot apply it (HTTP.configureHttpDispatcher)');
    }
    return false;
  }
  if (settingsManager === undefined) {
    http.configureHttpDispatcher();
  } else {
    requireMethods(settingsManager, ['getGlobalSettings', 'getHttpIdleTimeoutMs'], 'SettingsManager');
    // Explicit Desk policy wins over Pi's private proxy preference, including direct.
    // Legacy users keep Pi's existing precedence until they save a Desk policy.
    const managed = ['system', 'direct', 'manual'].includes(process.env.DESKTRON_NETWORK_MODE);
    http.applyHttpProxySettings(managed ? undefined : settingsManager.getGlobalSettings().httpProxy);
    http.configureHttpDispatcher(settingsManager.getHttpIdleTimeoutMs());
  }
  return true;
}

export async function loadPiFactory(root) {
  // Do not import dist/index.js: its UI/main re-exports are not needed by a
  // headless bridge and may be inconsistent with a separately moved factory.
  const sdk = await import(pathToFileURL(join(root, 'dist/core/sdk.js')).href);
  const names = ['createAgentSessionServices', 'createAgentSessionFromServices'];
  if (names.every(name => typeof sdk[name] === 'function')) return sdk;
  const services = join(root, 'dist/core/agent-session-services.js');
  const factory = existsSync(services) ? await import(pathToFileURL(services).href) : sdk;
  requireMethods(factory, names, 'SDK');
  return factory;
}
