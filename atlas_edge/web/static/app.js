/**
 * Lightweight auto-refresh: re-fetches the current page and swaps in just the
 * region marked `data-autorefresh`, instead of a full `<meta refresh>`
 * navigation. No flash, scroll position and focus outside that region are
 * preserved, and it stops fetching entirely while the tab is hidden.
 *
 * Usage: wrap the part of the page that goes stale in
 *   <div data-autorefresh data-interval="5000"> ... </div>
 * Set `data-active="0"` to skip polls while it's set — for a region that's
 * reached a final state (e.g. a finished job) this is effectively permanent;
 * a page can also flip it back and forth at runtime (e.g. to pause polling
 * while the user has something mid-edit) and polling resumes on its own.
 */
(function () {
  "use strict";

  function findRoot() {
    return document.querySelector("[data-autorefresh]");
  }

  function isActive(root) {
    return root && root.dataset.active !== "0";
  }

  // Returns the freshly-fetched replacement element, or null on failure.
  async function tick(root) {
    if (document.hidden) return null;
    let html;
    try {
      const res = await fetch(location.pathname + location.search, {
        headers: { "X-Requested-With": "fetch" },
        cache: "no-store",
      });
      if (!res.ok) return null;
      html = await res.text();
    } catch {
      return null; // network hiccup — try again next tick
    }
    const next = new DOMParser()
      .parseFromString(html, "text/html")
      .querySelector("[data-autorefresh]");
    if (next && root.isConnected) {
      root.replaceWith(next);
      reviveScripts(next); // parsed <script> tags are inert until re-created
    }
    return next;
  }

  // A page's own inline <script> (e.g. wiring up checkboxes) has to survive
  // repeated swaps — scripts parsed via DOMParser never execute on insertion,
  // so each one is recreated to force the browser to actually run it again.
  function reviveScripts(node) {
    node.querySelectorAll("script").forEach((old) => {
      const s = document.createElement("script");
      for (const attr of old.attributes) s.setAttribute(attr.name, attr.value);
      s.textContent = old.textContent;
      old.replaceWith(s);
    });
  }

  function start() {
    const first = findRoot();
    if (!first) return;
    const interval = parseInt(first.dataset.interval, 10) || 5000;

    // The timer chain itself never stops on its own — only navigating away
    // kills it. Each tick just skips the actual fetch while inactive, so
    // flipping `data-active` back on later resumes polling automatically.
    const loop = async () => {
      const root = findRoot();
      if (isActive(root)) await tick(root);
      setTimeout(loop, interval);
    };
    setTimeout(loop, interval);

    document.addEventListener("visibilitychange", () => {
      const root = findRoot();
      if (!document.hidden && isActive(root)) tick(root);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
