document.addEventListener("click", (e) => {
  const trigger = e.target.closest("[data-toggle]");
  if (!trigger) return;
  e.preventDefault();
  const target = document.getElementById(trigger.dataset.toggle);
  if (target) target.classList.toggle("d-none");
});

document.addEventListener("submit", (e) => {
  const message = e.target.dataset.confirm;
  if (message && !confirm(message)) e.preventDefault();
});