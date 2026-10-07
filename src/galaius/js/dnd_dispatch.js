([fx, fy, tx, ty]) => {
    const fromEl = document.elementFromPoint(fx, fy);
    const toEl = document.elementFromPoint(tx, ty);
    if (!fromEl || !toEl) return;
    const dt = new DataTransfer();
    const opts = {bubbles: true, cancelable: true, dataTransfer: dt};
    fromEl.dispatchEvent(new DragEvent('dragstart', opts));
    toEl.dispatchEvent(new DragEvent('dragenter', opts));
    toEl.dispatchEvent(new DragEvent('dragover', opts));
    toEl.dispatchEvent(new DragEvent('drop', opts));
    fromEl.dispatchEvent(new DragEvent('dragend', opts));
}
