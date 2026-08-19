/* The historical viewer.
 *
 * Two render modes, and the reason for them is the shape of the data rather than taste.
 * A capture holds a row a minute at rest and two hundred a second while something moves --
 * twelve thousand to one. Drawn on one axis at a day wide, the bursts are sub-pixel smears
 * that bury the trend they sit on, and the trend is most of what there is.
 *
 *   overview  the steady trend as a band, and bursts as marks on a rail beneath it
 *   detail    one episode, at full rate, where two hundred a second is the point
 *
 * The switch is not a guess about density: /events says exactly where the bursts are, and
 * clicking one is what changes the window. The server sends min/max per bucket either way,
 * so a detail window whose rows all fit comes back with min == max and the band it draws
 * collapses to the line it should be. One drawing path, no special case.
 *
 * Nothing here polls. The page shows what the archive held when it was loaded and says so;
 * refreshing is the reader's job, and the stamp in the corner is what makes that honest.
 */

(function () {
  "use strict";

  var state = {
    spec: null,
    board: null,
    span: 0,            // seconds; 0 means everything the archive holds
    from: null,         // Date, or null for "as far back as there is"
    to: null,
    detail: null,       // the episode being looked at, or null
    plots: []
  };

  var el = {
    archive: document.getElementById("archive"),
    board: document.getElementById("board"),
    asof: document.getElementById("asof"),
    refresh: document.getElementById("refresh"),
    rail: document.getElementById("rail"),
    railnote: document.getElementById("railnote"),
    events: document.getElementById("eventlist"),
    tiles: document.getElementById("tiles"),
    detail: document.getElementById("detail"),
    detailtext: document.getElementById("detailtext"),
    back: document.getElementById("back")
  };

  /* -- time ---------------------------------------------------------------- */

  /* The archive is naive local time throughout: logger.py writes datetime.now() with no
     zone, and a zoneless ISO string is read as local here too, so the two agree without
     anybody converting anything. */
  function iso(date) {
    var pad = function (n, w) { return String(n).padStart(w || 2, "0"); };
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) +
      "T" + pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
  }

  function clock(date) {
    var pad = function (n) { return String(n).padStart(2, "0"); };
    return pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
  }

  function stamp(date) {
    return date.toLocaleDateString() + " " + clock(date);
  }

  /* An unbounded window lets the server take its bucket grid from the rows themselves, which
     is what stops a short archive collapsing: the grid is uniform across whatever window is
     asked for, so two minutes of data inside a day is two buckets and a dot. Asking for a
     span is still exact -- it is only the *default* that follows the data. */
  function window_() {
    if (state.detail) return { from: state.detail.from, to: state.detail.to };
    if (!state.span) return { from: null, to: null };
    var to = new Date();
    return { from: new Date(to.getTime() - state.span * 1000), to: to };
  }

  function query(extra) {
    var w = window_();
    var parts = [];
    if (state.board) parts.push("board=" + encodeURIComponent(state.board));
    if (w.from) parts.push("from=" + encodeURIComponent(iso(w.from)));
    if (w.to) parts.push("to=" + encodeURIComponent(iso(w.to)));
    if (extra) parts.push(extra);
    return parts.length ? "?" + parts.join("&") : "";
  }

  /* -- loading ------------------------------------------------------------- */

  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error(url + " -> " + response.status);
      return response.json();
    });
  }

  function boot() {
    getJSON("/spec").then(function (spec) {
      state.spec = spec;
      el.archive.textContent = spec.archive || "archive";
      spec.boards.forEach(function (name) {
        var option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        el.board.appendChild(option);
      });
      state.board = spec.boards[0] || null;
      buildTiles();
      refresh();
    }).catch(function (error) {
      el.events.innerHTML = "";
      el.events.appendChild(note("could not load /spec: " + error.message));
    });
  }

  function refresh() {
    if (!state.board) {
      el.events.innerHTML = "";
      el.events.appendChild(note("no boards in the archive yet"));
      return;
    }
    var width = Math.max(200, Math.round(el.tiles.clientWidth || 900));
    Promise.all([
      getJSON("/events" + query()),
      getJSON("/range" + query("width=" + width))
    ]).then(function (both) {
      drawEvents(both[0].events || []);
      drawRange(both[1]);
    }).catch(function (error) {
      el.railnote.textContent = "could not load: " + error.message;
    });
  }

  /* -- the rail and the list ----------------------------------------------- */

  function note(text) {
    var div = document.createElement("div");
    div.className = "empty";
    div.textContent = text;
    return div;
  }

  function drawEvents(events) {
    var w = window_();
    el.rail.innerHTML = "";
    el.events.innerHTML = "";

    if (!events.length) {
      el.railnote.textContent = "no bursts in this window";
      el.events.appendChild(note("Nothing was moving. At a row a minute that is most of the time."));
      return;
    }

    var lo = w.from ? w.from.getTime() : Date.parse(events[0].start);
    var hi = w.to ? w.to.getTime() : Date.parse(events[events.length - 1].end);
    var span = Math.max(1, hi - lo);

    events.forEach(function (event) {
      var start = Date.parse(event.start);
      var end = Date.parse(event.end);

      /* Position on the rail, with a floor on the width. A half-second burst in a day is
         0.0006% of the track, and a mark nobody can see or click is the same as no mark. */
      var mark = document.createElement("button");
      mark.type = "button";
      mark.className = "railmark";
      mark.style.left = (100 * (start - lo) / span) + "%";
      mark.style.width = Math.max(0.4, 100 * (end - start) / span) + "%";
      mark.title = stamp(new Date(start)) + " — " + event.duration_s.toFixed(2) + "s";
      mark.setAttribute("aria-label", "Event at " + stamp(new Date(start)));
      mark.addEventListener("click", function () { zoom(event); });
      el.rail.appendChild(mark);

      var row = document.createElement("button");
      row.type = "button";
      row.className = "event";
      var when = document.createElement("div");
      when.className = "t";
      when.textContent = stamp(new Date(start));
      var meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = event.duration_s.toFixed(2) + " s · " + event.rows + " rows" +
        (event.peak_g === null ? "" : " · peak " + event.peak_g.toFixed(2) + " g") +
        (event.peak_dps === null ? "" : " / " + event.peak_dps.toFixed(0) + " dps");
      row.appendChild(when);
      row.appendChild(meta);
      row.addEventListener("click", function () { zoom(event); });
      el.events.appendChild(row);
    });

    el.railnote.textContent = events.length + " burst" + (events.length === 1 ? "" : "s") +
      " · click one to see it at full rate";
  }

  function zoom(event) {
    /* A little air either side, so the episode is not flush against the axis and whatever
       the board was doing just before it is visible. */
    var start = Date.parse(event.start);
    var end = Date.parse(event.end);
    var pad = Math.max(1000, (end - start) * 0.25);
    state.detail = { from: new Date(start - pad), to: new Date(end + pad), event: event };
    el.detail.hidden = false;
    el.detailtext.textContent = "Event at " + stamp(new Date(start)) + " · " +
      event.duration_s.toFixed(2) + " s at full rate";
    refresh();
  }

  function overview() {
    state.detail = null;
    el.detail.hidden = true;
    refresh();
  }

  /* -- the tiles ------------------------------------------------------------ */

  function palette() {
    var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    return (state.spec.palettes && (dark ? state.spec.palettes.dark : state.spec.palettes.light)) || {};
  }

  function buildTiles() {
    el.tiles.innerHTML = "";
    state.plots = [];
    state.spec.tiles.forEach(function (tile) {
      var box = document.createElement("div");
      box.className = "tile";
      /* PLACEMENT is (row, column, span) on a twelve-column grid -- the same declaration
         the dashboard lays itself out from, so the two agree without either restating it. */
      var place = tile.placement || [0, 0, 12];
      box.style.gridColumn = (place[1] + 1) + " / span " + (place[2] || 12);
      box.style.gridRow = String(place[0] + 1);

      var head = document.createElement("h3");
      head.appendChild(document.createTextNode(tile.title));
      var unit = document.createElement("span");
      unit.className = "unit";
      unit.textContent = tile.unit || "";
      head.appendChild(unit);
      box.appendChild(head);

      var host = document.createElement("div");
      box.appendChild(host);
      el.tiles.appendChild(box);
      state.plots.push({ tile: tile, host: host, plot: null });
    });
  }

  /* Two uPlot series per column -- the maxima and the minima -- with a band filled between
     them. When a bucket held one row the two are equal and the band has no height, so a
     sparse window and a dense one are drawn by the same code and the sparse one comes out
     as the plain line it always was. */
  function optionsFor(tile, width) {
    var colours = palette();
    var series = [{}];
    var bands = [];
    tile.series.forEach(function (one) {
      var colour = one.colour || colours[one.column] || "#888";
      var top = series.length;
      series.push({ label: one.label, stroke: colour, width: 1.25, points: { show: false } });
      series.push({ label: one.label + " min", stroke: colour, width: 1.25,
                    points: { show: false }, show: true });
      bands.push({ series: [top, top + 1], fill: rgba(colour, 0.22) });
    });
    return {
      width: width,
      height: 132,
      series: series,
      bands: bands,
      legend: { show: false },
      cursor: { drag: { x: true, y: false } },
      scales: { x: { time: true } },
      axes: [
        { stroke: cssVar("--muted"), grid: { stroke: cssVar("--rule"), width: 1 } },
        { stroke: cssVar("--muted"), grid: { stroke: cssVar("--rule"), width: 1 },
          size: 48 }
      ]
    };
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
  }

  function rgba(colour, alpha) {
    if (colour.charAt(0) !== "#" || colour.length !== 7) return colour;
    var r = parseInt(colour.substr(1, 2), 16);
    var g = parseInt(colour.substr(3, 2), 16);
    var b = parseInt(colour.substr(5, 2), 16);
    return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
  }

  function drawRange(payload) {
    var freshest = payload.t.length ? new Date(payload.t[payload.t.length - 1] * 1000) : null;
    showAsOf(freshest, payload);

    state.plots.forEach(function (slot) {
      var data = [payload.t];
      var any = false;
      slot.tile.series.forEach(function (one) {
        var column = payload.columns[one.column];
        if (column) {
          any = true;
          data.push(column.max);
          data.push(column.min);
        } else {
          data.push(new Array(payload.t.length).fill(null));
          data.push(new Array(payload.t.length).fill(null));
        }
      });

      var width = Math.max(160, slot.host.clientWidth || el.tiles.clientWidth || 600);
      if (slot.plot) { slot.plot.destroy(); slot.plot = null; }
      if (!payload.t.length || !any) {
        slot.host.innerHTML = "";
        return;
      }
      slot.host.innerHTML = "";
      slot.plot = new uPlot(optionsFor(slot.tile, width), data, slot.host);
    });
  }

  function showAsOf(freshest, payload) {
    if (!freshest) {
      el.asof.textContent = "no data in this window";
      el.asof.classList.remove("stale");
      return;
    }
    var age = (Date.now() - freshest.getTime()) / 60000;
    el.asof.textContent = "data as of " + clock(freshest) +
      " · " + payload.rows + " rows" + (payload.downsampled ? " (enveloped)" : "") +
      (payload.unreadable ? " · " + payload.unreadable + " file(s) unreadable" : "");
    /* The push runs every few minutes, so anything much older than that is not the copy
       being slow -- it is a capture that has stopped, and the page should not look the same
       as when everything is working. Files this account cannot read count the same way: what
       is on screen is not all there is, and that must not look like all there is. */
    el.asof.classList.toggle("stale", age > 15 || payload.unreadable > 0);
  }

  /* -- controls ------------------------------------------------------------- */

  function spans() {
    var buttons = document.querySelectorAll(".when button");
    Array.prototype.forEach.call(buttons, function (button) {
      button.setAttribute("aria-pressed", String(Number(button.dataset.span) === state.span));
      button.addEventListener("click", function () {
        state.span = Number(button.dataset.span);
        state.detail = null;
        el.detail.hidden = true;
        Array.prototype.forEach.call(buttons, function (other) {
          other.setAttribute("aria-pressed", String(other === button));
        });
        refresh();
      });
    });
  }

  el.board.addEventListener("change", function () {
    state.board = el.board.value;
    overview();
  });
  el.refresh.addEventListener("click", refresh);
  el.back.addEventListener("click", overview);

  var resizing = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizing);
    resizing = setTimeout(refresh, 250);
  });

  spans();
  boot();
})();
