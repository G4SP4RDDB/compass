// Plain module, not a class: three one-liners closing over one
// page-singleton DOM element don't get more real encapsulation from being a
// class than from being a module — see the plan's note on where the
// class/module line is drawn (GraphRenderer/ExecModal have actual
// multi-step lifecycle; this doesn't).
const tooltip = document.getElementById("tooltip") as HTMLElement;

export function showTooltip(ev: MouseEvent, html: string): void {
  tooltip.innerHTML = html;
  tooltip.style.display = "block";
  positionTooltip(ev);
}

export function positionTooltip(ev: MouseEvent): void {
  tooltip.style.left = (ev.clientX + 14) + "px";
  tooltip.style.top = (ev.clientY + 14) + "px";
}

export function hideTooltip(): void {
  tooltip.style.display = "none";
}
