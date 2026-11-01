# Fixed command terminals only. Addresses come from the launch-time environment, not script text.
if ($env:DESKTRON_NETWORK_MODE -notin @('system', 'direct', 'manual')) { return }
$ErrorActionPreference = 'Stop'
if (-not ('DeskNetworkProxyPolicy' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Net;
public sealed class DeskNetworkProxyPolicy : IWebProxy {
    public ICredentials Credentials { get; set; }
    private readonly string http = Environment.GetEnvironmentVariable("HTTP_PROXY");
    private readonly string https = Environment.GetEnvironmentVariable("HTTPS_PROXY");
    private readonly string[] bypass = (Environment.GetEnvironmentVariable("NO_PROXY") ?? "").Split(',');
    private string ProxyFor(Uri uri) { return uri.Scheme == "https" ? https : http; }
    public bool IsBypassed(Uri uri) {
        if (String.IsNullOrEmpty(ProxyFor(uri))) return true;
        string host = uri.DnsSafeHost.Trim('[', ']');
        foreach (string raw in bypass) {
            string rule = raw.Trim();
            if (rule == "*") return true;
            if (rule.StartsWith(".")) {
                string root = rule.Substring(1);
                if (host.Equals(root, StringComparison.OrdinalIgnoreCase) ||
                    host.EndsWith(rule, StringComparison.OrdinalIgnoreCase)) return true;
            } else if (host.Equals(rule.Trim('[', ']'), StringComparison.OrdinalIgnoreCase)) return true;
        }
        return false;
    }
    public Uri GetProxy(Uri uri) { return IsBypassed(uri) ? uri : new Uri(ProxyFor(uri)); }
}
'@ -ErrorAction Stop
}
$deskProxyPolicy = New-Object DeskNetworkProxyPolicy
[System.Net.WebRequest]::DefaultWebProxy = $deskProxyPolicy
# PowerShell 7 uses HttpClient; Windows PowerShell 5 uses WebRequest.
$deskHttpType = 'System.Net.Http.HttpClient' -as [type]
if ($deskHttpType) {
    $deskDefaultProxy = $deskHttpType.GetProperty('DefaultProxy')
    if ($deskDefaultProxy) { $deskDefaultProxy.SetValue($null, $deskProxyPolicy, $null) }
}
