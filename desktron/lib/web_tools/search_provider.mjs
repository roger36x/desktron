export const SEARCH_TRANSPORT_DIRECT_HTTP = 'direct_http';
export const SEARCH_TRANSPORT_BROWSER_APP_CONTROL = 'app_control_browser';

export class SearchProviderError extends Error {
  constructor(code, message, options = {}) {
    super(message || code);
    this.name = 'SearchProviderError';
    this.code = code;
    this.retryable = Boolean(options.retryable);
    this.browserRecommended = Boolean(options.browserRecommended);
  }
}

export function assertSearchProvider(provider) {
  if (!provider || typeof provider !== 'object' ||
      typeof provider.id !== 'string' || !provider.id.trim() ||
      ![SEARCH_TRANSPORT_DIRECT_HTTP, SEARCH_TRANSPORT_BROWSER_APP_CONTROL].includes(provider.transport) ||
      !Number.isSafeInteger(provider.limitMax) || provider.limitMax < 1 || provider.limitMax > 10 ||
      (provider.transport === SEARCH_TRANSPORT_DIRECT_HTTP &&
        (typeof provider.buildRequest !== 'function' || typeof provider.parseResponse !== 'function')) ||
      (provider.transport === SEARCH_TRANSPORT_BROWSER_APP_CONTROL && typeof provider.search !== 'function')) {
    throw new TypeError('invalid_search_provider');
  }
  return provider;
}
