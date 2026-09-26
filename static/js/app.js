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

    // Keyboard shortcuts: 1-9 to select options
    window.addEventListener('keydown', (e) => {
      // Ignore if user is typing in textarea or input
      if (['INPUT', 'TEXTAREA'].includes(e.target.tagName)) return;

      const num = parseInt(e.key, 10);
      if (!isNaN(num) && num >= 1 && num <= optionLabels.length) {
        const targetRadio = optionLabels[num - 1].querySelector('input[type="radio"]');
        if (targetRadio) {
          targetRadio.checked = true;
          targetRadio.focus();
        }
      }
    });
  }

  // 3. Quiz Result page shortcut (Enter or Space to advance)
  const nextBtn = document.getElementById('next-question-btn');
  if (nextBtn) {
    window.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        nextBtn.click();
      }
    });
  }

  // 4. Copy Extracted Text
  const copyBtn = document.getElementById('copy-extracted-btn');
  const extractedPre = document.getElementById('extracted-text-content');
  if (copyBtn && extractedPre) {
    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(extractedPre.innerText);
        const origText = copyBtn.innerHTML;
        copyBtn.innerHTML = '✓ Copied!';
        copyBtn.classList.add('btn-accent');
        setTimeout(() => {
          copyBtn.innerHTML = origText;
          copyBtn.classList.remove('btn-accent');
        }, 2000);
      } catch (err) {
        console.error('Failed to copy text', err);
      }
    });
  }
});
