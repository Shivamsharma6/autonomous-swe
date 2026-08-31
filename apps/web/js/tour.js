// Optional help. Native dialogs provide focus containment, dismissal and restoration.
export function initTour() {
  const dialog = document.getElementById('helpDialog');
  document.getElementById('tourHelpBtn')?.addEventListener('click', () => {
    if (!dialog.open) dialog.showModal();
  });
  document.getElementById('closeHelp')?.addEventListener('click', () => dialog.close());
}
