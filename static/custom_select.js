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

  // Create trigger (accessible button acting as a combobox)
  const trigger = document.createElement("div");
  trigger.className = "custom-select-trigger";
  trigger.setAttribute("tabindex", "0");
  trigger.setAttribute("role", "combobox");
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-controls", selectId + "-options-list");
  trigger.setAttribute(
    "aria-label",
    select.getAttribute("aria-label") ||
      (selectId === "modelSelect" ? "Select AI Model" : "Select option"),
  );

  // Create options container (accessible listbox)
  const optionsContainer = document.createElement("div");
  optionsContainer.className = "custom-select-options";
  optionsContainer.id = selectId + "-options-list";
  optionsContainer.setAttribute("role", "listbox");
  optionsContainer.setAttribute("tabindex", "-1");

  wrapper.appendChild(trigger);
  wrapper.appendChild(optionsContainer);

  let highlightedIndex = -1;
  const customOptions = [];

  // Populate options
  Array.from(select.options).forEach((option, index) => {
    const customOption = document.createElement("div");
    customOption.className = "custom-select-option";
    customOption.textContent = option.text;
    customOption.dataset.value = option.value;
    customOption.setAttribute("role", "option");
    customOption.setAttribute(
      "aria-selected",
      option.selected ? "true" : "false",
    );
    customOption.id = `${selectId}-option-${index}`;

    if (option.title) {
      customOption.title = option.title;
    }
    if (option.selected) {
      trigger.innerHTML = `${option.text}`;
      if (option.title) {
        trigger.title = option.title;
      }
      customOption.classList.add("selected");
      highlightedIndex = index;
    }

    customOption.addEventListener("click", function (e) {
      e.stopPropagation();
      selectOption(index);
    });

    // Handle mouse hover to coordinate keyboard highlight visual states
    customOption.addEventListener("mouseenter", function () {
      setHighlighted(index);
    });

    optionsContainer.appendChild(customOption);
    customOptions.push(customOption);
  });

  // Function to handle choosing a specific option
  function selectOption(index) {
    const option = select.options[index];
    if (!option) return;

    select.value = option.value;
    trigger.innerHTML = `${option.text}`;
    if (option.title) {
      trigger.title = option.title;
    } else {
      trigger.removeAttribute("title");
    }

    // Update selected class and ARIA selected attribute
    customOptions.forEach((child, idx) => {
      if (idx === index) {
        child.classList.add("selected");
        child.setAttribute("aria-selected", "true");
      } else {
        child.classList.remove("selected");
        child.setAttribute("aria-selected", "false");
      }
    });

    // Dispatch change event on original select to trigger main app state/theme changes
    select.dispatchEvent(new Event("change"));
    closeDropdown();
    trigger.focus(); // Retain focus on the trigger after selection
  }

  // Set the highlighted state for keyboard navigation/hovering
  function setHighlighted(index) {
    highlightedIndex = index;
    customOptions.forEach((child, idx) => {
      if (idx === index) {
        child.classList.add("highlighted");
        // Scroll option into view within the container if it goes off-screen
        if (optionsContainer.classList.contains("open")) {
          child.scrollIntoView({ block: "nearest" });
        }
      } else {
        child.classList.remove("highlighted");
      }
    });
  }

  // Open the dropdown container and update state attributes
  function openDropdown() {
    // Close any other open custom selects
    document.querySelectorAll(".custom-select-options").forEach((el) => {
      if (el !== optionsContainer) {
        el.classList.remove("open");
        const otherTrigger = el.previousSibling;
        if (otherTrigger) {
          otherTrigger.setAttribute("aria-expanded", "false");
        }
      }
    });

    optionsContainer.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");

    // Initialize highlight to current selection
    const selectedIdx = Array.from(select.options).findIndex((o) => o.selected);
    if (selectedIdx !== -1) {
      setHighlighted(selectedIdx);
    }
  }

  // Close the dropdown container and reset highlights
  function closeDropdown() {
    optionsContainer.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
    highlightedIndex = -1;
    customOptions.forEach((child) => child.classList.remove("highlighted"));
  }

  // Toggle options on click
  trigger.addEventListener("click", function (e) {
    e.stopPropagation();
    const isOpen = optionsContainer.classList.contains("open");
    if (isOpen) {
      closeDropdown();
    } else {
      openDropdown();
    }
  });

  // Keyboard navigation event listeners for accessible interactive control
  trigger.addEventListener("keydown", function (e) {
    const isOpen = optionsContainer.classList.contains("open");

    switch (e.key) {
      case "Enter":
      case " ":
        e.preventDefault();
        if (isOpen) {
          if (
            highlightedIndex >= 0 &&
            highlightedIndex < customOptions.length
          ) {
            selectOption(highlightedIndex);
          } else {
            closeDropdown();
          }
        } else {
          openDropdown();
        }
        break;

      case "ArrowDown":
        e.preventDefault();
        if (!isOpen) {
          openDropdown();
        } else {
          let nextIndex = highlightedIndex + 1;
          if (nextIndex >= customOptions.length) {
            nextIndex = 0; // Wrap around
          }
          setHighlighted(nextIndex);
        }
        break;

      case "ArrowUp":
        e.preventDefault();
        if (!isOpen) {
          openDropdown();
        } else {
          let prevIndex = highlightedIndex - 1;
          if (prevIndex < 0) {
            prevIndex = customOptions.length - 1; // Wrap around
          }
          setHighlighted(prevIndex);
        }
        break;

      case "Escape":
        if (isOpen) {
          e.preventDefault();
          e.stopPropagation();
          closeDropdown();
          trigger.focus();
        }
        break;

      case "Tab":
        if (isOpen) {
          closeDropdown();
        }
        break;
    }
  });

  // Update custom select when the original select is programmatically changed
  select.addEventListener("change", function () {
    const selectedOption = select.options[select.selectedIndex];
    if (!selectedOption) return;

    trigger.innerHTML = `${selectedOption.text}`;
    if (selectedOption.title) {
      trigger.title = selectedOption.title;
    } else {
      trigger.removeAttribute("title");
    }

    customOptions.forEach((child) => {
      if (child.dataset.value === selectedOption.value) {
        child.classList.add("selected");
        child.setAttribute("aria-selected", "true");
      } else {
        child.classList.remove("selected");
        child.setAttribute("aria-selected", "false");
      }
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  createCustomSelect("modeSelector");
  createCustomSelect("modelSelect");

  // Close dropdowns on outside click and update ARIA state
  document.addEventListener("click", function () {
    document.querySelectorAll(".custom-select-options").forEach((el) => {
      el.classList.remove("open");
      const trigger = el.previousSibling;
      if (trigger && trigger.classList.contains("custom-select-trigger")) {
        trigger.setAttribute("aria-expanded", "false");
      }
    });
  });
});
