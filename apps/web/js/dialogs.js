// Keep native modal focus containment while making dismissal and focus restoration explicit.
const sessions = new WeakMap();
const initialized = new WeakSet();

function restoreFocus(dialog) {
  if (dialog.open) return; // Ignore a queued close from an earlier opening.
  const previous = sessions.get(dialog);
  sessions.delete(dialog);
  if (!previous || document.querySelector('dialog[open]')) return;
  const target = previous.isConnected && !previous.disabled && !previous.closest('.hidden, [hidden]')
    ? previous : document.getElementById('mainContent');
  target?.focus({ preventScroll: true });
}

export function closeDialog(dialog) {
  if (!dialog.open) return;
  dialog.close();
  restoreFocus(dialog);
}

export function showDialog(dialog, initialFocus) {
  if (dialog.open) return;
  if (!initialized.has(dialog)) {
    initialized.add(dialog);
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeDialog(dialog); });
    dialog.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !event.defaultPrevented) {
        event.preventDefault();
        closeDialog(dialog);
      }
    });
    dialog.addEventListener('close', () => restoreFocus(dialog));
  }
  sessions.set(dialog, document.activeElement);
  dialog.showModal();
  (initialFocus || dialog.querySelector('[autofocus], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled])'))?.focus();
}
