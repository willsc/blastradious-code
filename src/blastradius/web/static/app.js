// Theme toggle, dashboard auto-refresh and topology-map zoom/pan. Everything degrades gracefully.
(function () {
  var root = document.documentElement;
  try { var saved = localStorage.getItem("br-theme"); if (saved) root.setAttribute("data-theme", saved); } catch (e) {}
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-theme-toggle]");
    if (!btn) return;
    var dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("br-theme", next); } catch (e) {}
  });
  // Auto-refresh the dashboard every 60s so a wall screen stays current.
  if (document.body.dataset.autorefresh) {
    setTimeout(function () { location.reload(); }, 60000);
  }

  var view = document.querySelector("[data-mapview]");
  var svg = view && view.querySelector("svg");
  if (!svg) return;
  var naturalW = parseFloat(svg.getAttribute("width")), naturalH = parseFloat(svg.getAttribute("height"));
  var label = document.querySelector("[data-zoom-label]");
  var scale = 1;

  function zoom(next) {
    var cx = (view.scrollLeft + view.clientWidth / 2) / scale, cy = (view.scrollTop + view.clientHeight / 2) / scale;
    scale = Math.max(0.2, Math.min(3, next));
    svg.setAttribute("width", Math.round(naturalW * scale));
    svg.setAttribute("height", Math.round(naturalH * scale));
    view.scrollLeft = cx * scale - view.clientWidth / 2;
    view.scrollTop = cy * scale - view.clientHeight / 2;
    if (label) label.textContent = Math.round(scale * 100) + "%";
  }
  function fit() {
    zoom(Math.min(1, (view.clientWidth - 4) / naturalW, Math.max(0.35, (view.clientHeight - 4) / naturalH)));
    view.scrollLeft = 0; view.scrollTop = 0;
  }
  document.querySelectorAll("[data-zoom]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var action = btn.getAttribute("data-zoom");
      if (action === "in") zoom(scale * 1.25);
      else if (action === "out") zoom(scale / 1.25);
      else if (action === "fit") fit();
      else zoom(1);
    });
  });
  view.addEventListener("wheel", function (ev) {
    if (!ev.ctrlKey && !ev.metaKey) return;
    ev.preventDefault();
    zoom(scale * (ev.deltaY < 0 ? 1.12 : 1 / 1.12));
  }, { passive: false });

  // Drag anywhere to pan; a drag never counts as a click on the box underneath.
  var drag = null, moved = false;
  view.addEventListener("mousedown", function (ev) {
    if (ev.button !== 0) return;
    drag = { x: ev.clientX, y: ev.clientY, left: view.scrollLeft, top: view.scrollTop };
    moved = false;
  });
  window.addEventListener("mousemove", function (ev) {
    if (!drag) return;
    var dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
    if (!moved && Math.abs(dx) + Math.abs(dy) > 4) { moved = true; view.classList.add("dragging"); }
    if (moved) { view.scrollLeft = drag.left - dx; view.scrollTop = drag.top - dy; ev.preventDefault(); }
  });
  window.addEventListener("mouseup", function () { drag = null; view.classList.remove("dragging"); });
  view.addEventListener("click", function (ev) { if (moved) { ev.preventDefault(); ev.stopPropagation(); moved = false; } }, true);

  var focus = document.getElementById("map-focus");
  if (focus) {
    zoom(1);
    var box = focus.getBBox();
    view.scrollLeft = box.x - view.clientWidth / 2 + box.width / 2;
    view.scrollTop = box.y - view.clientHeight / 2 + box.height / 2;
  } else if (naturalW > view.clientWidth) {
    fit();
  }
})();
