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

(() => {
  const badges = document.querySelectorAll("[data-job-status]");
  if (badges.length === 0) return;
  const page = document.querySelector("[data-project-id]");
  if (!page) return;
  const projectId = page.dataset.projectId;
  const badgeJobIds = new Set([...badges].map((b) => b.dataset.jobId).filter(Boolean));
  const timer = setInterval(() => {
    fetch(`/api/projects/${projectId}/status`)
      .then((r) => r.json())
      .then((data) => {
        const stillActive = data.jobs.some((j) => badgeJobIds.has(String(j.id)));
        if (!stillActive) {
          clearInterval(timer);
          location.reload();
        }
      });
  }, 3000);
})();
