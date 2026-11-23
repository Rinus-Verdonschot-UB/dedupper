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

  initialiseFileInputs();
  initialiseRecordSelections();
})();
