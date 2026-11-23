(() => {
  function initialiseFileInputs() {
    const count = document.querySelector("#file-count");
    const container = document.querySelector("#file-inputs");
    if (!count || !container) return;

    function renderInputs() {
      const wanted = Number(count.value);
      const existing = [...container.querySelectorAll("input[type=file]")];
      const files = existing.map((input) => input.files);
      container.replaceChildren();

      for (let index = 0; index < wanted; index += 1) {
        const wrapper = document.createElement("div");
        wrapper.className = "file-field";
        const label = document.createElement("label");
        label.htmlFor = `file-${index + 1}`;
        label.textContent = `Source file ${index + 1}`;
        const input = document.createElement("input");
        input.id = `file-${index + 1}`;
        input.type = "file";
        input.name = "files";
        input.accept = ".ris,.txt,.nbib,.csv,.tsv,.xlsx,.xls";
        input.required = true;
        if (files[index] && files[index].length) {
          try {
            input.files = files[index];
          } catch (_) {
            // Browsers may disallow restoring a FileList; the original values
            // are kept whenever the platform permits it.
          }
        }
        wrapper.append(label, input);
        container.append(wrapper);
      }
    }

    count.addEventListener("change", renderInputs);
    renderInputs();
  }

  function initialiseRecordSelections() {
    document.querySelectorAll("[data-record-options]").forEach((group) => {
      const inputs = [...group.querySelectorAll("[data-record-selector]")];
      let lastInput = inputs.find((input) => input.checked) || null;

      inputs.forEach((input) => {
        input.addEventListener("click", (event) => {
          if (event.shiftKey && lastInput) {
            const start = inputs.indexOf(lastInput);
            const end = inputs.indexOf(input);
            const lower = Math.min(start, end);
            const upper = Math.max(start, end);
            inputs.forEach((candidate, index) => {
              if (index >= lower && index <= upper) candidate.checked = true;
            });
          } else if (!event.ctrlKey && !event.metaKey) {
            inputs.forEach((candidate) => {
              candidate.checked = candidate === input;
            });
          }

          if (!inputs.some((candidate) => candidate.checked)) {
            input.checked = true;
          }
          lastInput = input;
        });
      });
    });
  }

  // Busy feedback for long-running submits: a spinner overlay with a message,
  // announced to screen readers, and protection against double submits.
  function initialiseBusyForms() {
    const overlay = document.querySelector("#busy-overlay");
    const status = document.querySelector("#busy-status");

    function submitButtons(form) {
      const inside = [...form.querySelectorAll("button[type=submit], button:not([type]), input[type=submit]")];
      const outside = form.id
        ? [...document.querySelectorAll(`[form="${CSS.escape(form.id)}"]`)]
        : [];
      return inside.concat(outside);
    }

    function setButtonBusy(button, label) {
      if (!button || button.dataset.originalHtml !== undefined) return;
      button.dataset.originalHtml = button.innerHTML;
      button.classList.add("is-busy");
      button.setAttribute("aria-disabled", "true");
      button.innerHTML = '<span class="button-spinner" aria-hidden="true"></span>';
      button.append(document.createTextNode(label));
    }

    function resetButton(button) {
      if (button.dataset.originalHtml === undefined) return;
      button.innerHTML = button.dataset.originalHtml;
      delete button.dataset.originalHtml;
      button.classList.remove("is-busy");
      button.removeAttribute("aria-disabled");
      button.disabled = false;
    }

    function resetAll() {
      document.querySelectorAll("form[data-submitting]").forEach((form) => {
        delete form.dataset.submitting;
      });
      document.querySelectorAll("[data-original-html]").forEach(resetButton);
      if (overlay) overlay.hidden = true;
      if (status) status.textContent = "";
      document.body.removeAttribute("aria-busy");
    }

    document.querySelectorAll("form[data-busy]").forEach((form) => {
      form.addEventListener("submit", (event) => {
        if (form.dataset.submitting) {
          event.preventDefault();
          return;
        }
        form.dataset.submitting = "true";
        const message = form.dataset.busy;
        const detail = form.dataset.busyDetail || "";
        setButtonBusy(event.submitter, "Working…");
        // Disable after the browser has captured the form data, so the
        // clicked button's name/value (e.g. mode=manual) is still sent.
        window.setTimeout(() => {
          submitButtons(form).forEach((button) => { button.disabled = true; });
        }, 0);
        document.body.setAttribute("aria-busy", "true");
        if (overlay) {
          overlay.querySelector(".busy-message").textContent = message;
          overlay.querySelector(".busy-detail").textContent = detail;
          overlay.hidden = false;
        }
        if (status) status.textContent = `${message} ${detail}`.trim();
      });
    });

    // Downloads stay on this page, so only the button shows progress briefly.
    document.querySelectorAll("form[data-busy-download]").forEach((form) => {
      form.addEventListener("submit", (event) => {
        const button = event.submitter || form.querySelector("button");
        if (!button || button.dataset.originalHtml !== undefined) {
          event.preventDefault();
          return;
        }
        const label = form.dataset.busyDownload;
        setButtonBusy(button, label);
        if (status) status.textContent = label;
        window.setTimeout(() => {
          resetButton(button);
          if (status) status.textContent = "";
        }, 4000);
      });
    });

    // Returning with the back button can restore a page from cache in its
    // busy state; reset it.
    window.addEventListener("pageshow", (event) => {
      if (event.persisted) resetAll();
    });
  }

  initialiseFileInputs();
  initialiseRecordSelections();
  initialiseBusyForms();
})();
