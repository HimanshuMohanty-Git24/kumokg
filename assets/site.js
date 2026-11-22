// Copy buttons, install tabs, scroll reveal (IntersectionObserver, no scroll listeners), reduced-motion video handling.
(() => {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  document.querySelectorAll(".cmd").forEach((box) => {
    const btn = box.querySelector(".copy");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const text = box.dataset.copy;
      try { await navigator.clipboard.writeText(text); }
      catch {
        const ta = Object.assign(document.createElement("textarea"), { value: text });
        document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
      }
      btn.classList.add("done");
      btn.setAttribute("aria-label", "Copied");
      setTimeout(() => { btn.classList.remove("done"); btn.setAttribute("aria-label", "Copy command"); }, 1600);
    });
  });

  const tabs = [...document.querySelectorAll('.tabs [role="tab"]')];
  const panel = document.getElementById("cmd-install");
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
      panel.dataset.copy = tab.dataset.cmd;
      panel.querySelector(".cmd-text").textContent = tab.dataset.cmd;
    });
    tab.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      next.focus(); next.click();
    });
  });

  if (reduce) {
    document.querySelectorAll("video[autoplay]").forEach((v) => { v.removeAttribute("autoplay"); v.pause(); });
  } else if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
    }, { threshold: 0.12 });
    document.querySelectorAll(".reveal").forEach((el) => io.observe(el));
  } else {
    document.querySelectorAll(".reveal").forEach((el) => el.classList.add("in"));
  }
})();
