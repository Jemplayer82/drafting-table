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

  const MAX_FAILURES = 3;
  let failures = 0;

  const timer = setInterval(() => {
    fetch(`/api/projects/${projectId}/status`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        failures = 0;
        const stillActive = data.jobs.some((j) => badgeJobIds.has(String(j.id)));
        if (!stillActive) {
          clearInterval(timer);
          location.reload();
        }
      })
      .catch(() => {
        failures += 1;
        if (failures >= MAX_FAILURES) {
          clearInterval(timer);
          location.reload();
        }
      });
  }, 3000);
})();