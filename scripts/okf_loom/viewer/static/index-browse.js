/* Progressive index ordering and type filtering; SSR remains the canonical inventory. */
(function () {
  "use strict";
  var controls = document.querySelector("[data-index-browse-controls]");
  if (!controls) return;
  var order = controls.querySelector("[data-index-order]");
  var type = controls.querySelector("[data-index-type]");
  var clear = controls.querySelector("[data-index-clear]");
  var status = controls.querySelector("[data-index-status]");
  var noMatch = document.querySelector("[data-index-no-match]");
  var sections = Array.prototype.slice.call(document.querySelectorAll(".okf-section[data-index-section]"));
  var conceptCount = sections.reduce(function (count, section) {
    return count + section.querySelectorAll(".okf-card").length;
  }, 0);

  function compare(a, b, key) {
    if (key === "recent") {
      // F7: data-index-instant carries the metadata owner's effective change
      // instant, canonicalized to UTC at render time, so plain text order is
      // chronological order (authored offsets already resolved server-side).
      // "" means unknown time: dated cards first, unknown last, then a
      // stable title tie-break.
      var ai = String(a.dataset.indexInstant || "");
      var bi = String(b.dataset.indexInstant || "");
      if (ai !== bi) {
        if (!bi) return -1;
        if (!ai) return 1;
        return ai > bi ? -1 : 1;
      }
      // Equal instants AND unknown-time entries tie-break by concept
      // identity (data-concept-id, plain codepoint order) — the same stable
      // order the server's _recent_concepts_by_instant uses. Localized title
      // text differs per locale and diverged from the server suggestion
      // heads; the identity order never does.
      var aid = String(a.dataset.conceptId || "");
      var bid = String(b.dataset.conceptId || "");
      return aid < bid ? -1 : aid > bid ? 1 : 0;
    }
    if (key === "title") {
      return String(a.dataset.indexTitle || "").localeCompare(String(b.dataset.indexTitle || ""));
    }
    return Number(a.dataset.indexRecommended || 9999) - Number(b.dataset.indexRecommended || 9999) ||
      String(a.dataset.indexTitle || "").localeCompare(String(b.dataset.indexTitle || ""));
  }
  function apply() {
    var selected = type.value;
    var visible = 0;
    sections.forEach(function (section) {
      var list = section.querySelector(".okf-cardgrid");
      var cards = Array.prototype.slice.call(list.querySelectorAll(".okf-card"));
      cards.sort(function (a, b) { return compare(a, b, order.value); }).forEach(function (card) {
        list.appendChild(card);
        var show = !selected || card.dataset.indexType === selected;
        card.hidden = !show;
        if (show) visible++;
      });
      section.hidden = !cards.some(function (card) { return !card.hidden; });
    });
    if (noMatch) noMatch.hidden = !(conceptCount > 0 && !!selected && visible === 0);
    status.textContent = visible + (visible === 1 ? " concept shown." : " concepts shown.");
  }
  order.addEventListener("change", apply);
  type.addEventListener("change", apply);
  clear.addEventListener("click", function () {
    order.value = "recommended";
    type.value = "";
    apply();
    type.focus();
  });
  apply();
}());
