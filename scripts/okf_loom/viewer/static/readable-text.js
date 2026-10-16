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
        if(name[0]==="#"){var code=name[1].toLowerCase()==="x"?parseInt(name.slice(2),16):parseInt(name.slice(1),10);return code>0&&code<0x110000&&!(code>=0xd800&&code<=0xdfff)?String.fromCodePoint(code):all;}
        return Object.prototype.hasOwnProperty.call(ENTITIES,name.toLowerCase())?ENTITIES[name.toLowerCase()]:all;
      })
      .replace(/\uE000(\d+)\uE001/g,function(all,i){return Number(i)<escaped.length?escaped[Number(i)]:all;});
    return text.replace(/\s+/g," ").trim();
  }
  // Presentation follows retrieval's stop-word vocabulary, but never changes
  // ranking: only complete Unicode word terms receive emphasis.
  var STOPWORDS={};
  "a an and are as at be been being but by for from had has have having he her here hers him his how i if in into is it its itself me more most my no nor not of off on once only or other our out over own same she so some such than that the their them then there these they this those through to too under until up very was we were what when where which while who whom why will with would you your yours about above after again against all because before below between during each few further do does did doing down can could should shall may might must also via per within without".split(" ").forEach(function(t){STOPWORDS[t]=true;});
  window.okfHighlightPattern=function(query){
    var terms=(String(query||"").toLowerCase().match(/[\p{L}\p{N}\p{M}_]+/gu)||[]).filter(function(t,i,all){return !STOPWORDS[t]&&all.indexOf(t)===i;}).slice(0,32);
    if(!terms.length)return null;
    return new RegExp("(?<![\\p{L}\\p{N}\\p{M}_])(?:"+terms.join("|")+")(?![\\p{L}\\p{N}\\p{M}_])","giu");
  };
  window.okfReadableText=readableText;
})();
