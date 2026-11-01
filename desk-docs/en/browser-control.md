# Browser control and web verification

Desk offers two different capabilities: verification of public web pages without any login, and the built-in browser operated through App Control. They differ in identity, evidence, and authorization boundaries, and must not be swapped silently.

## Verifying public web pages

Pi's `web_search` opens a fixed DuckDuckGo search page in a disposable, anonymous, isolated background tab without switching the user's current page. The temporary tab is destroyed as soon as the call ends and is not written to the browser's tab restore records. `web_fetch` still reads links given by the user, search results, or links in pages already read, over controlled direct HTTP. Neither carries the user's cookies, SSO, Authorization, or browser identity. When the background browser is unavailable, search fails explicitly rather than silently working around it.

Search snippets are only leads for choosing sources; factual citations should come from the original pages fetched afterwards. Internal material, credentials, personal data, intranet addresses, and other content that must not leave the machine must never be used as public search queries. When `web_fetch` needs login, JavaScript rendering, clicks, forms, or screenshots, it suggests switching to the browser; the Agent must switch explicitly and cannot reuse the user's identity on its own. Fetching currently has no browser fallback, so that per-hop public address and redirect checks are preserved.

Codex and Claude use their own native web capabilities, whose return format is not exactly the same as Pi's Desk evidence schema. Every engine should distinguish "search candidates" from "original text already read".

## App Control tabs

When the Agent opens a web page itself, it uses an anonymous isolated tab by default, without the user's cookies or SSO, and it does not launch local client apps. When the user's login state is needed, the identity mode is used; the first visit to a site must be confirmed by the user, and afterwards it is reused only within the original authorization. The user needs to hand a page to the Agent only when the Agent must operate a page the user already has open.

Reading and clicking do not mean business success. After an action returns, check the page state, the server's receipt, or the target data; a fixed wait time does not replace verifying the result. Sensitive settings, login, and authorization screens are protected, and scripts cannot stand in for the person's own confirmation.

## The built-in browser and Feishu

The macOS desktop shell hosts browser pages in WKWebView. User pages use persistent storage, and Agent isolated tabs use non-persistent storage. The browser frame follows Desk's appearance, but Desk no longer injects forced dark mode into web pages; page content follows the site's own theme and `prefers-color-scheme` behavior.

Feishu conditional access may require client authorization and a device certificate. The newer native shell rewrites Feishu applinks to the client protocol and hands them to the system, handles `lark://` and `feishu://`, and, when a user page receives a client certificate challenge, chooses a valid identity from the keychain only by the issuers the server specifies. Agent isolated tabs do not launch the client and do not present device certificates.

These capabilities depend on the native shell currently running, not just on backend code. Having synced the source or restarted the backend does not prove that the new shell is loaded; when Feishu reports "device not authorized", first check the host version, whether clicking authorize launches the client, and whether the keychain holds the certificate the policy requires, before judging site permissions.

## Locating faults

The native browser view sits outside the workbench WebView. When the settings dialog is covered, a web page does not appear, or a tab cannot be restored, check both the front-end page state and the native view's display, mounting, and recycling; simply raising the CSS stacking order usually does not help.

Browser login state, site authorizations, and control credentials are local runtime data and never enter the repository or shared material. Whether a custom app can be operated by the Agent depends on the observation and operation capabilities it declares; see the [App Control contract](reference/app-control.md) and [Extending and contributing](extend.md).
