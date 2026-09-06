// Yantrika marketing site — shared, tiny, no dependencies.
// Mobile nav toggle only. Everything else on these pages is static HTML.
(function () {
  var btn = document.querySelector(".navtoggle");
  var nav = document.querySelector("nav.top");
  if (!btn || !nav) return;
  btn.addEventListener("click", function () {
    var open = nav.classList.toggle("open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
})();
