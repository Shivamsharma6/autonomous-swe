// Optional help. Native dialogs provide focus containment, dismissal and restoration.
import { showDialog, closeDialog } from './dialogs.js?v=20260831-clean-ui';

export function initTour() {
  const dialog = document.getElementById('helpDialog');
  document.getElementById('tourHelpBtn')?.addEventListener('click', () => {
    showDialog(dialog);
  });
  document.getElementById('closeHelp')?.addEventListener('click', () => closeDialog(dialog));
}
