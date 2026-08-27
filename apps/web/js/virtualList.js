// virtualList.js — lightweight windowing for the audit timeline.
// Fixed row height assumption keeps math simple and avoids layout thrash.
// For variable content we use average height + overscan.

export function createVirtualList({ container, rowHeight = 52, overscan = 8, renderRow }) {
  if (!container) return { setItems() {}, destroy() {} };

  const scroller = container; // the scroll container (terminal-container)
  const viewport = document.createElement('div');
  viewport.className = 'virtual-viewport';
  viewport.style.position = 'relative';
  viewport.style.width = '100%';

  const content = document.createElement('div');
  content.className = 'virtual-content';
  content.style.position = 'absolute';
  content.style.top = '0';
  content.style.left = '0';
  content.style.right = '0';

  // Replace original list with virtual structure, preserving original element for fallback
  const originalList = container.querySelector('.timeline-list, #eventList');
  let items = [];
  let totalHeight = 0;
  let rafId = null;

  // Create wrapper if not already virtualized
  function ensureStructure() {
    if (!viewport.parentElement) {
      // Move original list out, insert viewport
      if (originalList) {
        originalList.style.display = 'none';
      }
      scroller.appendChild(viewport);
      viewport.appendChild(content);
    }
  }

  function onScroll() {
    if (rafId) return;
    rafId = requestAnimationFrame(() => {
      rafId = null;
      render();
    });
  }

  function render() {
    if (!items.length) {
      content.innerHTML = '<li class="timeline-empty">No matching events found in audit trail.</li>';
      viewport.style.height = 'auto';
      content.style.transform = 'translateY(0)';
      return;
    }

    const scrollTop = scroller.scrollTop;
    const viewportH = scroller.clientHeight || 400;
    const startIdx = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
    const visibleCount = Math.ceil(viewportH / rowHeight) + overscan * 2;
    const endIdx = Math.min(items.length, startIdx + visibleCount);

    totalHeight = items.length * rowHeight;
    viewport.style.height = `${totalHeight}px`;
    content.style.transform = `translateY(${startIdx * rowHeight}px)`;

    // Render slice
    const slice = items.slice(startIdx, endIdx);
    content.replaceChildren();
    const frag = document.createDocumentFragment();
    for (const item of slice) {
      const el = renderRow(item);
      if (el) frag.appendChild(el);
    }
    content.appendChild(frag);
  }

  function setItems(newItems) {
    ensureStructure();
    items = Array.isArray(newItems) ? newItems : [];
    // If virtualization would hide content when few items, just render all
    if (items.length <= 40) {
      // Fallback: render all without virtualization for small lists
      viewport.style.height = 'auto';
      content.style.transform = 'translateY(0)';
      content.replaceChildren();
      const frag = document.createDocumentFragment();
      for (const item of items) {
        const el = renderRow(item);
        if (el) frag.appendChild(el);
      }
      content.appendChild(frag);
      return;
    }
    render();
  }

  scroller.addEventListener('scroll', onScroll, { passive: true });
  // Re-render on resize
  const ro = new ResizeObserver(() => render());
  ro.observe(scroller);

  function destroy() {
    scroller.removeEventListener('scroll', onScroll);
    ro.disconnect();
    if (rafId) cancelAnimationFrame(rafId);
    if (viewport.parentElement) viewport.remove();
    if (originalList) originalList.style.display = '';
  }

  return { setItems, destroy, _render: render };
}
