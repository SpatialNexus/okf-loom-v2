/* OKF Studio - live client (SSE + in-place DOM patching).
 *
 * The no-refresh contract (spec §7.3): when the bundle changes, every open
 * surface updates in place. We swap the SMALLEST affected region and never
 * re-render the whole page. Across every patch we preserve scroll position,
 * focus, active view mode, expanded/collapsed panels, open comment threads
 * + any in-flight comment draft, and graph zoom/selection.
 *
 * Responsibilities:
 *   - EventSource('/__events') with exponential-backoff reconnect; a subtle
 *     connection indicator; on reconnect, resync.
 *   - changed/created on the open concept → fetch /__data/doc, hand the doc
 *     to studio.js (okfLoomStudio.applyDoc) which renders it in the current view
 *     mode, then pulse-highlight the body.
 *   - graph/removed → delegate to studio.js.
 *   - presence/activity/comment → fan out via the hub so studio.js updates
 *     its panels.
 *   - resync → re-fetch open doc + tell studio to reload change list/comments.
 *
 * Security: untrusted strings never go through innerHTML here. Concept bodies
 * (/__data/doc) and derived index/Connections fragments (the page's own GET)
 * come from the SAME escaped renderer used on initial load - identical trust
 * level (§7.3). Page scripts and bootstrap are never transplanted.
 * All mutating requests in studio.js carry X-OKF-Token; live.js only reads.
 *
 * No framework, no bundler. Vanilla ES module. Degrades gracefully: if the
 * studio isn't enabled (window.__OKF_LOOM_STUDIO__ absent), this module is a no-op.
 */
