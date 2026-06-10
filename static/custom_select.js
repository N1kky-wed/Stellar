function createCustomSelect(selectId) {
  const select = document.getElementById(selectId);
  if (!select) return;

  // Hide original
  select.style.display = "none";

  // Create wrapper
  const wrapper = document.createElement("div");
  wrapper.className = "custom-select-wrapper";
  wrapper.id = selectId + "-wrapper";
  select.parentNode.insertBefore(wrapper, select.nextSibling);

  // Create trigger
  const trigger = document.createElement("div");
  trigger.className = "custom-select-trigger";

  // Create options container
  const optionsContainer = document.createElement("div");
  optionsContainer.className = "custom-select-options";

  wrapper.appendChild(trigger);
  wrapper.appendChild(optionsContainer);

  // Populate options
  Array.from(select.options).forEach((option) => {
    const customOption = document.createElement("div");
    customOption.className = "custom-select-option";
    customOption.textContent = option.text;
    customOption.dataset.value = option.value;
    if (option.title) {
      customOption.title = option.title;
    }
    if (option.selected) {
      trigger.innerHTML = `${option.text}`;
      if (option.title) {
        trigger.title = option.title;
      }
      customOption.classList.add("selected");
    }

    customOption.addEventListener("click", function (e) {
      e.stopPropagation();
      select.value = option.value;
      trigger.innerHTML = `${option.text}`;
      if (option.title) {
        trigger.title = option.title;
      } else {
        trigger.removeAttribute("title");
      }

      // Remove selected class from all
      Array.from(optionsContainer.children).forEach((child) =>
        child.classList.remove("selected"),
      );
      this.classList.add("selected");

      // Dispatch change event on original select
      select.dispatchEvent(new Event("change"));
      optionsContainer.classList.remove("open");
    });
    optionsContainer.appendChild(customOption);
  });

  // Toggle options
  trigger.addEventListener("click", function (e) {
    e.stopPropagation();
    const isOpen = optionsContainer.classList.contains("open");
    document
      .querySelectorAll(".custom-select-options")
      .forEach((el) => el.classList.remove("open")); // Close all others
    if (!isOpen) {
      optionsContainer.classList.add("open");
    }
  });

  // Update on original select change (if changed programmatically)
  select.addEventListener("change", function () {
    const selectedOption = select.options[select.selectedIndex];
    trigger.innerHTML = `${selectedOption.text}`;
    if (selectedOption && selectedOption.title) {
      trigger.title = selectedOption.title;
    } else {
      trigger.removeAttribute("title");
    }
    Array.from(optionsContainer.children).forEach((child) => {
      if (child.dataset.value === selectedOption.value) {
        child.classList.add("selected");
      } else {
        child.classList.remove("selected");
      }
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  createCustomSelect("modeSelector");
  createCustomSelect("modelSelect");

  // Close dropdowns on outside click
  document.addEventListener("click", function () {
    document
      .querySelectorAll(".custom-select-options")
      .forEach((el) => el.classList.remove("open"));
  });
});
