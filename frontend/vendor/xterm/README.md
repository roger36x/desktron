# xterm 资产

如本目录缺少这三个文件，可用任一方式补齐:

    npm pack @xterm/xterm@5.5.0 @xterm/addon-fit@0.10.0
    # 解包后:package/lib/xterm.js → xterm.js;package/css/xterm.css → xterm.css;
    #         addon-fit package/lib/addon-fit.js → addon-fit.js
    # 或 curl:
    curl -o xterm.js     https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js
    curl -o xterm.css    https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css
    curl -o addon-fit.js https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js

补齐后访问 http://127.0.0.1:7765/term.html 即可。