(function () {
  "use strict";

  // CSP-safe bootstrap read: prefer the JSON data block the server embeds
  // (a <script type="application/json" id="okf-studio-bootstrap">, which
  // script-src does not govern), falling back to window.__OKF_LOOM_STUDIO__ for
  // parity with the documented API if a host page sets it directly.
  function readBoot() {
    if (window.__OKF_LOOM_STUDIO__ && typeof window.__OKF_LOOM_STUDIO__ === "object") {
      return window.__OKF_LOOM_STUDIO__;
    }
    const node = document.getElementById("okf-studio-bootstrap");
    if (node) {
      try {
        const cfg = JSON.parse(node.textContent || "{}");
        window.__OKF_LOOM_STUDIO__ = cfg; // publish for parity + studio.js
        return cfg;
      } catch (e) { /* fall through */ }
    }
    return null;
  }
  const BOOT = readBoot();
  if (!BOOT || !BOOT.live) return; // live updates disabled or studio absent
  const CAN_READ_PRESENCE = BOOT.presence !== false && !/^public-/.test(BOOT.authority_mode || "");
  const CAN_PUBLISH_PRESENCE = CAN_READ_PRESENCE && BOOT.edit !== false;

  const REDUCED_MOTION =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- tiny pub/sub hub (window.okfLoomLive) -------------------------------
  // studio.js subscribes; live.js is the only publisher. Typed channels
  // mirror the SSE event types plus internal lifecycle signals.
  const listeners = Object.create(null);
  function on(type, fn) {
    (listeners[type] || (listeners[type] = [])).push(fn);
    return () => off(type, fn);
  }
  function off(type, fn) {
    const arr = listeners[type];
    if (!arr) return;
    const i = arr.indexOf(fn);
    if (i >= 0) arr.splice(i, 1);
  }
  function emit(type, data) {
    const arr = listeners[type];
    if (!arr) return [];
    // R8-RESYNC: collect handler return values so callers that need to await
    // an async refresh (doResync awaiting studio.js loadComments) can do so.
    const results = [];
    // Copy so a handler unsubscribing mid-dispatch doesn't skip entries.
    arr.slice().forEach((fn) => {
      try { results.push(fn(data)); } catch (e) { console.error("[okf-live] handler error", type, e); }
    });
    return results;
  }
  window.okfLoomLive = { on, off, emit, presenceAvailable: CAN_READ_PRESENCE, get state() { return conn.state; }, get rev() { return rev; } };

  // ---- Presence v2: canonical replica + graph-only browser publisher ------
  const P_RECORD_COMMON = new Set(["session_id","kind","actor","state","client_seq","updated_at","expires_at"]);
  const P_BROWSER_KEYS = new Set([...P_RECORD_COMMON,"label","focus","viewport","capability_expires_at"]);
  const P_AGENT_KEYS = new Set([...P_RECORD_COMMON,"focus","message"]);
  const P_BROWSER_STATES = new Set(["watching","editing","thinking"]);
  const P_AGENT_STATES = new Set(["idle","watching","editing","thinking"]);
  const presence = { rev:-1, records:new Map(), session:null, predecessor:null, lifecycle:0,getEpoch:0,
    active:false, left:true, clientSeq:0, queued:null, post:null, get:null, heartbeat:null,
    pointerTimer:null, retryTimer:null, lastViewport:null, sentViewport:null, pagehideLeft:false,
    graph:null, visible:!document.hidden,pointerInside:false,published:false,
    restoreGeneration:0,restorePromise:null,restoreConsumed:true,readySource:null,readyPromise:null };
  const P_GET_DEADLINE=5000, P_POST_DEADLINE=9000, P_MAX_ATTEMPTS=4, P_POINTER_MS=250;
  function plainObject(v){return !!v&&typeof v==="object"&&!Array.isArray(v)&&Object.getPrototypeOf(v)===Object.prototype;}
  function exactKeys(v,allowed){return Object.keys(v).every((key)=>allowed.has(key));}
  function finiteTime(v){return typeof v==="number"&&Number.isFinite(v)&&v>=0;}
  function validFocus(v){return v==null||(typeof v==="string"&&v.length>0&&v.length<=500&&!/[\u0000-\u001f\u007f]/.test(v));}
  function validRecord(row){
    if(!plainObject(row)||typeof row.session_id!=="string"||!row.session_id||!Number.isSafeInteger(row.client_seq)||row.client_seq<0||!finiteTime(row.updated_at)||!finiteTime(row.expires_at))return false;
    if(row.kind==="browser")return exactKeys(row,P_BROWSER_KEYS)&&row.actor==="browser"&&P_BROWSER_STATES.has(row.state)&&typeof row.label==="string"&&row.label.length>0&&row.label.length<=80&&!/[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/.test(row.label)&&validFocus(row.focus)&&finiteTime(row.capability_expires_at)&&plainObject(row.viewport)&&exactKeys(row.viewport,new Set(["schema","x","y"]))&&row.viewport.schema==="graph-viewport-v1"&&Number.isFinite(row.viewport.x)&&Number.isFinite(row.viewport.y)&&row.viewport.x>=0&&row.viewport.x<=1&&row.viewport.y>=0&&row.viewport.y<=1;
    if(row.kind==="agent")return exactKeys(row,P_AGENT_KEYS)&&row.session_id==="agent"&&(row.actor==="agent"||row.actor==="user")&&P_AGENT_STATES.has(row.state)&&validFocus(row.focus)&&(row.message==null||(typeof row.message==="string"&&row.message.length<=200));
    return false;
  }
  function validSession(v){return plainObject(v)&&exactKeys(v,new Set(["session_id","capability"]))&&typeof v.session_id==="string"&&v.session_id.length>=12&&v.session_id.length<=64&&typeof v.capability==="string"&&v.capability.length>20;}
  function validSnapshot(v){return plainObject(v)&&exactKeys(v,new Set(["version","presence_rev","records","browser_session"]))&&v.version===2&&Number.isSafeInteger(v.presence_rev)&&v.presence_rev>=0&&Array.isArray(v.records)&&v.records.length<=128&&v.records.every(validRecord)&&validSession(v.browser_session);}
  function currentPresence(){return {version:2,presence_rev:presence.rev,online:presence.visible&&conn.state==="online",records:Array.from(presence.records.values()).filter((r)=>r.session_id!==presence.predecessor).map((r)=>Object.assign({},r)),own_session_id:presence.session&&presence.session.session_id||null};}
  function publishPresence(reason){const value=currentPresence();value.reason=reason||"update";emit("presence",value);}
  const originalOn=on;
  window.okfLoomLive.on=function(type,fn){const dispose=originalOn(type,fn);if(type==="presence"&&presence.rev>=0)queueMicrotask(()=>{try{fn(currentPresence());}catch(e){console.error("[okf-live] presence replay",e);}});return dispose;};
  function abortableDelay(ms,signal){return new Promise((resolve,reject)=>{const t=setTimeout(resolve,ms);const stop=()=>{clearTimeout(t);reject(new DOMException("Aborted","AbortError"));};if(signal){if(signal.aborted)return stop();signal.addEventListener("abort",stop,{once:true});}});}
  function retryDelay(attempt,response){const h=response&&response.headers&&response.headers.get("Retry-After"),n=h==null?NaN:Number(h);if(Number.isFinite(n))return Math.min(4000,Math.max(250,n*1000));return Math.min(4000,250*Math.pow(2,attempt)+Math.floor(Math.random()*101));}
  async function fetchDeadline(url,options,deadline,controller){const timer=setTimeout(()=>controller.abort("deadline"),deadline);try{return await fetch(url,Object.assign({},options,{signal:controller.signal}));}finally{clearTimeout(timer);}}
  async function canonicalPresence(reason,rotate){
    if (!CAN_READ_PRESENCE) return false;
    if(presence.get)return presence.get.promise;
    const owner=presence.getEpoch,controller=new AbortController(),prior=presence.session;
    const promise=(async()=>{try{
      const headers={Accept:"application/json"};if(prior&&!rotate){headers["X-OKF-Presence-Session"]=prior.session_id;headers["X-OKF-Presence-Capability"]=prior.capability;}
      let response=await fetchDeadline("/__presence",{headers,cache:"no-store"},P_GET_DEADLINE,controller);
      if(response.status===403&&prior&&!rotate){response=await fetchDeadline("/__presence",{headers:{Accept:"application/json"},cache:"no-store"},P_GET_DEADLINE,controller);rotate=true;}
      if(!response.ok||owner!==presence.getEpoch||!presence.visible)return false;const data=await response.json();if(owner!==presence.getEpoch||!presence.visible||!validSnapshot(data)||data.presence_rev<presence.rev)return false;
      if(rotate&&prior&&prior.session_id!==data.browser_session.session_id)presence.predecessor=prior.session_id;
      presence.rev=data.presence_rev;presence.records=new Map(data.records.map((row)=>[row.session_id,row]));presence.session=data.browser_session;if(!prior||prior.session_id!==data.browser_session.session_id){presence.clientSeq=0;presence.left=false;presence.published=false;}
      publishPresence(reason||"canonical");return true;
    }catch(e){return false;}finally{if(presence.get&&presence.get.owner===owner)presence.get=null;}})();
    presence.get={owner,controller,promise};return promise;
  }
  function validDelta(d){if(!plainObject(d)||d.type!=="presence"||d.presence_version!==2||!Number.isSafeInteger(d.presence_rev)||d.presence_rev<0)return false;if(d.action==="upsert")return exactKeys(d,new Set(["type","presence_version","presence_rev","action","record"]))&&validRecord(d.record);if(d.action==="leave")return exactKeys(d,new Set(["type","presence_version","presence_rev","action","session_id","reason"]))&&typeof d.session_id==="string"&&typeof d.reason==="string";return false;}
  function handlePresenceDelta(d){if(!validDelta(d)){void canonicalPresence("invalid-delta",false);return;}if(d.presence_rev<=presence.rev)return;if(presence.rev<0||d.presence_rev!==presence.rev+1){void canonicalPresence("gap",false);return;}if(d.action==="upsert")presence.records.set(d.record.session_id,d.record);else presence.records.delete(d.session_id);presence.rev=d.presence_rev;publishPresence("delta");}
  function viewerLabel(){const sid=presence.session&&presence.session.session_id||"viewer";return "Viewer "+sid.replace(/[^A-Za-z0-9]/g,"").slice(-5).toUpperCase();}
  function postBody(viewport){const body={action:"upsert",actor:"browser",state:"watching",label:viewerLabel(),viewport};const selected=new URL(location.href).searchParams.get("selected");if(selected)body.focus=selected;return body;}
  function cancelPublisher(){presence.lifecycle++;presence.active=false;presence.queued=null;clearInterval(presence.heartbeat);presence.heartbeat=null;clearTimeout(presence.pointerTimer);presence.pointerTimer=null;clearTimeout(presence.retryTimer);presence.retryTimer=null;if(presence.post)presence.post.controller.abort("stopped");presence.post=null;}
  async function sendIntent(intent,owner){
    if(!presence.session||owner!==presence.lifecycle)return;const controller=new AbortController();presence.post={owner,controller};const terminal=Date.now()+P_POST_DEADLINE;let refreshed=false;
    try{for(let attempt=0;attempt<P_MAX_ATTEMPTS&&Date.now()<terminal;attempt++){
      if(owner!==presence.lifecycle||controller.signal.aborted)return;const body=Object.assign({},intent,presence.session,{client_seq:++presence.clientSeq});let response=null;
      try{response=await fetchDeadline("/__presence",{method:"POST",headers:{"Content-Type":"application/json",Accept:"application/json","X-OKF-Token":BOOT.token||""},body:JSON.stringify(body)},Math.min(3500,terminal-Date.now()),controller);}catch(e){if(controller.signal.aborted)return;}
      if(response&&response.ok){presence.left=intent.action==="leave";presence.published=intent.action==="upsert";return;}if(response&&response.status===400)return;
      if(response&&response.status===403){if(refreshed)return;refreshed=true;if(!await canonicalPresence("capability-refresh",true))return;continue;}
      if(response&&response.status!==429&&response.status!==503)return;
      const delay=Math.min(terminal-Date.now(),retryDelay(attempt,response));if(delay<=0)return;try{await abortableDelay(delay,controller.signal);}catch(e){return;}
    }}finally{if(presence.post&&presence.post.owner===owner){presence.post=null;if(owner===presence.lifecycle&&presence.queued){const next=presence.queued;presence.queued=null;void sendIntent(next,owner);}}}
  }
  function queueIntent(intent){if(!presence.active||!presence.session||presence.left)return;presence.queued=intent;if(!presence.post){const next=presence.queued;presence.queued=null;void sendIntent(next,presence.lifecycle);}}
  async function beginPresence(){if(!CAN_PUBLISH_PRESENCE||!presence.graph||!presence.visible||document.hidden||presence.active)return false;presence.lifecycle++;const owner=presence.lifecycle;presence.active=true;presence.pagehideLeft=false;if(!presence.session||presence.left){const ok=await canonicalPresence("lifecycle",!!presence.session);if(!ok||owner!==presence.lifecycle||!presence.visible){if(owner===presence.lifecycle)presence.active=false;return false;}presence.left=false;}if(owner!==presence.lifecycle)return false;presence.heartbeat=setInterval(()=>{if(presence.lastViewport)queueIntent(postBody(presence.lastViewport));},5000);if(presence.lastViewport)queueIntent(postBody(presence.lastViewport));return true;}
  function terminalLeave(keepalive){const publicationPending=!!presence.post||!!(presence.queued&&presence.queued.action==="upsert");const canLeave=!!presence.session&&(presence.published||publicationPending);const body=canLeave?Object.assign({action:"leave",client_seq:++presence.clientSeq},presence.session):null;const prior=presence.session;cancelPublisher();presence.left=canLeave;presence.published=false;if(canLeave&&prior)presence.predecessor=prior.session_id;if(body){try{fetch("/__presence",{method:"POST",headers:{"Content-Type":"application/json","X-OKF-Token":BOOT.token||""},body:JSON.stringify(body),keepalive:!!keepalive});}catch(e){}}}
  function suspendPresence(keepalive){presence.visible=false;presence.getEpoch++;presence.restoreGeneration++;presence.restorePromise=null;presence.restoreConsumed=true;presence.readySource=null;presence.readyPromise=null;if(presence.get)presence.get.controller.abort("suspended");presence.get=null;terminalLeave(keepalive);publishPresence("offline");}
  function restorePresence(reason){presence.visible=true;if(presence.restorePromise&&!presence.restoreConsumed)return presence.restorePromise;presence.restoreGeneration++;presence.restoreConsumed=false;presence.readySource=null;presence.readyPromise=null;var generation=presence.restoreGeneration;presence.restorePromise=canonicalPresence(reason,!!presence.left).then(function(ok){if(generation!==presence.restoreGeneration)return false;if(ok&&presence.pointerInside)void beginPresence();return ok;});return presence.restorePromise;}
  function reconcilePresenceReady(source){
    if(source&&presence.readySource===source&&presence.readyPromise)return presence.readyPromise;
    if(source)presence.readySource=source;
    if(presence.restorePromise&&!presence.restoreConsumed){
      presence.restoreConsumed=true;
      var generation=presence.restoreGeneration,completed=presence.restorePromise;
      presence.restorePromise=null;
      presence.readyPromise=completed.then(function(ok){
        if(generation!==presence.restoreGeneration||!presence.visible)return false;
        if(ok)return true;
        // The restore owner settled unsuccessfully. Ready is the authoritative
        // handoff point and owns exactly one fresh, bounded canonical fallback.
        return canonicalPresence("restore-ready-fallback",!!presence.left).then(function(fallbackOk){
          if(generation!==presence.restoreGeneration)return false;
          if(fallbackOk&&presence.pointerInside)void beginPresence();
          return fallbackOk;
        });
      });
      return presence.readyPromise;
    }
    presence.readyPromise=canonicalPresence("reconnect",false);
    return presence.readyPromise;
  }
  function pointerPosition(e){const r=presence.graph.getBoundingClientRect();if(!r.width||!r.height)return null;return {schema:"graph-viewport-v1",x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))};}
  function onPointerEnter(){presence.pointerInside=true;void beginPresence();}
  function onPointerMove(e){const v=pointerPosition(e);if(!v)return;presence.pointerInside=true;presence.lastViewport=v;if(!presence.active){void beginPresence();return;}if(presence.pointerTimer)return;presence.pointerTimer=setTimeout(()=>{presence.pointerTimer=null;const p=presence.sentViewport;if(p&&Math.hypot(v.x-p.x,v.y-p.y)<.006)return;presence.sentViewport=Object.assign({},v);queueIntent(postBody(v));},P_POINTER_MS);}
  function wirePresence(){if(!CAN_READ_PRESENCE)return;presence.graph=document.getElementById("okf-graph");if(!presence.graph)return;presence.graph.addEventListener("pointerenter",onPointerEnter);presence.graph.addEventListener("pointermove",onPointerMove,{passive:true});presence.graph.addEventListener("pointerleave",()=>{presence.pointerInside=false;terminalLeave(false);});void canonicalPresence("initial",false);}
  window.okfLoomLive.currentPresenceSnapshot=currentPresence;
  window.okfLoomLive._presenceForTest={refetch:canonicalPresence,delta:handlePresenceDelta,snapshot:()=>Object.assign(currentPresence(),{active:presence.active,left:presence.left,published:presence.published,queued:presence.queued,post:!!presence.post,heartbeat:!!presence.heartbeat,lifecycle:presence.lifecycle,clientSeq:presence.clientSeq,getPending:!!presence.get,getEpoch:presence.getEpoch,pointerInside:presence.pointerInside,visible:presence.visible,restoreGeneration:presence.restoreGeneration,restorePending:!!presence.restorePromise,restoreConsumed:presence.restoreConsumed}),begin:beginPresence,leave:terminalLeave,suspend:suspendPresence,restore:restorePresence,ready:reconcilePresenceReady};

  // ---- connection state ------------------------------------------------
  // `state` ∈ "online" | "reconnecting" | "offline". rev tracks the latest
  // bundle rev we've seen; a gap between events triggers a resync.
  const conn = { state: "connecting", backoff: 800, timer: null, source: null };
  let rev = BOOT.rev || 0;
  // Track the last event id we've processed (across SSE + polling) so the
  // poller can resume with ?since=<id>, and the last time SSE delivered
  // anything - used by the starvation watchdog to decide whether SSE is
  // being buffered by an intermediary proxy (Cloudflare tunnels, corporate
  // proxies, some CDNs) and we should fall back to polling.
  let lastEventId = null;
  let lastCursor = 0;
  let sseLastReceived = 0;
  let pollingTimer = null;
  let pollingActive = false;
  // R8-POLL: polling is a generation-owned, deadline-bounded, single-flight
  // operation. `pollingGeneration` is the ownership fence: it advances on
  // every start/stop so a late pollOnce commit (after suspension or restart)
  // is rejected before mutating cursor/rev/DOM. `pollingAbort` is the
  // generation-owned AbortController: stopPolling/_suspend abort it so an
  // IN-FLIGHT fetch is cancelled promptly — not just future ticks. The
  // absolute deadline (defined next to POLL_MS below) bounds each tick so a
  // single hung fetch can never block the next tick indefinitely.
  let pollingGeneration = 0;
  let pollingAbort = null;
  // R8-MINOR2: the backoff-clear timer is tracked so _suspend can cancel it;
  // an untracked timer can survive page suspension and mutate a later
  // lifecycle generation's conn.backoff.
  let backoffClearTimer = null;
  // Bundle C / INTENT-004 / §7.3 fix: the starvation watchdog's grace
  // window MUST be longer than the heartbeat interval (server-side 15s).
  // A grace shorter than the heartbeat arms false-positive polling on
  // every healthy idle stream. Default grace = 3 * heartbeat = 45s; both
  // the heartbeat interval and the grace are configurable via the
  // ``__OKF_LOOM_STUDIO__`` bootstrap so an operator can tune for unusual
  // proxies (a very aggressive proxy that buffers for 60s+ can extend
  // the grace; a tight localhost deployment can shorten it).
  const HEARTBEAT_INTERVAL_MS =
    (BOOT.sse_heartbeat_ms && Number.isFinite(BOOT.sse_heartbeat_ms) && BOOT.sse_heartbeat_ms > 0)
      ? Math.max(5000, BOOT.sse_heartbeat_ms)
      : 15000;
  const SSE_GRACE_MS =
    (BOOT.sse_grace_ms && Number.isFinite(BOOT.sse_grace_ms) && BOOT.sse_grace_ms > 0)
      ? Math.max(HEARTBEAT_INTERVAL_MS * 2, BOOT.sse_grace_ms)
      : HEARTBEAT_INTERVAL_MS * 3;
  const POLL_MS = 3000; // polling interval when SSE is starved.
  // R8-POLL: absolute per-tick deadline. Bounds a single pagination tick so a
  // hung fetch can never block the next scheduled tick indefinitely. Kept
  // comfortably above POLL_MS so a normal multi-page backlog tick completes,
  // but still bounded.
  const POLL_DEADLINE_MS = Math.min(POLL_MS * 2, 8000);

  // iter1 CRI-001/INTENT-006: event-id guard. SSE and the polling fallback
  // both arm during the boot window before `ready`, and a buffered proxy can
  // deliver the same logical change through both paths. Without a guard the
  // body patches (and pulses) twice for one edit. We key on
  // `<type>:<rev>:<ids>` and refuse to re-patch a key we've already applied.
  // The set is capped so a long session doesn't grow unbounded.
  const appliedKeys = new Set();
  const APPLIED_KEYS_CAP = 256;
  function eventKey(d) {
    if (!d || !d.type) return null;
    const ids = Array.isArray(d.ids) ? d.ids.join(",") : "";
    return d.type + ":" + (d.rev != null ? d.rev : "") + ":" + ids;
  }
  function markApplied(key) {
    if (!key) return;
    if (appliedKeys.size >= APPLIED_KEYS_CAP) {
      // Drop the oldest ~25% to amortise the cap check.
      const drop = Array.from(appliedKeys).slice(0, Math.ceil(APPLIED_KEYS_CAP / 4));
      drop.forEach((k) => appliedKeys.delete(k));
    }
    appliedKeys.add(key);
  }
  function wasApplied(key) { return key ? appliedKeys.has(key) : false; }
  // Tracks the latest content-rev we've rendered per concept (for the
  // ready/doc_revs resync path above).
  const lastDocRev = Object.create(null);

  function setState(state) {
    conn.state = state;
    emit("conn", { state });
  }

  function currentConceptId() {
    // Authoritative for serve mode: /tables/orders → "tables/orders".
    let p = window.location.pathname.replace(/^\/+/, "").replace(/\/+$/, "");
    if (p.endsWith(".md")) p = p.slice(0, -3);
    return p;
  }

  // R8-MAJOR: local isConceptPage. handleReady() calls this to decide whether
  // an authoritative doc_revs omission means "the open concept was removed",
  // but the function previously existed only inside studio.js's separate
  // closure — so the omission path raised ReferenceError and prevented stale-
  // document removal. Mirror studio.js's definition exactly so the two
  // modules agree on what a concept page is.
  function isConceptPage() {
    return !!(document.querySelector && document.querySelector("article.okf-page__main"));
  }

  // ---- SSE connect with exponential backoff ----------------------------
  function connect() {
    if (conn.source) { try { conn.source.close(); } catch (e) {} }
    let source;
    try {
      source = new EventSource("/__events" + (lastCursor ? "?since=" + encodeURIComponent(String(lastCursor)) : ""));
    } catch (e) {
      // EventSource unsupported - go offline and stop retrying.
      setState("offline");
      return;
    }
    conn.source = source;

    // While a connection attempt is pending, keep the owner state honest: the
    // session's first pending attempt (fresh boot, bfcache restore) stays
    // "connecting", and "reconnecting" is reserved for recovery after an
    // actual transport failure — source.onerror flips the state and every
    // retry re-enters here while it is still recovering. Announcing
    // "reconnecting" unconditionally (before any error) is what made every
    // fresh healthy load read as a recovery in progress. Flips to online on
    // first event.
    if (conn.state !== "reconnecting") setState("connecting");

    // R4-3: the ``ready`` control frame is reconciled by the module-scope
    // handleReady() helper (extracted so the seeding rule is unit-testable
    // without a real EventSource).
    source.addEventListener("ready", (e) => { handleReady(safeJson(e.data), source); });

    // All data events route through handleEvent (shared with the polling
    // fallback) and mark SSE as alive so the watchdog knows it's delivering.
    source.addEventListener("changed", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
    source.addEventListener("created", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
    source.addEventListener("removed", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
    source.addEventListener("graph", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
    source.addEventListener("presence", (e) => { markSseReceived(); handlePresenceDelta(safeJson(e.data)); });
    source.addEventListener("activity", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
     source.addEventListener("comment", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
     // INTENT4-001: listen for comment_link events so the change-list
     // back-link renders live (not just after page reload).
     source.addEventListener("comment_link", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
     // R4-6: the dispatcher understands agent_conflict and suggestions, but
     // without these EventSource listeners a real SSE frame of either type
     // is never routed through handleEvent — so studio.js's subscribers
     // (conflict modal / suggestion surfacing) never fire over normal SSE.
     source.addEventListener("agent_conflict", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
     source.addEventListener("suggestions", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
     // X3: proposal outbox notifications are retained cursor-bearing rows. They
     // are notification-only (ids + read_model_token, never proposal bytes);
     // studio.js refetches the inspection panel from the read routes. Without
     // this listener the frame was never dispatched, its cursor never
     // committed, and the next feed event looked like a cursor gap (resync).
     source.addEventListener("proposal", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
    // Bundle C / INTENT-004 / §7.3 fix: the server's heartbeat now ships
    // BOTH a ``: ping`` comment (HTTP-level keep-alive) AND a real
    // ``event: ping`` frame (app-level heartbeat the EventSource
    // dispatches). Listening for ``ping`` lets the watchdog count the
    // heartbeat as "stream alive" so a HEALTHY idle stream (no real
    // changes, only heartbeats) is NOT misclassified as buffered/starved
    // and never falls back to polling.
    source.addEventListener("ping", () => { markSseReceived(); });
    source.addEventListener("resync", (e) => {
      markSseReceived();
      const d = safeJson(e.data);
      // R5-2: validate the resync envelope. A resync signal must carry a
      // numeric rev; a malformed envelope (null, non-object, or missing rev)
      // must fail safe — reset cursor and trigger a canonical resync rather
      // than silently ignoring it.
      if (!d || typeof d !== "object") { lastCursor = 0; void doResync(); return; }
      noteRev(d.rev);
      // R5-2: reset cursor policy so the next poll fetches from the start,
      // ensuring no events were missed during the gap that triggered resync.
      lastCursor = 0;
      doResync();
    });

    source.onopen = () => {
      markSseReceived();
      // "ready" sets online; keep this as a fallback for browsers that
      // don't fire addEventListener('ready') before onopen.
      if (conn.state !== "online") setState("online");
    };
    source.onerror = () => {
      // EventSource auto-reconnects, but we also drive our own backoff so
      // we can show a honest indicator + resync after a long drop.
      setState("reconnecting");
      publishPresence("offline");
      scheduleReconnect();
    };
  }

  // ---- polling fallback (when SSE is buffered by a proxy) --------------
  // Some intermediaries (Cloudflare quick tunnels, corporate proxies, certain
  // CDNs) buffer the SSE text/event-stream response, so the browser's
  // EventSource connects but never receives frames. We detect that (no SSE
  // event within SSE_GRACE_MS) and fall back to polling /__data/events,
  // dispatching through the SAME handleEvent so the page patches identically.
  // This keeps the no-refresh contract alive behind any proxy.
  async function pollOnce() {
    // R8-POLL: capture the generation+abort owner at tick entry. Every cursor
    // /rev/DOM mutation is fenced by `myGen`; the fetch carries `tickSignal`
    // so suspension (stopPolling/_suspend) aborts the IN-FLIGHT request, not
    // only future ticks. An absolute deadline bounds the pagination loop.
    const myGen = pollingGeneration;
    const ownerSignal = pollingAbort ? pollingAbort.signal : null;
    const deadlineController = new AbortController();
    const onOwnerAbort = function () {
      try { deadlineController.abort(ownerSignal && ownerSignal.reason); }
      catch (e) { try { deadlineController.abort(); } catch (e2) {} }
    };
    if (ownerSignal) {
      if (ownerSignal.aborted) return;
      ownerSignal.addEventListener("abort", onOwnerAbort, { once: true });
    }
    const deadlineTimer = setTimeout(function () { try { deadlineController.abort(); } catch (e) {} }, POLL_DEADLINE_MS);
    const tickSignal = deadlineController.signal;
    try {
      // P2-1 (QUA3-004) + QUA4-005: when using a `since` cursor, pass
      // order=asc so the server honors the cursor (the desc default
      // ignores `since`). On boot (no cursor), use desc so the newest
      // events surface first.
      let pages = 0;
      while (pages++ < 100) {
        // R8-POLL: re-check ownership at every iteration so a stop/suspend
        // mid-pagination aborts cleanly without consuming more events.
        if (myGen !== pollingGeneration) return;
        if (ownerSignal && ownerSignal.aborted) return;
        if (tickSignal.aborted) return; // absolute deadline exceeded
        const order = lastCursor ? "asc" : "desc";
        const url = "/__data/events?limit=50&order=" + order + (lastCursor ? "&since=" + encodeURIComponent(String(lastCursor)) : "");
        let res;
        try {
          res = await fetch(url, { headers: { Accept: "application/json" }, signal: tickSignal });
        } catch (fetchErr) {
          // R8-POLL: abort by suspension/deadline/supersession is the expected
          // terminal state — stop this tick quietly. A genuine transport error
          // for a still-owned tick is swallowed so the next scheduled tick
          // retries (matching the previous transient-error behaviour).
          if (tickSignal.aborted || (ownerSignal && ownerSignal.aborted) || myGen !== pollingGeneration) return;
          return;
        }
        if (myGen !== pollingGeneration) return;
        if (!res.ok) return;
        const data = await res.json();
        if (myGen !== pollingGeneration) return;
        // R7-2: validate the COMPLETE discriminated response envelope before
        // mutating any cursor or revision ownership. The envelope is either a
        // typed resync signal or a normal event feed. Invalid discriminator
        // values, malformed sub-fields, or structurally broken events must
        // fail safe — stop this tick (the next interval retries) rather than
        // silently advancing revision/cursor state past a real gap.
        if (!_isValidPollEnvelope(data)) return;
        // R6-1: the server emits a typed resync response when the cursor is
        // expired / missing / future / gapped: {resync:true, reason, rev,
        // doc_revs}. The polling path MUST NOT consume `events` from such a
        // response and advance the stale cursor — that would loop forever on
        // the same invalid cursor without canonical reconciliation. Reset
        // cursor ownership and run ONE generation-fenced canonical resync
        // (doResync), then stop processing this response. doResync emits
        // "resync" → studio.js loadComments fetches doc+comments atomically;
        // the fallback (no studio) patches the open concept. doc_revs is
        // surfaced via the resync emit so consumers that need it can read it.
        if (data.resync === true) {
          if (typeof data.rev === "number") noteRev(data.rev);
          lastCursor = 0;
          try { await doResync(data.doc_revs || null); } catch (e) { /* doResync logs internally */ }
          return;
        }
        const events = data.events.slice();
        events.sort((a, b) => Number(a.event_cursor || 0) - Number(b.event_cursor || 0));
        if (typeof data.rev === "number") noteRev(data.rev);
        for (const ev of events) {
          // R8-POLL: stop applying events the moment ownership is lost.
          if (myGen !== pollingGeneration) return;
          handleEvent(ev);
        }
        if (events.length < 50) break;
      }
      if (myGen !== pollingGeneration) return;
      if (conn.state !== "online") setState("online");
    } catch (e) {
      // R8-POLL: a superseded/aborted tick must not surface as a transport
      // error; only a still-owned tick's transient error is interesting (and
      // here it is simply swallowed so the next tick retries).
      if (myGen !== pollingGeneration) return;
      /* transient fetch error; next tick retries */
    } finally {
      clearTimeout(deadlineTimer);
      if (ownerSignal) ownerSignal.removeEventListener("abort", onOwnerAbort);
    }
  }

  // R8-POLL: single-flight settlement scheduling. The next tick is scheduled
  // only AFTER the current tick settles (in a finally), so two pagination
  // loops can never overlap when a tick runs longer than POLL_MS (the
  // previous setInterval(pollOnce, POLL_MS) allowed exactly that).
  // R9-POLL: settlement scheduling is bound to the CAPTURED generation. A
  // stopped poll's finally(scheduleNextPoll) can observe a newly-active
  // (restarted) session and spawn a second timer chain; threading myGen
  // through the settlement chain prevents an old finally from creating a new
  // chain after stop→restart.
  function scheduleNextPoll(myGen) {
    if (myGen !== pollingGeneration || !pollingActive) return;
    pollingTimer = setTimeout(function () {
      pollingTimer = null;
      if (myGen !== pollingGeneration || !pollingActive) return;
      pollOnce().finally(function () { scheduleNextPoll(myGen); });
    }, POLL_MS);
  }

  function startPolling() {
    if (pollingActive) return;
    pollingActive = true;
    // R8-POLL: a fresh generation+abort owner for this polling session.
    pollingGeneration++;
    var myGen = pollingGeneration;
    pollingAbort = new AbortController();
    void canonicalPresence("poll-takeover", false);
    // Polling takeover is a transport gap. Canonical state is authoritative;
    // events only provide subsequent notifications.
    void doResync();
    // First poll immediately so a buffered client catches up at once, then
    // settle into the single-flight interval once the tick settles.
    pollOnce().finally(function () { scheduleNextPoll(myGen); });
  }

  function stopPolling() {
    pollingActive = false;
    // R8-POLL: advance the generation (fences any in-flight tick's late
    // commit) and abort the in-flight fetch via the generation owner —
    // suspension cancels the IN-FLIGHT request, not only future ticks.
    pollingGeneration++;
    if (pollingAbort) { try { pollingAbort.abort("suspended"); } catch (e) {} pollingAbort = null; }
    if (pollingTimer) { clearTimeout(pollingTimer); pollingTimer = null; }
  }

  // Watchdog: if SSE hasn't delivered ANY event within the grace window,
  // it's almost certainly being buffered - start polling. Re-checked on an
  // interval; if SSE recovers, markSseReceived() stops the poller.
  function starvationCheck() {
    if (pollingActive) return;
    const idle = Date.now() - (sseLastReceived || 0);
    if (idle >= SSE_GRACE_MS) startPolling();
  }

  function scheduleReconnect() {
    if (conn.timer) return;
    // The native EventSource may reconnect on its own; we additionally
    // probe. After several failed native retries the source closes; we
    // reopen with backoff. Cap at ~30s.
    const delay = Math.min(30000, conn.backoff);
    conn.timer = setTimeout(() => {
      conn.timer = null;
      conn.backoff = Math.min(30000, conn.backoff * 2);
      // If the native source is still open, leave it; otherwise reopen.
      if (conn.source && conn.source.readyState === EventSource.CLOSED) {
        connect();
      }
    }, delay);
  }
  function scheduleBackoffClear() {
    // R8-MINOR2: Reset backoff once we're stably online for a moment. The
    // timer is tracked so _suspend can cancel it; an untracked timer can
    // survive page suspension and mutate a later lifecycle generation's
    // conn.backoff.
    if (backoffClearTimer) clearTimeout(backoffClearTimer);
    backoffClearTimer = setTimeout(() => { backoffClearTimer = null; if (conn.state === "online") conn.backoff = 800; }, 2000);
  }

  function safeJson(raw) {
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
  }

  function noteRev(r) {
    if (typeof r === "number" && r > rev) rev = r;
  }

  // R7-2: validate the complete discriminated polling response envelope before
  // any rev/cursor mutation. The server's /__data/events endpoint returns
  // either a typed resync signal or a normal event feed. Every field that
  // carries ownership semantics must be structurally correct or the entire
  // response is rejected (fail safe: stop this tick, next interval retries).
  // This prevents a malformed envelope from silently advancing revision state
  // or corrupting the cursor while the payload is treated as an empty feed.
  function _isValidPollEnvelope(data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) return false;
    // rev, if present, must be a finite number.
    if ("rev" in data && (typeof data.rev !== "number" || !Number.isFinite(data.rev))) return false;
    if (data.resync === true) {
      // Typed resync branch: doc_revs, if present and non-null, must be a plain
      // object (not array, not string). An explicit null is equivalent to absent.
      if ("doc_revs" in data && data.doc_revs !== null && (typeof data.doc_revs !== "object" || Array.isArray(data.doc_revs))) return false;
      // reason, if present, must be a string.
      if ("reason" in data && typeof data.reason !== "string") return false;
      return true;
    }
    // Normal feed branch: events must be an array. A truthy non-array
    // (string/object) must not be silently coerced to an empty feed.
    if (!Array.isArray(data.events)) return false;
    // Each event must be a non-null object with a valid integer cursor. A
    // single malformed event rejects the entire page (the pagination contract
    // is all-or-nothing per page).
    for (let i = 0; i < data.events.length; i++) {
      const ev = data.events[i];
      if (!ev || typeof ev !== "object" || Array.isArray(ev)) return false;
      if (!Number.isSafeInteger(ev.event_cursor) || ev.event_cursor < 1) return false;
    }
    return true;
  }
  // Expose for deterministic browser tests (no production dependency).
  window.okfLoomLive._isValidPollEnvelopeForTest = _isValidPollEnvelope;

  // ---- unified event dispatch (shared by SSE + polling fallback) -------
  // Both delivery paths route events through here so the page patches
  // identically regardless of transport. noteRev is idempotent.
  function handleEvent(d) {
    if (!d || !d.type) return;
    const cursor = d.event_cursor;
    if (!Number.isSafeInteger(cursor) || cursor < 1) return;
    if (cursor <= lastCursor) return;
    // R7-1: on cursor gap, reset cursor ownership to 0 and trigger a canonical
    // resync, then RETURN without committing the gapped cursor or processing
    // the gapped event. The previous code reset lastCursor to 0 but then
    // immediately assigned the gapped cursor on the next line — so the next
    // poll resumed from the gapped cursor and silently skipped every event
    // between the old cursor and the gap. By returning here, lastCursor stays
    // at 0 until the resync replays the full backlog from the beginning; the
    // cursor is re-advanced naturally as the backlog is consumed. No
    // event-only notifications between the two cursors can be lost.
    if (lastCursor && cursor !== lastCursor + 1) { lastCursor = 0; void doResync(); return; }
    lastCursor = cursor;
    lastEventId = String(cursor);
    noteRev(d.rev);
    switch (d.type) {
      case "changed": onChange(d, "changed"); break;
      case "created": onChange(d, "created"); break;
      case "removed": onRemoved(d); break;
      case "graph":
        emit("graph", d);
        if (window.okfLoomStudio && typeof window.okfLoomStudio.refreshGraph === "function") {
          try { window.okfLoomStudio.refreshGraph(); } catch (e) { console.error(e); }
        }
        break;
      case "presence": emit("presence", d); break;
      case "activity": emit("activity", d); break;
      case "comment": emit("comment", d); break;
      case "comment_link": emit("comment_link", d); break;
      case "agent_conflict": emit("agent_conflict", d); break;
      case "suggestions": emit("suggestions", d); break;
      case "proposal": emit("proposal", d); break;
      // 'ready'/'resync' are SSE lifecycle control frames, not feed events.
    }
  }

  function markSseReceived() {
    sseLastReceived = Date.now();
    // SSE delivered - if we'd fallen back to polling, stop it (SSE is cheaper).
    if (pollingActive) stopPolling();
  }

  // R4-3: the ``ready`` control frame reconciles a reconnect. It is an SSE
  // lifecycle frame (NOT a feed event), so it is handled here at module scope
  // rather than inside the handleEvent switch. Extracted to a named function
  // so the seeding rule is unit-testable without a real EventSource.
  function handleReady(d, readySource) {
    if (readySource && readySource !== conn.source) return;
    markSseReceived();
    if (!d) return;
    // R8-MINOR1: advance rev monotonically via noteRev. The previous direct
    // assignment (`rev = d.rev || rev`) could REGRESS rev when a reconnect
    // delivered a lower/older rev, so a subsequent noteRev-gated change could
    // be ignored. noteRev only advances.
    if (typeof d.rev === "number") noteRev(d.rev);
    setState("online");
    presence.visible = true;
    void reconcilePresenceReady(readySource);
    conn.backoff = 800;
    scheduleBackoffClear();
      // iter1 CRI-001/INTENT-006: consume the Bundle-A `doc_revs` map (P1-6)
      // so a reconnect knows which docs changed while we were offline. If the
      // open concept's rev advanced, force a patch (the polling fallback may
      // have already delivered it, but the event-id guard in onChange makes
      // that idempotent). Older servers without doc_revs are unaffected.
      // R3-2: doc_revs contains concept_content_rev (content hash).
      // lastDocRev now stores the same. Unchanged concept must NOT trigger
      // a patch that invalidates annotation generations/editors.
      if (d.doc_revs && typeof d.doc_revs === "object") {
        const open = currentConceptId();
        // R6-2: a valid complete doc_revs map that does NOT contain the open
        // concept is authoritative absence — the concept was removed. Surface
        // it as open-removed so studio.js invalidates stale UI/marks/editor.
        if (open && !d.doc_revs.hasOwnProperty(open) && isConceptPage()) {
          emit("open-removed", { id: open });
        }
        // R5-11: an unseeded local revision means we have never reconciled
        // the visible page against canonical bytes in this session. The
        // initial SSR HTML and this ready frame do NOT share a verified
        // snapshot boundary — the page may have been served from a stale
        // cache or rendered before a concurrent edit landed. Seeding
        // lastDocRev from ready.doc_revs alone would bless those stale bytes
        // and keep them stale until another change arrives.
        //
        // Reconcile with ONE canonical fetch: patchOpenConcept fetches the
        // authoritative doc and applies a block-level diff. When the SSR is
        // already current the diff is empty (no editor/annotation-generation
        // invalidation, no pulse) and lastDocRev is seeded from the fetched
        // concept_content_rev. Only when the SSR is genuinely stale does the
        // patch advance the rendered bytes. lastDocRev is NEVER seeded on a
        // failed/aborted fetch (it stays null so the next ready retries).
        //
        // A genuine post-connect content change still arrives as a `changed`
        // event (or a later ready with a divergent rev) and patches then.
        const seen = open ? lastDocRev[open] : null;
        const fresh = open ? d.doc_revs[open] : null;
        if (open && (typeof fresh === "string" || typeof fresh === "number")) {
          if (seen == null) {
            patchOpenConcept(open, /*pulse*/ true).catch((err) => console.error("[okf-live] initial-ready reconciliation failed", err));
          } else if (String(fresh) !== String(seen)) {
            patchOpenConcept(open, /*pulse*/ true).catch((err) => console.error("[okf-live] ready-resync patch failed", err));
          }
        }
      }
  }

  // ---- changed / created handler ---------------------------------------
  // For the open concept: fetch the doc and apply it in place. Preserve
  // scroll/focus/drafts (studio.js.applyDoc owns the view-aware swap; the
  // comment composer + open panels are in separate DOM, untouched).
  function onChange(d, kind) {
    if (!d) return;
    noteRev(d.rev);
    const open = currentConceptId();
    const ids = Array.isArray(d.ids) ? d.ids : [];
    const affectsOpen = ids.indexOf(open) >= 0;

    // Always fan out so the change list / comments / presence update.
    emit(kind, d);

    if (!affectsOpen) return;
    // iter1 CRI-001: dedupe across SSE + polling so the same change can't
    // trigger two body patches / two pulses. The panels still update via
    // the emit() above (idempotent upserts), so the change list and
    // comments stay correct; only the expensive DOM patch is gated.
    const key = eventKey(d);
    if (wasApplied(key)) return;
    markApplied(key);
    patchOpenConcept(open, /*pulse*/ true).catch((e) => {
      console.error("[okf-live] patch failed", e);
    });
  }

  function onRemoved(d) {
    if (!d) return;
    noteRev(d.rev);
    emit("removed", d);
    const open = currentConceptId();
    if ((Array.isArray(d.ids) ? d.ids : []).indexOf(open) >= 0) {
      // The open concept was deleted. Surface honestly without a hard
      // reload; the user can navigate away.
      emit("open-removed", { id: open });
    }
  }

  // ---- derived reading regions ----------------------------------------
  // Like patchOpenConcept below, notifications only invalidate: read canonical
  // server rendering, then patch the smallest owned regions. Reusing the page's
  // GET keeps index grouping/recency and typed Connections identical to SSR,
  // including on public ingress and --no-edit. Never transplant head/bootstrap,
  // authored body, chrome, menus or the comment composer from that response.
  let derivedTimer = null;
  let derivedAbort = null;
  let derivedGeneration = 0;
  let derivedSuspended = false;
  const connectionSelector = ".okf-connections-disclosure, .okf-page__connections";
  function cancelDerivedRefresh() {
    ++derivedGeneration;
    clearTimeout(derivedTimer);
    derivedTimer = null;
    if (derivedAbort) derivedAbort.abort();
    derivedAbort = null;
  }
  function scheduleDerivedRefresh() {
    cancelDerivedRefresh(); // invalidate immediately, not when the timer fires
    if (derivedSuspended || !document.querySelector(".okf-viewer--index, article.okf-page__main")) return;
    derivedTimer = setTimeout(refreshDerivedRegions, 250);
  }
  // Retain keyed rows and descendants, even when a card changes type group.
  // Reader-owned disclosure/filter state is not server state.
  function derivedKey(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) return String(node.nodeType);
    if (node.matches(".okf-card__comments")) return node.tagName + "|comment-count";
    const classes = (node.getAttribute("class") || "").split(/\s+/).filter((name) => name !== "okf-presence-focus").join(" ");
    return [node.tagName, classes, node.id,
      node.getAttribute("data-concept-id") || "", node.getAttribute("data-index-type") || "",
      node.getAttribute("value") || ""].join("|");
  }
  function patchDerivedNode(node, fresh, rows) {
    if (node.nodeType !== Node.ELEMENT_NODE) {
      if (node.nodeValue !== fresh.nodeValue) node.nodeValue = fresh.nodeValue;
      return;
    }
    const readerAttribute = (name) => name === "open" ||
      (name === "hidden" && node.matches(".okf-card, [data-index-section]"));
    Array.from(node.attributes).forEach((attr) => {
      if (!readerAttribute(attr.name) && !fresh.hasAttribute(attr.name)) node.removeAttribute(attr.name);
    });
    Array.from(fresh.attributes).forEach((attr) => {
      if (!readerAttribute(attr.name) && node.getAttribute(attr.name) !== attr.value) node.setAttribute(attr.name, attr.value);
    });
    const unused = new Set(node.childNodes), available = new Map();
    unused.forEach((child) => {
      const key = derivedKey(child);
      if (!available.has(key)) available.set(key, {nodes: [], next: 0});
      available.get(key).nodes.push(child);
    });
    let cursor = node.firstChild;
    Array.from(fresh.childNodes).forEach((child) => {
      const key = derivedKey(child);
      let retained = child.nodeType === Node.ELEMENT_NODE && child.matches(".okf-card, .okf-conn")
        ? rows.get(child.getAttribute("data-concept-id")) : null;
      if (!retained) {
        const bucket = available.get(key);
        // Repeated text/element keys keep their original order. Skip entries
        // already claimed by a keyed row without rescanning the sibling list.
        while (bucket && bucket.next < bucket.nodes.length && !unused.has(bucket.nodes[bucket.next])) bucket.next++;
        if (bucket && bucket.next < bucket.nodes.length) retained = bucket.nodes[bucket.next++];
      }
      unused.delete(retained);
      if (!retained) retained = child.cloneNode(false);
      patchDerivedNode(retained, child, rows);
      if (retained !== cursor) node.insertBefore(retained, cursor);
      cursor = retained.nextSibling;
    });
    unused.forEach((child) => { if (child.parentNode === node) child.remove(); });
  }
  function patchDerivedRegions(fresh) {
    const indexPage = document.body.classList.contains("okf-viewer--index");
    if (indexPage !== fresh.body.classList.contains("okf-viewer--index")) return;
    const x = window.scrollX, y = window.scrollY, active = document.activeElement;
    const derivedFocus = active && active.closest && active.closest(".okf-card, .okf-type-nav, " + connectionSelector);
    const selection = window.getSelection();
    const savedSelection = selection && selection.rangeCount ? {
      anchor: selection.anchorNode, start: selection.anchorOffset,
      focus: selection.focusNode, end: selection.focusOffset,
    } : null;
    const rows = new Map();
    if (indexPage) {
      document.querySelectorAll(".okf-card[data-concept-id]").forEach((row) => rows.set(row.dataset.conceptId, row));
      // Keep selects themselves (and any native popup) alive.
      [".okf-hero__stats", "select[data-index-type]", "[data-index-empty]"].forEach((selector) => {
        const node = document.querySelector(selector), next = fresh.querySelector(selector);
        if (!node || !next) return;
        const value = node.value;
        patchDerivedNode(node, next, rows);
        if (node.tagName === "SELECT") node.value = Array.from(node.options).some((option) => option.value === value) ? value : "";
      });
      const host = document.querySelector("main.okf-index");
      if (!host) return;
      const selector = ".okf-type-nav, [data-index-section]";
      const old = Array.from(host.querySelectorAll(selector));
      const next = Array.from(fresh.querySelectorAll("main.okf-index > :is(.okf-type-nav, [data-index-section])"));
      const retained = new Set();
      // Keep new/empty inventories in the same place, without a blank frame.
      const marker = document.createComment("index inventory");
      if (old.length) old[old.length - 1].after(marker);
      else host.insertBefore(marker, host.querySelector(".okf-index__overview"));
      next.forEach((section) => {
        const node = old.find((candidate) => derivedKey(candidate) === derivedKey(section)) || section.cloneNode(false);
        patchDerivedNode(node, section, rows);
        retained.add(node);
        if (node.nextSibling !== marker) host.insertBefore(node, marker);
      });
      old.forEach((node) => { if (!retained.has(node)) node.remove(); });
      marker.remove();
      document.body.dataset.okfConceptRoutes = fresh.body.dataset.okfConceptRoutes || "{}";
    } else {
      const host = document.querySelector("article.okf-page__main");
      if (!host || !fresh.querySelector("article.okf-page__main")) return;
      const old = host.querySelector(connectionSelector), next = fresh.querySelector(connectionSelector);
      const after = old ? old.nextSibling : host.querySelector(".okf-prevnext, .okf-document-end");
      // Outgoing and incoming rows can name the same concept: their identities
      // stay scoped to each list, not one global row pool.
      if (next) {
        let node = old && old.tagName === next.tagName ? old : next.cloneNode(false);
        if (old && old.tagName !== next.tagName) {
          // Crossing the disclosure threshold must not discard focused links
          // or a selection in the common Connections navigation.
          const nav = old.matches(".okf-page__connections") ? old : old.querySelector(".okf-page__connections");
          if (next.matches(".okf-page__connections")) node = nav;
          else {
            if (nav.contains(active) || (savedSelection && nav.contains(savedSelection.anchor))) node.open = true;
            node.appendChild(nav);
          }
        }
        patchDerivedNode(node, next, rows);
        if (node !== old) {
          host.insertBefore(node, after);
          if (old && old.parentNode === host) old.remove();
        }
      } else if (old) old.remove();
    }
    window.dispatchEvent(new CustomEvent("okf-loom:derivedPatched"));
    if (active && active.isConnected && document.activeElement !== active && typeof active.focus === "function") active.focus({ preventScroll: true });
    else if (derivedFocus && !active.isConnected) {
      const fallback = document.querySelector("main.okf-index, article.okf-page__main");
      fallback.setAttribute("tabindex", "-1");
      fallback.focus({ preventScroll: true });
    }
    if (savedSelection && savedSelection.anchor.isConnected && savedSelection.focus.isConnected &&
        (selection.anchorNode !== savedSelection.anchor || selection.anchorOffset !== savedSelection.start ||
         selection.focusNode !== savedSelection.focus || selection.focusOffset !== savedSelection.end)) {
      try { selection.setBaseAndExtent(savedSelection.anchor, savedSelection.start, savedSelection.focus, savedSelection.end); } catch (e) { /* selected content was removed */ }
    }
    window.scrollTo(x, y);
  }
  async function refreshDerivedRegions() {
    derivedTimer = null;
    const own = derivedGeneration, nav = _navEpoch, path = location.pathname;
    const controller = new AbortController();
    derivedAbort = controller;
    const deadline = setTimeout(() => controller.abort(), 10000);
    controller.signal.addEventListener("abort", () => clearTimeout(deadline), { once: true });
    try {
      const response = await fetch(path, { cache: "no-store", headers: { Accept: "text/html" }, signal: controller.signal });
      if (!response.ok) return;
      const html = await response.text();
      if (controller.signal.aborted || derivedSuspended || own !== derivedGeneration || nav !== _navEpoch || path !== location.pathname) return;
      patchDerivedRegions(new DOMParser().parseFromString(html, "text/html"));
    } catch (e) { /* Keep visible content on read failure; next notification retries. */ }
    finally {
      clearTimeout(deadline);
      if (derivedAbort === controller) derivedAbort = null;
    }
  }
  ["changed", "created", "removed", "graph", "resync"].forEach((type) => on(type, scheduleDerivedRefresh));
  on("conn", (data) => { if (data.state === "online") scheduleDerivedRefresh(); });

  // ---- the in-place patch (sacred state-preservation contract) ---------
  // Per-concept fetch sequence: two rapid `changed` events fire two
  // concurrent /__data/doc fetches, and without a fence the OLDER response
  // can resolve last and overwrite the newer body. Each call
  // takes a ticket; only the holder of the latest ticket may apply.
    const _docFetchSeq = {};
    let _testDisconnectEpoch = 0;
    let _navEpoch = 0;
    // R4-1: the production navigation/route owner. ``_navEpoch`` is the
    // COMMIT fence: a delayed response captured against a stale epoch is
    // rejected even if the route string cycled back to the same concept
    // (the real A→B→A race — by the time the late A response arrives,
    // ``currentConceptId()`` is "A" again, so the route-string check alone
    // cannot fence it). ``_routeAbort`` is the IN-FLIGHT cancellation
    // owner: it aborts the outstanding /__data/doc fetch promptly on
    // route/page departure so the work stops instead of relying on the
    // commit fence alone. Both advance together on popstate/pagehide.
    let _routeAbort = new AbortController();
    function _advanceRouteOwner() {
      cancelDerivedRefresh();
      _navEpoch++;
      try { _routeAbort.abort("navigation"); } catch (e) {}
      _routeAbort = new AbortController();
    }
    // R3-1 seam (kept for the existing fence test): advances/checks the
    // epoch and aborts route fetches.
    function _bumpNavEpoch() { _advanceRouteOwner(); }
    function studioDocumentPublication() {
      const s = window.okfLoomStudio && window.okfLoomStudio.state;
      return s && typeof s.documentPublication === "number" ? s.documentPublication : null;
    }
    function studioShownRev() {
      const s = window.okfLoomStudio && window.okfLoomStudio.state;
      return (s && s.doc && s.doc.concept_content_rev) || null;
    }
    async function patchOpenConcept(conceptId, pulse, rereadAfterPublication) {
        const disconnectEpoch = _testDisconnectEpoch;
        const navEpoch = _navEpoch;
        const routeSignal = _routeAbort.signal;
        // Q4: Studio's publication counter at request start (see the fence
        // after the response below).
        const publication = studioDocumentPublication();
        // R4-4: capture the previously-rendered content rev for this concept
        // BEFORE applying the patch so we can signal a content-revision
        // advance (which forces studio.js to canonical-refetch comment
        // resolutions and recompute marks).
        // R6-2: when lastDocRev[id] is unseeded (first-ever reconcile, e.g.
        // the initial-ready path), fall back to the rev ACTUALLY rendered by
        // studio.js (state.doc.concept_content_rev). The SSR HTML and the
        // ready frame do NOT share a verified snapshot boundary, so seeding
        // from lastDocRev alone would miss a real SSR→canonical replacement
        // and never emit doc-rev-changed — leaving revision-qualified
        // comments/marks stale. Comparing against the actually-rendered rev
        // makes the initial-ready canonical replacement atomically trigger a
        // comment/mark refresh.
        var fallbackRenderedRev = null;
        try {
          if (window.okfLoomStudio && window.okfLoomStudio.state && window.okfLoomStudio.state.doc) {
            var fr = window.okfLoomStudio.state.doc.concept_content_rev;
            if (typeof fr === "string" && fr) fallbackRenderedRev = fr;
          }
        } catch (e) {}
        const priorContentRev = (conceptId ? lastDocRev[conceptId] : null) || fallbackRenderedRev;
      const seq = (_docFetchSeq[conceptId] = (_docFetchSeq[conceptId] || 0) + 1);
      let res;
      try {
        res = await fetch("/__data/doc?id=" + encodeURIComponent(conceptId), {
          headers: { Accept: "application/json" },
          signal: routeSignal,
        });
      } catch (err) {
        // R4-1: route departure (navigation/pagehide/disconnect) aborts the
        // in-flight fetch via the route owner. A superseded/aborted fetch is
        // the expected terminal state, not a transport error to surface.
        if (routeSignal.aborted || (err && err.name === "AbortError")) return;
        throw err;
      }
      if (!res.ok) throw new Error("doc fetch " + res.status);
      let doc;
      try {
        doc = await res.json();
      } catch (err) {
        // R4-1: cancellation can also land after the response headers but
        // before the body is consumed — route departure aborts the in-flight
        // owner at any await point. A body-read abort for the departed route
        // is the same expected terminal state as an aborted fetch, not a
        // transport error to surface. Malformed JSON and genuine transport or
        // HTTP failures for the current owner still escape below; the
        // route/sequence commit fences stay after this boundary.
        if (routeSignal.aborted || (err && err.name === "AbortError")) return;
        throw err;
      }
        // R3-1/R4-1: verify route/session hasn't changed since fetch started.
        // A delayed concept-A response must never replace concept B's DOM,
        // and must not replace A's DOM after A→B→A: navEpoch advanced on
        // each hop, so the captured epoch no longer matches even though
        // currentConceptId() has cycled back to "A".
        if (_navEpoch !== navEpoch) return;
        if (currentConceptId() !== conceptId) return;
        if (_docFetchSeq[conceptId] !== seq || disconnectEpoch !== _testDisconnectEpoch) return;
        // Q4: Studio committed a document of ANOTHER revision while this read
        // was in flight (a comments load, a text capture, or Studio's own
        // first publication, which no generation counts). This response was
        // read before what the page now shows: read the canonical document
        // once more instead of committing it. A same-revision answer commits.
        if (publication != null && studioDocumentPublication() !== publication) {
          const shownRev = studioShownRev();
          if (shownRev && shownRev !== doc.concept_content_rev) {
            if (!rereadAfterPublication) return patchOpenConcept(conceptId, pulse, true);
            return;
          }
        }

    // Capture the bits we promise to preserve BEFORE the DOM mutates.
    const savedScrollX = window.scrollX;
    const savedScrollY = window.scrollY;
    const active = document.activeElement;
    const activeId = active && active.id ? active.id : null;

    // Delegate the view-aware swap to studio.js when present; else fall
    // back to a plain rendered-body swap so live.js is self-sufficient.
    let studioHandledPulse = false;
    if (window.okfLoomStudio && typeof window.okfLoomStudio.applyDoc === "function") {
      // studio.js.applyDoc owns the block-level diff (iter1 CRI-001) and
      // preserves in-body selection across the patch. It also re-applies
      // comment marks (CRI-002), repositions margin markers, AND pulses
      // only the changed blocks (scoped pulse) - so live.js must NOT also
      // pulse the whole body. applyDoc returns true when it handled the
      // pulse itself.
      try { studioHandledPulse = !!window.okfLoomStudio.applyDoc(doc, { pulse: pulse }); }
      catch (e) { console.error("[okf-live] applyDoc threw", e); studioHandledPulse = false; }
    } else {
      // Fallback (no studio): preserve selection across the whole-body swap.
      const selSnapshot = saveBodySelection();
      const body = document.querySelector(".okf-page__body");
      if (body) body.innerHTML = doc.html || "";
      restoreBodySelection(selSnapshot);
      // studio.js dispatches this after its block diff; the fallback swap
      // must too, or renderers.js never re-runs (mermaid/hljs/KaTeX plus
      // the table/code/image enhancers) over the fresh body.
      try { window.dispatchEvent(new CustomEvent("okf-loom:bodyPatched")); } catch (e) {}
    }

      // R5-3: Record what we rendered so the ready/doc_revs path can detect
      // drift — but ONLY when applyDoc actually committed the document.
      // If studio.js returned false (DOM patch failed) or threw, we must
      // NOT advance lastDocRev; live.js must not claim success on a
      // partially-applied document.
      // R3-2: store concept_content_rev (content hash), NOT doc.rev (bundle
      // revision), so unchanged reconnect does not invalidate generation/editor.
      // R6-7: the fallback path (no studio.js) also seeds lastDocRev on a
      // successful patch so the ready/doc_revs path can detect drift.
      var patchSucceeded = studioHandledPulse !== false;
      if (!window.okfLoomStudio || typeof window.okfLoomStudio.applyDoc !== "function") {
        patchSucceeded = true; // fallback path always succeeds (innerHTML swap)
      }
        if (conceptId && doc.concept_content_rev && patchSucceeded) {
          // R4-4: a live network patch that advanced the content revision
          // leaves persisted comment resolutions keyed to the OLD revision.
          // Signal studio.js to canonical-refetch comments and recompute
          // marks/health against the new bytes. Only emitted when we had a
          // real prior baseline (the first-ever render seeds the baseline
          // silently below).
          if (priorContentRev && priorContentRev !== doc.concept_content_rev) {
            emit("doc-rev-changed", { id: conceptId, concept_content_rev: doc.concept_content_rev });
          }
          lastDocRev[conceptId] = doc.concept_content_rev;
        }

    // R6-8: the post-commit side effects (scroll/focus restore, fallback
    // pulse, "patched" emit) describe a SUCCESSFUL patch. When applyDoc
    // returned false (staging failed / DOM patch rejected) we must NOT pulse,
    // steal focus, or announce "patched" — that would claim success for a
    // document that was not applied. lastDocRev seeding above is already
    // gated on patchSucceeded; gate the rest the same way.
    if (patchSucceeded) {
      // Restore scroll exactly (the page header/sidebar are unchanged, so
      // pixel-identical scroll reads as "no jump").
      window.scrollTo(savedScrollX, savedScrollY);

      // Restore focus if the previously-focused element still exists. If it
      // lived inside the swapped body it's gone - move focus to the body
      // container so keyboard users aren't dropped.
      if (activeId) {
        const restored = document.getElementById(activeId);
        if (restored && typeof restored.focus === "function") {
          try { restored.focus({ preventScroll: true }); } catch (e) {}
        } else {
          const body = document.querySelector(".okf-page__body");
          if (body) { body.setAttribute("tabindex", "-1"); try { body.focus({ preventScroll: true }); } catch (e) {} }
        }
      }

      // iter1 CRI-001: only the fallback path pulses the whole body. When
      // studio.js ran, it already pulse-highlighted the individual changed
      // blocks (scoped pulse), so a body-wide flash would double up.
      if (pulse && !studioHandledPulse) pulseHighlight();
      emit("patched", { id: conceptId, rev: doc.rev });
    }
  }

  // ---- selection save/restore (fallback path) -------------------------
  // Captures the selected text + its character offsets within the body so a
  // whole-body innerHTML swap can attempt to restore the same selection.
  // Best-effort: if the selected text no longer exists after the swap, the
  // selection is simply cleared (no false match). The studio.js path uses a
  // richer block-aware restore (see studio.js saveSelectionAcrossPatch).
  function saveBodySelection() {
    const sel = window.getSelection && window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    const body = document.querySelector(".okf-page__body");
    if (!body || !body.contains(sel.anchorNode)) return null;
    return { text: sel.toString() };
  }
  function restoreBodySelection(snapshot) {
    if (!snapshot || !snapshot.text) return;
    const body = document.querySelector(".okf-page__body");
    if (!body) return;
    const found = findTextNode(body, snapshot.text);
    if (!found) return;
    const range = document.createRange();
    range.setStart(found.node, found.start);
    range.setEnd(found.node, found.end);
    const sel = window.getSelection();
    if (!sel) return;
    sel.removeAllRanges();
    try { sel.addRange(range); } catch (e) {}
  }
  function findTextNode(root, text) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
      const idx = node.nodeValue ? node.nodeValue.indexOf(text) : -1;
      if (idx >= 0) return { node: node, start: idx, end: idx + text.length };
    }
    return null;
  }

  function pulseHighlight() {
    if (REDUCED_MOTION) return; // respect preference: no flash
    const body = document.querySelector(".okf-page__body");
    if (!body) return;
    body.classList.remove("okf-pulse");
    // Force reflow so the animation restarts on consecutive patches.
    void body.offsetWidth;
    body.classList.add("okf-pulse");
  }

  // ---- resync ----------------------------------------------------------
  // R6-1: one generation-fenced canonical doc+comments resync. The
  // emit("resync") fires studio.js loadComments() which fetches
  // /__comments + /__data/doc atomically through commitDocument.
  // If studio.js is absent, fall back to patchOpenConcept. The generation
  // fence ensures repeated responses don't loop: a newer doResync
  // superseds an older one, and the older one's post-await commit is
  // skipped.
  // R7-5: the latest pending doc_revs payload is retained so a newer
  // doResync(newDocRevs) that arrives while a resync is in flight is DRAINED
  // after the current operation settles — not lost and not replaced by the
  // older in-flight call's stale docRevs.
  // R10-BFCACHE: a queued doResync now returns a DEFERRED promise that resolves
  // only when the drained resync actually completes (not immediately). This
  // closes the intersection where a persisted pageshow arrives while an OLD
  // resync is still in flight: previously pageshow's doResync queued pending
  // and resolved instantly, so connect() ran before the restored lifecycle's
  // canonical reconciliation, and the suspended old op then cleared the
  // restore-epoch pending. Now the deferred keeps the pageshow caller waiting
  // until the drain settles, and the suspend-epoch check preserves (drains)
  // pending queued by a LATER lifecycle while discarding only same-epoch
  // stale pending.
    let _resyncGeneration = 0;
    let _resyncInFlight = false;
    // R11-BFCACHE: the pending slot now carries a LIST of drain waiters, not a
    // single deferred. When a newer doResync supersedes an already-queued one,
    // every accumulated waiter MUST stay pending and resolve only after the
    // LATEST queued payload drains to a terminal state. The previous single-
    // waiter design resolved the superseded deferred immediately at queue time,
    // so a pageshow (BFCache restore) caller could run connect() while the
    // replacement reconciliation was still pending.
    // Slot shape: { docRevs, waiters: [{ resolve, reject, suspendEpoch }] }.
    let _pendingResync = null;
    // R9-RESYNC-FENCE: a suspend epoch that _suspend advances. doResync captures
    // it at entry and re-checks after its awaited handlers; if the page was
    // suspended mid-resync, the post-await commit/drain is fenced (the restored
    // lifecycle runs its own canonical resync on pageshow). This is distinct from
    // _resyncGeneration, which a newer doResync (same lifecycle) also bumps — the
    // suspend epoch isolates "page frozen" from "superseded by a newer resync".
    let _resyncSuspendEpoch = 0;
    // R11-BFCACHE: drain the latest queued payload and resolve/reject EVERY
    // accumulated waiter only after that drained resync reaches a terminal
    // state. Declared as a hoisted function so doResync can reference it.
    function _drainResyncWaiters(slot) {
      return doResync(slot.docRevs).then(
        function (value) {
          for (let i = 0; i < slot.waiters.length; i++) {
            try { slot.waiters[i].resolve(value); } catch (e) {}
          }
        },
        function (err) {
          for (let i = 0; i < slot.waiters.length; i++) {
            try { slot.waiters[i].reject(err); } catch (e) {}
          }
        }
      );
    }
    async function doResync(docRevs) {
      // R6-1: if a resync is already in flight, bump the generation so the
      // in-flight one knows it's superseded, but don't start a parallel one.
      const myGen = ++_resyncGeneration;
      const mySuspendEpoch = _resyncSuspendEpoch;
      if (_resyncInFlight) {
        // R7-5: retain the LATEST payload — a newer doResync(newDocRevs)
        // supersedes any older pending payload. The in-flight operation drains
        // exactly this value after it settles.
        // R10-BFCACHE: return a promise representing completion of the
        // QUEUED/drained resync. A pageshow (BFCache restore) that arrives while
        // an old resync is in-flight must AWAIT the drained restore before
        // reconnecting; previously this resolved instantly and connect() ran
        // before canonical reconciliation completed.
        // R11-BFCACHE: preserve EVERY pending waiter. A superseding doResync no
        // longer resolves the older waiter at queue time — all waiters resolve
        // together once the LATEST queued payload drains. The earlier single-
        // waiter design resolved the superseded deferred immediately, letting
        // pageshow call connect() while the replacement reconciliation was still
        // pending.
        let pResolve, pReject;
        const deferred = new Promise(function (res, rej) { pResolve = res; pReject = rej; });
        const waiter = { resolve: pResolve, reject: pReject, suspendEpoch: mySuspendEpoch };
        if (_pendingResync) {
          _pendingResync.docRevs = docRevs || null; // latest payload wins
          _pendingResync.waiters.push(waiter);
        } else {
          _pendingResync = { docRevs: docRevs || null, waiters: [waiter] };
        }
        return deferred;
      }
    _resyncInFlight = true;
    var suspended = false;
    try {
      // R6-1: surface doc_revs (when available from a typed polling resync
      // envelope) so studio.js's resync handler can read per-concept revs.
      // R8-RESYNC: collect handler return values; studio.js's resync handler
      // returns the loadComments() promise. Await it so `_resyncInFlight`
      // spans the REAL asynchronous refresh, not merely the synchronous emit
      // — otherwise a newer doResync sees the in-flight flag already cleared
      // while the actual refresh work is still running, and the two overlap.
      const handlerResults = emit("resync", { doc_revs: docRevs || null });
      const open = currentConceptId();
      if (open && document.querySelector(".okf-page__body")) {
        if (window.okfLoomStudio && typeof window.okfLoomStudio.applyDoc === "function") {
          // R8-RESYNC: studio.js owns the canonical refresh (loadComments
          // fetches doc+comments atomically through commitDocument). Await
          // any refresh promises returned by resync handlers so doResync
          // settles only once the Studio refresh is actually complete.
          const refreshPromises = [];
          for (let i = 0; i < handlerResults.length; i++) {
            const r = handlerResults[i];
            if (r && typeof r.then === "function") refreshPromises.push(r);
          }
          if (refreshPromises.length) {
            try { await Promise.all(refreshPromises); } catch (e) { /* handler logs internally */ }
          }
        } else {
          // R6-1: only fall back to patchOpenConcept when studio.js is absent.
          try { await patchOpenConcept(open, false); } catch (e) { console.error(e); }
        }
      }
      // R9-RESYNC-FENCE: re-check the suspend epoch after the awaited handlers.
      // A pagehide/pagefreeze during the refresh bumped the epoch; this resync
      // (captured by the prior lifecycle) must not commit post-await state or
      // drain a pending payload that belongs to the restored lifecycle.
      if (mySuspendEpoch !== _resyncSuspendEpoch) suspended = true;
    } finally {
      _resyncInFlight = false;
    }
      if (suspended) {
        // R9-RESYNC-FENCE: the page was suspended during the resync.
        // R10/R11/R12-BFCACHE: order every accumulated waiter against the
        // LATEST drain. Waiters are partitioned by suspend epoch only to decide
        // WHAT drains and whether a later reconciliation exists to order against:
        //   - If restore-epoch waiters exist, a later reconciliation IS pending.
        //     Chain EVERY waiter (restore AND stale) to that restore drain so all
        //     resolve together once the latest queued payload reaches terminal —
        //     no waiter settles before it. (The latest payload in slot.docRevs is
        //     the restore payload, since restore-epoch doResync calls queue after
        //     any same-epoch stale ones, so a stale PAYLOAD is never separately
        //     drained.) The previous code resolved stale waiters IMMEDIATELY at
        //     this point, so across repeated pagehide/pageshow cycles an earlier
        //     restore waiter could settle while the latest reconciliation
        //     remained pending.
        //   - If NO restore-epoch work exists, there is no later reconciliation
        //     to order against, so resolving the stale waiters now cannot settle
        //     anything "before the latest drain". Draining the stale payload
        //     would risk committing state for an abandoned lifecycle
        //     (R9-RESYNC-FENCE), so resolve the waiters without draining and let
        //     the restored/fresh lifecycle run its own resync.
        if (_pendingResync) {
          const slot = _pendingResync;
          _pendingResync = null;
          const restoreWaiters = [];
          const staleWaiters = [];
          for (let i = 0; i < slot.waiters.length; i++) {
            const w = slot.waiters[i];
            if (w.suspendEpoch > mySuspendEpoch) restoreWaiters.push(w);
            else staleWaiters.push(w);
          }
          if (restoreWaiters.length) {
            return _drainResyncWaiters({ docRevs: slot.docRevs, waiters: staleWaiters.concat(restoreWaiters) });
          }
          for (let j = 0; j < staleWaiters.length; j++) { try { staleWaiters[j].resolve(); } catch (e) {} }
        }
        return;
      }
      // R7-5: if a newer doResync arrived while we were in flight, drain the
      // LATEST pending payload (not this call's potentially-stale local
      // docRevs). R10/R11-BFCACHE: chain ALL accumulated deferreds to the
      // drained resync so every queued caller awaits the ACTUAL drained
      // completion — an earlier caller (e.g. pageshow) must not resolve before
      // the latest queued resync reaches a terminal state.
      if (_pendingResync) {
        const pending = _pendingResync;
        _pendingResync = null;
        return _drainResyncWaiters(pending);
      }
    }

  // Expose for studio.js / debugging / the command palette.
  window.okfLoomLive.patchNow = () => patchOpenConcept(currentConceptId(), true);
  window.okfLoomLive.resync = doResync;
  window.okfLoomLive.currentConceptId = currentConceptId;
  // Deterministic browser-test seam: routes a synthetic notification through
  // the same cursor/dedupe/refetch dispatcher as SSE and polling.
   window.okfLoomLive._dispatchEventForTest = handleEvent;
   window.okfLoomLive._pollOnceForTest = pollOnce;
   window.okfLoomLive._connectForTest = connect;
    window.okfLoomLive._bumpNavEpoch = _bumpNavEpoch;
    window.okfLoomLive._getNavEpoch = function() { return _navEpoch; };
    // R4: minimal read-only lifecycle seams for deterministic browser tests.
    // No production behaviour depends on these.
    window.okfLoomLive._dispatchReadyForTest = handleReady;
    // R8-POLL: deterministic polling-lifecycle seams so browser tests can drive
    // start/stop (and thus the generation+abort owner) without waiting for the
    // 45s starvation watchdog. No production behaviour depends on these.
    window.okfLoomLive._startPollingForTest = startPolling;
    window.okfLoomLive._stopPollingForTest = stopPolling;
    window.okfLoomLive._getLastDocRevForTest = function (id) { return id ? lastDocRev[id] : null; };
    window.okfLoomLive._clearLastDocRevForTest = function (id) { if (id) delete lastDocRev[id]; };
    window.okfLoomLive._seedLastDocRevForTest = function (id, value) { if (id) lastDocRev[id] = value; };
    window.okfLoomLive._routeAbortedForTest = function () { return _routeAbort.signal.aborted; };
      window.okfLoomLive._disconnectForTest = function () {
        _testDisconnectEpoch++;
        // R6-3: use the same _suspend path as production pagehide.
        _suspend();
      };
  // Bundle C / INTENT-004: minimal debug accessors so the browser test
  // (tests/test_studio_iter1_browser.py::test_sse_watchdog_quiet_bundle_*)
  // can verify that a HEALTHY idle stream (heartbeat-only) never arms the
  // polling fallback. Read-only — no production behaviour depends on these.
  Object.defineProperty(window.okfLoomLive, "_debug", {
    get() {
      return {
        pollingActive,
        pollingGeneration,
        pollingTimerPending: pollingTimer !== null,
        pollingAbortAborted: pollingAbort ? pollingAbort.signal.aborted : null,
        backoffClearPending: backoffClearTimer !== null,
          resyncInFlight: _resyncInFlight,
          resyncGeneration: _resyncGeneration,
          resyncSuspendEpoch: _resyncSuspendEpoch,
          resyncPendingWaiters: _pendingResync ? _pendingResync.waiters.length : 0,
        sseLastReceived,
        graceMs: SSE_GRACE_MS,
        heartbeatMs: HEARTBEAT_INTERVAL_MS,
        connState: conn.state,
        connSourceOpen: !!(conn.source && conn.source.readyState === EventSource.OPEN),
        connSource: !!(conn.source),
        lastCursor,
      };
    },
    configurable: true,
  });

  // ---- synchronous suspend (every pagehide) + BFCache restore ----------
  // R6-3: suspend MUST close the EventSource and clear ALL timers
  // (polling interval, reconnect backoff, watchdog, backoff-clear) so no
  // late callback can fire on a frozen/cached page. Advances route/disconnect
  // owners so in-flight fetches are aborted and late responses are fenced.
  // R8-MINOR2: the backoff-clear timer is also cancelled here.
  function _suspend() {
    derivedSuspended = true;
    _advanceRouteOwner();
    // R9-RESYNC-FENCE: advance the suspend epoch so an in-flight doResync's
    // post-await commit/drain is fenced. The in-flight operation clears
    // _resyncInFlight in its finally, but it must not commit canonical state
    // or drain a pending payload that belongs to the restored lifecycle.
    _resyncSuspendEpoch++;
    if (conn.source) { try { conn.source.close(); } catch (e) {} conn.source = null; }
    stopPolling();
    if (conn.timer) { clearTimeout(conn.timer); conn.timer = null; }
    if (watchdogTimer) { clearInterval(watchdogTimer); watchdogTimer = null; }
    if (backoffClearTimer) { clearTimeout(backoffClearTimer); backoffClearTimer = null; }
    suspendPresence(true);
  }

  // ---- boot ------------------------------------------------------------
  // Connect once DOM is ready (head wiring is before </head>; the body may
  // not be parsed yet). We also re-run presence/highlight wiring after load.
  // The starvation watchdog runs every 2s: if SSE hasn't delivered in the
  // grace window (proxy buffering), it arms the polling fallback.
  let watchdogTimer = null;
  function boot() {
    connect();
    wirePresence();
    watchdogTimer = setInterval(starvationCheck, 2000);
    // R4-1: production navigation/route owner. A persistent live.js
    // session (SPA-style routing, back/forward, or bfcache restore) must
    // fence in-flight route fetches and advance the commit epoch so a
    // delayed response for a prior route cannot commit after A→B→A.
    window.addEventListener("popstate", _advanceRouteOwner);
    // R6-3: pagehide fires on EVERY navigation AND bfcache freeze. Suspend
    // synchronously — NOT once — so repeated BFCache cycles are handled.
    // Without the persistent listener, a page restored from bfcache would
    // have no pagehide handler for the second departure.
    window.addEventListener("pagehide", _suspend);
    // R6-3: BFCache restore. When the browser serves the page from the
    // back-forward cache (event.persisted === true), the EventSource was
    // killed by _suspend on freeze. Create exactly ONE fresh transport
    // and trigger a canonical resync so the restored page catches up.
    window.addEventListener("pageshow", function (event) {
      if (!event.persisted) return;
      derivedSuspended = false;
      // BFCache restore: SSE source was killed by _suspend on freeze.
      // Reset ALL transport state.
      lastCursor = 0;
      sseLastReceived = 0;
      conn.backoff = 800;
      conn.timer = null;
      stopPolling();
      presence.visible = true;
      if (presence.graph) void restorePresence("bfcache");
      if (!watchdogTimer) watchdogTimer = setInterval(starvationCheck, 2000);
      // R9-BFCACHE: the canonical resync MUST complete BEFORE connect() wires
      // SSE event delivery. The previous code called connect() first, so fresh
      // SSE frames could mutate the restored page while canonical document/
      // comment reconciliation was still pending — contradicting the source
      // comment that canonical reconciliation completes before later events are
      // applied. doResync awaits the async Studio refresh (loadComments fetches
      // doc+comments atomically); only once it settles do we open the transport
      // and start receiving SUBSEQUENT events. The nav-epoch guard prevents a
      // reconnect if the page was suspended again during the resync.
      var restoreNavEpoch = _navEpoch;
      doResync().then(function () {
        if (_navEpoch !== restoreNavEpoch) return; // suspended again mid-resync
        if (conn.source) return; // a concurrent path already reconnected
        connect();
      }, function (e) {
        console.error("[okf-live] bfcache canonical resync failed", e);
        if (_navEpoch !== restoreNavEpoch) return;
        if (!conn.source) connect();
      });
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }

  // On tab refocus, if we've been disconnected, nudge a resync.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") { suspendPresence(false); return; }
    presence.visible = true;
    if (presence.graph) void restorePresence("visibility");
    if (conn.state !== "online") connect();
  });
})();
// R4-SENTINEL-1783823494
