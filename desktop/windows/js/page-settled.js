// WebView2 can expose the initial about:blank document as complete while the
// requested navigation is still pending. It is never a settled public page.
// Include the URL in the sample so equally sized documents cannot look stable
// across a navigation. This is only a read-only observation, not a retry.
(() => {
  if (location.href === 'about:blank' || document.readyState !== 'complete') return null;
  return JSON.stringify([location.href, document.getElementsByTagName('*').length]);
})()
