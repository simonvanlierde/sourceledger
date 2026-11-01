const navigation = document.querySelector(".site-navigation");
const wide = window.matchMedia("(min-width: 64rem)");
const updateNavigation = () => {
  navigation.open = wide.matches;
};
updateNavigation();
wide.addEventListener("change", updateNavigation);

const status = document.querySelector(".copy-status");
let statusTimer;
for (const pre of document.querySelectorAll("pre")) {
  const code = pre.querySelector("code");
  if (code) code.tabIndex = 0;
  if (!code || !navigator.clipboard || code.classList.contains("language-console")) continue;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "code-copy";
  button.textContent = "Copy";
  button.setAttribute("aria-label", "Copy this code block");
  button.addEventListener("click", async () => {
    button.disabled = true;
    clearTimeout(statusTimer);
    try {
      await navigator.clipboard.writeText(code.textContent);
      status.textContent = "Copied to clipboard.";
    } catch {
      status.textContent = "Copy failed. Select the code and copy it manually.";
    } finally {
      button.disabled = false;
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
