// 轻量内嵌页跟随 Desk 的显式明暗选择。页面独立打开时才回退到本机缓存；
// 同源 iframe 直接观察宿主 data-theme，切换外观不需要重载。
(() => {
  const root = document.documentElement;
  const host = () => {
    try { return parent !== window ? parent.document.documentElement : null; } catch { return null; }
  };
  const desired = () => {
    const h = host();
    if (h && h.dataset.theme) return h.dataset.theme === "light" ? "light" : "dark";
    const asked = new URLSearchParams(location.search).get("theme");
    if (asked === "light" || asked === "dark") return asked;
    try {
      const pref = localStorage.getItem("cd_theme_pref");
      if (pref === "light" || pref === "dark") return pref;
      return localStorage.getItem("cd_theme") === "light" ? "light" : "dark";
    } catch { return "dark"; }
  };
  const apply = () => { root.dataset.theme = desired(); };
  apply();
  const h = host();
  if (h) new MutationObserver(apply).observe(h, { attributes: true, attributeFilter: ["data-theme"] });
})();
