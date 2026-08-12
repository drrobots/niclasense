/* The browser dashboard.
 *
 * Reads /spec for the tile layout (so tiles.py stays the only place it is declared),
 * subscribes to /stream for samples, and draws one uPlot chart per tile.
 *
 * The autoscale rule is shared with view.py's _draw_traces, and the order of its
 * three steps matters: take min/max over the *undecimated* window, widen to min_span, then
 * pad 12%. Scaling the strided data instead makes a tile's range jitter as samples move in
 * and out of the stride, and padding before widening changes the floor a quiet sensor sits
 * at. Either mistake shows up as tiles that breathe differently from the offline viewer,
 * which is a miserable thing to chase.
 */

(function () {
  "use strict";

  var FPS = 20;
  var WINDOW_CHOICES = [10, 30, 60, 300];
  var DEFAULT_WINDOW = 30;
  var THEME_KEY = "nicla-theme";
  var WINDOW_KEY = "nicla-window";
  var UNITS_KEY = "nicla-alt-units";
  var LAYOUT_KEY = "nicla-layout";

  var spec = null;
  var colIndex = {};      // column name -> position in a sample row
  var store = {};         // column name -> Column
  var timeCol = null;     // t_ms, in seconds
  var tiles = [];         // one runtime entry per tile
  var capture = {};       // capture tile's DOM nodes
  var windowS = DEFAULT_WINDOW;
  var altUnits = false;   // show tiles declaring an alt_unit in it, per tab
  var layout = { hidden: {}, span: {}, order: null };
  var layoutItems = [];   // every placeable thing, tiles plus the capture tile
  var lastStatus = {};
  var dirty = false;      // samples have arrived since the last frame

  // ---------------------------------------------------------------------
  // Theme
  //
  // Colours live in dash.css as custom properties and are read back out here, so a chart
  // and the page around it can never disagree about what "muted" means. getComputedStyle
  // is far too slow to call per axis per frame, hence the cache -- which the toggle drops.

  var themeCache = {};

  function themed(name) {
    if (!(name in themeCache)) {
      themeCache[name] =
        getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }
    return themeCache[name];
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme");
  }

  function applyTheme(name) {
    document.documentElement.setAttribute("data-theme", name);
    themeCache = {};
    try {
      localStorage.setItem(THEME_KEY, name);
    } catch (e) {
      /* private browsing; the theme just will not persist */
    }
    tiles.forEach(function (tile) {
      if (tile.chart) {
        tile.chart.redraw();
      }
    });
  }

  function initialTheme() {
    var saved = null;
    try {
      saved = localStorage.getItem(THEME_KEY);
    } catch (e) { /* ignore */ }
    if (saved === "light" || saved === "dark") {
      return saved;
    }
    return window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }

  /* Trace colours are chosen against near-black. Four of them are close to invisible on
     white, so the light theme substitutes darker equivalents from tiles.LIGHT_OVERRIDES
     and passes everything else through unchanged. */
  function strokeFor(hex) {
    if (currentTheme() !== "light") {
      return hex;
    }
    return spec.light_overrides[hex] || hex;
  }

  // ---------------------------------------------------------------------
  // Storage
  //
  // One buffer per column we actually draw. Appending linearly into a double-length array
  // and compacting when it fills keeps every window a contiguous subarray, which a ring
  // buffer does not -- and contiguity is what lets the min/max scan and uPlot both read the
  // samples without copying them somewhere else first. Compaction costs one memmove per
  // `capacity` samples, which at 200 Hz is a few times a minute.

  function Column(capacity) {
    this.capacity = capacity;
    this.data = new Float64Array(capacity * 2);
    this.n = 0;
  }

  Column.prototype.push = function (value) {
    if (this.n === this.data.length) {
      this.data.copyWithin(0, this.n - this.capacity);
      this.n = this.capacity;
    }
    this.data[this.n++] = value;
  };

  Column.prototype.reset = function () {
    this.n = 0;
  };

  /* First index at or after `t`. The buffer is time-ordered, so this is a bisect; a linear
     scan is fine at 30 seconds and quite slow at 300. */
  function lowerBound(column, t) {
    var lo = 0;
    var hi = column.n;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (column.data[mid] < t) {
        lo = mid + 1;
      } else {
        hi = mid;
      }
    }
    return lo;
  }

  // ---------------------------------------------------------------------
  // Formatting

  /* The %6.2f-style formats in tiles.py, enough of printf to honour them. The width is not
     cosmetic: the readouts are monospace and a number that changes width makes the whole
     line twitch as it updates. */
  function formatValue(fmt, value) {
    var match = /%(\d*)(?:\.(\d+))?f/.exec(fmt);
    if (!match || !isFinite(value)) {
      return String(value);
    }
    var text = value.toFixed(match[2] === undefined ? 6 : +match[2]);
    var width = match[1] ? +match[1] : 0;
    while (text.length < width) {
      text = " " + text;
    }
    return text;
  }

  // ---------------------------------------------------------------------
  // Units
  //
  // A tile may declare an alt_unit in tiles.py, and the viewer switches every such tile
  // between its declared unit and that one. Display only: the samples in `store` stay
  // exactly as the board sent them, so the conversion cannot leak into anything that is
  // recorded, and switching back is not lossy.

  /* The active {mul, add, min_span, unit} for a tile, or null when it is showing the unit
     its column is named in. */
  function activeUnit(tileSpec) {
    return altUnits && tileSpec.alt_unit ? tileSpec.alt_unit : null;
  }

  function convert(unit, value) {
    return unit ? value * unit.mul + unit.add : value;
  }

  function unitLabel(tileSpec) {
    var unit = activeUnit(tileSpec);
    return unit ? unit.unit : tileSpec.unit;
  }

  function minSpan(tileSpec) {
    var unit = activeUnit(tileSpec);
    return unit ? unit.min_span : tileSpec.min_span;
  }

  function thousands(value) {
    return Math.round(value || 0).toLocaleString("en-US");
  }

  function shorten(text, width) {
    /* Keep the tail of an over-long path; the file name matters more than its parents. */
    return text.length <= width ? text : "..." + text.slice(-(width - 3));
  }

  // ---------------------------------------------------------------------
  // Layout
  //
  // There is no user interface for any of this at present. There was a dialog -- a row per
  // tile with a checkbox, a width and up/down -- and it was removed; what follows is
  // deliberately not, because the model is the awkward half and the dialog was the easy
  // one. Anything wanting to drive it, a control put back here or a person at a console,
  // writes the same shape to localStorage under LAYOUT_KEY and reloads:
  //
  //     {"hidden": {"iaq": true}, "span": {"temperature": 6}, "order": ["gyroscope", ...]}
  //
  // hidden is by tile name, span is in grid columns (1..12), order is tile names and may
  // name only some of them -- see orderedItems. Anything absent falls back to the declared
  // layout in tiles.py, and clearing the key restores it entirely. loadLayout runs at
  // start-up and applyLayout on every change, so the path is exercised on every page load
  // rather than kept warm on trust; saveLayout is the one piece nothing currently calls.

  function placeOnGrid(node, placement) {
    var row = placement[0];
    var first = placement[1];
    var span = placement[2];
    node.style.gridRow = String(row + 1);
    node.style.gridColumn = (first + 1) + " / span " + span;
  }

  /* Two layout modes, and the second one exists because of the first.

     The declared mode is tiles.py's PLACEMENT: absolute (row, column, span) for every
     tile, hand-packed, with a hole left for the capture tile. It is a deliberate
     arrangement and it is what the page opens on.

     Hide a tile in that mode, or widen one, and you get a hole or an overlap -- absolute
     coordinates do not close up behind anything. So the moment the viewer changes
     anything, the whole grid switches to auto-flow: every tile keeps a width and the
     browser packs them in order. That is not a new mechanism to trust; it is what
     dash.css already does at every width below 1180px, where the same tiles flow into two
     columns with their placement overridden. This just reaches for it a bit sooner.

     Which means the hand-packed layout is never half-applied: it is all of it, or none. */
  function customLayout() {
    if (layout.order) {
      return true;
    }
    var name;
    for (name in layout.hidden) {
      if (layout.hidden[name]) { return true; }
    }
    for (name in layout.span) {
      if (layout.span[name]) { return true; }
    }
    return false;
  }

  function spanOf(item) {
    return layout.span[item.name] || item.placement[2];
  }

  function isHidden(item) {
    return !!layout.hidden[item.name];
  }

  function orderedItems() {
    if (!layout.order) {
      return layoutItems.slice();
    }
    var byName = {};
    layoutItems.forEach(function (item) { byName[item.name] = item; });
    var ordered = [];
    layout.order.forEach(function (name) {
      if (byName[name]) {
        ordered.push(byName[name]);
        delete byName[name];
      }
    });
    /* Anything the saved order does not mention -- a tile added to tiles.py since it was
       saved -- goes at the end rather than disappearing. A stored layout must never be
       able to hide a tile added to tiles.py after it was written. */
    layoutItems.forEach(function (item) {
      if (byName[item.name]) { ordered.push(item); }
    });
    return ordered;
  }

  function applyLayout() {
    var grid = document.getElementById("grid");
    var custom = customLayout();
    grid.classList.toggle("custom", custom);

    orderedItems().forEach(function (item) {
      item.node.hidden = isHidden(item);
      if (custom) {
        item.node.style.gridRow = "";
        item.node.style.gridColumn = "span " + spanOf(item);
        /* Appending a node already in the grid moves it, which is how the order is
           applied: auto-flow packs in DOM order. */
        grid.appendChild(item.node);
      } else {
        placeOnGrid(item.node, item.placement);
      }
    });

    /* uPlot sizes itself in pixels and cannot know its cell changed. A tile that was
       hidden measures zero while hidden, so this has to run after the display flip
       rather than before it. */
    tiles.forEach(function (tile) {
      if (tile.chart && !tile.node.hidden) {
        tile.chart.setSize({
          width: Math.max(tile.chartNode.clientWidth, 80),
          height: Math.max(tile.chartNode.clientHeight, 80),
        });
      }
    });
    dirty = true;
  }

  function saveLayout() {
    try {
      localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
    } catch (e) { /* ignore */ }
  }

  function loadLayout() {
    var stored = null;
    try {
      stored = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "null");
    } catch (e) { /* a corrupt value is the same as no value */ }
    if (stored && typeof stored === "object") {
      layout.hidden = stored.hidden && typeof stored.hidden === "object" ? stored.hidden : {};
      layout.span = stored.span && typeof stored.span === "object" ? stored.span : {};
      layout.order = stored.order instanceof Array ? stored.order : null;
    }
  }

  function buildTile(tileSpec) {
    var node = document.createElement("section");
    node.className = tileSpec.series.length > 1 ? "tile multi" : "tile";
    placeOnGrid(node, tileSpec.placement);

    var head = document.createElement("div");
    head.className = "tile-head";

    var title = document.createElement("span");
    title.className = "tile-title";
    title.textContent = tileSpec.title;

    var readout = document.createElement("span");
    readout.className = "tile-readout";
    readout.textContent = "--";

    var unit = document.createElement("span");
    unit.className = "tile-unit";
    unit.textContent = unitLabel(tileSpec);

    head.appendChild(title);
    head.appendChild(readout);
    head.appendChild(unit);

    var chart = document.createElement("div");
    chart.className = "tile-chart";

    var sub = document.createElement("div");
    sub.className = "tile-sub";

    /* The calibration state goes under the chart rather than beside the unit, where the
       matplotlib version puts it. A two-column tile is about 170 px wide and "calibrating"
       does not fit next to a title and a number; the line below is empty anyway. */
    var accuracy = null;
    if (tileSpec.bsec) {
      accuracy = document.createElement("span");
      accuracy.className = "tile-accuracy";
      sub.appendChild(accuracy);
    }

    node.appendChild(head);
    node.appendChild(chart);
    node.appendChild(sub);

    return {
      spec: tileSpec,
      node: node,
      chartNode: chart,
      readout: readout,
      unit: unit,
      accuracy: accuracy,
      sub: sub,
      chart: null,
      /* Read by the uPlot scale callbacks below. Held here rather than computed inside
         them because they have only the strided data to work from, and the rule needs the
         window before striding. */
      ylim: [0, 1],
      scratch: null,
    };
  }

  /* The capture's state, along the header rather than in a tile of its own.

     It was a tile, in a slot tiles.py reserved for it, and it was the odd one out there:
     every other cell draws a sensor over the window you chose, and this one draws nothing
     and answers "is this working" instead. As a tile it also cost the grid its most
     awkward constraint -- a hole in the middle of row 1 that the other tiles had to be
     packed around, and that had to be understood by anything rearranging them.

     In the header it is visible at every width, next to the source it describes, and the
     grid is twelve equal cells of sensor with nothing special in the middle. */
  function buildCaptureBar() {
    var bar = document.getElementById("capture");
    bar.textContent = "";

    var fields = [
      ["rate", "rate", "Hz"],
      ["rows", "rows", null],
      ["logging", "log_rate", null],
      ["buffered", "buffered", null],
      ["loss", "loss", null],
      ["file", "file", null],
    ];
    var nodes = { node: bar };
    fields.forEach(function (field) {
      var group = document.createElement("span");
      group.className = "capture-field capture-" + field[1];

      var label = document.createElement("span");
      label.className = "capture-label";
      label.textContent = field[0];

      var value = document.createElement("span");
      value.className = "capture-value";
      value.textContent = "--";

      group.appendChild(label);
      group.appendChild(value);
      if (field[2]) {
        var unit = document.createElement("span");
        unit.className = "capture-label";
        unit.textContent = field[2];
        group.appendChild(unit);
      }
      bar.appendChild(group);
      nodes[field[1]] = value;
    });
    capture = nodes;
  }

  // ---------------------------------------------------------------------
  // Charts

  function axisCommon(size, space) {
    return {
      stroke: function () { return themed("--muted"); },
      font: "10px ui-monospace, Menlo, monospace",
      size: size,
      space: space,
      grid: {
        stroke: function () { return themed("--grid"); },
        width: 1,
      },
      ticks: {
        stroke: function () { return themed("--grid"); },
        width: 1,
        size: 3,
      },
    };
  }

  function makeChart(tile) {
    var xAxis = axisCommon(24, 70);
    xAxis.values = function (u, splits) {
      return splits.map(function (v) { return v.toFixed(0); });
    };

    var yAxis = axisCommon(44, 30);

    var series = [{}];
    tile.spec.series.forEach(function (item) {
      series.push({
        label: item.label,
        /* A function, not a colour: uPlot calls it on every draw, so a theme switch is a
           redraw with nothing to reconfigure. This is the specific reason uPlot suits this
           page better than a library that bakes styling into a config object. */
        stroke: function () { return strokeFor(item.colour); },
        width: 1.25,
        points: { show: false },
      });
    });

    var options = {
      width: Math.max(tile.chartNode.clientWidth, 80),
      height: Math.max(tile.chartNode.clientHeight, 80),
      padding: [6, 6, 0, 0],
      legend: { show: false },
      cursor: {
        /* Shared key, so a cursor in one tile marks the same instant in all of them --
           which is how you tell whether a bump in the gyro lines up with one in the
           accelerometer. */
        sync: { key: "nicla" },
        drag: { setScale: false },
        points: { show: false },
      },
      scales: {
        x: { time: false, range: function () { return xRange(); } },
        y: { range: function () { return tile.ylim; } },
      },
      axes: [xAxis, yAxis],
      series: series,
    };

    /* uPlot wants one array per configured series plus one for x, from the very first
       call: given fewer it constructs without laying itself out and never recovers, which
       shows up as a tile whose canvas stays at the browser default 300x150 and empty. */
    var initial = [[0]];
    tile.spec.series.forEach(function () {
      initial.push([0]);
    });
    tile.chart = new uPlot(options, initial, tile.chartNode);
    tile.scratch = tile.spec.series.map(function () {
      return new Float64Array(spec.max_points);
    });

    /* Tiles are sized by the grid, which is sized by the viewport; uPlot needs pixels. */
    if (window.ResizeObserver) {
      new ResizeObserver(function () {
        tile.chart.setSize({
          width: Math.max(tile.chartNode.clientWidth, 80),
          height: Math.max(tile.chartNode.clientHeight, 80),
        });
      }).observe(tile.chartNode);
    }
  }

  var xWindow = [0, 1];

  function xRange() {
    return xWindow;
  }

  // ---------------------------------------------------------------------
  // Sample ingest

  function resetBuffers() {
    Object.keys(store).forEach(function (name) {
      store[name].reset();
    });
  }

  function ingest(text) {
    var lines = text.split("\n");
    var names = Object.keys(store);
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (!line || line.charCodeAt(0) === 35 /* '#' */) {
        continue;
      }
      var fields = line.split(",");
      if (fields.length < spec.columns.length) {
        continue;
      }
      var t = +fields[colIndex.t_ms] / 1000.0;
      /* t_ms restarts from zero on every board reset. Carrying the old samples across one
         draws a line back through time and wrecks every tile's scale until the window
         slides past it, so drop what came before. */
      if (timeCol.n > 0 && t < timeCol.data[timeCol.n - 1]) {
        resetBuffers();
      }
      timeCol.push(t);
      for (var j = 0; j < names.length; j++) {
        var name = names[j];
        if (name !== "t_ms") {
          store[name].push(+fields[colIndex[name]]);
        }
      }
      dirty = true;
    }
  }

  // ---------------------------------------------------------------------
  // Frame

  function measuredHz(times, first, count) {
    if (count < 2) {
      return 0;
    }
    var span = times[first + count - 1] - times[first];
    return span > 0 ? (count - 1) / span : 0;
  }

  function refresh() {
    if (timeCol.n === 0) {
      return;
    }
    var times = timeCol.data;
    var tEnd = times[timeCol.n - 1];
    var tStart = tEnd - windowS;
    var first = lowerBound(timeCol, tStart);
    var count = timeCol.n - first;
    if (count < 1) {
      return;
    }

    xWindow = [tStart, Math.max(tEnd, tStart + 1e-3)];

    /* Rounded up, unlike plot.py's floor: matplotlib is happy to be handed half again as
       many points as asked for, but here the per-tile scratch arrays are exactly
       max_points long and a longer x array than y array draws a trace that stops short of
       the right edge -- which reads as the stream lagging rather than as a bug. */
    var stride = Math.max(1, Math.ceil(count / spec.max_points));
    var drawn = Math.floor((count - 1) / stride) + 1;

    if (!refresh.xs) {
      refresh.xs = new Float64Array(spec.max_points);
    }
    var xs = refresh.xs.subarray(0, drawn);
    for (var k = 0; k < drawn; k++) {
      xs[k] = times[first + k * stride];
    }

    tiles.forEach(function (tile) {
      /* A hidden tile is not drawn and not measured. The min/max below runs over every
         sample in the window rather than over the strided points, so skipping one the
         viewer has turned off is the cheapest thing on this page. */
      if (tile.node.hidden) {
        return;
      }
      var data = [xs];
      var low = null;
      var high = null;
      var latest = [];

      var unit = activeUnit(tile.spec);

      tile.spec.series.forEach(function (item, index) {
        var column = store[item.column];
        var values = column.data;
        var ys = tile.scratch[index].subarray(0, drawn);
        for (var k = 0; k < drawn; k++) {
          ys[k] = convert(unit, values[first + k * stride]);
        }
        data.push(ys);

        /* Min/max over every sample in the window, not over the strided points above --
           see the note at the top of this file. Converted afterwards rather than inside
           the loop: the conversion is affine with a positive multiplier, so it cannot
           reorder the extremes, and this is the loop that runs over every sample. */
        var vmin = Infinity;
        var vmax = -Infinity;
        for (var i = first; i < column.n; i++) {
          var v = values[i];
          if (v < vmin) { vmin = v; }
          if (v > vmax) { vmax = v; }
        }
        if (vmin === Infinity) {
          return;
        }
        vmin = convert(unit, vmin);
        vmax = convert(unit, vmax);
        latest.push([item.label, convert(unit, values[column.n - 1])]);
        low = low === null ? vmin : Math.min(low, vmin);
        high = high === null ? vmax : Math.max(high, vmax);
      });

      if (low !== null) {
        /* Widen about the midpoint to the floor, so a quiet sensor reads as quiet instead
           of being blown up to its own noise. The floor comes from whichever unit is
           showing: it is a judgement about what counts as flat, not a length that can be
           converted along with the data. */
        var floor = minSpan(tile.spec);
        if (high - low < floor) {
          var middle = (high + low) / 2;
          low = middle - floor / 2;
          high = middle + floor / 2;
        }
        var span = high - low;
        tile.ylim = [low - span * 0.12, high + span * 0.12];
      }

      tile.chart.setData(data);

      if (latest.length) {
        tile.readout.textContent = tile.spec.value
          ? formatValue(tile.spec.fmt, latest[0][1])
          : latest.map(function (pair) {
              return pair[0] + " " + formatValue(tile.spec.fmt, pair[1]);
            }).join("  ");
      }

      if (tile.accuracy) {
        var accColumn = store.bsec_acc;
        var acc = accColumn.n ? String(Math.round(accColumn.data[accColumn.n - 1])) : "0";
        var note = spec.accuracy_notes[acc] || { note: null, colour: null };
        tile.accuracy.textContent = note.note || "";
        tile.accuracy.style.color = note.colour || "";
      }

      if (tile.spec.quaternion) {
        tile.sub.textContent = "quat  " + ["qx", "qy", "qz", "qw"].map(function (name) {
          var column = store[name];
          return name + " " + formatValue("%6.3f", column.n ? column.data[column.n - 1] : 0);
        }).join("  ");
      }
    });

    document.getElementById("clock").textContent = "t + " + tEnd.toFixed(1) + " s";
    var hz = measuredHz(times, first, count);
    capture.rate.textContent = hz ? hz.toFixed(0) : "--";
    capture.buffered.textContent = thousands(timeCol.n);
  }

  function updateStatus(status) {
    lastStatus = status;
    /* Shorter than the tile's 40: this is on one line with everything else now, and the
       file name matters more than the directories above it. */
    capture.file.textContent = status.csv ? shorten(status.csv, 28) : "not logging";
    capture.file.title = status.csv || "";
    var rows = thousands(status.rows || 0);
    if (status.log_rate) {
      rows += "   bursts " + thousands(status.bursts || 0);
    }
    capture.rows.textContent = rows;
    capture.log_rate.textContent = status.log_rate ? String(status.log_rate) : "all";
    /* toggle rather than assigning className, which would drop capture-value with it and
       leave the number unstyled the first time a burst fired. */
    capture.log_rate.classList.toggle("bursting", !!status.bursting);

    var losses = [];
    if (status.dropped) {
      losses.push("dropped " + thousands(status.dropped));
    }
    if (status.malformed) {
      losses.push("malformed " + thousands(status.malformed));
    }
    capture.loss.textContent = losses.length ? losses.join("   ") : "no drops";
    capture.node.classList.toggle("lossy", losses.length > 0);

    if (status.source) {
      document.getElementById("source").textContent = status.source;
    }
  }

  function notice(text) {
    var node = document.getElementById("notice");
    if (text === null) {
      node.hidden = true;
      return;
    }
    node.textContent = text;
    node.hidden = false;
  }

  // ---------------------------------------------------------------------
  // Wiring

  function buildControls() {
    var select = document.getElementById("window");
    WINDOW_CHOICES.filter(function (value) {
      return value >= spec.min_window_s && value <= spec.max_window_s;
    }).forEach(function (value) {
      var option = document.createElement("option");
      option.value = String(value);
      option.textContent = value + " s";
      select.appendChild(option);
    });
    select.value = String(windowS);
    select.addEventListener("change", function () {
      windowS = +select.value;
      try {
        localStorage.setItem(WINDOW_KEY, select.value);
      } catch (e) { /* ignore */ }
      dirty = true;
    });

    var button = document.getElementById("theme");
    button.textContent = currentTheme() === "light" ? "dark" : "light";
    button.addEventListener("click", function () {
      var next = currentTheme() === "light" ? "dark" : "light";
      applyTheme(next);
      button.textContent = next === "light" ? "dark" : "light";
    });

    buildUnitsControl();
  }

  /* The units toggle, driven entirely by /spec: it appears only if some tile declares an
     alt_unit, and it is labelled with the unit it would switch to rather than with
     anything this file knows about temperature. Adding a second convertible tile in
     tiles.py therefore needs no change here. */
  function buildUnitsControl() {
    var convertible = spec.tiles.filter(function (tileSpec) {
      return !!tileSpec.alt_unit;
    });
    if (!convertible.length) {
      return;
    }

    var button = document.getElementById("units");
    var declared = convertible[0].unit;
    var alternative = convertible[0].alt_unit.unit;

    function label() {
      /* The unit you would get by pressing it, which is the convention the theme button
         next to it already uses. */
      button.textContent = altUnits ? declared : alternative;
    }

    button.hidden = false;
    label();
    button.addEventListener("click", function () {
      altUnits = !altUnits;
      try {
        localStorage.setItem(UNITS_KEY, altUnits ? "1" : "0");
      } catch (e) { /* ignore */ }
      label();
      tiles.forEach(function (tile) {
        if (tile.spec.alt_unit) {
          tile.unit.textContent = unitLabel(tile.spec);
        }
      });
      /* The next frame redraws from `store`, which was never converted, so the switch
         costs nothing and the whole window changes units rather than only what arrives
         after it. */
      dirty = true;
    });
  }

  function connect() {
    var stream = new EventSource("/stream");

    stream.onmessage = function (event) {
      notice(null);
      ingest(event.data);
    };

    stream.addEventListener("status", function (event) {
      try {
        updateStatus(JSON.parse(event.data));
      } catch (e) { /* a torn status is not worth killing the page over */ }
    });

    stream.addEventListener("ended", function (event) {
      var reason = "stopped";
      try {
        reason = JSON.parse(event.data).reason || reason;
      } catch (e) { /* ignore */ }
      notice("The capture ended (" + reason + "). Tiles show its last samples.");
      /* Otherwise EventSource reconnects to a server that is on its way out, and the
         notice is replaced by a browser-generated connection error. */
      stream.close();
    });

    stream.onerror = function () {
      if (stream.readyState !== EventSource.CLOSED) {
        notice("Lost the dashboard server -- reconnecting.");
      }
    };
  }

  function frame() {
    if (dirty) {
      dirty = false;
      refresh();
    }
    setTimeout(function () {
      requestAnimationFrame(frame);
    }, 1000 / FPS);
  }

  function start(loaded) {
    spec = loaded;
    spec.columns.forEach(function (name, index) {
      colIndex[name] = index;
    });

    /* Only the columns something draws are buffered. The board sends 27 and this page
       reads about half of them; the rest would be megabytes of Float64Array nobody looks
       at. */
    var needed = { t_ms: true, bsec_acc: true, qx: true, qy: true, qz: true, qw: true };
    spec.tiles.forEach(function (tile) {
      tile.series.forEach(function (item) {
        needed[item.column] = true;
      });
    });

    var longest = WINDOW_CHOICES[WINDOW_CHOICES.length - 1];
    var capacity = Math.ceil(longest * (spec.sample_hz || 200) * 1.1);
    Object.keys(needed).forEach(function (name) {
      store[name] = new Column(capacity);
    });
    timeCol = store.t_ms;

    document.getElementById("source").textContent = spec.source || "attached";

    var grid = document.getElementById("grid");
    tiles = spec.tiles.map(buildTile);
    tiles.forEach(function (tile) {
      grid.appendChild(tile.node);
    });
    buildCaptureBar();

    /* Everything the viewer can show, hide, resize or reorder, in the order the declared
       layout draws them -- by row, then by column. That is the reading order of the page,
       and it is the order the flowed layout should start from; the order the tiles happen
       to appear in tiles.py is not. */
    layoutItems = tiles.map(function (tile) {
      return {
        name: tile.spec.name,
        title: tile.spec.title,
        node: tile.node,
        placement: tile.spec.placement,
      };
    });
    layoutItems.sort(function (a, b) {
      return (a.placement[0] - b.placement[0]) || (a.placement[1] - b.placement[1]);
    });

    buildControls();
    tiles.forEach(makeChart);
    /* After makeChart, because applying a stored layout resizes the charts it shows. */
    applyLayout();
    connect();
    requestAnimationFrame(frame);
  }

  applyTheme(initialTheme());
  try {
    var savedWindow = +localStorage.getItem(WINDOW_KEY);
    if (WINDOW_CHOICES.indexOf(savedWindow) !== -1) {
      windowS = savedWindow;
    }
    /* Read here rather than in buildUnitsControl because the tiles are built before the
       controls are, and a tile's unit label is written once at construction. */
    altUnits = localStorage.getItem(UNITS_KEY) === "1";
  } catch (e) { /* ignore */ }
  loadLayout();

  fetch("/spec")
    .then(function (response) { return response.json(); })
    .then(start)
    .catch(function (error) {
      notice("Could not load the dashboard layout: " + error);
    });
})();
