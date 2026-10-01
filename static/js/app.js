/**
 * Strathmore Study Buddy - Client Interactions & Enhancements
 */

document.addEventListener('DOMContentLoaded', () => {
  // 1. Instant Client-Side Course Search
  const searchInput = document.getElementById('course-search');
  if (searchInput) {
    const courseCards = document.querySelectorAll('.course-card-item');
    const emptyNotice = document.getElementById('search-empty-state');

    searchInput.addEventListener('input', (e) => {
      const query = e.target.value.toLowerCase().trim();
      let visibleCount = 0;

      courseCards.forEach(card => {
        const text = card.textContent.toLowerCase();
        const matches = text.includes(query);
        card.style.display = matches ? '' : 'none';
        if (matches) visibleCount++;
      });

      if (emptyNotice) {
        emptyNotice.style.display = (visibleCount === 0 && courseCards.length > 0) ? 'block' : 'none';
      }
    });
  }

  // Keys typed into these go to the field, not to page shortcuts
  const isTextEntry = (el) => {
    if (el.isContentEditable) return true;
    if (el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
    if (el.tagName !== 'INPUT') return false;
    return !['radio', 'checkbox', 'button', 'submit', 'reset'].includes(el.type);
  };
  const hasModifier = (e) => e.ctrlKey || e.altKey || e.metaKey;

  // 2. Interactive MCQ selection & Keyboard shortcuts (1, 2, 3, 4, Enter)
  const optionLabels = document.querySelectorAll('.option-label');
  if (optionLabels.length > 0) {
    // Click feedback
    optionLabels.forEach(label => {
      const radio = label.querySelector('input[type="radio"]');
      if (radio) {
        label.addEventListener('click', () => {
          radio.checked = true;
        });
      }
    });

    // Keyboard shortcuts: 1-9 to select options. Focus lands on the radio,
    // so radios must not count as text entry or the next press is ignored.
    window.addEventListener('keydown', (e) => {
      if (hasModifier(e) || isTextEntry(e.target)) return;

      const num = parseInt(e.key, 10);
      if (!isNaN(num) && num >= 1 && num <= optionLabels.length) {
        const targetRadio = optionLabels[num - 1].querySelector('input[type="radio"]');
        if (targetRadio) {
          e.preventDefault();
          targetRadio.checked = true;
          targetRadio.focus();
        }
      }
    });
  }

  // Submit once: a double click would post the answer twice
  document.querySelectorAll('form.answer-form').forEach(form => {
    form.addEventListener('submit', () => {
      form.querySelectorAll('button[type="submit"]').forEach(btn => {
        btn.disabled = true;
      });
    });
  });

  // 3. Quiz Result page shortcut (Enter or Space to advance)
  const nextBtn = document.getElementById('next-question-btn');
  if (nextBtn) {
    window.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      if (hasModifier(e) || e.repeat) return;
      // A focused link, button or field handles Enter/Space itself
      if (e.target.closest('a, button, input, select, textarea, summary, [contenteditable], [tabindex]')) return;
      e.preventDefault();
      nextBtn.click();
    });
  }

  // 4. Copy Extracted Text (the page may show only a preview; copy it all)
  const copyBtn = document.getElementById('copy-extracted-btn');
  const extractedPre = document.getElementById('extracted-text-content');
  if (copyBtn && extractedPre) {
    const origLabel = copyBtn.innerHTML;
    let resetTimer = null;
    const flash = (label, ok) => {
      clearTimeout(resetTimer);
      copyBtn.innerHTML = label;
      copyBtn.classList.toggle('btn-accent', ok);
      resetTimer = setTimeout(() => {
        copyBtn.innerHTML = origLabel;
        copyBtn.classList.remove('btn-accent');
      }, 2000);
    };

    copyBtn.addEventListener('click', async () => {
      try {
        const url = copyBtn.dataset.fullTextUrl;
        let text = extractedPre.textContent;
        if (url) {
          const resp = await fetch(url, { credentials: 'same-origin' });
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          text = await resp.text();
        }
        await navigator.clipboard.writeText(text);
        flash('✓ Copied!', true);
      } catch (err) {
        console.error('Failed to copy text', err);
        flash('Copy failed', false);
      }
    });
  }
});
