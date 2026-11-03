function formatRemaining(target) {
  const ms = new Date(target).getTime() - Date.now();
  if (Number.isNaN(ms)) return "";
  if (ms <= 0) return "now";
  const total = Math.floor(ms / 1000);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const parts = [];
  if (days) parts.push(days + "d");
  if (hours) parts.push(hours + "h");
  if (minutes) parts.push(minutes + "m");
  if (!days && seconds) parts.push(seconds + "s");
  if (!parts.length) parts.push("under a minute");
  return parts.join(" ");
}

function tick() {
  document.querySelectorAll("[data-countdown]").forEach((node) => {
    node.textContent = formatRemaining(node.dataset.countdown);
  });
  const clock = document.getElementById("clock");
  if (!clock) return;
  const zone = clock.dataset.timezone || "UTC";
  try {
    clock.textContent = new Intl.DateTimeFormat(undefined, {
      timeZone: zone,
      weekday: "short",
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date());
  } catch (error) {
    clock.textContent = zone;
  }
}

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const message = form.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

tick();
setInterval(tick, 1000);
