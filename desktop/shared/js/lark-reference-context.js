(() => {
  const pane = document.querySelector('.desktron-focused-chat-pane');
  if (!pane || !pane.isConnected) return {ok:false,error:'chat_not_focused'};
  const text = String(pane.innerText || pane.textContent || '')
    .replace(/\r\n?/g, '\n').replace(/[^\S\n]+/g, ' ')
    .replace(/ ?\n ?/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  if (!text) return {ok:false,error:'chat_empty'};
  return {ok:true,title:String(document.title || '飞书聊天').slice(0,240),
    url:String(location.href || '').split('#')[0].slice(0,2000),text:text.slice(0,60000),
    truncated:text.length > 60000};
})();
