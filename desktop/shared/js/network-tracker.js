(()=>{if(globalThis.__deskNetworkTracker)return;globalThis.__deskNetworkTracker=true;
let pending=0,last=Date.now();const publish=()=>{const root=document.documentElement;if(!root)return;
root.setAttribute('data-desk-network-pending',String(pending));root.setAttribute('data-desk-network-last',String(last));};
const begin=()=>{pending+=1;last=Date.now();publish();let ended=false;return()=>{if(ended)return;ended=true;
pending=Math.max(0,pending-1);last=Date.now();publish();};};
const oldFetch=globalThis.fetch;if(typeof oldFetch==='function')globalThis.fetch=function(...args){const end=begin();
try{return oldFetch.apply(this,args).finally(end)}catch(error){end();throw error}};
const oldSend=globalThis.XMLHttpRequest?.prototype?.send;if(oldSend)globalThis.XMLHttpRequest.prototype.send=function(...args){
const end=begin();this.addEventListener('loadend',end,{once:true});try{return oldSend.apply(this,args)}catch(error){end();throw error}};
publish();document.addEventListener('readystatechange',publish);})();
