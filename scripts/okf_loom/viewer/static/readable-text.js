/* Presentation-only Markdown to plain text. Never use this for cited source,
 * search ranking, integrity bytes, or Context Pack serialization. */
(function () {
  "use strict";
  var ENTITIES={amp:"&",lt:"<",gt:">",quot:'"',apos:"'",nbsp:" ",mdash:"—",ndash:"–",hellip:"…"};
  function readableText(markdown) {
    var escaped=[];
    function stash(value){escaped.push(value);return "\uE000"+(escaped.length-1)+"\uE001";}
    var text=String(markdown||"")
      .replace(/\\([!-\/:-@\[-`{-~])/g,function(_,ch){return stash(ch);})
      .replace(/^[ \t]{0,3}(?:`{3,}|~{3,})[^\n]*$/gm," ")
      .replace(/^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$/gm," ")
      .replace(/^[ \t]*\|(.*)\|[ \t]*$/gm,function(_,cells){return cells.split("|").map(function(c){return c.trim();}).filter(Boolean).join(" · ");})
      .replace(/^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/gm," ")
      .replace(/^[ \t]{0,3}(?:>[ \t]?)+/gm,"")
      .replace(/^[ \t]{0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/gm,"$1")
      .replace(/^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?/gm,"")
      .replace(/<!--[\s\S]*?-->/g," ")
      .replace(/<((?:https?|mailto):[^<>\s]+)>/gi,"$1")
      .replace(/<\/?[A-Za-z][\w:-]*(?:\s[^<>]*)?\/?>/g," ")
      .replace(/!\[([^\]]*)\]\([^)]*\)/g,"$1")
      .replace(/\[([^\]]+)\]\((?:[^()\s]|\([^()\s]*\))*(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)/g,"$1")
      .replace(/!?\[([^\]]+)\]\[[^\]]*\]/g,"$1")
      .replace(/\[\^[^\]\s]+\]/g,"")
      .replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g,function(_,ticks,code){return stash(code);})
      .replace(/(\*{1,3}|~~)(?=\S)([\s\S]*?\S)\1/g,"$2")
      .replace(/(^|[^0-9A-Za-z_])(_{1,3})(?=\S)([\s\S]*?\S)\2(?![0-9A-Za-z_])/g,"$1$3")
      .replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi,function(all,name){
        if(name[0]==="#"){var code=name[1].toLowerCase()==="x"?parseInt(name.slice(2),16):parseInt(name.slice(1),10);return code>0&&code<0x110000?String.fromCodePoint(code):all;}
        return Object.prototype.hasOwnProperty.call(ENTITIES,name.toLowerCase())?ENTITIES[name.toLowerCase()]:all;
      })
      .replace(/\uE000(\d+)\uE001/g,function(all,i){return Number(i)<escaped.length?escaped[Number(i)]:all;});
    return text.replace(/\s+/g," ").trim();
  }
  window.okfReadableText=readableText;
})();
