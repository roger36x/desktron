# Network and proxy (Windows)

Configure how Desk connects in "Settings → Network and proxy". This entry point needs both a Windows native shell and a backend that support the feature; an older shell shows that unified configuration is not supported yet. macOS and Linux do not offer this configuration for now and keep their existing behavior.

## Connection methods

- **Follow system**: reads the fixed Windows HTTP system proxy and passes the same rules to the browser and the backend. Without a fixed proxy, no application-level proxy is used; a system VPN/TUN may still affect connections.
- **Direct**: clears the application-level proxy inherited by Desk's child processes and makes the built-in browser use no proxy. It does not turn off a system VPN/TUN.
- **Manual**: enter the proxy host or IP and the actual port. Use the proxy client's HTTP or mixed port, for example address `127.0.0.1` and port `7890`; the port is up to you and not a fixed requirement.

Only unauthenticated HTTP proxies are supported for now; subscription links, SOCKS-only ports, and addresses containing a user name or password are not accepted. HTTPS websites can be reached through an HTTP proxy, so there is no need to enter HTTPS website addresses.

Bypass addresses are comma-separated and support host names, IPs, and domain suffixes such as `.example.com`. This machine's `localhost`, `127.0.0.1`, and `::1` always bypass the proxy. When the system proxy includes PAC, auto-discovery, `<local>`, or other rules that cannot be passed equivalently to the engines, an explicit error is reported rather than switching to direct automatically; you can enter an HTTP proxy address and port manually instead.

## Saving and taking effect

The configuration is saved in `%USERPROFILE%\.config\desktron\network.json`; it does not change the Windows system proxy, engine login information, or an existing `env` file. Until it is saved for the first time, the original system and environment settings stay in use.

Saving only updates the local file; it does not interrupt running turns and does not change the network immediately. Wait for the turn to end, then quit Desk normally and reopen it. Restarting only the backend cannot update the running WebView2 browser configuration. The settings page checks the configuration loaded by the browser and the backend, and shows explicitly when they are out of sync and not fully in effect. After the fixed system proxy changes, Desk also needs to be reopened.

Once saved, the unified configuration takes precedence over proxy variables in the old `env` and Pi's `httpProxy` preference; it does not delete those old settings. Engine plugins you modified yourself and Git/npm-specific proxy settings are outside what the unified configuration guarantees; check them separately for conflicts.

## Verifying separately

"Test proxy port" only connects to the given proxy's TCP port; it does not visit external pages, log in, or send model requests. A reachable port does not mean HTTP forwarding, web search, or model calls work.

After reopening Desk, the user verifies web search, installation downloads, and one request with the engine in use, each separately. A failed test never automatically retries installation, switches the proxy, or lowers TLS verification.
