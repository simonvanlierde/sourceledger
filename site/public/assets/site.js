const navigation = document.querySelector(".site-navigation");
const wide = window.matchMedia("(min-width: 64rem)");
const updateNavigation = () => {
  navigation.open = wide.matches;
};
updateNavigation();
wide.addEventListener("change", updateNavigation);

// Only a box that scrolls needs to take focus, so keyboard users can scroll it. Table boxes
// are regions from the build; a scrollable code block becomes one, named after the heading above.
const headingName = (el) => {
  for (let node = el.closest("pre")?.previousElementSibling; node; node = node.previousElementSibling) {
    if (/^H[1-6]$/.test(node.tagName)) return node.textContent.trim();
  }
  return "";
};
// Blocks under one heading are numbered, so no two regions or buttons share a name.
const codeName = (code) => {
  const heading = headingName(code);
  const same = [...document.querySelectorAll("pre code")].filter((c) => headingName(c) === heading);
  const number = same.length > 1 ? ` ${same.indexOf(code) + 1} of ${same.length}` : "";
  return `Code${number}${heading ? `: ${heading}` : ""}`;
};
const scrollable = new ResizeObserver((entries) => {
  for (const { target } of entries) {
    const overflows = target.scrollWidth > target.clientWidth;
    if (target.classList.contains("table-scroll")) {
      if (overflows) target.tabIndex = 0;
      else target.removeAttribute("tabindex");
    } else if (overflows) {
      target.tabIndex = 0;
      target.setAttribute("role", "region");
      target.setAttribute("aria-label", codeName(target));
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
  button.setAttribute("aria-label", codeName(code).replace(/^Code/, "Copy code"));
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
