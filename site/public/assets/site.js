const navigation = document.querySelector(".site-navigation");
const wide = window.matchMedia("(min-width: 64rem)");
const updateNavigation = () => {
  navigation.open = wide.matches;
};
updateNavigation();
wide.addEventListener("change", updateNavigation);

// Label each cell with its column, for the stacked layout on narrow screens.
for (const table of document.querySelectorAll("article > .table-scroll table")) {
  const headers = [...table.querySelectorAll("thead th")].map((th) => th.textContent.trim());
  for (const row of table.querySelectorAll("tbody tr")) {
    row.querySelectorAll("td").forEach((td, i) => {
      if (headers[i]) td.dataset.label = headers[i];
    });
  }
}

// Only a box that scrolls needs to take focus, so keyboard users can scroll it; a table that
// scrolls is also a region, named after its caption or the heading above it.
const tableName = (box) => {
  const caption = box.querySelector("caption");
  if (caption) return caption.textContent.trim();
  let node = box.previousElementSibling;
  while (node && !/^H[1-6]$/.test(node.tagName)) node = node.previousElementSibling;
  return node ? node.textContent.trim() : "Table";
};
const scrollable = new ResizeObserver((entries) => {
  for (const { target } of entries) {
    const overflows = target.scrollWidth > target.clientWidth;
    const table = target.classList.contains("table-scroll");
    if (overflows) {
      target.tabIndex = 0;
      if (table) target.setAttribute("role", "region");
      if (table) target.setAttribute("aria-label", tableName(target));
    } else {
      for (const name of ["tabindex", "role", "aria-label"]) target.removeAttribute(name);
    }
  }
});
for (const box of document.querySelectorAll(".table-scroll, pre code")) scrollable.observe(box);

const status = document.querySelector(".copy-status");
let statusTimer;
for (const pre of document.querySelectorAll("pre")) {
  const code = pre.querySelector("code");
  if (!code || !navigator.clipboard || code.classList.contains("language-console")) continue;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "code-copy";
  button.textContent = "Copy";
  button.setAttribute("aria-label", "Copy this code block");
  // aria-disabled rather than disabled, so focus stays on the button while it copies.
  button.addEventListener("click", async () => {
    if (button.getAttribute("aria-disabled") === "true") return;
    button.setAttribute("aria-disabled", "true");
    clearTimeout(statusTimer);
    try {
      await navigator.clipboard.writeText(code.textContent);
      status.textContent = "Copied to clipboard.";
    } catch {
      status.textContent = "Copy failed. Select the code and copy it manually.";
    } finally {
      button.removeAttribute("aria-disabled");
      statusTimer = setTimeout(() => {
        status.textContent = "";
      }, 5000);
    }
  });
  pre.append(button);
}
const example = document.querySelector("[data-revision-example]");
if (example) {
  const controls = example.querySelector(".revision-controls");
  const buttons = controls.querySelectorAll("button");
  const panels = example.querySelectorAll("[data-version-panel]");
  const selectVersion = (version) => {
    for (const button of buttons) button.setAttribute("aria-pressed", String(button.dataset.version === version));
    for (const panel of panels) panel.hidden = panel.dataset.versionPanel !== version;
  };
  controls.hidden = false;
  for (const button of buttons) button.addEventListener("click", () => selectVersion(button.dataset.version));
  selectVersion("after");
}
