window.stellar = window.stellar || {};
window.stellar.send = function (prompt, silent = false) {
  const chatInput = document.getElementById("chatInput");
  const sendBtn = document.getElementById("sendBtn");
  if (chatInput && sendBtn) {
    chatInput.value = prompt;
    // Since handleSend is scoped, the easiest global hook is to trigger a click
    if (silent) {
      // We can't trigger silent globally via click, so we dispatch a custom event
      const event = new CustomEvent("stellarSend", {
        detail: { prompt, silent },
      });
      window.dispatchEvent(event);
    } else {
      sendBtn.click();
    }
  }
};

window.stellar.currentProcessingContainer = null;

// Global tracking of the last interacted element
let lastInteractedElement = null;
document.addEventListener(
  "click",
  (e) => {
    lastInteractedElement = e.target;
  },
  true,
);
document.addEventListener(
  "submit",
  (e) => {
    lastInteractedElement = e.target;
  },
  true,
);
document.addEventListener(
  "change",
  (e) => {
    lastInteractedElement = e.target;
  },
  true,
);

// Centralized autofix trigger
window.stellar.triggerAutofix = function (containerElement, targetEl, error) {
  if (!window.stellar || !window.stellar.send) return;

  let container = containerElement;
  if (typeof containerElement === "string") {
    container = document.getElementById(containerElement);
  }
  if (!container) return;

  // Protect user messages from autofix mechanisms
  if (
    container.classList.contains("user-msg") ||
    container.closest(".user-msg")
  )
    return;

  // Prevent multiple concurrent fixes for the same container
  if (container.dataset.autofixTriggered === "true") return;
  container.dataset.autofixTriggered = "true";

  const errMsg = error ? error.toString() : "Unknown JavaScript error";
  console.warn(
    "Autofix triggered for container:",
    container.id,
    "Error:",
    errMsg,
  );

  // Provide visual feedback
  if (targetEl) {
    targetEl.style.backgroundColor = "#eab308";
    targetEl.style.color = "#000";
    targetEl.style.opacity = "0.8";
    if (targetEl.tagName === "TR") {
      const firstTd = targetEl.querySelector("td");
      if (firstTd)
        firstTd.innerHTML =
          "Fixing... <span class='animate-spin' style='display:inline-block;'>⟳</span>";
    } else {
      targetEl.innerHTML =
        "Fixing... <span class='animate-spin' style='display:inline-block; margin-left: 4px;'>⟳</span>";
      targetEl.disabled = true;
    }
  } else {
    const firstBtn = container.querySelector("button, .btn, a");
    if (firstBtn) {
      firstBtn.style.backgroundColor = "#eab308";
      firstBtn.style.color = "#000";
      firstBtn.innerHTML =
        "Fixing... <span class='animate-spin' style='display:inline-block; margin-left: 4px;'>⟳</span>";
    } else {
      const feedbackDiv = document.createElement("div");
      feedbackDiv.style.padding = "10px";
      feedbackDiv.style.margin = "10px 0";
      feedbackDiv.style.backgroundColor = "rgba(234, 179, 8, 0.1)";
      feedbackDiv.style.border = "1px solid #eab308";
      feedbackDiv.style.borderRadius = "8px";
      feedbackDiv.style.color = "#eab308";
      feedbackDiv.style.fontSize = "12px";
      feedbackDiv.innerHTML =
        "Self-correcting UI error... <span class='animate-spin' style='display:inline-block; margin-left: 4px;'>⟳</span>";
      container.appendChild(feedbackDiv);
    }
  }

  // Send feedback to LLM
  window.stellar.send(
    `[SYSTEM AUTO-FEEDBACK: The user clicked/interacted with an element in your UI, but your JavaScript crashed with the following error: "${errMsg}". Please analyze your code, fix the logic or syntax error, and output the fully corrected HTML block.

IMPORTANT: If you originally used the request_user_interaction tool to present this UI, you MUST invoke the request_user_interaction tool again with your corrected HTML UI so the interaction flow can resume. In all cases, you MUST include this exact tag at the very end of your response so the system can hot-swap the old broken UI with your new version:
<div style="display:none" data-autofix-replace="${container.id}"></div>

Respond ONLY with the newly corrected raw HTML block/tool call and this tag.]`,
    true,
  );
};

// Global error handler
window.addEventListener("error", (event) => {
  handleGlobalError(event.error || event.message);
});

// Global unhandled promise rejection handler
window.addEventListener("unhandledrejection", (event) => {
  handleGlobalError(event.reason);
});

function handleGlobalError(error) {
  if (!error) return;

  // Case 1: Initial load / execution error
  if (window.stellar.currentProcessingContainer) {
    window.stellar.triggerAutofix(
      window.stellar.currentProcessingContainer,
      null,
      error,
    );
    return;
  }

  // Case 2: Interaction / click error
  if (lastInteractedElement) {
    const genUiContainer = lastInteractedElement.closest('[id^="gen-ui-"]');
    if (genUiContainer) {
      window.stellar.triggerAutofix(
        genUiContainer,
        lastInteractedElement,
        error,
      );
      lastInteractedElement = null; // Clear to prevent double triggers
    }
  }
}

const defaultAgentSettings = {
  logs_and_preferences: true,
  generate_image: true,
  make_presentation: true,
  web_search: true,
  lab_execute: true,
  manage_files: true,
  read_tool_output: true,
  analyze_youtube_video: true,
  send_self_email: true,
  schedule_task: true,
  request_user_interaction: true,
  notifications_enabled: true,
};
let agentSettings =
  JSON.parse(localStorage.getItem("agentSettings")) || defaultAgentSettings;

function applySettingsUI() {
  for (const [tool, enabled] of Object.entries(agentSettings)) {
    const checkbox = document.getElementById("toolToggle-" + tool);
    if (checkbox) {
      checkbox.checked = enabled;
    }
  }
}

// PWA Installation & Service Worker Integration
let deferredPrompt = null;

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/service-worker.js")
      .then((reg) => {
        console.log(
          "[PWA] Service Worker registered successfully with scope:",
          reg.scope,
        );

        if (
          agentSettings.notifications_enabled &&
          Notification.permission === "granted"
        ) {
          subscribeUserToPush();
        } else {
          reg.pushManager.getSubscription().then((sub) => {
            if (sub) {
              sub
                .unsubscribe()
                .then(() =>
                  console.log(
                    "[PWA] Unsubscribed from push notifications locally.",
                  ),
                );
            }
          });
        }

        reg.addEventListener("updatefound", () => {
          const newWorker = reg.installing;
          newWorker.addEventListener("statechange", () => {
            if (newWorker.state === "activated") {
              console.log("[PWA] New updates activated. Hot-reloading...");
              // Don't reload during login — SW update mid-auth kills the session
              if (!document.querySelector("#googleAuthBtn")) {
                window.location.reload();
              }
            }
          });
        });
      })
      .catch((err) =>
        console.error("[PWA] Service Worker registration failed:", err),
      );

    navigator.serviceWorker.addEventListener("controllerchange", () => {
      console.log("[PWA] SW controller changed. Hot-reloading...");
      // Don't reload during login — SW update mid-auth kills the session
      if (!document.querySelector("#googleAuthBtn")) {
        window.location.reload();
      }
    });
  });
}

window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredPrompt = e;

  const isStandalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    navigator.standalone;
  if (isStandalone) return;

  if (
    localStorage.getItem("pwa_installed") === "true" ||
    localStorage.getItem("pwa_prompt_dismissed") === "true"
  ) {
    return;
  }

  showInstallBanner("android");
});

// Listen to native appinstalled event
window.addEventListener("appinstalled", () => {
  console.log("[PWA] App installed successfully!");
  localStorage.setItem("pwa_installed", "true");
  document.getElementById("pwa-install-banner")?.remove();
});

// Show custom install prompt for iOS/Android if not in standalone mode
window.addEventListener("load", () => {
  const isStandalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    navigator.standalone;
  if (!isStandalone) {
    const ua = navigator.userAgent.toLowerCase();
    const isIOS = /ipad|iphone|ipod/.test(ua) && !window.MSStream;
    const isAndroid = /android/.test(ua);

    if (
      localStorage.getItem("pwa_installed") !== "true" &&
      localStorage.getItem("pwa_prompt_dismissed") !== "true"
    ) {
      if (isIOS) {
        setTimeout(() => showInstallBanner("ios"), 5000);
      } else if (isAndroid && !deferredPrompt) {
        setTimeout(() => {
          if (!deferredPrompt) showInstallBanner("android-fallback");
        }, 6000);
      }
    }
  }
});

function showInstallBanner(platform) {
  const isStandalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    navigator.standalone;
  if (isStandalone) return;

  if (
    localStorage.getItem("pwa_installed") === "true" ||
    localStorage.getItem("pwa_prompt_dismissed") === "true"
  ) {
    return;
  }

  if (document.getElementById("pwa-install-banner")) return;

  const banner = document.createElement("div");
  banner.id = "pwa-install-banner";
  banner.className = "pwa-install-banner";

  let content = "";
  if (platform === "ios") {
    content = `
            <div class="pwa-banner-icon">
              <img src="/static/icon.svg" alt="Stellar">
            </div>
            <div class="pwa-banner-text">
              <h4>Install Stellar</h4>
              <p>Tap <span class="pwa-highlight-btn">⎋ (Share)</span> then <strong>"Add to Home Screen"</strong> for native notifications & full experience.</p>
            </div>
            <button class="pwa-banner-close" id="pwa-ios-close-btn">×</button>
          `;
  } else {
    content = `
            <div class="pwa-banner-icon">
              <img src="/static/icon.svg" alt="Stellar">
            </div>
            <div class="pwa-banner-text">
              <h4>Install Stellar App</h4>
              <p>Add to your home screen for instant access and native background updates.</p>
            </div>
            <div class="pwa-banner-actions">
              <button class="pwa-install-btn" id="pwa-install-action-btn">Install</button>
              <button class="pwa-banner-close-text" id="pwa-install-close-btn">Dismiss</button>
            </div>
          `;
  }

  banner.innerHTML = content;
  document.body.appendChild(banner);

  setTimeout(() => banner.classList.add("active"), 100);

  if (platform === "ios") {
    const iosCloseBtn = document.getElementById("pwa-ios-close-btn");
    iosCloseBtn?.addEventListener("click", () => {
      localStorage.setItem("pwa_prompt_dismissed", "true");
      banner.remove();
    });
  } else {
    const actionBtn = document.getElementById("pwa-install-action-btn");
    const closeBtn = document.getElementById("pwa-install-close-btn");

    actionBtn?.addEventListener("click", () => {
      if (deferredPrompt) {
        deferredPrompt.prompt();
        deferredPrompt.userChoice.then((choiceResult) => {
          if (choiceResult.outcome === "accepted") {
            localStorage.setItem("pwa_installed", "true");
          }
          deferredPrompt = null;
          banner.remove();
        });
      } else {
        alert(
          "To install, tap the browser's menu (three dots) and select 'Install app' or 'Add to Home screen'.",
        );
        banner.remove();
      }
    });

    closeBtn?.addEventListener("click", () => {
      localStorage.setItem("pwa_prompt_dismissed", "true");
      banner.remove();
    });
  }
}

// Setup event listener for agent tools changes
window.addEventListener("load", () => {
  document.body.classList.remove("preload");
});
document.addEventListener("DOMContentLoaded", () => {
  console.log("DOM LOADED FIRED IN MAIN JS!");
  applySettingsUI();

  // Wire up the custom confirmation modal buttons
  const cancelConfirmationBtn = document.getElementById(
    "cancelConfirmationBtn",
  );
  const confirmBtn = document.getElementById("confirmBtn");
  const confirmationModalBackdrop = document.getElementById(
    "confirmationModalBackdrop",
  );

  if (cancelConfirmationBtn) {
    cancelConfirmationBtn.addEventListener("click", hideConfirmationModal);
  }
  if (confirmBtn) {
    confirmBtn.addEventListener("click", () => {
      if (typeof confirmationCallback === "function") {
        confirmationCallback();
      }
      hideConfirmationModal();
    });
  }
  if (confirmationModalBackdrop) {
    confirmationModalBackdrop.addEventListener("click", (e) => {
      if (e.target === confirmationModalBackdrop) {
        hideConfirmationModal();
      }
    });
  }

  // Handle global Escape key to close the confirmation modal
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const backdrop = document.getElementById("confirmationModalBackdrop");
      if (backdrop && backdrop.style.display === "flex") {
        hideConfirmationModal();
      }
    }
  });

  document
    .getElementById("closeBrowserPaneBtn")
    ?.addEventListener("click", () => {
      document.getElementById("browserPane").style.display = "none";
      document.body.classList.remove("browser-open");
    });

  document
    .getElementById("toggleBrowserPaneBtn")
    ?.addEventListener("click", () => {
      const pane = document.getElementById("browserPane");
      if (pane.style.display === "none") {
        pane.style.display = "flex";
        document.body.classList.add("browser-open");
      } else {
        pane.style.display = "none";
        document.body.classList.remove("browser-open");
      }
    });

  const profileNotificationsToggle = document.getElementById(
    "profile-notifications-toggle",
  );
  if (profileNotificationsToggle) {
    profileNotificationsToggle.checked =
      agentSettings.notifications_enabled !== false;
    profileNotificationsToggle.addEventListener("change", (e) => {
      agentSettings.notifications_enabled = e.target.checked;
      localStorage.setItem("agentSettings", JSON.stringify(agentSettings));
      if (e.target.checked) requestNotificationPermission();
    });
  }

  const profilePureBlackToggle = document.getElementById(
    "profile-pure-black-toggle",
  );
  if (profilePureBlackToggle) {
    profilePureBlackToggle.checked =
      localStorage.getItem("pureBlackMode") !== "false";
    profilePureBlackToggle.addEventListener("change", (e) => {
      if (e.target.checked) {
        bodyElement.classList.add("pure-black");
        localStorage.setItem("pureBlackMode", "true");
      } else {
        bodyElement.classList.remove("pure-black");
        localStorage.setItem("pureBlackMode", "false");
      }
    });
  }

  const toolsList = document.getElementById("agentToolsSettingsList");
  if (toolsList) {
    toolsList.addEventListener("change", function (e) {
      if (e.target.type === "checkbox") {
        const toolName = e.target.id.replace("toolToggle-", "");
        agentSettings[toolName] = e.target.checked;
        localStorage.setItem("agentSettings", JSON.stringify(agentSettings));

        if (toolName === "notifications_enabled" && e.target.checked) {
          requestNotificationPermission();
        }
      }
    });
  }

  // Logs & Preferences Management Logic
  const manageLogsBtn = document.getElementById("manageLogsBtn");
  const logsPrefsModal = document.getElementById("logsPrefsModal");
  const closeLogsPrefsBtn = document.getElementById("closeLogsPrefsBtn");
  const logsPrefsList = document.getElementById("logsPrefsList");
  const refreshLogsBtn = document.getElementById("refreshLogsBtn");
  const saveAllLogsBtn = document.getElementById("saveAllLogsBtn");

  function updateMemoryTokenFootprint() {
    const textareas = document.querySelectorAll(".log-entry-textarea");
    const textList = Array.from(textareas)
      .map((ta) => ta.value.trim())
      .filter((t) => t.length > 0);

    if (textList.length === 0) {
      const footprintEl = document.getElementById("memoryTokenFootprint");
      if (footprintEl) footprintEl.textContent = "0 Tokens";
      return;
    }

    fetch("/api/utils/count_tokens", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text_list: textList }),
    })
      .then((res) => res.json())
      .then((data) => {
        const footprintEl = document.getElementById("memoryTokenFootprint");
        if (footprintEl && data.token_count !== undefined) {
          footprintEl.textContent = `${data.token_count.toLocaleString()} Tokens`;
        }
      })
      .catch((err) => console.error("Error counting tokens:", err));
  }

  function fetchAndRenderLogs() {
    logsPrefsList.innerHTML =
      '<div style="color:var(--secondary-text-color); text-align:center; padding:20px;">Synchronizing memory nodes...</div>';
    fetch("/api/logs_preferences")
      .then((res) => res.json())
      .then((data) => {
        logsPrefsList.innerHTML = "";
        const logs = data.logs || [];
        if (logs.length === 0) {
          logsPrefsList.innerHTML =
            '<div style="color:var(--secondary-text-color); text-align:center; padding:20px;">No memory nodes stored.</div>';
          updateMemoryTokenFootprint();
          return;
        }
        logs.forEach((log, index) => {
          const item = document.createElement("div");
          item.style.cssText =
            "background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.1); border-radius:12px; padding:15px; display:flex; flex-direction:column; gap:10px; position:relative;";

          const textarea = document.createElement("textarea");
          textarea.value = log;
          textarea.className = "log-entry-textarea";
          textarea.style.cssText =
            "background:rgba(0,0,0,0.2); color:var(--primary-text-color); border:1px solid rgba(255,255,255,0.05); border-radius:8px; padding:10px; height:auto; font-family:inherit; font-size:0.9rem; resize:none; overflow:hidden;";

          // Auto-resize to content
          setTimeout(() => {
            textarea.style.height = "auto";
            textarea.style.height = textarea.scrollHeight + "px";
          }, 0);

          textarea.oninput = () => {
            textarea.style.height = "auto";
            textarea.style.height = textarea.scrollHeight + "px";
          };
          const actions = document.createElement("div");
          actions.style.cssText =
            "display:flex; justify-content:flex-end; gap:10px;";

          const deleteBtn = document.createElement("button");
          deleteBtn.innerHTML = "Delete";
          deleteBtn.style.cssText =
            "background:rgba(255,68,68,0.1); color:#ff4444; border:1px solid rgba(255,68,68,0.2); padding:5px 12px; border-radius:6px; cursor:pointer; font-size:0.8rem;";
          deleteBtn.onclick = () => {
            fetch(`/api/logs_preferences?index=${index}`, {
              method: "DELETE",
            })
              .then((res) => res.json())
              .then((res) => {
                if (res.success) {
                  item.remove();
                  updateMemoryTokenFootprint();
                  // Re-render to update indices if needed, but remove from DOM immediately for snappy feel
                  fetchAndRenderLogs();
                }
              });
          };
          item.appendChild(textarea);
          item.appendChild(actions);
          actions.appendChild(deleteBtn);
          logsPrefsList.appendChild(item);
        });
        updateMemoryTokenFootprint();
      });
  }

  if (manageLogsBtn) {
    manageLogsBtn.onclick = (e) => {
      e.stopPropagation();
      logsPrefsModal.style.display = "flex";
      fetchAndRenderLogs();
    };
  }

  if (closeLogsPrefsBtn)
    closeLogsPrefsBtn.onclick = () => (logsPrefsModal.style.display = "none");
  if (refreshLogsBtn) refreshLogsBtn.onclick = fetchAndRenderLogs;

  if (saveAllLogsBtn) {
    saveAllLogsBtn.onclick = () => {
      const textareas = document.querySelectorAll(".log-entry-textarea");
      const updatedLogs = Array.from(textareas)
        .map((t) => t.value.trim())
        .filter((v) => v !== "");

      const originalText = saveAllLogsBtn.textContent;
      saveAllLogsBtn.disabled = true;
      saveAllLogsBtn.textContent = "Saving...";

      fetch("/api/logs_preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ logs: updatedLogs }),
      })
        .then((res) => res.json())
        .then((res) => {
          // Highlight save action success/failure with text transitions and color changes
          if (res.success) {
            saveAllLogsBtn.textContent = "✓ Saved Successfully";
            saveAllLogsBtn.style.background = "#22c55e"; // Success green accent
            fetchAndRenderLogs();
            setTimeout(() => {
              saveAllLogsBtn.textContent = originalText;
              saveAllLogsBtn.style.background = "";
              saveAllLogsBtn.disabled = false;
            }, 2000);
          } else {
            saveAllLogsBtn.textContent = "Error Saving";
            saveAllLogsBtn.style.background = "#ef4444"; // Error red accent
            setTimeout(() => {
              saveAllLogsBtn.textContent = originalText;
              saveAllLogsBtn.style.background = "";
              saveAllLogsBtn.disabled = false;
            }, 2000);
          }
        })
        .catch(() => {
          saveAllLogsBtn.textContent = "Error Saving";
          saveAllLogsBtn.style.background = "#ef4444"; // Error red accent
          setTimeout(() => {
            saveAllLogsBtn.textContent = originalText;
            saveAllLogsBtn.style.background = "";
            saveAllLogsBtn.disabled = false;
          }, 2000);
        });
    };
  }
});
const stopBtn = document.getElementById("stopBtn");
const chatSearchInput = document.getElementById("chatSearchInput");
const messagesDiv = document.getElementById("messages");
const chatInput = document.getElementById("chatInput");
const sendBtn = document.getElementById("sendBtn");
const modeSelector = { value: "stellar", addEventListener: () => {} };
const clearHistoryBtn = document.getElementById("clearHistoryBtn");
const modelSelect = document.getElementById("modelSelect");
const inputContainer = document.getElementById("inputContainer");
const chatContainer = document.getElementById("chatContainer");
const editModalBackdrop = document.getElementById("editModalBackdrop");
const editMarkdownInput = document.getElementById("editMarkdownInput");
const cancelEditBtn = document.getElementById("cancelEditBtn");
const saveEditBtn = document.getElementById("saveEditBtn");
const modelSelectWidthHelper = document.getElementById(
  "modelSelectWidthHelper",
);
const bodyElement = document.body;
if (localStorage.getItem("pureBlackMode") !== "false") {
  bodyElement.classList.add("pure-black");
}
const regenerateModalBackdrop = document.getElementById(
  "regenerateModalBackdrop",
);
const regenerateModalTitle = document.getElementById("regenerateModalTitle");
const regenerateFeedbackInput = document.getElementById(
  "regenerateFeedbackInput",
);
const cancelRegenerateBtn = document.getElementById("cancelRegenerateBtn");
const saveRegenerateBtn = document.getElementById("saveRegenerateBtn");
const regenerateWithoutFeedbackBtn = document.getElementById(
  "regenerateWithoutFeedbackBtn",
);
const fileUploadInput = document.getElementById("fileUpload");

const authContainer = document.getElementById("authContainer");
const sidebarToggleBtn = document.getElementById("sidebarToggleBtn");
const sidebar = document.getElementById("sidebar");
const sidebarCloseBtn = document.getElementById("sidebarCloseBtn");
const newChatBtn = document.getElementById("newChatBtn");
const chatList = document.getElementById("chatList");
const chatTitle = document.getElementById("chatTitle");
const profileIcon = document.getElementById("profileIcon");
const profileModal = document.getElementById("profileModal");
const agentSettingsModal = document.getElementById("agentSettingsModal");
const openAgentSettingsBtn = document.getElementById("openAgentSettingsBtn");
const agentSettingsCloseBtn = document.getElementById("agentSettingsCloseBtn");
const profileCloseBtn = document.getElementById("profileCloseBtn");
const profileUsernameDisplay = document.getElementById(
  "profileUsernameDisplay",
);
const changePasswordForm = document.getElementById("changePasswordForm");
const newDisplayNameInput = document.getElementById("newDisplayName");
const passwordChangeMessage = document.getElementById("passwordChangeMessage");
const logoutButtonProfile = document.getElementById("logoutButtonProfile");
const sidebarUsername = document.getElementById("sidebarUsername");
const tokenCountBar = document.getElementById("tokenCountBar");
const stagedFilesContainer = document.getElementById("stagedFilesContainer");
const chatScrollToBottomBtn = document.getElementById("chatScrollToBottomBtn");
function copyCode(buttonElement, codeEl) {
  const codeToCopy = codeEl.textContent;
  navigator.clipboard
    .writeText(codeToCopy)
    .then(() => {
      buttonElement.innerHTML =
        '<svg><use xlink:href="#icon-check"></use></svg> Copied!';
      buttonElement.classList.add("copied");
      buttonElement.disabled = true;

      setTimeout(() => {
        buttonElement.innerHTML =
          '<svg><use xlink:href="#icon-copy"></use></svg> Copy';
        buttonElement.classList.remove("copied");
        buttonElement.disabled = false;
      }, 1500);
    })
    .catch((err) => {
      console.error("Failed to copy code: ", err);
      buttonElement.textContent = "Error";
      setTimeout(() => {
        buttonElement.innerHTML =
          '<svg><use xlink:href="#icon-copy"></use></svg> Copy';
        buttonElement.disabled = false;
      }, 2000);
    });
}
let renderHtmlDebounceTimer = null;
let livePreviewTimer = null;
let scrapingQueue = [];
let scrapingTimerId = null;
const SCRAPING_DISPLAY_DELAY = 15000;
const CLIENT_ID = Math.random().toString(36).substring(2, 15);
let globalEventSource = null;

function initGlobalEventStream() {
  if (globalEventSource) globalEventSource.close();

  globalEventSource = new EventSource("/api/user/events");

  globalEventSource.onmessage = (e) => {
    const data = JSON.parse(e.data);

    // Ignore events triggered by this exact browser tab
    if (data.client_id === CLIENT_ID) return;

    if (data.type === "new_message") {
      if (data.chat_id === currentChatId && !data.message.hidden) {
        // Absolute deduplication: If message ID is already in the DOM, skip it entirely.
        if (document.querySelector(`.message[data-id="${data.message.id}"]`))
          return;

        if (data.message.type === "user") {
          appendUserMessage(data.message.content, data.message.id);
        } else {
          // If a stream is actively rendering, it handles AI output via SSE. Skip global injection.
          if (!isProcessing) {
            if (data.message.is_research) {
              appendResearchOutput(data.message.content, data.message.id);
            } else {
              appendStellarMessage(data.message.content, data.message.id);
            }
          }
        }
      }
    }

    if (data.type === "query_started") {
      if (data.chat_id === currentChatId) {
        console.log(
          "Remote device started a query. Reconnecting stream locally...",
        );
        reconnectToStream(data.query_id, data.mode, data.chat_id);
      }
    }
  };

  globalEventSource.onerror = () => {
    globalEventSource.close();
    setTimeout(initGlobalEventStream, 5000);
  };
}

let currentMode = "stellar";
let currentStreamQueryId = null;
let currentEditingMsg = null,
  currentEditingMsgId = null;
let confirmationCallback = null;
let lastRefinedQuery = "";
let isProcessing = false;
let taskStartTime = null;
let notifiedForLongTask = false;
let currentStatusText = "Idle";
let longTaskStatusInterval = null;

function startLongTaskMonitor(id) {
  if (longTaskStatusInterval) clearInterval(longTaskStatusInterval);
  longTaskStatusInterval = setInterval(() => {
    if (isProcessing && taskStartTime && Date.now() - taskStartTime > 20000) {
      // Update main status bar
      setStatus(currentStatusText);
      // Update current placeholder status if id provided
      if (id) {
        const msgDiv = messagesDiv.querySelector(`.message[data-id="${id}"]`);
        if (msgDiv) {
          const statusSpan = msgDiv.querySelector(".placeholder-status");
          if (statusSpan) {
            // This triggers the re-render with the "I'll notify you" text
            updateStellarMessagePlaceholder(id, currentStatusText);
          }
        }
      }
    }
  }, 5000);
}

function stopLongTaskMonitor() {
  if (longTaskStatusInterval) {
    clearInterval(longTaskStatusInterval);
    longTaskStatusInterval = null;
  }
}

function urlB64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding)
    .replace(/\-/g, "+")
    .replace(/_/g, "/");

  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);

  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

async function subscribeUserToPush() {
  if (!("serviceWorker" in navigator)) return;
  if (!("PushManager" in window)) {
    console.warn("[PWA] Push notifications are not supported on this browser.");
    return;
  }

  try {
    const reg = await navigator.serviceWorker.ready;
    let subscription = await reg.pushManager.getSubscription();

    if (!subscription) {
      const res = await fetch("/api/pwa/vapid_public_key");
      const data = await res.json();
      if (!data.success || !data.publicKey) {
        console.error("[PWA] Failed to fetch VAPID public key:", data.message);
        return;
      }

      const applicationServerKey = urlB64ToUint8Array(data.publicKey);
      subscription = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: applicationServerKey,
      });
      console.log("[PWA] Successfully subscribed to Push Manager.");
    }

    const subJSON = subscription.toJSON();
    const subscribeRes = await fetch("/api/pwa/subscribe", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        subscription: {
          endpoint: subJSON.endpoint,
          keys: {
            p256dh: subJSON.keys.p256dh,
            auth: subJSON.keys.auth,
          },
        },
      }),
    });
    const subscribeData = await subscribeRes.json();
    if (subscribeData.success) {
      console.log("[PWA] Push subscription saved on backend.");
    } else {
      console.error(
        "[PWA] Failed to save push subscription on backend:",
        subscribeData.message,
      );
    }
  } catch (err) {
    console.error("[PWA] Error during push subscription workflow:", err);
  }
}

async function requestNotificationPermission() {
  if (!("Notification" in window)) return;
  if (Notification.permission === "default") {
    await Notification.requestPermission();
  }
  if (Notification.permission === "granted") {
    await subscribeUserToPush();
  }
}

function notifyUser(title, body) {
  if (!agentSettings.notifications_enabled) return;
  if (!("Notification" in window) || Notification.permission !== "granted")
    return;
  try {
    new Notification(title, {
      body: body,
    });
  } catch (e) {
    console.error("Failed to create notification:", e);
  }
}

let historyLoaded = false;
let currentRegeneratingStep = null;
let sseEventSource = null;
let stagedFiles = [];
let authImageUrls = [];
let currentAuthImageIndex = 0;
let currentChatId = null;
let currentUsername = null;

let currentSearchTerm = "";

const renderer = new marked.Renderer();

const originalLink = renderer.link.bind(renderer);
renderer.link = (token) => {
  // Support both old (href, title, text) and new ({href, title, text}) Marked.js APIs
  const isObj = typeof token === "object" && token !== null;
  let href = isObj ? token.href : token; // token is href in old API if called with apply
  // If old API, arguments might be used, but let's try to be safe
  if (!isObj) {
    href = arguments[0];
  }

  const videoExts = [".mp4", ".webm", ".ogg", ".mov", ".mkv", ".m4v"];
  const isVideo = videoExts.some((ext) =>
    href.toLowerCase().split("?")[0].endsWith(ext),
  );
  const isAudio = [".mp3", ".wav", ".ogg"].some((ext) =>
    href.toLowerCase().split("?")[0].endsWith(ext),
  );

  if (isVideo) {
    return `<div class="video-preview-wrapper" style="margin: 10px 0;">
                              <video controls playsinline style="max-width: 100%; border-radius: 12px; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 4px 15px rgba(0,0,0,0.3);">
                                  <source src="${href}">
                                  Your browser does not support the video tag.
                              </video>
                              <div style="margin-top: 5px;">
                                  <a href="${href}" target="_blank" rel="noopener noreferrer" style="font-size: 0.85em; opacity: 0.8;">[Open Video in New Tab]</a>
                              </div>
                          </div>`;
  }

  if (isAudio) {
    return `<div class="audio-preview-wrapper" style="margin: 10px 0;">
                              <audio controls style="width: 100%;">
                                  <source src="${href}">
                                  Your browser does not support the audio element.
                              </audio>
                              <div style="margin-top: 5px;">
                                  <a href="${href}" target="_blank" rel="noopener noreferrer" style="font-size: 0.85em; opacity: 0.8;">[Open Audio in New Tab]</a>
                              </div>
                          </div>`;
  }

  let html = isObj
    ? originalLink(token)
    : originalLink.apply(renderer, arguments);
  return html.replace("<a ", '<a target="_blank" rel="noopener noreferrer" ');
};

const originalImage = renderer.image.bind(renderer);
renderer.image = (token) => {
  const isObj = typeof token === "object" && token !== null;
  let processedHref = isObj ? token.href : arguments[0];
  let title = isObj ? token.title : arguments[1];
  let text = isObj ? token.text : arguments[2];

  if (processedHref && processedHref.startsWith("http://")) {
    // Route insecure HTTP images through our backend proxy to avoid Mixed Content errors
    processedHref = `/image-proxy?url=${encodeURIComponent(processedHref)}`;
  }

  if (isObj) {
    return originalImage({ ...token, href: processedHref });
  } else {
    return originalImage(processedHref, title, text);
  }
};

marked.setOptions({
  renderer: renderer,
  breaks: true, // This converts single \n characters into <br> tags
  gfm: true, // Ensures GitHub Flavored Markdown is enabled
});

// Helper to extract a balanced raw HTML block from the start of a source string
function getHtmlBlockPrefix(src) {
  const blockStartRegex =
    /^(?:[ \t]*)(<div|<style|<script|<section|<svg|<table|<iframe|<form|<canvas|<article|<aside|<header|<footer|<main|<!--)/i;
  const match = src.match(blockStartRegex);
  if (!match) return null;

  const matchIndex = match.index;
  if (src.substring(0, matchIndex).trim() !== "") {
    return null;
  }

  const htmlStart = src.substring(matchIndex);
  const tagMatch = match[1].toLowerCase();

  let closingTag = "";
  let isNestedTag = false;
  let tagName = "";

  if (tagMatch.startsWith("<!--")) {
    closingTag = "-->";
  } else {
    const tagParts = tagMatch.match(/<([a-zA-Z0-9]+)/);
    if (tagParts) {
      tagName = tagParts[1].toLowerCase();
      closingTag = "</" + tagName + ">";
      isNestedTag = [
        "div",
        "section",
        "article",
        "aside",
        "header",
        "footer",
        "main",
        "form",
        "table",
      ].includes(tagName);
    }
  }

  if (!closingTag) return null;

  let closeIndex = -1;
  if (isNestedTag) {
    let depth = 0;
    const openPattern = new RegExp("<" + tagName + "[\\s>]", "gi");
    const closePattern = new RegExp("</" + tagName + ">", "gi");

    let currentPos = 0;
    while (currentPos < htmlStart.length) {
      openPattern.lastIndex = currentPos;
      const nextOpen = openPattern.exec(htmlStart);

      closePattern.lastIndex = currentPos;
      const nextClose = closePattern.exec(htmlStart);

      if (!nextClose) {
        closeIndex = htmlStart.length;
        break;
      }

      if (nextOpen && nextOpen.index < nextClose.index) {
        depth++;
        currentPos = nextOpen.index + nextOpen[0].length;
      } else {
        depth--;
        if (depth === 0) {
          closeIndex = nextClose.index + nextClose[0].length;
          break;
        }
        currentPos = nextClose.index + nextClose[0].length;
      }
    }
  } else {
    const idx = htmlStart.toLowerCase().indexOf(closingTag);
    if (idx !== -1) {
      closeIndex = idx + closingTag.length;
    } else {
      closeIndex = htmlStart.length;
    }
  }

  if (closeIndex === -1) {
    closeIndex = htmlStart.length;
  }

  return src.substring(0, matchIndex + closeIndex);
}

// Disable indented code blocks, keeping fenced code blocks intact
marked.use({
  tokenizer: {
    code(src) {
      if (src.trim().startsWith("```") || src.trim().startsWith("~~~")) {
        return false;
      }
      const matchedHtml = getHtmlBlockPrefix(src);
      if (matchedHtml) {
        return { type: "html", raw: matchedHtml, text: matchedHtml };
      }
      return false;
    },
  },
});

// --- Safe Script Deferral Lexical Scanner ---
function deferScriptsInHtml(html) {
  if (!html) return "";
  let result = "";
  let i = 0;
  while (i < html.length) {
    const scriptOpenMatch = html.slice(i).match(/^<script\b[^>]*>/i);
    if (scriptOpenMatch) {
      const openTag = scriptOpenMatch[0];
      i += openTag.length;

      let inString = null;
      let inComment = null;
      let scriptContent = "";
      let closed = false;

      while (i < html.length) {
        if (
          !inString &&
          !inComment &&
          html.slice(i).toLowerCase().startsWith("</script>")
        ) {
          i += 9;
          try {
            const encoded = btoa(unescape(encodeURIComponent(scriptContent)));
            const attributesStr = openTag
              .slice(openTag.indexOf("script") + 6, -1)
              .trim();
            const encodedAttrs = btoa(
              unescape(encodeURIComponent(attributesStr || "")),
            );
            result += `<div class="deferred-script" style="display:none;" data-script="${encoded}" data-attributes="${encodedAttrs}"></div>`;
          } catch (e) {
            console.error("Failed to encode script:", e);
          }
          closed = true;
          break;
        }

        const char = html[i];
        const nextChar = html[i + 1];

        if (!inString) {
          if (inComment === "single" && char === "\n") {
            inComment = null;
          } else if (
            inComment === "multi" &&
            char === "*" &&
            nextChar === "/"
          ) {
            inComment = null;
            scriptContent += "*/";
            i += 2;
            continue;
          } else if (!inComment) {
            if (char === "/" && nextChar === "/") {
              inComment = "single";
              scriptContent += "//";
              i += 2;
              continue;
            } else if (char === "/" && nextChar === "*") {
              inComment = "multi";
              scriptContent += "/*";
              i += 2;
              continue;
            }
          }
        }

        if (!inComment) {
          if (inString) {
            if (char === "\\") {
              scriptContent += html.slice(i, i + 2);
              i += 2;
              continue;
            } else if (char === inString) {
              inString = null;
            }
          } else {
            if (char === "'" || char === '"' || char === "`") {
              inString = char;
            }
          }
        }

        scriptContent += char;
        i++;
      }
      if (!closed) {
        result += openTag + scriptContent;
      }
    } else {
      result += html[i];
      i++;
    }
  }
  return result;
}

// --- Protect Math and SVG Blocks from Marked.js Escaping ---
const originalMarkedParse = marked.parse;
marked.parse = function (text, options) {
  if (!text) return "";
  const mathBlocks = [];
  const svgBlocks = [];

  // 0. Unwrap Generative UI/HTML placed in code blocks by the LLM, unless it contains the raw-code comment
  let processedText = text.replace(
    /```(?:html|xml|css|svg)?\s*?\n([\s\S]*?)\n\s*```/gi,
    (match, codeContent) => {
      if (codeContent.includes("<!--raw-code-->")) {
        return match;
      }
      const hasLang = match.match(/^```(html|xml|css|svg)\b/i);
      const trimmed = codeContent.trim();
      const looksLikeHtml =
        trimmed.startsWith("<") &&
        (trimmed.includes("<div") ||
          trimmed.includes("<svg") ||
          trimmed.includes("<style") ||
          trimmed.includes("<span") ||
          trimmed.includes("<iframe"));
      if (hasLang || looksLikeHtml) {
        return "\n" + codeContent.trim() + "\n\n";
      }
      return match;
    },
  );

  // 1. Protect SVGs first
  processedText = processedText.replace(/(<svg[\s\S]*?<\/svg>)/gi, (match) => {
    svgBlocks.push(match);
    return `SVGBLOCKPLACEHOLDER${svgBlocks.length - 1}SVGBLOCK`;
  });

  // 2. Protect Math - Improved to avoid currency conflicts ($2,000) and JS template literals (${var})
  processedText = processedText.replace(
    /(\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$(?![\s{])[^$\n]{1,500}?(?<!\s)\$(?!\d))/g,
    (match) => {
      let transformed = match;
      if (match.startsWith("$") && !match.startsWith("$$")) {
        transformed = "\\(" + match.slice(1, -1) + "\\)";
      }
      mathBlocks.push(transformed);
      return `MATHBLOCKPLACEHOLDER${mathBlocks.length - 1}MATHBLOCK`;
    },
  );

  // Parse the markdown safely
  let html = originalMarkedParse.call(marked, processedText, options);

  // 3. Restore Math
  mathBlocks.forEach((block, index) => {
    const placeholderRegex = new RegExp(
      `MATHBLOCKPLACEHOLDER${index}MATHBLOCK`,
      "g",
    );
    html = html.replace(placeholderRegex, () => block);
  });

  // 4. Restore SVGs intact or escaped if inside code block
  svgBlocks.forEach((block, index) => {
    const placeholderRegex = new RegExp(
      `SVGBLOCKPLACEHOLDER${index}SVGBLOCK`,
      "g",
    );
    html = html.replace(placeholderRegex, (match, offset, str) => {
      const before = str.substring(0, offset);
      const codeStart = before.lastIndexOf("<code");
      const codeEnd = before.lastIndexOf("</code>");

      // If the last thing opened before us was a <code tag, we are inside a code block
      if (codeStart > codeEnd) {
        return block
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#39;");
      } else {
        return block;
      }
    });
  });

  // 5. Proxy raw insecure HTTP images that escaped the renderer
  html = html.replace(
    /<img[^>]+src=["'](http:\/\/[^"']+)["']/gi,
    (match, url) => {
      return match.replace(url, `/image-proxy?url=${encodeURIComponent(url)}`);
    },
  );

  // 6. Defer any script tags to prevent HTML parser leaks and premature termination
  html = deferScriptsInHtml(html);

  return html;
};

// --- Live Image Auto-Refresh Protocol ---
setInterval(() => {
  document.querySelectorAll(".message-content img").forEach((img) => {
    try {
      // Only refresh if visibly in viewport to save bandwidth
      const rect = img.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight) return;

      let src = img.getAttribute("src");
      if (!src) return;

      let refreshRate = 0;
      // Detect `#refresh=X` or `&refresh=X` in both raw and URL-encoded strings
      const match = src.match(/(?:%23|#|%26|&|\?)refresh(?:%3D|=)(\d+)/i);
      if (match) {
        refreshRate = parseInt(match[1], 10);
      } else if (
        src.includes("COUNTER") ||
        src.includes("cgi-bin/viewer/video.jpg") ||
        src.includes("cgi-bin/camera")
      ) {
        refreshRate = 5; // Default 5s fallback for known IP camera patterns
      }

      if (refreshRate > 0) {
        const now = Date.now();
        const lastRefresh = parseInt(img.dataset.lastRefresh || "0", 10);

        if (img.dataset.failed === "true") return; // Stop refreshing if it failed

        if (now - lastRefresh >= refreshRate * 1000) {
          img.dataset.lastRefresh = now;

          let urlObj = new URL(img.src, window.location.origin);
          urlObj.searchParams.set("_t", now); // Cache buster

          // Flicker-free swap: load frame in background first
          const tempImg = new Image();
          tempImg.onload = () => {
            img.src = urlObj.toString();
          };
          tempImg.onerror = () => {
            img.dataset.failed = "true"; // Mark as failed to prevent further requests
          };
          tempImg.src = urlObj.toString();
        }
      }
    } catch (e) {}
  });
}, 1000);

const MAX_TOKEN_LIMIT = 250000;
const TOKEN_COUNT_INTERVAL_MS = 600000;

function toggleSendStopButtons(showStop) {
  if (showStop) {
    const hasText = chatInput && chatInput.value.trim().length > 0;
    if (hasText) {
      sendBtn.style.display = "inline-flex";
      sendBtn.disabled = false;
      stopBtn.style.display = "none";
    } else {
      sendBtn.style.display = "none";
      sendBtn.disabled = true;
      stopBtn.style.display = "inline-flex";
    }
  } else {
    sendBtn.style.display = "inline-flex";
    sendBtn.disabled = false;
    stopBtn.style.display = "none";
  }
}
const katexDelimiters = [
  { left: "$$", right: "$$", display: true },
  { left: "\\(", right: "\\)", display: false },
  { left: "\\[", right: "\\]", display: true },
];

/* Custom model themes removed to follow a persistent, unified dark theme */

if (typeof hljs !== "undefined") {
  hljs.configure({ ignoreUnescapedHTML: true });
}
const turndownService = new TurndownService({
  headingStyle: "atx",
  hr: "---",
  bulletListMarker: "*",
  codeBlockStyle: "fenced",
  emDelimiter: "_",
  strongDelimiter: "**",
  linkStyle: "inlined",
});
turndownService.keep(["table", "thead", "tbody", "tr", "th", "td"]);

function scrollToBottom() {
  // Check both containers as scrolling might be at window level or element level
  const scrollTargets = [
    { el: messagesDiv, isWindow: false },
    { el: window, isWindow: true },
  ];

  scrollTargets.forEach((target) => {
    if (!target.el && !target.isWindow) return;

    const element = target.isWindow ? document.documentElement : target.el;
    const scrollHeight = element.scrollHeight;

    // Use setTimeout to ensure all DOM updates (like images/SVGs) have finished layout
    setTimeout(() => {
      target.el.scrollTo({
        top: scrollHeight,
        behavior: "smooth",
      });

      // Fallback for immediate jump
      setTimeout(() => {
        if (target.el.scrollTop + element.clientHeight < scrollHeight - 100) {
          if (target.isWindow) window.scrollTo(0, scrollHeight);
          else target.el.scrollTop = scrollHeight;
        }
      }, 500);
    }, 50);
  });
}
function escapeHtml(unsafe) {
  if (typeof unsafe !== "string") return "";
  return unsafe
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
function wrapTables(htmlContent) {
  if (typeof htmlContent !== "string" || !htmlContent.includes("<table"))
    return htmlContent;
  try {
    const tempDiv = document.createElement("div");
    tempDiv.innerHTML = htmlContent;
    tempDiv.querySelectorAll("table").forEach((table) => {
      if (
        !table.parentElement ||
        !table.parentElement.classList.contains("table-wrapper")
      ) {
        const wrapper = document.createElement("div");
        wrapper.className = "table-wrapper";
        table.parentNode.insertBefore(wrapper, table);
        wrapper.appendChild(table);
      }
    });
    return tempDiv.innerHTML;
  } catch (e) {
    return htmlContent;
  }
}

function processCodeBlocks(containerElement) {
  if (typeof hljs === "undefined" || !containerElement) return;
  // Protect user messages from unwrap/iframe/rendering mechanisms
  if (
    containerElement.classList.contains("user-msg") ||
    containerElement.closest(".user-msg")
  )
    return;

  containerElement.querySelectorAll("pre").forEach((pre) => {
    if (pre.classList.contains("user-msg") || pre.closest(".user-msg")) return;
    if (pre.parentElement.classList.contains("code-content-original")) {
      return;
    }

    const codeEl = pre.querySelector("code");
    if (!codeEl) return;

    const rawCode = codeEl.textContent;

    const langMatch = codeEl.className.match(/language-(\w+)/i);
    const lang = langMatch ? langMatch[1].toLowerCase() : null;
    const isWebVisual = ["html", "xml", "css", "svg"].includes(lang);

    const trimmedCode = rawCode.trim();
    const isRawCode = rawCode.includes("<!--raw-code-->");

    // Only iframe when explicitly opted-in via <!--raw-code--> comment.
    // Auto-detecting looksLikeHtml causes gen-ui HTML blocks to be incorrectly sandboxed.
    if (isRawCode) {
      const iframe = document.createElement("iframe");
      iframe.className = "code-preview-iframe";
      iframe.setAttribute(
        "sandbox",
        "allow-scripts allow-same-origin allow-forms allow-popups",
      );
      iframe.setAttribute(
        "allow",
        "autoplay; clipboard-write; encrypted-media; picture-in-picture",
      );
      iframe.style.width = "100%";
      iframe.style.height = "700px";
      iframe.style.border = "none";
      iframe.srcdoc = rawCode;

      pre.parentNode.insertBefore(iframe, pre);
      pre.remove();
      return;
    }
    codeEl.removeAttribute("data-highlighted");
    codeEl.className = codeEl.className.replace(/hljs.*/g, "").trim();
    try {
      hljs.highlightElement(codeEl);
    } catch (err) {
      console.error("Highlighting error:", err);
    }

    const isPreviewable = lang === "html";
    const isRunnable = [
      "python",
      "c",
      "cpp",
      "javascript",
      "typescript",
      "java",
      "go",
      "rust",
      "php",
      "ruby",
    ].includes(lang);

    const wrapper = document.createElement("div");
    wrapper.className = "code-content-original";
    pre.parentNode.insertBefore(wrapper, pre);
    wrapper.appendChild(pre);

    const controls = document.createElement("div");
    controls.className = "code-controls";

    if (isRunnable && lang !== "html") {
      const runBtn = document.createElement("button");
      runBtn.className = "run-code-btn";
      runBtn.innerHTML =
        '<svg style="width:12px; height:12px;"><use xlink:href="#icon-play"></use></svg> Run';
      runBtn.setAttribute("aria-label", "Run code");
      controls.appendChild(runBtn);
    }

    const copyBtn = document.createElement("button");
    copyBtn.className = "copy-code-btn";
    copyBtn.innerHTML = '<svg><use xlink:href="#icon-copy"></use></svg> Copy';
    copyBtn.setAttribute("aria-label", "Copy code");
    copyBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      copyCode(copyBtn, codeEl);
    });
    controls.appendChild(copyBtn);

    const downloadBtn = document.createElement("button");
    downloadBtn.className = "download-code-btn";
    downloadBtn.innerHTML =
      '<svg style="width:12px; height:12px;"><use xlink:href="#icon-download"></use></svg> Download';
    downloadBtn.setAttribute("aria-label", "Download code file");
    controls.appendChild(downloadBtn);

    if (isPreviewable) {
      const previewContainer = document.createElement("div");
      previewContainer.className = "code-preview-container";
      const iframe = document.createElement("iframe");
      iframe.className = "code-preview-iframe";
      iframe.setAttribute(
        "sandbox",
        "allow-scripts allow-same-origin allow-forms allow-popups",
      );
      iframe.setAttribute(
        "allow",
        "autoplay; clipboard-write; encrypted-media; picture-in-picture",
      );
      try {
        const noScrollbarStyles =
          "<style>::-webkit-scrollbar { width: 0px; background: transparent; } html { scrollbar-width: none; }</style>";
        const modifiedCode = rawCode.includes("<head>")
          ? rawCode.replace("</head>", `${noScrollbarStyles}</head>`)
          : `${noScrollbarStyles}${rawCode}`;
        iframe.srcdoc = modifiedCode;
      } catch (e) {
        iframe.srcdoc = `<html><body><p style='color:red;'>Error: ${escapeHtml(e.message)}</p></body></html>`;
      }
      previewContainer.appendChild(iframe);
      wrapper.appendChild(previewContainer);
      const toggleBtn = document.createElement("button");
      toggleBtn.className = "toggle-view-btn";
      toggleBtn.textContent = "View Code";
      toggleBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (pre.style.display === "none") {
          pre.style.display = "block";
          previewContainer.style.display = "none";
          toggleBtn.textContent = "View Preview";
        } else {
          pre.style.display = "none";
          previewContainer.style.display = "block";
          toggleBtn.textContent = "View Code";
        }
      });
      controls.appendChild(toggleBtn);

      const fullscreenBtn = document.createElement("button");
      fullscreenBtn.className = "fullscreen-btn";
      fullscreenBtn.innerHTML = `<svg style="width:12px; height:12px;" viewBox="0 0 24 24" fill="currentColor"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>`;
      fullscreenBtn.setAttribute("aria-label", "Toggle fullscreen preview");
      fullscreenBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (previewContainer.requestFullscreen) {
          previewContainer.requestFullscreen();
        } else if (previewContainer.webkitRequestFullscreen) {
          previewContainer.webkitRequestFullscreen();
        } else if (previewContainer.msRequestFullscreen) {
          previewContainer.msRequestFullscreen();
        }
      });
      controls.appendChild(fullscreenBtn);
      wrapper.insertBefore(controls, pre);
      pre.style.display = "none";
      previewContainer.style.display = "block";
    } else {
      wrapper.insertBefore(controls, pre);
    }
  });
}

function processGenerativeUI(containerElement) {
  if (!containerElement) return;
  // Protect user messages from unwrap/iframe/rendering mechanisms
  if (
    containerElement.classList.contains("user-msg") ||
    containerElement.closest(".user-msg")
  )
    return;

  // 0. Execute any inline or deferred scripts to ensure generative UI interactivity works.
  const rawScripts = containerElement.querySelectorAll(
    "script, div.deferred-script",
  );
  const externalScriptPromises = [];
  const inlineScripts = [];

  rawScripts.forEach((oldScript) => {
    if (
      oldScript.classList.contains("user-msg") ||
      oldScript.closest(".user-msg")
    )
      return;
    let scriptContent = "";
    let attributesStr = "";
    let isExternal = false;
    let extSrc = "";
    let extType = "";
    let extCrossOrigin = "";

    if (
      oldScript.tagName.toLowerCase() === "div" &&
      oldScript.classList.contains("deferred-script")
    ) {
      try {
        scriptContent = decodeURIComponent(
          escape(atob(oldScript.getAttribute("data-script") || "")),
        );
        attributesStr = decodeURIComponent(
          escape(atob(oldScript.getAttribute("data-attributes") || "")),
        );
        if (attributesStr) {
          const srcMatch = attributesStr.match(/src=["']([^"']+)["']/i);
          if (srcMatch) {
            isExternal = true;
            extSrc = srcMatch[1];
          }
          const typeMatch = attributesStr.match(/type=["']([^"']+)["']/i);
          if (typeMatch) extType = typeMatch[1];
          const crossMatch = attributesStr.match(
            /crossorigin=["']([^"']+)["']/i,
          );
          if (crossMatch) extCrossOrigin = crossMatch[1];
        }
      } catch (e) {
        console.error("Failed to decode deferred script:", e);
      }
    } else {
      isExternal = !!oldScript.src;
      extSrc = oldScript.src || "";
      extType = oldScript.type || "";
      extCrossOrigin = oldScript.crossOrigin || "";
      scriptContent = oldScript.innerHTML || "";
    }

    if (isExternal) {
      const p = new Promise((resolve, reject) => {
        const newScript = document.createElement("script");
        newScript.src = extSrc;
        if (extType) newScript.type = extType;
        if (extCrossOrigin) newScript.crossOrigin = extCrossOrigin;
        newScript.onload = resolve;
        newScript.onerror = resolve; // Resolve anyway to continue
        document.body.appendChild(newScript);
      });
      externalScriptPromises.push(p);
      oldScript.remove();
    } else {
      if (scriptContent.trim() !== "") {
        // Assign a unique ID to the container if it doesn't have one
        if (!containerElement.id) {
          containerElement.id =
            "gen-ui-" +
            Date.now() +
            "-" +
            Math.random().toString(36).substr(2, 5);
        }

        // Wrap the script in an IIFE and shadow 'document' with a Proxy
        // that restricts DOM queries to this specific message's container!
        const wrappedScript = `
                    (function() {
                        var _container = window.document.getElementById('${containerElement.id}');
                        var document = new Proxy(window.document, {
                            get: function(target, prop) {
                                if (prop === 'getElementById') {
                                    return function(id) {
                                        var el = _container ? _container.querySelector('[id="' + id.replace(/"/g, '\\\\"') + '"]') : null;
                                        return el || target.getElementById(id);
                                    };
                                }
                                if (prop === 'querySelector') {
                                    return function(selector) {
                                        if (selector === 'body' || selector === 'html' || selector === 'head') return target.querySelector(selector);
                                        var el = _container ? _container.querySelector(selector) : null;
                                        return el || target.querySelector(selector);
                                    };
                                }
                                if (prop === 'querySelectorAll') {
                                    return function(selector) {
                                        if (selector === 'body' || selector === 'html' || selector === 'head') return target.querySelectorAll(selector);
                                        var els = _container ? _container.querySelectorAll(selector) : [];
                                        return els.length > 0 ? els : target.querySelectorAll(selector);
                                    };
                                }
                                var value = target[prop];
                                return typeof value === 'function' ? value.bind(target) : value;
                            }
                        });
                        ${scriptContent}
                        
                        // Function to bind inline on* events to IIFE scope
                        function bindEvents(root) {
                            root.querySelectorAll("*").forEach(function(el) {
                                var attrs = Array.from(el.attributes);
                                attrs.forEach(function(attr) {
                                    if (attr.name.indexOf("on") === 0) {
                                        var eventName = attr.name.slice(2);
                                        var handlerStr = attr.value;
                                        el.removeAttribute(attr.name);
                                        el[attr.name] = null; // Unbind native compiled handler
                                        el.addEventListener(eventName, function(event) {
                                            try {
                                                eval(handlerStr);
                                            } catch(e) {
                                                console.error("Error executing event handler:", e);
                                                if (window.stellar && window.stellar.triggerAutofix) {
                                                    window.stellar.triggerAutofix(_container, event.currentTarget || event.target, e);
                                                }
                                            }
                                        });
                                    }
                                });
                            });
                        }
                        
                        if (_container) {
                            // Bind initially existing elements
                            bindEvents(_container);
                            
                            // Setup MutationObserver to bind events on newly injected HTML
                            var observer = new MutationObserver(function(mutations) {
                                mutations.forEach(function(mutation) {
                                    mutation.addedNodes.forEach(function(node) {
                                        if (node.nodeType === 1) { // ELEMENT_NODE
                                            bindEvents(node);
                                        }
                                    });
                                });
                            });
                            observer.observe(_container, { childList: true, subtree: true });
                        }
                    })();
                    `;
        inlineScripts.push(wrappedScript);
      }
      oldScript.remove();
    }
  });

  Promise.all(externalScriptPromises).then(() => {
    inlineScripts.forEach((scriptContent) => {
      const newScript = document.createElement("script");
      newScript.textContent = scriptContent;
      if (window.stellar)
        window.stellar.currentProcessingContainer = containerElement;
      try {
        document.body.appendChild(newScript);
      } finally {
        if (window.stellar) window.stellar.currentProcessingContainer = null;
      }
    });
  });

  // 1. Lazy load images
  const suiImages = containerElement.querySelectorAll(
    '.sui-img, [class*="sui-"] img',
  );
  suiImages.forEach((img) => {
    img.setAttribute("loading", "lazy");
  });

  // 2. Tabs logic
  const tabContainers = containerElement.querySelectorAll(".sui-tabs");
  tabContainers.forEach((container) => {
    const navBtns = container.querySelectorAll(".sui-tab-btn");
    const panels = container.querySelectorAll(".sui-tab-panel");
    navBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = btn.getAttribute("data-tab");
        if (!target) return;

        navBtns.forEach((b) => b.classList.remove("active"));
        panels.forEach((p) => p.classList.remove("active"));

        btn.classList.add("active");
        let targetPanel;
        try {
          targetPanel = container.querySelector(target);
        } catch (e) {}
        if (!targetPanel)
          targetPanel = container.querySelector(
            `.sui-tab-panel[data-tab="${target}"]`,
          );
        if (!targetPanel)
          targetPanel = container.querySelector(`.sui-tab-panel#${target}`);
        if (targetPanel) targetPanel.classList.add("active");
      });
    });
  });

  // 3. Before/After slider
  const baContainers = containerElement.querySelectorAll(".sui-before-after");
  baContainers.forEach((container) => {
    const slider = container.querySelector(".sui-ba-slider");
    const afterImg = container.querySelector(".sui-after-img");
    const line = container.querySelector(".sui-ba-line");
    if (slider && afterImg && line) {
      slider.addEventListener("input", (e) => {
        const val = e.target.value;
        afterImg.style.clipPath = `polygon(0 0, ${val}% 0, ${val}% 100%, 0 100%)`;
        line.style.left = `${val}%`;
      });
    }
  });

  // 4. Lightbox for gallery
  const galleryImages = containerElement.querySelectorAll(
    ".sui-gallery img, .sui-img",
  );
  if (galleryImages.length > 0) {
    let lightbox = document.getElementById("sui-lightbox");
    if (!lightbox) {
      lightbox = document.createElement("div");
      lightbox.id = "sui-lightbox";
      lightbox.innerHTML = `<span id="sui-lightbox-close">&times;</span><img src="" alt="">`;
      document.body.appendChild(lightbox);

      const closeBtn = document.getElementById("sui-lightbox-close");
      const closeLb = () => lightbox.classList.remove("active");

      closeBtn.addEventListener("click", closeLb);
      lightbox.addEventListener("click", (e) => {
        if (e.target === lightbox) closeLb();
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeLb();
      });
    }
    const lbImg = lightbox.querySelector("img");

    galleryImages.forEach((img) => {
      img.addEventListener("click", () => {
        lbImg.src = img.src;
        lightbox.classList.add("active");
      });
    });
  }
}

function createOutputPanel(wrapper, runButton) {
  const existingPanel = wrapper.querySelector(".code-output-container");
  if (existingPanel) {
    existingPanel.remove();
  }

  const outputContainer = document.createElement("div");
  outputContainer.className = "code-output-container";

  outputContainer.dataset.containerId = "";

  outputContainer.innerHTML = `
          <div class="code-output-panel">
              <div class="code-output-header">
                  <span class="status-text">Output</span>
                  <button class="close-output-btn" title="Close Output">×</button>
              </div>
              <div class="code-output-content">
                  <span class="output-line"></span>
              </div>
          </div>
      `;

  wrapper.appendChild(outputContainer);

  outputContainer
    .querySelector(".close-output-btn")
    .addEventListener("click", async () => {
      const containerId = outputContainer.dataset.containerId;

      if (containerId) {
        try {
          await fetch("/api/stop_container", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ container_id: containerId }),
          });
          console.log(`Stop request sent for container: ${containerId}`);
        } catch (error) {
          console.error("Failed to send stop request:", error);
        }
      }

      outputContainer.remove();
      runButton.classList.remove("running");
      runButton.disabled = false;
      runButton.innerHTML =
        '<svg style="width:14px; height:14px;"><use xlink:href="#icon-play"></use></svg> Run';
    });

  return outputContainer.querySelector(".code-output-content");
}
messagesDiv.addEventListener("click", async function (event) {
  const downloadButton = event.target.closest(".download-code-btn");
  if (downloadButton) {
    event.stopPropagation();
    const codeContentWrapper = downloadButton.closest(".code-content-original");
    if (!codeContentWrapper) return;
    const codeElement = codeContentWrapper.querySelector("code");
    if (!codeElement) return;
    const codeToDownload = codeElement.textContent;
    const langClass = codeElement.className.match(/language-(\w+)/);
    let extension = langClass ? langClass[1] : "txt";
    const extensionMap = {
      python: "py",
      javascript: "js",
      java: "java",
      c: "c",
      cpp: "cpp",
      csharp: "cs",
      php: "php",
      typescript: "ts",
      kotlin: "kt",
      go: "go",
      rust: "rs",
      html: "html",
      css: "css",
      json: "json",
      xml: "xml",
      yaml: "yaml",
      markdown: "md",
    };
    extension = extensionMap[extension] || "txt";
    const fileName = `stellar_code.${extension}`;
    const blob = new Blob([codeToDownload], {
      type: "text/plain;charset=utf-8",
    });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = fileName;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(link.href);
    return;
  }

  const clickable = event.target.closest(
    ".message-content .analysis-placeholder, .message-content .analysis-indicator.clickable",
  );
  if (clickable) {
    const msgEl = clickable.closest(".message");
    if (msgEl) {
      toggleAnalysisDetails(msgEl);
    }
    return;
  }

  const runButton = event.target.closest(".run-code-btn");
  if (!runButton) return;

  if (runButton.classList.contains("stopping")) {
    return;
  }

  const wrapper = runButton.closest(".code-content-original");
  const codeEl = wrapper?.querySelector("code");
  if (!wrapper || !codeEl) return;

  event.stopPropagation();

  if (runButton.classList.contains("running")) {
    runButton.classList.remove("running");
    runButton.classList.add("stopping");
    runButton.disabled = true;
    runButton.innerHTML = "Stopping...";

    if (runButton._abortController) {
      runButton._abortController.abort();
      runButton._abortController = null;
    }

    const outputPanel = wrapper.querySelector(".code-output-container");
    const containerId = outputPanel ? outputPanel.dataset.containerId : null;
    if (containerId) {
      fetch("/api/stop_container", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ container_id: containerId }),
      }).catch((err) => {
        console.error("Failed to send stop request:", err);
      });
    }
    return;
  }

  const langMatch = codeEl.className.match(/language-(\w+)/);
  const lang = langMatch ? langMatch[1] : "unknown";
  const code = codeEl.textContent;

  let processId = null;

  const controller = new AbortController();
  runButton._abortController = controller;

  runButton.classList.add("running");
  runButton.disabled = false;
  runButton.innerHTML =
    '<svg style="width:12px; height:12px; fill: currentColor; margin-right: 4px; vertical-align: middle;"><use xlink:href="#icon-stop"></use></svg>Stop';

  const outputContentDiv = createOutputPanel(wrapper, runButton);
  outputContentDiv.innerHTML =
    '<span class="output-line" style="color: var(--secondary-text-color);">Connecting...</span>';

  const backendRunnableLanguages = [
    "python",
    "javascript",
    "php",
    "ruby",
    "go",
    "c",
    "cpp",
    "java",
    "rust",
    "typescript",
  ];

  if (backendRunnableLanguages.includes(lang)) {
    executeCode(code, lang, outputContentDiv, runButton, processId, controller);
  } else {
    outputContentDiv.innerHTML = `<span class="output-line error">Execution is not supported for '${lang}' language.</span>`;
    runButton.classList.remove("running");
    runButton.disabled = false;
    runButton.innerHTML =
      '<svg style="width:14px; height:14px;"><use xlink:href="#icon-play"></use></svg> Run';
    runButton._abortController = null;
  }
});

async function executeCode(
  backendCode,
  lang,
  outputContentDiv,
  runButton,
  processId = null,
  controller = null,
) {
  outputContentDiv.innerHTML =
    '<span class="output-line initial-status" style="color: var(--secondary-text-color);">Preparing sandbox environment...</span>';
  let firstOutputReceived = false;
  const signal = controller ? controller.signal : null;

  try {
    const payload = {
      code: backendCode,
      language: lang,
      processId: processId,
    };

    const response = await fetch("/api/run_code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: signal,
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({
        error: `Execution failed with status: ${response.status}`,
      }));
      throw new Error(errorData.error);
    }

    if (!response.body) {
      throw new Error("Streaming response not available.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const outputPanel = outputContentDiv.closest(".code-output-container");

    while (true) {
      if (signal && signal.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }

      const { done, value } = await reader.read();
      if (done) break;

      const chunk = decoder.decode(value, { stream: true });
      const sseLines = chunk.split("\n\n");

      sseLines.forEach((line) => {
        if (!line.startsWith("data: ")) return;

        if (!firstOutputReceived) {
          outputContentDiv.innerHTML = "";
          firstOutputReceived = true;
        }

        try {
          const data = JSON.parse(line.substring(6));
          if (data.type === "container_id" && data.id && outputPanel) {
            outputPanel.dataset.containerId = data.id;
          } else if (data.type === "port_info") {
            const linkEl = document.createElement("a");
            linkEl.href = data.url;
            linkEl.target = "_blank";
            linkEl.rel = "noopener noreferrer";
            linkEl.textContent = `Server is live: ${data.url}`;
            linkEl.style.cssText =
              "color: #00c292; font-weight: bold; text-decoration: underline;";
            const p = document.createElement("p");
            p.style.marginBottom = "10px";
            p.appendChild(linkEl);
            outputContentDiv.appendChild(p);
          } else if (data.type === "log") {
            const lineEl = document.createElement("span");
            lineEl.className = "output-line";
            lineEl.textContent = data.content;
            outputContentDiv.appendChild(lineEl);
          } else if (data.type === "error") {
            const errorLine = document.createElement("span");
            errorLine.className = "output-line error";
            errorLine.textContent = `Error: ${data.content}`;
            outputContentDiv.appendChild(errorLine);
          }
        } catch (e) {}
      });
      outputContentDiv.scrollTop = outputContentDiv.scrollHeight;
    }
  } catch (error) {
    if (error.name === "AbortError") {
      const abortLine = document.createElement("span");
      abortLine.className = "output-line error";
      abortLine.style.color = "var(--secondary-text-color)";
      abortLine.textContent = "\n[Execution terminated by user]";
      outputContentDiv.appendChild(abortLine);
    } else {
      if (!firstOutputReceived) {
        outputContentDiv.innerHTML = "";
      }
      const errorLine = document.createElement("span");
      errorLine.className = "output-line error";
      errorLine.textContent = `Error: ${error.message}`;
      outputContentDiv.appendChild(errorLine);
    }
  } finally {
    runButton.classList.remove("running");
    runButton.classList.remove("stopping");
    runButton.disabled = false;
    runButton.innerHTML =
      '<svg style="width:14px; height:14px;"><use xlink:href="#icon-play"></use></svg> Run';
    runButton._abortController = null;
    if (!firstOutputReceived && (!signal || !signal.aborted)) {
      outputContentDiv.innerHTML =
        '<span class="output-line" style="color: var(--secondary-text-color);">[Execution finished with no output]</span>';
    }
  }
}

function renderMath(element) {
  if (!element || typeof renderMathInElement !== "function") {
    return;
  }
  // Protect user messages from unwrap/iframe/rendering mechanisms
  if (element.classList.contains("user-msg") || element.closest(".user-msg"))
    return;
  try {
    renderMathInElement(element, {
      delimiters: katexDelimiters,
      throwOnError: false,
    });
  } catch (katexError) {}
}

function addOutputCopyButton(messageElement) {
  if (
    !messageElement ||
    messageElement.classList.contains("user-msg") ||
    messageElement.classList.contains("placeholder-message")
  ) {
    return;
  }
  const existingContainer = messageElement.querySelector(
    ".output-copy-btn-container",
  );
  if (existingContainer) {
    existingContainer.remove();
  }

  const contentDiv = messageElement.querySelector(".message-content");
  if (!contentDiv) return;

  const container = document.createElement("div");
  container.className = "output-copy-btn-container";
  const button = document.createElement("button");
  button.className = "output-copy-btn";
  button.setAttribute("aria-label", "Copy content");
  button.innerHTML = `<svg><use xlink:href="#icon-copy"></use></svg>`;
  const tooltip = document.createElement("span");
  tooltip.className = "copy-tooltip";
  tooltip.textContent = "Copy";

  button.addEventListener("click", (e) => {
    e.stopPropagation();
    let textToCopy = messageElement.rawMarkdownData;
    if (!textToCopy) {
      if (typeof turndownService !== "undefined") {
        // Clone the node to clean up any UI-only elements before converting
        const clone = contentDiv.cloneNode(true);

        // Remove UI elements that shouldn't be in the markdown
        clone
          .querySelectorAll(
            ".yt-player-container, .app-iframe-container, .analysis-indicator, .analysis-content, .code-controls",
          )
          .forEach((el) => el.remove());

        textToCopy = turndownService.turndown(clone.innerHTML);
      } else {
        textToCopy = contentDiv.textContent || contentDiv.innerText || "";
      }
    }

    if (!textToCopy) {
      return;
    }

    navigator.clipboard
      .writeText(textToCopy)
      .then(() => {
        button.innerHTML = `<svg><use xlink:href="#icon-check"></use></svg>`;
        tooltip.textContent = "Copied!";
        tooltip.classList.add("visible");
        button.classList.add("copied");
        button.disabled = true;
        setTimeout(() => {
          tooltip.classList.remove("visible");
          setTimeout(
            () => {
              tooltip.textContent = "Copy";
              button.innerHTML = `<svg><use xlink:href="#icon-copy"></use></svg>`;
              button.classList.remove("copied");
              button.disabled = false;
            },
            parseFloat(getComputedStyle(tooltip).transitionDuration) * 1000 ||
              150,
          );
        }, 1500);
      })
      .catch((err) => {
        const originalContent = button.innerHTML;
        button.textContent = "Error";
        setTimeout(() => {
          button.innerHTML = originalContent;
          button.disabled = false;
        }, 1500);
      });
  });

  container.appendChild(button);
  container.appendChild(tooltip);

  const delButton = document.createElement("button");
  delButton.className = "output-copy-btn";
  delButton.title = "Delete Message";
  delButton.innerHTML = `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M19,4H15.5L14.5,3H9.5L8.5,4H5V6H19V4M6,19A2,2 0 0,0 8,21H16A2,2 0 0,0 18,19V7H6V19Z"/></svg>`;
  delButton.addEventListener("click", (e) => {
    e.stopPropagation();
    const msgId = messageElement.dataset.id;
    if (msgId) {
      deleteMessageFromServer(msgId, messageElement);
    }
  });
  container.appendChild(delButton);

  messageElement.appendChild(container);
}

async function deleteMessageFromServer(messageId, messageElement) {
  try {
    // Transient messages (e.g. '123_user', 'error_...') won't exist in the database.
    // Just remove them from the DOM without throwing a 403 error.
    if (typeof messageId === "string" && !/^\d+$/.test(messageId)) {
      messageElement.remove();
      return;
    }

    const response = await fetch("/api/messages/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ message_id: messageId }),
    });

    if (response.ok) {
      messageElement.remove();
    } else {
      const err = await response.json();
      alert("Failed to delete message: " + (err.error || "Unknown error"));
    }
  } catch (e) {
    console.error("Delete error:", e);
    alert("An error occurred while deleting the message.");
  }
}

function appendUserMessage(
  text,
  id,
  attachedFiles = [],
  isLocalFiles = false,
  timestamp = null,
) {
  const lastEditIcon = document.querySelector(
    ".user-msg:last-of-type .edit-prompt-wrapper",
  );
  if (lastEditIcon) {
    lastEditIcon.remove();
  }

  const msg = document.createElement("div");
  msg.classList.add("message", "user-msg");
  msg.dataset.id = id;

  if (attachedFiles && attachedFiles.length > 0) {
    const filesContainer = document.createElement("div");
    filesContainer.style.cssText =
      "display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; justify-content: flex-end;";

    attachedFiles.forEach((file) => {
      const item = document.createElement("div");
      item.style.cssText =
        "display: flex; flex-direction: column; align-items: center; width: 80px; padding: 5px; background: rgba(0,0,0,0.2); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px;";

      const preview = document.createElement("div");
      preview.style.cssText =
        "width: 60px; height: 60px; border-radius: 4px; overflow: hidden; display: flex; align-items: center; justify-content: center; background: rgba(255,255,255,0.1); margin-bottom: 4px; font-size: 0.75rem; color: #aaa; word-break: break-all; text-align: center;";

      const fileType = isLocalFiles ? file.type || "" : file.mime_type || "";
      const fileName = isLocalFiles
        ? file.name
        : file.display_name || file.name || "FILE";
      const isImage = fileType.startsWith("image/");

      if (isImage && isLocalFiles) {
        const img = document.createElement("img");
        img.src = URL.createObjectURL(file);
        img.style.cssText = "width: 100%; height: 100%; object-fit: cover;";
        preview.appendChild(img);
      } else {
        preview.textContent = fileName
          .split(".")
          .pop()
          .toUpperCase()
          .substring(0, 4);
      }

      const nameSpan = document.createElement("span");
      nameSpan.textContent = fileName;
      nameSpan.style.cssText =
        "font-size: 0.65rem; color: var(--primary-text-color); width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: center;";

      item.appendChild(preview);
      item.appendChild(nameSpan);
      filesContainer.appendChild(item);
    });
    msg.appendChild(filesContainer);
  }

  let contentDiv = null;
  if (text) {
    contentDiv = document.createElement("div");
    contentDiv.classList.add("message-content");
    contentDiv.textContent = text;
    msg.appendChild(contentDiv);
  }

  const editWrapper = document.createElement("div");
  editWrapper.className = "edit-prompt-wrapper";

  const editIcon = document.createElement("span");
  editIcon.className = "edit-prompt-icon";
  editIcon.title = "Edit & Resubmit Prompt";
  editIcon.innerHTML = `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>`;

  const deleteIcon = document.createElement("span");
  deleteIcon.className = "edit-prompt-icon delete-msg-icon";
  deleteIcon.title = "Delete Message";
  deleteIcon.innerHTML = `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M19,4H15.5L14.5,3H9.5L8.5,4H5V6H19V4M6,19A2,2 0 0,0 8,21H16A2,2 0 0,0 18,19V7H6V19Z"/></svg>`;

  editWrapper.appendChild(editIcon);
  editWrapper.appendChild(deleteIcon);
  msg.appendChild(editWrapper);

  deleteIcon.addEventListener("click", (e) => {
    e.stopPropagation();
    deleteMessageFromServer(id, msg);
  });

  editIcon.addEventListener("click", (e) => {
    e.stopPropagation();

    const originalPromptBubble = e.target.closest(".user-msg");
    const filesContainer = originalPromptBubble.querySelector(
      'div[style*="justify-content: flex-end"]',
    );

    if (contentDiv) contentDiv.style.display = "none";
    editWrapper.style.display = "none";
    if (filesContainer) filesContainer.style.display = "none";

    const editContainer = document.createElement("div");
    editContainer.className = "edit-container";

    const editTextarea = document.createElement("textarea");
    editTextarea.className = "edit-textarea";
    editTextarea.value = text;

    const adjustHeight = () => {
      editTextarea.style.height = "auto";
      editTextarea.style.height = editTextarea.scrollHeight + "px";
    };
    editTextarea.addEventListener("input", adjustHeight);

    const buttonContainer = document.createElement("div");
    buttonContainer.className = "edit-buttons";

    const saveButton = document.createElement("button");
    saveButton.className = "edit-save-btn";
    saveButton.textContent = "Save & Resubmit";

    const cancelButton = document.createElement("button");
    cancelButton.className = "edit-cancel-btn";
    cancelButton.textContent = "Cancel";

    buttonContainer.appendChild(cancelButton);
    buttonContainer.appendChild(saveButton);
    editContainer.appendChild(editTextarea);
    editContainer.appendChild(buttonContainer);

    originalPromptBubble.appendChild(editContainer);

    // Save scroll position
    const scrollTarget = messagesDiv;
    const currentScroll = scrollTarget.scrollTop;

    // Prevent browser from snapping to top
    editTextarea.focus({ preventScroll: true });
    adjustHeight();

    // Force restore scroll position
    scrollTarget.scrollTop = currentScroll;

    const cleanupAndExitEditMode = () => {
      editContainer.remove();
      if (contentDiv) contentDiv.style.display = "block";
      editWrapper.style.display = "flex";
      if (filesContainer) filesContainer.style.display = "flex";
    };
    cancelButton.addEventListener("click", cleanupAndExitEditMode);

    saveButton.addEventListener("click", async () => {
      const correctedText = editTextarea.value.trim();
      const messageId = originalPromptBubble.dataset.id;

      if (!correctedText || correctedText === text) {
        cleanupAndExitEditMode();
        return;
      }
      if (isProcessing) {
        setStatus("Cannot edit while a response is being generated.", true);
        setTimeout(() => setStatus(currentStatusText, false), 3000);
        cleanupAndExitEditMode();
        return;
      }

      try {
        const response = await fetch("/api/messages/delete_after", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message_id: messageId,
            chat_id: currentChatId,
          }),
        });

        if (!response.ok) {
          const errData = await response.json();
          throw new Error(errData.error || "Failed to update chat history.");
        }

        let currentElem = originalPromptBubble;
        while (currentElem) {
          let toRemove = currentElem;
          currentElem = currentElem.nextElementSibling;
          toRemove.remove();
        }

        chatInput.value = correctedText;
        handleSend();
      } catch (error) {
        console.error("Error during edit & resubmit:", error);
        setStatus(`Error: ${error.message}`, true);
      } finally {
        cleanupAndExitEditMode();
      }
    });
  });

  const timeDiv = document.createElement("div");
  timeDiv.className = "message-timestamp";
  timeDiv.textContent = formatMsgTime(timestamp);
  msg.appendChild(timeDiv);

  if (messagesDiv) {
    const placeholder = messagesDiv.querySelector(".placeholder-message");
    if (placeholder) {
      messagesDiv.insertBefore(msg, placeholder);
    } else {
      messagesDiv.appendChild(msg);
    }
  }
  scrollToBottom();
}
function flashVisualFeedback() {
  const fileUploadButton = document.querySelector(".file-upload-label-button");
  if (fileUploadButton) {
    fileUploadButton.classList.add("file-feedback-active");
    setTimeout(() => {
      fileUploadButton.classList.remove("file-feedback-active");
    }, 500); // Duration matches the animation
  }
}
function appendResearchOutput(markdownText, id, timestamp = null) {
  const msg = document.createElement("div");
  msg.classList.add("message", "stellar-msg", "research-output");
  msg.dataset.id = id;
  const contentDiv = document.createElement("div");
  contentDiv.classList.add("message-content");
  msg.rawMarkdownData = markdownText;
  try {
    let htmlText = marked.parse(markdownText || "");
    htmlText = wrapTables(htmlText);
    contentDiv.innerHTML = htmlText;
    msg.appendChild(contentDiv);
    processCodeBlocks(contentDiv);
    processGenerativeUI(contentDiv);
    renderMath(contentDiv);
    addOutputCopyButton(msg);
    setTimeout(scrollToBottom, 50);
  } catch (e) {
    contentDiv.textContent = "Error.";
    scrollToBottom();
  }
  const timeDiv = document.createElement("div");
  timeDiv.className = "message-timestamp";
  timeDiv.textContent = formatMsgTime(timestamp);
  msg.appendChild(timeDiv);
  if (messagesDiv) messagesDiv.appendChild(msg);
  updateTokenCount();
  createAndAppendResearchButtons(msg, id, markdownText);
}

function createAndAppendResearchButtons(
  msgContainer,
  messageId,
  content,
  visualizationHtml = null,
) {
  if (!msgContainer || !msgContainer.classList.contains("research-output"))
    return;
  const existingButtonsDiv = msgContainer.querySelector(".message-buttons");
  if (existingButtonsDiv) existingButtonsDiv.remove();

  const buttonsDiv = document.createElement("div");
  buttonsDiv.classList.add("message-buttons");
  const editButton = document.createElement("button");
  editButton.textContent = "Edit";
  editButton.classList.add("edit-paper-btn");
  editButton.addEventListener("click", function () {
    const msgDiv = this.closest(".message.research-output");
    if (msgDiv) {
      const currentHTML = msgDiv.querySelector(".message-content").innerHTML;
      const id = msgDiv.dataset.id;
      showEditModal(id, currentHTML, msgDiv);
    }
  });
  const downloadButton = document.createElement("button");
  downloadButton.textContent = "Download";
  downloadButton.classList.add("download-btn");
  downloadButton.addEventListener("click", () => {
    const msgDiv = downloadButton.closest(".message.research-output");
    if (msgDiv) {
      const currentHTML = msgDiv.querySelector(".message-content").innerHTML;
      const currentWidth = msgDiv.offsetWidth;
      downloadHtml(currentHTML, currentWidth);
    }
  });

  // Visualize Button
  const vizBtn = document.createElement("button");
  vizBtn.classList.add("download-btn", "visualize-btn");
  vizBtn.innerHTML = `<svg style="width:16px;height:16px;margin-right:6px;" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>Visualize`;
  vizBtn.style.marginLeft = "10px";
  vizBtn.style.cursor = "pointer";

  if (visualizationHtml) {
    msgContainer.dataset.vizHtml = visualizationHtml;
  }

  vizBtn.addEventListener("click", async () => {
    const contentDiv = msgContainer.querySelector(".message-content");
    let vizContainer = msgContainer.querySelector(".viz-container");

    if (msgContainer.classList.contains("showing-viz")) {
      msgContainer.classList.remove("showing-viz");
      contentDiv.style.display = "block";
      if (vizContainer) vizContainer.style.display = "none";
      vizBtn.innerHTML = `<svg style="width:16px;height:16px;margin-right:6px;" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>Visualize`;
      return;
    }

    const showContainer = (htmlContent) => {
      if (!vizContainer) {
        vizContainer = document.createElement("div");
        vizContainer.className = "viz-container";
        vizContainer.style.width = "100%";
        vizContainer.style.height = "600px";
        vizContainer.style.border = "1px solid var(--model-color-border)";
        vizContainer.style.borderRadius = "8px";
        vizContainer.style.marginTop = "15px";
        vizContainer.style.overflow = "hidden";
        vizContainer.style.position = "relative";

        const iframe = document.createElement("iframe");
        iframe.style.width = "100%";
        iframe.style.height = "100%";
        iframe.style.border = "none";
        const injectedStyles = `<style>
                              body, html {
                                  background: transparent !important;
                                  color: #f0f0f0 !important;
                                  color-scheme: dark;
                                  margin: 0;
                              }
                              ::-webkit-scrollbar { width: 0px; background: transparent; }
                              html { scrollbar-width: none; }
                          </style>`;
        iframe.srcdoc = htmlContent.replace(
          "</head>",
          `${injectedStyles}</head>`,
        );
        vizContainer.appendChild(iframe);

        const fsBtn = document.createElement("button");
        fsBtn.innerHTML = `<svg style="width:20px;height:20px;" viewBox="0 0 24 24" fill="currentColor"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>`;
        fsBtn.style.position = "absolute";
        fsBtn.style.top = "10px";
        fsBtn.style.right = "10px";
        fsBtn.style.background = "rgba(0,0,0,0.6)";
        fsBtn.style.color = "white";
        fsBtn.style.border = "none";
        fsBtn.style.borderRadius = "4px";
        fsBtn.style.padding = "5px";
        fsBtn.style.cursor = "pointer";
        fsBtn.style.zIndex = "10";
        fsBtn.title = "Fullscreen";
        fsBtn.addEventListener("click", () => {
          if (vizContainer.requestFullscreen) vizContainer.requestFullscreen();
          else if (vizContainer.webkitRequestFullscreen)
            vizContainer.webkitRequestFullscreen(); // Safari
          else if (vizContainer.msRequestFullscreen)
            vizContainer.msRequestFullscreen(); // IE11
        });
        vizContainer.appendChild(fsBtn);

        contentDiv.parentNode.insertBefore(
          vizContainer,
          contentDiv.nextSibling,
        );
      } else {
        vizContainer.style.display = "block";
      }

      msgContainer.classList.add("showing-viz");
      contentDiv.style.display = "none";
      vizBtn.textContent = "Show Paper";
    };

    if (msgContainer.dataset.vizHtml) {
      showContainer(msgContainer.dataset.vizHtml);
      return;
    }

    vizBtn.disabled = true;
    vizBtn.innerHTML = "Generating...";

    try {
      const res = await fetch("/api/visualize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: content || contentDiv.innerText,
          message_id: messageId,
        }),
      });
      const result = await res.json();

      if (result.success && result.html) {
        msgContainer.dataset.vizHtml = result.html;
        showContainer(result.html);
      } else {
        alert(
          "Failed to generate visualization: " +
            (result.error || "Unknown error"),
        );
      }
    } catch (e) {
      console.error("Viz error:", e);
      alert("Error requesting visualization.");
    } finally {
      vizBtn.disabled = false;
      if (!msgContainer.classList.contains("showing-viz")) {
        vizBtn.innerHTML = `<svg style="width:16px;height:16px;margin-right:6px;" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>Visualize`;
      }
    }
  });

  buttonsDiv.appendChild(editButton);
  buttonsDiv.appendChild(downloadButton);
  buttonsDiv.appendChild(vizBtn);
  msgContainer.appendChild(buttonsDiv);
}

function setStatus(text, isError = false) {
  currentStatusText = text;
  updateFavicon();
}
function updateFavicon() {
  let link = document.getElementById("dynamic-favicon");
  if (!link) return;

  // Dynamically retrieve the current theme color from body's CSS custom property
  const activeColor =
    getComputedStyle(document.body)
      .getPropertyValue("--model-color-start")
      .trim() || "#7b61ff";
  let color = activeColor;

  let innerSVG =
    '<polygon points="12 2 2 7 2 17 12 22 22 17 22 7"/><circle cx="12" cy="12" r="4"/><path d="M12 2v6M22 7l-6 3M22 17l-6-3M12 22v-6M2 17l6-3M2 7l6 3"/>'; // Stellar (AI Core)

  const analysisAreaHasContent =
    document.getElementById("analysis-progress-area")?.childElementCount > 0;
  const stagedFilesExist =
    stagedFilesContainer?.childElementCount > 0 &&
    stagedFilesContainer?.style.display !== "none";
  const isActive =
    (currentStatusText && currentStatusText.toLowerCase() !== "idle") ||
    isProcessing ||
    analysisAreaHasContent ||
    stagedFilesExist;

  let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">`;
  if (isActive) {
    svg += `<g><animate attributeName="opacity" values="0.3;1;0.3" dur="1.5s" repeatCount="indefinite" />${innerSVG}</g>`;
  } else {
    svg += `${innerSVG}`;
  }
  svg += `</svg>`;

  link.href = "data:image/svg+xml;base64," + btoa(svg);
}
function adjustTextareaHeight() {
  if (!chatInput) return;
  try {
    if (chatInput.value.length === 0) {
      chatInput.style.height = "";
      chatInput.style.overflowY = "hidden";
    } else {
      chatInput.style.height = "auto";
      const newHeight = Math.min(chatInput.scrollHeight, 240);
      chatInput.style.height = `${newHeight}px`;
      chatInput.style.overflowY = newHeight >= 240 ? "auto" : "hidden";
    }
  } catch (e) {}
  if (typeof window.updateHasContent === "function") {
    window.updateHasContent();
  }
}
function showEditModal(id, htmlContent, msgDiv) {
  currentEditingMsg = msgDiv;
  currentEditingMsgId = id;
  try {
    editMarkdownInput.value = turndownService.turndown(htmlContent || "");
  } catch (e) {
    editMarkdownInput.value = "Error converting.";
  }
  if (editModalBackdrop) editModalBackdrop.style.display = "flex";
  if (editMarkdownInput) setTimeout(() => editMarkdownInput.focus(), 100);
}
function hideEditModal() {
  if (editModalBackdrop) editModalBackdrop.style.display = "none";
  currentEditingMsg = null;
  currentEditingMsgId = null;
  if (editMarkdownInput) editMarkdownInput.value = "";
}

/* Helper functions for custom confirmation modal to prevent accidental deletion/clear actions */
function showConfirmationModal(title, message, onConfirm) {
  const backdrop = document.getElementById("confirmationModalBackdrop");
  const titleEl = document.getElementById("confirmationModalTitle");
  const messageEl = document.getElementById("confirmationModalMessage");

  if (!backdrop || !titleEl || !messageEl) return;

  titleEl.textContent = title;
  messageEl.textContent = message;
  confirmationCallback = onConfirm;

  backdrop.style.display = "flex";

  // Shift focus to cancel button by default to prevent accidental trigger
  const cancelBtn = document.getElementById("cancelConfirmationBtn");
  if (cancelBtn) cancelBtn.focus();
}

function hideConfirmationModal() {
  const backdrop = document.getElementById("confirmationModalBackdrop");
  if (backdrop) {
    backdrop.style.display = "none";
  }
  confirmationCallback = null;
}

// REPLACE the existing handleModeChange function with this new one
function handleModeChange() {
  currentMode = "stellar";
  updateFavicon();

  chatContainer.style.display = "flex";
  inputContainer.style.display = "flex";
  updateModelSelectTheme();
  toggleScrollButton();

  chatInput.placeholder = "Send a message to Stellar...";

  enableDisableModelOptions();
  chatInput.focus();
  adjustTextareaHeight();
}
function enableDisableModelOptions() {
  for (const option of modelSelect.options) {
    option.disabled = false;
    option.style.opacity = "1";
    option.style.cursor = "pointer";
  }
}
function updateModelSelectWidth() {
  if (!modelSelect || !modelSelectWidthHelper) return;
  const selectedOption = modelSelect.options[modelSelect.selectedIndex];
  if (selectedOption) {
    modelSelectWidthHelper.textContent = selectedOption.text;
    const textWidth = modelSelectWidthHelper.offsetWidth;
    const padding = 28;
    const borderWidth = 2;
    const buffer = 10;
    const newWidth = textWidth + padding + borderWidth + buffer;
    modelSelect.style.width = `${Math.max(100, newWidth)}px`;
  } else {
    modelSelect.style.width = "auto";
  }
}
function updateModelSelectTheme() {
  if (!modelSelect || !bodyElement) return;
  const selectedOption = modelSelect.options[modelSelect.selectedIndex];
  if (selectedOption) {
    const themeName = selectedOption.text.toLowerCase().replace(/\s+/g, "-");
    const preserveBgOff = bodyElement.classList.contains("bg-off");
    const preservePureBlack = bodyElement.classList.contains("pure-black");
    bodyElement.className = `theme-${themeName}`;
    if (preserveBgOff) bodyElement.classList.add("bg-off");
    if (preservePureBlack) bodyElement.classList.add("pure-black");
  }
  updateFavicon();
}

/* ALL DEPRECATED FUNCTIONS REMOVED */

function downloadHtml(contentToDownload, currentWidth = 900) {
  if (!contentToDownload) {
    setStatus("No content.", true);
    setTimeout(() => setStatus(currentStatusText), 2000);
    return;
  }
  setStatus("Preparing download...");
  const styles = ` body { font-family: 'Poppins', sans-serif; line-height: 1.7; color: #333; background-color: #f0f2f5; margin: 0; padding: 20px; } .main-content-wrapper { background-color: #fdfdff; border: 1px solid #eee; padding: 30px; margin: 0 auto; max-width: ${currentWidth}px; } h1, h2, h3, h4, h5, h6 { color: #302b63; margin-top: 1.5em; margin-bottom: 0.5em; } h1 { font-size: 2em; border-bottom: 1px solid #ddd; padding-bottom: 0.3em;} h2 { font-size: 1.6em; } h3 { font-size: 1.3em; } p { margin: 1em 0; } a { color: #7b61ff; text-decoration: none; } a:hover { text-decoration: underline; } pre { background-color: #f0f0f0; padding: 15px; border-radius: 4px; overflow-x: auto; border: 1px solid #ddd; color: #333; white-space: pre-wrap; word-wrap: break-word; font-family: monospace; line-height: 1.6; } code { font-family: monospace; background-color: #eee; padding: 0.2em 0.4em; border-radius: 3px; } pre > code { background-color: transparent; padding: 0; border: none; } .table-wrapper { overflow-x: auto; max-width: 100%; margin: 1em 0; border: 1px solid #ccc; border-radius: 4px; } table { border-collapse: collapse; width: 100%; margin: 0; border: none; } th, td { border: 1px solid #ccc; padding: 8px 12px; text-align: left; word-break: break-word; border-width: 0 0 1px 0; } tr td:first-child, tr th:first-child { border-left: none; } tr td:last-child, tr th:last-child { border-right: none; } tr:last-child td { border-bottom: none; } th { background-color: #f2f2f2; font-weight: 600; } ul, ol { margin-left: 2em; margin-bottom: 1em; } blockquote { border-left: 4px solid #ccc; padding-left: 1em; margin-left: 0; color: #666; } .katex { font-size: 1.1em; } .katex-display { display: block; text-align: center; margin: 1em 0; } .katex-display > .katex { display: inline-block; text-align: initial; } .dl-header, .dl-footer { padding: 10px 30px; max-width: ${currentWidth}px; margin: 20px auto; font-size: 0.9em; color: #555; text-align: center; } .dl-header { border-bottom: 1px dashed #ccc; } .dl-footer { border-top: 1px dashed #ccc; margin-top: 30px; } `;
  const filenameBase = (lastRefinedQuery || "stellar_research")
    .substring(0, 50)
    .replace(/[^a-z0-9_\-\.]/gi, "_")
    .toLowerCase();
  const html = `<!DOCTYPE html> <html lang="en"> <head> <meta charset="UTF-8"> <title>Stellar Research: ${filenameBase}</title> <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600&display=swap" rel="stylesheet"> <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.css" crossorigin="anonymous"> <style>${styles}</style> </head> <body> <div class="dl-header"> <h1>Stellar Research Paper</h1> <p>Generated: ${new Date().toLocaleString()}</p> ${lastRefinedQuery ? `<p>Query: "${escapeHtml(lastRefinedQuery)}"</p>` : ""} </div> <div class="main-content-wrapper"> ${contentToDownload} </div> <div class="dl-footer"> <p>Stellar AI.</p> </div> </body> </html>`;
  try {
    const blob = new Blob([html], { type: "text/html;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${filenameBase}.html`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    setStatus(currentStatusText);
  } catch (e) {
    setStatus("Download failed.", true);
    appendStellarMessage("DL err.", Date.now() + "_dl_err");
    setTimeout(() => setStatus(currentStatusText), 3000);
  }
}

function handleFileUpload(filesOrEvent) {
  const files = filesOrEvent.target
    ? filesOrEvent.target.files || filesOrEvent.dataTransfer?.files
    : filesOrEvent;

  if (!files || !files.length) return;

  const newFilesArray = Array.from(files);
  let addedCount = 0;
  newFilesArray.forEach((file) => {
    if (
      !stagedFiles.some((sf) => sf.name === file.name && sf.size === file.size)
    ) {
      stagedFiles.push(file);
      addedCount++;
    } else {
    }
  });

  if (addedCount > 0) {
    updateStagedFilesUI();
    flashVisualFeedback();
  }

  if (
    filesOrEvent.target &&
    filesOrEvent.target.type === "file" &&
    filesOrEvent.target.id === "fileUpload"
  ) {
    filesOrEvent.target.value = null;
  }
}

function handlePasteEvent(event) {
  const files = event.clipboardData?.files;
  if (files && files.length > 0) {
    const targetElement = event.target;
    const isModalInput = targetElement.closest(
      "#editModalBackdrop, #regenerateModalBackdrop",
    );
    const isChatInputArea = targetElement === chatInput;
    const isChatContainerArea =
      targetElement === chatContainer ||
      targetElement === messagesDiv ||
      document.body === targetElement;

    if (!isModalInput && !isChatInputArea && isChatContainerArea) {
      event.preventDefault();
      handleFileUpload({ dataTransfer: { files: files } });
    }
  }
}

function updateStagedFilesUI() {
  const container = document.getElementById("stagedFilesContainer");
  if (!container) return;

  if (stagedFiles.length === 0) {
    container.innerHTML = "";
    container.style.display = "none";
    return;
  }

  container.style.display = "flex";
  container.style.flexWrap = "wrap";
  container.style.gap = "10px";
  container.innerHTML = "";

  stagedFiles.forEach((file, index) => {
    const item = document.createElement("div");
    item.style.cssText =
      "position: relative; display: flex; flex-direction: column; align-items: center; width: 80px; padding: 5px; background: rgba(0,0,0,0.4); border: 1px solid var(--input-border); border-radius: 8px;";

    const preview = document.createElement("div");
    preview.style.cssText =
      "width: 60px; height: 60px; border-radius: 4px; overflow: hidden; display: flex; align-items: center; justify-content: center; background: rgba(255,255,255,0.1); margin-bottom: 4px; font-size: 0.75rem; color: #aaa; word-break: break-all; text-align: center;";

    if (file.type.startsWith("image/")) {
      const img = document.createElement("img");
      img.src = URL.createObjectURL(file);
      img.style.cssText = "width: 100%; height: 100%; object-fit: cover;";
      preview.appendChild(img);
    } else {
      preview.textContent = file.name.split(".").pop().toUpperCase();
    }

    const name = document.createElement("span");
    name.textContent = file.name;
    name.style.cssText =
      "font-size: 0.65rem; color: var(--primary-text-color); width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: center;";

    const removeBtn = document.createElement("button");
    removeBtn.innerHTML = "×";
    removeBtn.style.cssText =
      "position: absolute; top: -5px; right: -5px; width: 18px; height: 18px; border-radius: 50%; background: #ff4444; color: white; border: none; cursor: pointer; font-size: 10px; display: flex; align-items: center; justify-content: center;";
    removeBtn.onclick = (e) => {
      e.stopPropagation();
      removeStagedFile(index);
    };

    item.appendChild(removeBtn);
    item.appendChild(preview);
    item.appendChild(name);
    container.appendChild(item);
  });
}

function removeStagedFile(index) {
  if (index >= 0 && index < stagedFiles.length) {
    stagedFiles.splice(index, 1);
    updateStagedFilesUI();
  }
}

window.addEventListener("stellarSend", (e) => {
  const chatInput = document.getElementById("chatInput");
  if (chatInput) {
    chatInput.value = e.detail.prompt;
    handleSend(e.detail.silent);
  }
});

async function handleSend(silent = false) {
  const lastEditIcon = document.querySelector(
    ".user-msg:last-of-type .edit-prompt-wrapper",
  );
  if (lastEditIcon) {
    lastEditIcon.remove();
  }

  if (isProcessing) {
    // --- LIVE INTERRUPT: Inject follow-up message into active stream ---
    const injectQuery = chatInput.value.trim();
    if (!injectQuery) return;

    chatInput.value = "";
    adjustTextareaHeight();
    toggleSendStopButtons(true);

    // Show the message in the UI immediately
    const injectMsgId = Date.now() + "_inject";
    hideWelcomeScreen();
    appendUserMessage(injectQuery, injectMsgId, [], true);
    scrollToBottom();

    // Send to backend inject endpoint
    try {
      const injectRes = await fetch("/api/inject_message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chat_id: currentChatId,
          message: injectQuery,
          client_id: CLIENT_ID,
        }),
      });
      if (injectRes.ok) {
        const injectData = await injectRes.json();
        // Update the DOM message with the real DB ID
        const injectMsgEl = document.querySelector(
          `.user-msg[data-id="${injectMsgId}"]`,
        );
        if (injectMsgEl && injectData.message_id) {
          injectMsgEl.dataset.id = injectData.message_id;
        }
        setStatus("Follow-up sent! Stellar will address it...");
        setTimeout(() => setStatus(currentStatusText, false), 3000);
      } else {
        setStatus("Could not inject message", true);
        setTimeout(() => setStatus(currentStatusText, false), 3000);
      }
    } catch (e) {
      setStatus("Injection failed: " + e.message, true);
      setTimeout(() => setStatus(currentStatusText, false), 3000);
    }
    return;
  }

  if (chatContainer.style.display === "none") {
    setStatus("Please log in to send messages.", true);
    setTimeout(() => setStatus(currentStatusText, false), 3000);
    return;
  }

  const query = chatInput.value.trim();
  if (!query && stagedFiles.length === 0) return;

  const selectedModel = modelSelect.value;
  isProcessing = true;
  taskStartTime = Date.now();
  notifiedForLongTask = false;
  requestNotificationPermission();

  sendBtn.disabled = true;
  toggleSendStopButtons(true);
  setStatus("Processing...");

  // Immediately move this chat to the top of the sidebar list
  if (currentChatId) {
    const activeChatItem = document.querySelector(
      `.chat-item[data-chat-id="${currentChatId}"]`,
    );
    const chatList = document.getElementById("chatList");
    if (activeChatItem && chatList && chatList.firstChild !== activeChatItem) {
      chatList.prepend(activeChatItem);
    }
  }

  const userMsgId = Date.now() + "_user";
  if (query || stagedFiles.length > 0) {
    if (!silent) {
      hideWelcomeScreen();
      appendUserMessage(query, userMsgId, [...stagedFiles], true);
    }
  }
  updateTokenCount();
  const originalQuery = query;
  chatInput.value = "";
  adjustTextareaHeight();
  toggleSendStopButtons(true);

  let serverFileIds = [];
  let hasFilesToSend = stagedFiles.length > 0;

  if (hasFilesToSend) {
    setStatus("Uploading files...");
    try {
      const formData = new FormData();
      stagedFiles.forEach((file) => {
        formData.append("file", file);
      });

      const uploadResponse = await fetch("/upload_files", {
        method: "POST",
        body: formData,
      });
      if (!uploadResponse.ok) {
        const errData = await uploadResponse.json().catch(() => ({
          error: `Upload HTTP ${uploadResponse.status}`,
        }));
        throw new Error(
          errData.error || `Upload HTTP ${uploadResponse.status}`,
        );
      }
      const uploadData = await uploadResponse.json();

      if (uploadData.uploaded_files && uploadData.uploaded_files.length > 0) {
        serverFileIds = uploadData.uploaded_files;
      }

      stagedFiles = [];
      updateStagedFilesUI();
    } catch (uploadError) {
      setStatus(`File Upload Failed: ${uploadError.message}`, true);
      isProcessing = false;
      sendBtn.disabled = false;
      toggleSendStopButtons(false);
      stagedFiles = [];
      updateStagedFilesUI();
      setTimeout(() => setStatus(currentStatusText, false), 4000);
      return;
    }
  }

  // ... rest of the function for other modes ...
  let placeholderId = `placeholder-${Date.now()}`;
  let placeholderText = query ? "Processing query..." : "Processing files...";
  if (serverFileIds.length > 0) {
    placeholderText = `Analyzing ${serverFileIds.length} files...`;
  }

  let endpoint = "/register_query";
  let streamUrl = "/refine_stream";
  let modeForBackend = "refine";
  let isResearchOutputExpected = false;

  try {
    const placeholderMsg = document.createElement("div");
    placeholderMsg.classList.add(
      "message",
      "stellar-msg",
      "placeholder-message",
    );
    if (isResearchOutputExpected) {
      placeholderMsg.classList.add("research-output");
    }
    placeholderMsg.dataset.id = placeholderId;
    const contentDiv = document.createElement("div");
    contentDiv.classList.add("message-content");
    const statusSpan = document.createElement("span");
    statusSpan.className = "placeholder-status";
    statusSpan.textContent = placeholderText;
    contentDiv.appendChild(statusSpan);
    if (serverFileIds.length > 0) {
      const detailsDiv = document.createElement("div");
      detailsDiv.className = "analysis-content";
      detailsDiv.style.display = "none";
      detailsDiv.innerHTML =
        "<small><i>Waiting for analysis details...</i></small>";
      contentDiv.appendChild(detailsDiv);
    }
    placeholderMsg.appendChild(contentDiv);
    if (messagesDiv) messagesDiv.appendChild(placeholderMsg);
    scrollToBottom();

    startLongTaskMonitor(placeholderId);
    initAndStartStreaming(
      originalQuery,
      selectedModel,
      modeForBackend,
      serverFileIds,
      currentChatId,
      placeholderId,
      userMsgId,
      isResearchOutputExpected,
      silent,
    );
  } catch (err) {
    let displayErr = err.message || "Unknown error";
    if (
      displayErr.toLowerCase().includes("failed to fetch") ||
      displayErr.toLowerCase().includes("networkerror")
    ) {
      displayErr = "Network Connection Error";
    }
    const errorMsg = `Error: ${displayErr}`;
    updateStellarMessagePlaceholder(placeholderId, errorMsg, true);
    setStatus(`Error: ${displayErr}`, true);
    isProcessing = false;
    sendBtn.disabled = false;
    toggleSendStopButtons(false);
    setTimeout(() => setStatus(currentStatusText, false), 4000);
  }
}

function wrapNakedHtmlBlocks(text) {
  if (!text) return text;

  function processNakedHtmlInSegment(segment) {
    const blockStartRegex =
      /^(?:[ \t]*)(<div|<style|<script|<section|<svg|<table|<iframe|<form|<canvas|<article|<aside|<header|<footer|<main|<!--)/im;
    let result = "";
    let remaining = segment;

    while (remaining) {
      const match = remaining.match(blockStartRegex);
      if (!match) {
        result += remaining;
        break;
      }

      const matchIndex = match.index;
      result += remaining.substring(0, matchIndex);

      const htmlStart = remaining.substring(matchIndex);
      const tagMatch = match[1].toLowerCase();

      let closingTag = "";
      let isNestedTag = false;
      let tagName = "";

      if (tagMatch.startsWith("<!--")) {
        closingTag = "-->";
      } else {
        const tagParts = tagMatch.match(/<([a-zA-Z0-9]+)/);
        if (tagParts) {
          tagName = tagParts[1].toLowerCase();
          closingTag = "</" + tagName + ">";
          isNestedTag = [
            "div",
            "section",
            "article",
            "aside",
            "header",
            "footer",
            "main",
            "form",
            "table",
          ].includes(tagName);
        }
      }

      if (!closingTag) {
        result += htmlStart;
        break;
      }

      let closeIndex = -1;
      if (isNestedTag) {
        let depth = 0;
        const openPattern = new RegExp("<" + tagName + "[\\s>]", "gi");
        const closePattern = new RegExp("</" + tagName + ">", "gi");

        let currentPos = 0;
        while (currentPos < htmlStart.length) {
          openPattern.lastIndex = currentPos;
          const nextOpen = openPattern.exec(htmlStart);

          closePattern.lastIndex = currentPos;
          const nextClose = closePattern.exec(htmlStart);

          if (!nextClose) {
            closeIndex = htmlStart.length;
            break;
          }

          if (nextOpen && nextOpen.index < nextClose.index) {
            depth++;
            currentPos = nextOpen.index + nextOpen[0].length;
          } else {
            depth--;
            if (depth === 0) {
              closeIndex = nextClose.index + nextClose[0].length;
              break;
            }
            currentPos = nextClose.index + nextClose[0].length;
          }
        }
      } else {
        const idx = htmlStart.toLowerCase().indexOf(closingTag);
        if (idx !== -1) {
          closeIndex = idx + closingTag.length;
        } else {
          closeIndex = htmlStart.length;
        }
      }

      if (closeIndex === -1) {
        closeIndex = htmlStart.length;
      }

      const rawHtmlBlock = htmlStart.substring(0, closeIndex);
      result += "\n```html\n" + rawHtmlBlock.trim() + "\n```\n";
      remaining = htmlStart.substring(closeIndex);
    }
    return result;
  }

  const parts = text.split(/(```[\s\S]*?```)/g);
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 0) {
      parts[i] = processNakedHtmlInSegment(parts[i]);
    }
  }
  return parts.join("");
}

function appendStellarMessage(markdownText, id, timestamp = null) {
  const msg = document.createElement("div");
  msg.classList.add("message", "stellar-msg");
  msg.dataset.id = id;
  const contentDiv = document.createElement("div");
  contentDiv.classList.add("message-content");
  msg.rawMarkdownData = markdownText;
  try {
    markdownText = wrapNakedHtmlBlocks(markdownText);
    let htmlContent = marked.parse(markdownText || "");
    htmlContent = wrapTables(htmlContent);
    contentDiv.innerHTML = htmlContent;
    msg.appendChild(contentDiv);
    unwrapVisuals(contentDiv);
    processCodeBlocks(contentDiv);
    processGenerativeUI(contentDiv);
    renderMath(contentDiv);
    addOutputCopyButton(msg);
    setTimeout(scrollToBottom, 50);
  } catch (e) {
    contentDiv.textContent = "Error rendering message content.";
  }
  const timeDiv = document.createElement("div");
  timeDiv.className = "message-timestamp";
  timeDiv.textContent = formatMsgTime(timestamp);
  msg.appendChild(timeDiv);
  if (messagesDiv) {
    messagesDiv.appendChild(msg);
    scrollToBottom();
  }
  updateTokenCount();
}
function updateStellarMessagePlaceholder(
  id,
  newText,
  isError = false,
  analysisDetails = null,
  timeoutVal = null,
) {
  const msgDiv = messagesDiv.querySelector(`.message[data-id="${id}"]`);
  if (!msgDiv) {
    if (isError) appendStellarMessage(`Error: ${newText}`, id + "_err");
    return;
  }
  const statusSpan = msgDiv.querySelector(".placeholder-status");
  const detailsDiv = msgDiv.querySelector(".analysis-content");

  if (statusSpan) {
    // Ensure status container is styled correctly
    const genUiContainer = msgDiv.querySelector(".generative-ui-container");
    const isUiActive =
      genUiContainer && genUiContainer.style.display === "block";

    if (isUiActive) {
      statusSpan.style.display = "none";
    } else if (
      !statusSpan.style.display ||
      statusSpan.style.display !== "flex"
    ) {
      statusSpan.style.display = "flex";
      statusSpan.style.flexDirection = "column";
      statusSpan.style.gap = "12px";
      statusSpan.innerHTML = ""; // clear initial placeholder text if needed
    }

    let displayMsg = newText;
    let showNotificationMeta = false;
    if (isProcessing && taskStartTime && Date.now() - taskStartTime > 20000) {
      const hasPermission =
        "Notification" in window && Notification.permission === "granted";
      if (agentSettings.notifications_enabled && hasPermission) {
        showNotificationMeta = true;
      } else if (!newText.endsWith("...")) {
        displayMsg = newText + "...";
      }
    }

    // 1. Mark previous active as completed
    const currentActiveItem = statusSpan.querySelector(".status-item.active");

    // Check if the exact same message is being set again to avoid duplicates
    if (
      currentActiveItem &&
      currentActiveItem.querySelector(".status-text").textContent ===
        displayMsg &&
      !isError
    ) {
      // Do nothing if it's identical text
    } else {
      if (currentActiveItem) {
        if (currentActiveItem.dataset.timerId) {
          clearInterval(parseInt(currentActiveItem.dataset.timerId, 10));
        }
        currentActiveItem.classList.remove("active");
        if (isError) {
          currentActiveItem.classList.add("error");
          currentActiveItem.querySelector(".status-icon-container").innerHTML =
            '<svg class="status-error-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
        } else {
          currentActiveItem.classList.add("completed");
          currentActiveItem.querySelector(".status-icon-container").innerHTML =
            '<svg class="checkmark" viewBox="0 0 24 24" fill="currentColor" style="width:18px;height:18px;"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>';
        }
      }

      // 2. Remove obsolete 'thinking' items to avoid UI clutter
      if (displayMsg.endsWith(" is thinking...")) {
        const allItems = statusSpan.querySelectorAll(".status-item");
        allItems.forEach((item) => {
          const textEl = item.querySelector(".status-text");
          if (textEl && textEl.textContent.endsWith(" is thinking...")) {
            item.remove();
          }
        });
      }

      // 3. Create new active status
      if (
        !isError &&
        newText &&
        newText.toLowerCase() !== "idle" &&
        newText.toLowerCase() !== "done"
      ) {
        const item = document.createElement("div");
        item.className = "status-item active";
        let timeoutHtml = "";
        if (timeoutVal) {
          timeoutHtml = `<div class="status-timeout" style="margin-left:auto; font-size:0.85em; opacity:0.8; white-space:nowrap; padding-left:10px;"><span class="countdown-timer" data-time="${timeoutVal}">${timeoutVal}s</span></div>`;
        }
        item.innerHTML = `
                              <div class="status-icon-container">
                                  <svg class="tool-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" style="width:18px;height:18px;">
                                      <circle cx="12" cy="12" r="10" opacity="0.2"></circle>
                                      <path d="M12 2a10 10 0 0 1 10 10"></path>
                                  </svg>
                              </div>
                              <div class="status-text">${escapeHtml(displayMsg)}</div>
                              ${timeoutHtml}
                          `;
        statusSpan.appendChild(item);

        if (timeoutVal) {
          const timerSpan = item.querySelector(".countdown-timer");
          let timeLeft = timeoutVal;
          const intervalId = setInterval(() => {
            timeLeft--;
            if (timeLeft <= 0) {
              clearInterval(intervalId);
              if (timerSpan) timerSpan.textContent = "0s";
            } else {
              if (timerSpan) timerSpan.textContent = timeLeft + "s";
            }
          }, 1000);
          item.dataset.timerId = intervalId;
        }
      } else if (isError) {
        const item = document.createElement("div");
        item.className = "status-item error";
        item.innerHTML = `
                              <div class="status-icon-container">
                                  <svg class="status-error-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px;"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                              </div>
                              <div class="status-text">${escapeHtml(displayMsg)}</div>
                          `;
        statusSpan.appendChild(item);
      }
    }

    // Handle meta-status notification line (Option 1)
    let metaNotify = statusSpan.querySelector(".status-meta-notify");
    if (showNotificationMeta) {
      if (!metaNotify) {
        metaNotify = document.createElement("div");
        metaNotify.className = "status-meta-notify";
        metaNotify.style.fontSize = "0.85em";
        metaNotify.style.marginTop = "4px";
        metaNotify.style.opacity = "0.8";
        metaNotify.textContent =
          "System will notify you when this task is complete.";
      }
      statusSpan.appendChild(metaNotify); // Always append to move it to the bottom
    } else if (metaNotify) {
      metaNotify.remove();
    }

    if (isError) {
      msgDiv.classList.remove("placeholder-message");

      statusSpan.style.color = "#ff6b6b";
      statusSpan.classList.remove("clickable", "analysis-placeholder");
      statusSpan.onclick = null;
    } else {
      statusSpan.style.color = "";
      // Only add clickable/analysis classes if we actually have details to show
      if (
        analysisDetails &&
        detailsDiv &&
        !statusSpan.classList.contains("clickable")
      ) {
        statusSpan.classList.add("clickable", "analysis-placeholder");
        if (!statusSpan.onclick) {
          statusSpan.onclick = () => toggleAnalysisDetails(msgDiv);
        }
      } else if (!analysisDetails) {
        // Ensure they are removed if this is just a status update
        statusSpan.classList.remove("clickable", "analysis-placeholder");
        statusSpan.onclick = null;
      }
    }
  }

  if (detailsDiv && analysisDetails) {
    let html = "";
    if (typeof analysisDetails === "string") {
      html = `<pre>${escapeHtml(analysisDetails)}</pre>`;
    } else if (
      typeof analysisDetails === "object" &&
      analysisDetails !== null
    ) {
      html += "<small>Results:</small><ul>";
      for (const [f, r] of Object.entries(analysisDetails)) {
        const s = r
          ? r.substring(0, 300) + (r.length > 300 ? "..." : "")
          : "(None)";
        html += `<li><strong>${escapeHtml(f)}:</strong><pre>${escapeHtml(s)}</pre></li>`;
      }
      html += "</ul>";
    } else {
      html = "<small><i>No details.</i></small>";
    }
    detailsDiv.innerHTML = html;
  }
}
function extractJson(str) {
  let firstOpen = str.indexOf("{");
  if (firstOpen === -1) return null;
  let braceCount = 0;
  for (let i = firstOpen; i < str.length; i++) {
    if (str[i] === "{") braceCount++;
    else if (str[i] === "}") braceCount--;
    if (braceCount === 0) {
      return {
        json: str.substring(firstOpen, i + 1),
        rest: str.substring(i + 1),
      };
    }
  }
  return null;
}

function robustParseSpecial(content, marker) {
  const index = content.indexOf(marker);
  if (index === -1) return null;
  const before = content.substring(0, index);
  const afterMarker = content.substring(index + marker.length).trim();
  const extracted = extractJson(afterMarker);
  if (!extracted) return { before: content, json: null, after: "" };
  try {
    return {
      before,
      json: JSON.parse(extracted.json),
      after: extracted.rest,
    };
  } catch (e) {
    console.error("Failed to parse special data JSON:", e);
    return { before: content, json: null, after: "" };
  }
}

function renderPresentationViewer(container, data) {
  if (!data || !data.slides || data.slides.length === 0) return;

  let currentIndex = 0;
  const regeneratingSlides = new Set();

  const viewer = document.createElement("div");
  viewer.className = "pres-viewer";
  viewer.dataset.presId = data.presentation_id;
  viewer.dataset.topic = data.topic;
  viewer.dataset.style = data.style;
  viewer.dataset.context = data.additional_context || "";
  viewer.dataset.numSlides = data.num_slides;

  const slideContainer = document.createElement("div");
  slideContainer.className = "pres-slide-container";

  const bgBlur = document.createElement("div");
  bgBlur.className = "pres-slide-bg-blur";
  bgBlur.style.backgroundImage = `url(${data.slides[0]})`;
  slideContainer.appendChild(bgBlur);

  const loader = document.createElement("div");
  loader.className = "pres-regen-loader";
  loader.innerHTML = `<div class="wave-dots"><div class="wave-dot"></div><div class="wave-dot"></div><div class="wave-dot"></div></div><div class="pres-regen-text">Regenerating Slide...</div>`;
  slideContainer.appendChild(loader);

  const feedbackOverlay = document.createElement("div");
  feedbackOverlay.className = "pres-feedback-overlay";
  feedbackOverlay.innerHTML = `
                  <div class="pres-feedback-box">
                      <div class="pres-feedback-title" style="font-family: 'Inter', sans-serif; font-weight: 900; color: #00E5FF; margin-bottom: 15px; font-size: 0.8em; letter-spacing: 1px; text-transform: uppercase;">Improve Slide ${currentIndex + 1}</div>
                      <input type="text" class="pres-feedback-input" placeholder="e.g. Add more statistics, change the image to a factory...">
                      <div class="pres-feedback-actions">
                          <button class="pres-feedback-btn pres-btn-cancel">Cancel</button>
                          <button class="pres-feedback-btn pres-btn-confirm">Regenerate</button>
                      </div>
                  </div>
              `;
  slideContainer.appendChild(feedbackOverlay);

  const feedbackInput = feedbackOverlay.querySelector(".pres-feedback-input");
  const cancelBtn = feedbackOverlay.querySelector(".pres-btn-cancel");
  const confirmBtn = feedbackOverlay.querySelector(".pres-btn-confirm");

  const img = document.createElement("img");
  img.className = "pres-slide-img";
  img.src = data.slides[0];
  slideContainer.appendChild(img);

  const controls = document.createElement("div");
  controls.className = "pres-controls";

  const nav = document.createElement("div");
  nav.className = "pres-nav";

  const prevBtn = document.createElement("button");
  prevBtn.className = "pres-arrow";
  prevBtn.innerHTML = "❮";
  prevBtn.disabled = true;

  const nextBtn = document.createElement("button");
  nextBtn.className = "pres-arrow";
  nextBtn.innerHTML = "❯";
  if (data.slides.length <= 1) nextBtn.disabled = true;

  const info = document.createElement("span");
  info.className = "pres-info";
  info.textContent = `Slide 1 of ${data.slides.length}`;

  nav.appendChild(prevBtn);
  nav.appendChild(info);
  nav.appendChild(nextBtn);

  const actions = document.createElement("div");
  actions.className = "pres-actions";

  const regenBtn = document.createElement("button");
  regenBtn.className = "pres-btn pres-regen-btn";
  regenBtn.innerHTML = `<svg style="width:16px;height:16px" viewBox="0 0 24 24" fill="currentColor"><path d="M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z"/></svg> Regenerate`;

  const downloadBtn = document.createElement("a");
  downloadBtn.className = "pres-btn pres-download-btn";
  downloadBtn.href = data.pptx_url;
  downloadBtn.target = "_blank";
  downloadBtn.rel = "noopener noreferrer";
  downloadBtn.innerHTML = `<svg style="width:16px;height:16px" viewBox="0 0 24 24" fill="currentColor"><path d="M5,20H19V18H5M19,9H15V3H9V9H5L12,16L19,9Z"/></svg> Download PPTX`;

  actions.appendChild(regenBtn);
  actions.appendChild(downloadBtn);

  controls.appendChild(nav);
  controls.appendChild(actions);

  viewer.appendChild(slideContainer);
  viewer.appendChild(controls);
  container.appendChild(viewer);

  const updateSlide = (index, isRegenFinished = false) => {
    currentIndex = index;

    if (isRegenFinished) {
      regeneratingSlides.delete(index);
    }

    if (regeneratingSlides.has(index)) {
      loader.classList.add("active");
    } else {
      loader.classList.remove("active");
      img.style.opacity = "0.3";
    }

    const nextImg = new Image();
    let slideUrl = viewer._slides[currentIndex];
    nextImg.src = slideUrl;

    nextImg.onload = () => {
      img.src = nextImg.src;
      bgBlur.style.backgroundImage = `url(${nextImg.src})`;
      img.style.opacity = "1";
      if (!regeneratingSlides.has(index)) {
        loader.classList.remove("active");
      }
    };

    info.textContent = `Slide ${currentIndex + 1} of ${viewer._slides.length}`;
    prevBtn.disabled = currentIndex === 0;
    nextBtn.disabled = currentIndex === viewer._slides.length - 1;
  };

  prevBtn.onclick = () => {
    if (currentIndex > 0) updateSlide(currentIndex - 1);
  };
  nextBtn.onclick = () => {
    if (currentIndex < data.slides.length - 1) updateSlide(currentIndex + 1);
  };

  regenBtn.onclick = () => {
    feedbackOverlay.querySelector(".pres-feedback-title").textContent =
      `Improve Slide ${currentIndex + 1}`;
    feedbackOverlay.classList.add("active");
    feedbackInput.focus();
  };

  cancelBtn.onclick = () => {
    feedbackOverlay.classList.remove("active");
    feedbackInput.value = "";
  };

  confirmBtn.onclick = () => {
    const feedback = feedbackInput.value.trim();
    if (!feedback) return;

    regeneratingSlides.add(currentIndex);
    loader.classList.add("active");
    feedbackOverlay.classList.remove("active");
    feedbackInput.value = "";

    const promptText = `Regenerate slide number ${currentIndex + 1} (index ${currentIndex}) of presentation ${data.presentation_id} with this feedback: ${feedback}. Original topic: ${data.topic}, style: ${data.style}, context: ${data.additional_context || ""}`;

    const chatField = document.getElementById("chatInput");
    if (chatField) {
      chatField.value = promptText;
      handleSend(true);
    }
  };

  feedbackInput.onkeydown = (e) => {
    if (e.key === "Enter") confirmBtn.click();
    if (e.key === "Escape") cancelBtn.click();
  };

  // Store references for easy update
  viewer._slides = data.slides;
  viewer._updateSlide = updateSlide;
}

function handleRegeneratedSlide(data) {
  const viewers = document.querySelectorAll(
    `.pres-viewer[data-pres-id="${data.presentation_id}"]`,
  );
  const timestampedUrl =
    data.url + (data.url.includes("?") ? "&" : "?") + "t=" + Date.now();
  viewers.forEach((viewer) => {
    if (viewer._slides && data.slide_index < viewer._slides.length) {
      viewer._slides[data.slide_index] = timestampedUrl;
      viewer._updateSlide(data.slide_index, true);
    }
  });
}

function openInBrowserPane(url, tabName) {
  const browserPane = document.getElementById("browserPane");
  const tabsContainer = document.getElementById("browserTabsContainer");
  const contentContainer = document.getElementById("browserContentContainer");

  if (!browserPane || !tabsContainer || !contentContainer) return;

  // Check if tab for this URL already exists
  let existingWrapper = Array.from(
    contentContainer.querySelectorAll(".browser-iframe-wrapper"),
  ).find((w) => w.dataset.rawUrl === url);

  if (existingWrapper) {
    // Reactivate existing tab
    tabsContainer
      .querySelectorAll(".browser-tab")
      .forEach((tab) => tab.classList.remove("active"));
    contentContainer
      .querySelectorAll(".browser-iframe-wrapper")
      .forEach((wrap) => wrap.classList.remove("active"));

    const existingTab = tabsContainer.querySelector(
      `.browser-tab[data-target-id="${existingWrapper.id}"]`,
    );
    if (existingTab) existingTab.classList.add("active");
    existingWrapper.classList.add("active");

    // Refresh the iframe to show new changes
    const iframe = existingWrapper.querySelector("iframe");
    if (iframe) {
      const sep = url.includes("?") ? "&" : "?";
      iframe.src = url + sep + "t=" + Date.now();
    }

    browserPane.style.display = "flex";
    document.body.classList.add("browser-open");
    const toggleBtn = document.getElementById("toggleBrowserPaneBtn");
    if (toggleBtn) toggleBtn.style.display = "flex";
    return;
  }

  // Animate in and show
  browserPane.style.display = "flex";
  document.body.classList.add("browser-open");
  const toggleBtn = document.getElementById("toggleBrowserPaneBtn");
  if (toggleBtn) toggleBtn.style.display = "flex";

  // Deactivate all existing tabs
  tabsContainer
    .querySelectorAll(".browser-tab")
    .forEach((tab) => tab.classList.remove("active"));
  contentContainer
    .querySelectorAll(".browser-iframe-wrapper")
    .forEach((wrap) => wrap.classList.remove("active"));

  const tabId = "tab-" + Date.now();

  // Create Tab
  const tab = document.createElement("div");
  tab.className = "browser-tab active";
  tab.dataset.targetId = tabId;
  tab.innerHTML = `
          <svg class="browser-tab-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9"></path></svg>
          <span class="browser-tab-title">${tabName}</span>
          <div style="display: flex; gap: 4px; margin-left: auto;">
            <button class="browser-tab-refresh" title="Refresh tab" style="background:none; border:none; color:inherit; cursor:pointer; padding:2px; opacity:0.7; display:flex; align-items:center; justify-content:center;">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"></path><path d="M3 3v5h5"></path></svg>
            </button>
            <button class="browser-tab-close" title="Close tab" style="background:none; border:none; color:inherit; cursor:pointer; padding:2px; opacity:0.7; display:flex; align-items:center; justify-content:center;">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"></path></svg>
            </button>
          </div>
        `;

  // Create Content Wrapper
  const wrapper = document.createElement("div");
  wrapper.className = "browser-iframe-wrapper active";
  wrapper.id = tabId;
  wrapper.dataset.rawUrl = url;

  function loadIframeOrError() {
    const sep = url.includes("?") ? "&" : "?";
    const noCacheUrl = url + sep + "t=" + Date.now();

    wrapper.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--secondary-text-color);">
              <style>@keyframes spinner-spin { to { transform: rotate(360deg); } }</style>
              <div style="width:30px;height:30px;border:3px solid rgba(255,255,255,0.1);border-top-color:var(--model-color-start);border-radius:50%;animation:spinner-spin 1s linear infinite;margin-bottom:15px;"></div>
              <div style="font-size:0.9rem;">Connecting to project...</div>
          </div>`;

    fetch("/api/utils/check_url?url=" + encodeURIComponent(noCacheUrl))
      .then((res) => res.json())
      .then((data) => {
        if (data.status === 200) {
          wrapper.innerHTML = `<iframe src="${noCacheUrl}" sandbox="allow-scripts allow-forms allow-popups allow-same-origin" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>`;
        } else {
          wrapper.innerHTML = `
                  <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--primary-text-color);text-align:center;padding:20px;background:rgba(0,0,0,0.2);">
                    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--model-color-start)" stroke-width="2" style="margin-bottom:16px;">
                      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
                      <line x1="12" y1="9" x2="12" y2="13"></line>
                      <line x1="12" y1="17" x2="12.01" y2="17"></line>
                    </svg>
                    <h3 style="margin:0 0 8px 0;font-size:1.2rem;">Project is Offline</h3>
                    <p style="margin:0 0 16px 0;color:var(--secondary-text-color);font-size:0.9rem;max-width:300px;line-height:1.5;">
                      The server at <strong>${url}</strong> cannot be reached. It may have been shut down or is currently restarting.
                    </p>
                    <button class="retry-btn" style="padding:8px 16px;background:var(--model-color-start);border:none;border-radius:6px;color:#fff;cursor:pointer;font-size:0.9rem;transition:transform 0.2s;">
                      Try Again
                    </button>
                  </div>
                `;
          wrapper.querySelector(".retry-btn").addEventListener("click", () => {
            loadIframeOrError();
          });
        }
      })
      .catch((err) => {
        // Fallback if the check endpoint itself fails
        wrapper.innerHTML = `<iframe src="${noCacheUrl}" sandbox="allow-scripts allow-forms allow-popups allow-same-origin" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>`;
      });
  }

  loadIframeOrError();

  // Tab Switching
  tab.addEventListener("click", (e) => {
    if (
      e.target.closest(".browser-tab-close") ||
      e.target.closest(".browser-tab-refresh")
    )
      return;
    tabsContainer
      .querySelectorAll(".browser-tab")
      .forEach((t) => t.classList.remove("active"));
    contentContainer
      .querySelectorAll(".browser-iframe-wrapper")
      .forEach((w) => w.classList.remove("active"));
    tab.classList.add("active");
    wrapper.classList.add("active");
  });

  // Tab Refresh
  tab.querySelector(".browser-tab-refresh").addEventListener("click", (e) => {
    e.stopPropagation();
    loadIframeOrError();
  });

  // Tab Closing
  tab.querySelector(".browser-tab-close").addEventListener("click", (e) => {
    e.stopPropagation();
    const wasActive = tab.classList.contains("active");
    tab.remove();
    wrapper.remove();

    if (wasActive) {
      const remainingTabs = tabsContainer.querySelectorAll(".browser-tab");
      if (remainingTabs.length > 0) {
        const lastTab = remainingTabs[remainingTabs.length - 1];
        lastTab.classList.add("active");
        document
          .getElementById(lastTab.dataset.targetId)
          .classList.add("active");
      } else {
        browserPane.style.display = "none";
        document.body.classList.remove("browser-open");
        const toggleBtn = document.getElementById("toggleBrowserPaneBtn");
        if (toggleBtn) toggleBtn.style.display = "none";
      }
    }
  });

  tabsContainer.appendChild(tab);
  contentContainer.appendChild(wrapper);

  // Scroll to the new tab
  tab.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function unwrapVisuals(container) {
  if (!container) return;
  // Protect user messages from unwrap/iframe/rendering mechanisms
  if (
    container.classList.contains("user-msg") ||
    container.closest(".user-msg")
  )
    return;

  // 1. YouTube Video Rendering
  container.querySelectorAll("a").forEach((link) => {
    if (link.classList.contains("user-msg") || link.closest(".user-msg"))
      return;
    const url = link.href;
    if (!url) return;

    // Regex to find YouTube video ID and optional time parameter
    const ytMatch = url.match(
      /(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([a-zA-Z0-9_-]{11})(?:[&?]t=(\d+h)?(\d+m)?(\d+s)?)?/,
    );
    if (ytMatch && ytMatch[1]) {
      const videoId = ytMatch[1];

      // Parse start time if present (handles s, m, h)
      let startTime = 0;
      if (ytMatch[2] || ytMatch[3] || ytMatch[4]) {
        const h = parseInt(ytMatch[2]) || 0;
        const m = parseInt(ytMatch[3]) || 0;
        const s = parseInt(ytMatch[4]) || 0;
        startTime = h * 3600 + m * 60 + s;
      } else if (url.includes("t=")) {
        // Fallback for simple seconds parameter like t=160
        const simpleMatch = url.match(/[&?]t=(\d+)/);
        if (simpleMatch) startTime = parseInt(simpleMatch[1]);
      }

      const playerDiv = document.createElement("div");
      playerDiv.className = "yt-player-container";
      playerDiv.style.width = "100%";
      playerDiv.style.maxWidth = "900px";
      playerDiv.style.aspectRatio = "16 / 9";
      playerDiv.style.margin = "15px auto";
      playerDiv.style.borderRadius = "12px";
      playerDiv.style.overflow = "hidden";
      playerDiv.style.border = "1px solid rgba(255,255,255,0.1)";
      playerDiv.style.boxShadow = "0 10px 30px rgba(0,0,0,0.3)";

      const embedUrl = `https://www.youtube-nocookie.com/embed/${videoId}${startTime ? "?start=" + startTime : ""}`;
      playerDiv.innerHTML = `<iframe width="100%" height="100%" src="${embedUrl}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>`;

      // Place the player after the link
      link.parentNode.insertBefore(playerDiv, link.nextSibling);

      // Hide the original raw text link so it looks cleaner
      link.style.display = "none";
    } else if (
      url.endsWith("stellarai.live") ||
      url.endsWith("stellarai.live/")
    ) {
      // Check if the application is online before rendering the iframe
      fetch("/api/utils/check_url?url=" + encodeURIComponent(url))
        .then((response) => response.json())
        .then((data) => {
          if (data.status === 200) {
            let tabName = "App";
            try {
              const urlObj = new URL(url);
              tabName = urlObj.hostname.split(".")[0];
            } catch (e) {}

            if (window.innerWidth > 768) {
              openInBrowserPane(url, tabName);
            } else {
              const iframeContainer = document.createElement("div");
              iframeContainer.className = "app-iframe-container";
              iframeContainer.style.width = "100%";
              iframeContainer.style.maxWidth = "1000px";
              iframeContainer.style.height = "600px"; // Give it a fixed height or aspect ratio
              iframeContainer.style.margin = "15px auto";
              iframeContainer.style.borderRadius = "12px";
              iframeContainer.style.overflow = "hidden";
              iframeContainer.style.border = "1px solid rgba(255,255,255,0.2)";
              iframeContainer.style.boxShadow = "0 10px 30px rgba(0,0,0,0.3)";

              iframeContainer.innerHTML = `<iframe width="100%" height="100%" src="${url}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>`;

              // Place the iframe after the link
              link.parentNode.insertBefore(iframeContainer, link.nextSibling);
            }
          }
        })
        .catch((err) => console.error("Error checking URL status:", err));

      // We keep the original link visible as requested by the user
    }
  });

  // 2. Fix SVGs trapped in <pre><code> blocks (markdown) or just as text
  container.querySelectorAll("pre code, .message-content").forEach((el) => {
    if (el.classList.contains("user-msg") || el.closest(".user-msg")) return;
    const className = el.className || "";
    let content = el.innerHTML;

    // If it's a code block, use textContent to avoid double-escaping issues
    if (el.tagName === "CODE") {
      content = el.textContent.trim();

      // CRITICAL FIX: Only unwrap if the block IS the SVG or explicitly marked.
      // If it's a Python/JS file containing an SVG string, leave it as code!
      const isAppCode =
        /language-(html|python|javascript|typescript|css|php|rust|go)/i.test(
          className,
        );
      const isExplicitVisual = /language-(svg|xml)/i.test(className);
      const isNakedSvg =
        content.startsWith("<svg") && content.endsWith("</svg>");

      // LOGIC: If it's a known app language and NOT explicitly marked as an SVG,
      // do NOT unwrap it. Show the code instead.
      if (isAppCode && !isExplicitVisual) return;

      // Proceed only if it's a naked SVG or explicitly requested as a visual
      if (!isNakedSvg && !isExplicitVisual) return;
    }

    const svgMatch = content.match(/<svg[\s\S]*?<\/svg>/i);
    if (svgMatch) {
      let svgCode = svgMatch[0];

      // CRITICAL: Strip hallucinated tool-call metadata/file paths
      svgCode = svgCode.replace(/@[^'"\s>]*\.txt/g, "");
      svgCode = svgCode.replace(/@\/home\/stellaradmin\/[^'"\s>]+/g, "");
      svgCode = svgCode.replace(/my_app\/venv\/[^'"\s>]+/g, "");

      if (el.tagName === "CODE") {
        const pre = el.parentElement;
        const div = document.createElement("div");
        div.className = "rendered-visual-wrapper";
        div.innerHTML = svgCode;
        if (pre && pre.parentNode) {
          pre.parentNode.replaceChild(div, pre);
        }
      } else if (el.classList.contains("message-content")) {
        // If it's the main container, we need to replace the raw string with the actual DOM nodes
        // but only if it's not already rendered. We'll do a surgical replacement.
        const wrapper = document.createElement("div");
        wrapper.className = "rendered-visual-wrapper";
        wrapper.innerHTML = svgCode;

        // Replace the first occurrence of the raw SVG string in the container's HTML
        // We use a safe temporary placeholder to avoid recursive regex mess
        const placeholder = `__SVG_PLACEHOLDER_${Date.now()}__`;
        el.innerHTML = el.innerHTML.replace(svgMatch[0], placeholder);
        el.innerHTML = el.innerHTML.replace(placeholder, wrapper.outerHTML);
      }
    }
  });

  // 2. Ensure all rendered SVGs are transparent and responsive
  container.querySelectorAll("svg").forEach((svg) => {
    if (svg.classList.contains("user-msg") || svg.closest(".user-msg")) return;
    svg.style.backgroundColor = "transparent";
    svg.style.maxWidth = "100%";
    svg.style.height = "auto";
    // Remove any solid background rects if they somehow got through
    const firstRect = svg.querySelector("rect:first-child");
    if (
      firstRect &&
      (firstRect.getAttribute("width") === "100%" ||
        firstRect.getAttribute("width") === svg.getAttribute("width")) &&
      (firstRect.getAttribute("height") === "100%" ||
        firstRect.getAttribute("height") === svg.getAttribute("height"))
    ) {
      const fill = firstRect.getAttribute("fill");
      if (fill && fill !== "none" && fill !== "transparent") {
        // If it's a solid dark or light background, hide it
        firstRect.style.display = "none";
      }
    }
  });
}

function finalizeStellarMessage(
  placeholderId,
  finalContent,
  messageDbId,
  analysisContextUsed = null,
  timestamp = null,
) {
  const placeholderDiv = messagesDiv.querySelector(
    `.message[data-id="${placeholderId}"]`,
  );
  if (!placeholderDiv) {
    if (analysisContextUsed) {
      appendStellarMessageWithAnalysis(
        finalContent,
        messageDbId,
        analysisContextUsed,
        timestamp,
      );
    } else {
      appendStellarMessage(finalContent, messageDbId, timestamp);
    }
    return;
  }

  const isResearch = placeholderDiv.classList.contains("research-output");

  placeholderDiv.dataset.id = messageDbId;
  placeholderDiv.classList.remove("placeholder-message");
  let contentDiv = placeholderDiv.querySelector(".message-content");
  if (!contentDiv) {
    contentDiv = document.createElement("div");
    contentDiv.classList.add("message-content");
    placeholderDiv.innerHTML = "";
    placeholderDiv.appendChild(contentDiv);
  } else {
    contentDiv.innerHTML = "";
  }

  try {
    let htmlContent = "";

    const autofixMatch = finalContent.match(/data-autofix-replace="([^"]+)"/);
    if (autofixMatch) {
      const replaceId = autofixMatch[1];
      const targetContainer = document.getElementById(replaceId);
      if (targetContainer) {
        const targetMsgDiv = targetContainer.closest(".message");
        if (targetMsgDiv) {
          // Protect user messages from autofix replacement
          if (
            targetMsgDiv.classList.contains("user-msg") ||
            targetMsgDiv.closest(".user-msg")
          ) {
            return;
          }
          placeholderDiv.style.display = "none";
          if (messageDbId) deleteMessageFromServer(messageDbId, placeholderDiv);

          const strippedContent = finalContent
            .replace(/<div[^>]*data-autofix-replace=[^>]*><\/div>/g, "")
            .trim();
          const oldContentDiv = targetMsgDiv.querySelector(".message-content");

          let contentToParse = wrapNakedHtmlBlocks(strippedContent);
          let newHtml = marked.parse(contentToParse);
          newHtml = wrapTables(newHtml);
          oldContentDiv.innerHTML = newHtml;

          const originalMessageId = targetMsgDiv.dataset.id;
          if (originalMessageId) {
            fetch("/update_message", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              credentials: "include",
              body: JSON.stringify({
                id: originalMessageId,
                content: strippedContent,
              }),
            }).catch((err) =>
              console.error(
                "Failed to persist autofix update to server database:",
                err,
              ),
            );
          }

          unwrapVisuals(oldContentDiv);
          processCodeBlocks(oldContentDiv);
          processGenerativeUI(oldContentDiv);
          renderMath(oldContentDiv);

          const prevUserMsg = placeholderDiv.previousElementSibling;
          if (
            prevUserMsg &&
            prevUserMsg.classList.contains("user-msg") &&
            prevUserMsg.innerText.includes("SYSTEM AUTO-FEEDBACK")
          ) {
            prevUserMsg.style.display = "none";
            const uid = prevUserMsg.dataset.id;
            if (uid) deleteMessageFromServer(uid, prevUserMsg);
          }
          return;
        }
      }
    }

    let spec = robustParseSpecial(finalContent, "PRESENTATION_DATA:");
    if (spec && spec.json) {
      htmlContent = marked.parse(spec.before || "");
      contentDiv.innerHTML = htmlContent;
      renderPresentationViewer(contentDiv, spec.json);
      if (spec.after && spec.after.trim()) {
        const extraDiv = document.createElement("div");
        extraDiv.innerHTML = marked.parse(spec.after);
        contentDiv.appendChild(extraDiv);
      }
    } else {
      spec = robustParseSpecial(finalContent, "REGENERATED_SLIDE:");
      if (spec && spec.json) {
        htmlContent = marked.parse(spec.before || "");
        contentDiv.innerHTML = htmlContent;
        handleRegeneratedSlide(spec.json);
        if (spec.after && spec.after.trim()) {
          const extraDiv = document.createElement("div");
          extraDiv.innerHTML = marked.parse(spec.after);
          contentDiv.appendChild(extraDiv);
        }
      } else {
        let contentToParse = wrapNakedHtmlBlocks(finalContent || "");
        htmlContent = marked.parse(contentToParse);
        htmlContent = wrapTables(htmlContent);
        contentDiv.innerHTML = htmlContent;
      }
    }
    unwrapVisuals(contentDiv);
    processCodeBlocks(contentDiv);
    processGenerativeUI(contentDiv);
    if (analysisContextUsed) {
      addAnalysisIndicator(placeholderDiv, analysisContextUsed);
    }

    if (isResearch) {
      createAndAppendResearchButtons(
        placeholderDiv,
        messageDbId,
        contentDiv.innerHTML,
      );
    }
    placeholderDiv.rawMarkdownData = finalContent;
    addOutputCopyButton(placeholderDiv);

    const timeDiv = document.createElement("div");
    timeDiv.className = "message-timestamp";
    timeDiv.textContent = formatMsgTime(timestamp);
    placeholderDiv.appendChild(timeDiv);

    setTimeout(() => {
      renderMath(contentDiv);
      scrollToBottom();
    }, 150);
  } catch (e) {
    contentDiv.textContent = "Error.";
    if (analysisContextUsed) {
      addAnalysisIndicator(placeholderDiv, analysisContextUsed);
    }
    if (isResearch) {
      createAndAppendResearchButtons(
        placeholderDiv,
        messageDbId,
        "<p>Error</p>",
      );
    }
    addOutputCopyButton(placeholderDiv);
    scrollToBottom();
  }
  updateTokenCount();
}
function addAnalysisIndicator(messageDiv, analysisContext) {
  if (!messageDiv || !analysisContext) return;
  const contentDiv = messageDiv.querySelector(".message-content");
  if (!contentDiv) return;
  contentDiv
    .querySelectorAll(".analysis-indicator, .analysis-content")
    .forEach((el) => el.remove());
  const ind = document.createElement("span");
  ind.className = "analysis-indicator clickable";
  ind.textContent = " Analyzed files";
  ind.style.marginLeft = "10px";
  const det = document.createElement("div");
  det.className = "analysis-content";
  det.style.display = "none";
  det.style.marginTop = "10px";
  det.style.paddingTop = "10px";
  det.style.borderTop = "1px dashed var(--stellar-msg-border)";
  det.innerHTML = `<small><i>Context:</i></small><div class="formatted-analysis-context">${formatAnalysisContextForDisplay(analysisContext)}</div>`;
  contentDiv.appendChild(ind);
  contentDiv.appendChild(det);
}
function formatAnalysisContextForDisplay(rawContext) {
  if (!rawContext || typeof rawContext !== "string")
    return "<p>No context.</p>";
  try {
    let html = "";
    try {
      html = marked.parse(rawContext || "");
    } catch (e) {
      return `<pre>Parse err: ${escapeHtml(e.message)}\n\n${escapeHtml(rawContext)}</pre>`;
    }
    const p = new DOMParser();
    const d = p.parseFromString(
      `<div id="ctx-wrap">${html}</div>`,
      "text/html",
    );
    const w = d.getElementById("ctx-wrap");
    if (!w) return `<pre>${escapeHtml(rawContext)}</pre>`;
    w.querySelectorAll("details").forEach((det) => {
      let s = det.querySelector("summary");
      if (!s) {
        s = d.createElement("summary");
        s.textContent = "Details";
        det.insertBefore(s, det.firstChild);
      }
      s.style.cursor = "pointer";
      s.style.fontWeight = "bold";
      s.style.color = "var(--primary-text-color)";
      s.style.padding = "8px";
      s.style.display = "block";
      s.style.backgroundColor = "rgba(255, 255, 255, 0.03)";
      s.style.borderRadius = "4px 4px 0 0";
      let cw = s.nextElementSibling;
      if (cw && (cw.tagName === "DIV" || cw.tagName === "PRE")) {
        cw.style.padding = "10px";
        cw.style.borderTop = "1px dashed rgba(255, 255, 255, 0.1)";
        cw.style.marginTop = "0";
        cw.style.backgroundColor = "rgba(0,0,0,0.1)";
      } else {
        const nw = d.createElement("div");
        nw.style.padding = "10px";
        nw.style.borderTop = "1px dashed rgba(255, 255, 255, 0.1)";
        nw.style.marginTop = "0";
        nw.style.backgroundColor = "rgba(0,0,0,0.1)";
        while (s.nextSibling) {
          nw.appendChild(s.nextSibling);
        }
        det.appendChild(nw);
      }
      det.style.border = "1px solid rgba(255, 255, 255, 0.1)";
      det.style.borderRadius = "4px";
      det.style.marginBottom = "10px";
      det.style.backgroundColor = "rgba(0, 0, 0, 0.05)";
    });
    w.querySelectorAll(":scope > pre").forEach((pre) => {
      if (pre.closest("details")) return;
      pre.style.marginTop = "10px";
      pre.style.marginBottom = "10px";
      pre.style.padding = "12px";
      pre.style.fontSize = "0.9em";
      pre.style.backgroundColor = "rgba(0,0,0,0.25)";
      pre.style.border = "1px solid rgba(255,255,255,0.15)";
      pre.style.borderRadius = "4px";
      pre.style.whiteSpace = "pre-wrap";
      pre.style.wordWrap = "break-word";
    });
    let finalHtml = w.innerHTML;
    finalHtml = finalHtml.replace(/##\s*(.*?)\s*(<br\s*\/?>)?/g, "<h3>$1</h3>");
    finalHtml = finalHtml.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");
    finalHtml = finalHtml.replace(/__(.*?)__/g, "<strong>$1</strong>");
    finalHtml = finalHtml.replace(/\*(.*?)\*/g, "<em>$1</em>");
    finalHtml = finalHtml.replace(/_(.*?)_/g, "<em>$1</em>");
    finalHtml = finalHtml.replace(/`([^`]+)`/g, "<code>$1</code>");
    return finalHtml;
  } catch (e) {
    return `<pre>${escapeHtml(rawContext)}</pre>`;
  }
}
function appendStellarMessageWithAnalysis(
  markdownText,
  id,
  analysisContext,
  timestamp = null,
) {
  const msg = document.createElement("div");
  msg.classList.add("message", "stellar-msg");
  msg.dataset.id = id;
  const contentDiv = document.createElement("div");
  contentDiv.classList.add("message-content");
  msg.rawMarkdownData = markdownText;
  try {
    markdownText = wrapNakedHtmlBlocks(markdownText);
    let htmlContent = marked.parse(markdownText || "");
    htmlContent = wrapTables(htmlContent);
    contentDiv.innerHTML = htmlContent;
    msg.appendChild(contentDiv);
    unwrapVisuals(contentDiv);
    processCodeBlocks(contentDiv);
    processGenerativeUI(contentDiv);
    addAnalysisIndicator(msg, analysisContext);
    addOutputCopyButton(msg);
    setTimeout(() => {
      renderMath(contentDiv);
      const analysisEl = msg.querySelector(
        ".analysis-content .formatted-analysis-context",
      );
      if (analysisEl) renderMath(analysisEl);
      scrollToBottom();
    }, 150);
  } catch (e) {
    contentDiv.textContent = "Error.";
    addAnalysisIndicator(msg, analysisContext);
    addOutputCopyButton(msg);
    scrollToBottom();
  }
  msg.appendChild(contentDiv);
  const timeDiv = document.createElement("div");
  timeDiv.className = "message-timestamp";
  timeDiv.textContent = formatMsgTime(timestamp);
  msg.appendChild(timeDiv);
  if (messagesDiv) messagesDiv.appendChild(msg);
}

function startSseConnection(
  queryId,
  modeForBackend,
  chatId,
  placeholderId,
  userMessageId,
  isResearchOutputExpected,
  silent,
) {
  const maxRetries = 3;
  let retryCount = 0;

  const attemptStreamConnection = async () => {
    try {
      let streamUrl = "/refine_stream";

      const params = new URLSearchParams({
        query_id: queryId,
        chat_id: chatId,
      });

      if (sseEventSource && sseEventSource.readyState !== EventSource.CLOSED) {
        sseEventSource.close();
      }
      sseEventSource = new EventSource(`${streamUrl}?${params.toString()}`);

      let fullResponseContent = "";
      let analysisContextReceived = null;
      let analysisResultsReceived = null;
      let streamEndedCleanly = false;

      sseEventSource.onmessage = async (event) => {
        if (!event.data || event.data.trim() === "") return;
        try {
          const data = JSON.parse(event.data);

          if (data.type === "scraping_url" && data.url) {
            scrapingQueue.push(data.url);
            if (scrapingTimerId === null) {
              processScrapingQueue();
            }
          }

          if (data.type === "injection_ack") {
            // Live follow-up was acknowledged by the backend
            setStatus("Stellar received your follow-up!");
            setTimeout(() => setStatus(currentStatusText, false), 2000);
            return;
          }

          if (data.type === "generative_ui") {
            // Render the UI into the placeholder message directly
            const msgDiv = document.querySelector(
              `.message[data-id="${placeholderId}"]`,
            );
            if (msgDiv) {
              let contentDiv = msgDiv.querySelector(".message-content");
              if (!contentDiv) {
                contentDiv = document.createElement("div");
                contentDiv.classList.add("message-content");
                msgDiv.appendChild(contentDiv);
              }

              // Hide other sibling status elements
              const statusSpan = contentDiv.querySelector(
                ".placeholder-status",
              );
              if (statusSpan) statusSpan.style.display = "none";
              const detailsDiv = contentDiv.querySelector(".analysis-content");
              if (detailsDiv) detailsDiv.style.display = "none";

              let genUiContainer = contentDiv.querySelector(
                ".generative-ui-container",
              );
              if (!genUiContainer) {
                genUiContainer = document.createElement("div");
                genUiContainer.classList.add("generative-ui-container");
                contentDiv.appendChild(genUiContainer);
              }
              genUiContainer.style.display = "block";
              // Expose the finish hook for this specific interaction BEFORE rendering/processing scripts
              window.stellar.finish = async function (resultData) {
                // Automatically provide visual feedback by disabling interactive elements on the next tick
                // to prevent Safari from invalidating/aborting the user gesture fetch request.
                setTimeout(() => {
                  const interactables = genUiContainer.querySelectorAll(
                    'button, input, select, textarea, [role="button"], a',
                  );
                  interactables.forEach((el) => {
                    el.disabled = true;
                    el.style.opacity = "0.5";
                    el.style.cursor = "wait";
                    el.style.pointerEvents = "none"; // Prevents clicks on divs/svgs
                  });
                }, 0);

                try {
                  await fetch("/api/generative_ui/finish", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      interaction_id: data.interaction_id,
                      data: resultData,
                    }),
                  });
                } catch (e) {
                  console.error("Failed to finish interaction:", e);
                }
              };

              genUiContainer.innerHTML = data.html;
              unwrapVisuals(genUiContainer);
              processCodeBlocks(genUiContainer);
              processGenerativeUI(genUiContainer);
            }
          }

          if (data.status) {
            currentStatusText = data.status;
            const simpleStatus = currentStatusText.replace(
              /\s*\(Attempt \d+\/\d+\)/i,
              "",
            );
            setStatus(simpleStatus, !!data.error);

            // --- UNLOAD GENERATIVE UI ON NEXT VALID STATUS ---
            const statusLower = data.status.toLowerCase();
            const isThinking =
              statusLower.includes("thinking") ||
              statusLower.includes("wait") ||
              statusLower === "refined_ready";

            if (!isThinking) {
              const activePlaceholder = messagesDiv.querySelector(
                `.message[data-id="${placeholderId}"]`,
              );
              if (activePlaceholder) {
                const genUiContainer = activePlaceholder.querySelector(
                  ".generative-ui-container",
                );
                if (genUiContainer && genUiContainer.style.display !== "none") {
                  genUiContainer.style.display = "none";
                  // Restore status and details display
                  const statusSpan = activePlaceholder.querySelector(
                    ".placeholder-status",
                  );
                  if (statusSpan) statusSpan.style.display = "flex";
                  const detailsDiv =
                    activePlaceholder.querySelector(".analysis-content");
                  if (detailsDiv) detailsDiv.style.display = "block";
                }
              }
            }
            // --- END UNLOAD ---
          }

          if (data.analysis_results) {
            analysisResultsReceived = data.analysis_results;
            updateStellarMessagePlaceholder(
              placeholderId,
              data.status || "Processing...",
              false,
              analysisResultsReceived,
              data.timeout,
            );
          } else if (data.status) {
            updateStellarMessagePlaceholder(
              placeholderId,
              data.status || "Processing...",
              false,
              null,
              data.timeout,
            );
          }

          if (data.error) {
            const errMsg =
              (typeof data.error === "string" ? data.error : data.status) ||
              "Unknown error";
            cleanupStream(
              true,
              errMsg + (data.details ? ` Details: ${data.details}` : ""),
              placeholderId,
            );
            streamEndedCleanly = true;
            return;
          }

          if (data.analysis_context_used) {
            analysisContextReceived = data.analysis_context_used;
          }

          const finalContent = data.refined_query || data.result;
          const isFinalMessage =
            (data.status === "refined_ready" ||
              data.status === "display_result") &&
            finalContent !== undefined;

          if (isFinalMessage) {
            streamEndedCleanly = true;
            fullResponseContent = finalContent;
            const messageDbId = data.message_id || placeholderId;
            const userMessageDbId = data.user_message_id;

            if (userMessageId && userMessageDbId) {
              const lastUserMsg = document.querySelector(
                `.message.user-msg[data-id="${userMessageId}"]`,
              );
              if (lastUserMsg) {
                lastUserMsg.dataset.id = userMessageDbId;
              }
            }

            const placeholderDiv = messagesDiv.querySelector(
              `.message[data-id="${placeholderId}"]`,
            );
            if (
              placeholderDiv &&
              isResearchOutputExpected &&
              !placeholderDiv.classList.contains("research-output")
            ) {
              placeholderDiv.classList.add("research-output");
            }

            finalizeStellarMessage(
              placeholderId,
              fullResponseContent,
              messageDbId,
              analysisContextReceived,
              data.timestamp,
            );

            if (data.status === "display_result" && data.file_url) {
              const finalMsgDiv = messagesDiv.querySelector(
                `.message[data-id="${messageDbId}"]`,
              );
              if (
                finalMsgDiv &&
                finalMsgDiv.classList.contains("research-output")
              ) {
                const buttonsDiv =
                  finalMsgDiv.querySelector(" .message-buttons");
                if (buttonsDiv) {
                  let viewBtn = buttonsDiv.querySelector(" .view-html-btn");
                  if (!viewBtn) {
                    viewBtn = document.createElement("a");
                    viewBtn.classList.add("download-btn", "view-html-btn");
                    viewBtn.target = "_blank";
                    buttonsDiv.appendChild(viewBtn);
                  }
                  viewBtn.textContent = "View Paper";
                  viewBtn.href = data.file_url;
                }
              }
            }

            cleanupStream(false, null, placeholderId);

            const numMessagesInChat = Array.from(messagesDiv.children).filter(
              (child) =>
                child.classList.contains("user-msg") ||
                child.classList.contains("stellar-msg"),
            ).length;
            if (
              chatId &&
              (numMessagesInChat === 2 ||
                (numMessagesInChat > 2 && (numMessagesInChat - 2) % 10 === 1))
            ) {
              const userQueryNode = document.querySelector(
                `.message.user-msg[data-id="${userMessageDbId || userMessageId}"] .message-content`,
              );
              const userQueryText = userQueryNode
                ? userQueryNode.textContent
                : "New Chat";
              const chatNameResponse = await fetch(
                `/api/chats/${chatId}/name`,
                {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    first_message_content: userQueryText,
                  }),
                },
              );
              const chatNameData = await chatNameResponse.json();
              if (chatNameData.success) {
                updateChatNameInList(chatId, chatNameData.name);
              }
            }
          }
        } catch (err) {
          console.error("Error processing stream update:", err);
          const errMsg = err && err.message ? err.message : String(err);
          const errStack = err && err.stack ? err.stack.split("\n")[0] : "";
          updateStellarMessagePlaceholder(
            placeholderId,
            "Error processing stream update: " + errMsg + " " + errStack,
            true,
          );
          setStatus("Stream error: " + errMsg, true);
        }
      };

      sseEventSource.onerror = (err) => {
        if (streamEndedCleanly) return;
        if (sseEventSource) sseEventSource.close();

        if (retryCount < maxRetries) {
          retryCount++;
          setTimeout(attemptStreamConnection, 2000 * retryCount);
        } else {
          cleanupStream(
            true,
            "Stream connection failed after retries.",
            placeholderId,
          );
        }
      };

      sseEventSource.onclose = () => {
        if (streamEndedCleanly) return;
        if (sseEventSource.readyState === EventSource.CLOSED) {
          cleanupStream(
            true,
            "Stream connection unexpectedly closed.",
            placeholderId,
          );
        }
      };
    } catch (err) {
      if (retryCount < maxRetries) {
        retryCount++;
        setTimeout(attemptStreamConnection, 2000 * retryCount);
      } else {
        cleanupStream(
          true,
          `Failed to establish stream: ${err.message}`,
          placeholderId,
        );
      }
    }
  };
  attemptStreamConnection();
}

async function initAndStartStreaming(
  userQuery,
  modelId,
  modeForBackend,
  pendingFiles,
  chatId,
  placeholderId,
  userMessageId,
  isResearchOutputExpected,
  silent = false,
) {
  if (currentStreamQueryId) currentStreamQueryId = null;

  try {
    const registerPayload = {
      query: userQuery || " ",
      model_id: modelId,
      mode: modeForBackend,
      pending_files: pendingFiles,
      chat_id: chatId,
      hidden: silent,
      disabled_tools: Object.keys(agentSettings).filter(
        (t) => !agentSettings[t],
      ),
      client_id: CLIENT_ID,
    };

    const registerResponse = await fetch("/register_query", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(registerPayload),
    });

    if (!registerResponse.ok) {
      const errData = await registerResponse.json().catch(() => ({}));
      throw new Error(
        errData.error || `Register fail HTTP ${registerResponse.status}`,
      );
    }
    const registerData = await registerResponse.json();
    if (!registerData.query_id)
      throw new Error("No query_id received from registration.");

    currentStreamQueryId = registerData.query_id;
    lastRefinedQuery = userQuery;

    startSseConnection(
      currentStreamQueryId,
      modeForBackend,
      chatId,
      placeholderId,
      userMessageId,
      isResearchOutputExpected,
      silent,
    );
  } catch (err) {
    let displayErr = err.message || "Unknown error";
    updateStellarMessagePlaceholder(
      placeholderId,
      `Error: ${displayErr}`,
      true,
    );
    setStatus(`Error: ${displayErr}`, true);
    cleanupStream(true, displayErr, placeholderId);
  }
}

function reconnectToStream(queryId, mode, chatId) {
  currentStreamQueryId = queryId;
  isProcessing = true;
  sendBtn.disabled = true;
  toggleSendStopButtons(true);
  setStatus("Reconnecting...", false);

  let placeholderMsg = document.querySelector(".placeholder-message");
  let placeholderId = placeholderMsg
    ? placeholderMsg.dataset.id
    : `placeholder-${Date.now()}`;
  let isResearchOutputExpected = mode.startsWith("search");

  if (!placeholderMsg) {
    placeholderMsg = document.createElement("div");
    placeholderMsg.classList.add(
      "message",
      "stellar-msg",
      "placeholder-message",
    );
    if (isResearchOutputExpected) {
      placeholderMsg.classList.add("research-output");
    }
    placeholderMsg.dataset.id = placeholderId;
    const contentDiv = document.createElement("div");
    contentDiv.classList.add("message-content");
    const statusSpan = document.createElement("span");
    statusSpan.className = "placeholder-status";
    statusSpan.textContent = "Reconnecting to stream...";
    contentDiv.appendChild(statusSpan);
    placeholderMsg.appendChild(contentDiv);
    if (messagesDiv) messagesDiv.appendChild(placeholderMsg);
    scrollToBottom();
  }

  startSseConnection(
    queryId,
    mode,
    chatId,
    placeholderId,
    null,
    isResearchOutputExpected,
    false,
  );
}
function cleanupStream(
  showErrorStatus = false,
  errorMsg = null,
  placeholderId,
) {
  stopLongTaskMonitor();
  toggleSendStopButtons(false);
  if (sseEventSource) {
    sseEventSource.close();
    sseEventSource = null;
  }
  hideLivePreview();
  clearTimeout(scrapingTimerId);
  scrapingTimerId = null;
  scrapingQueue = [];
  isProcessing = false;

  sendBtn.disabled = false;

  if (
    !showErrorStatus &&
    taskStartTime &&
    (Date.now() - taskStartTime > 10000 ||
      document.visibilityState === "hidden")
  ) {
    if ("serviceWorker" in navigator && "PushManager" in window) {
      navigator.serviceWorker.ready
        .then((reg) => {
          return reg.pushManager.getSubscription();
        })
        .then((sub) => {
          if (!sub) {
            notifyUser(
              "Stellar Task Complete",
              "Your request has been processed successfully.",
            );
          }
        })
        .catch(() => {
          notifyUser(
            "Stellar Task Complete",
            "Your request has been processed successfully.",
          );
        });
    } else {
      notifyUser(
        "Stellar Task Complete",
        "Your request has been processed successfully.",
      );
    }
  }
  taskStartTime = null;
  notifiedForLongTask = false;

  const finalStatus = showErrorStatus ? errorMsg || "Stream Error" : "Idle";
  const shouldBeIdle = !isProcessing && stagedFiles.length === 0;
  setStatus(
    showErrorStatus ? finalStatus : shouldBeIdle ? "Idle" : currentStatusText,
    showErrorStatus,
  );
  if (showErrorStatus) {
    if (errorMsg === "Stopped by user." && placeholderId) {
      const msgDiv = messagesDiv.querySelector(
        `.message[data-id="${placeholderId}"]`,
      );
      if (msgDiv) {
        msgDiv.remove();
      }
    } else {
      updateStellarMessagePlaceholder(
        placeholderId,
        `Error: ${errorMsg || "Failed"}`,
        true,
      );
      setTimeout(() => {
        const shouldBeIdleAfterError =
          !isProcessing && stagedFiles.length === 0;
        setStatus(shouldBeIdleAfterError ? "Idle" : currentStatusText, false);
      }, 4000);
    }
  }

  const latestUserMessage = [...document.querySelectorAll(".user-msg")].pop();
  if (latestUserMessage) {
    const editIconWrapper = latestUserMessage.querySelector(
      ".edit-prompt-wrapper",
    );
    if (editIconWrapper) {
      editIconWrapper.style.display = "flex";
    }
  }
}

function toggleAnalysisDetails(messageElement) {
  if (!messageElement) return;
  const det = messageElement.querySelector(".analysis-content");
  if (det) {
    const isHidden = det.style.display === "none";
    det.style.display = isHidden ? "block" : "none";
    if (isHidden) {
      det
        .querySelectorAll(".formatted-analysis-context pre code")
        .forEach((c) => {
          if (!c.dataset.highlighted) {
            try {
              if (typeof hljs !== "undefined") {
                hljs.highlightElement(c);
                c.dataset.highlighted = "yes";
              }
            } catch (e) {}
          }
        });
      setTimeout(
        () => det.scrollIntoView({ behavior: "smooth", block: "nearest" }),
        100,
      );
    }
  }
}

function showWelcomeScreen() {
  const welcomeScreen = document.getElementById("welcomeScreen");
  if (welcomeScreen) {
    welcomeScreen.style.display = "flex";
    welcomeScreen.classList.remove("fade-out");
    const greeting = document.getElementById("welcomeGreeting");
    if (greeting) {
      const hour = new Date().getHours();
      let timeGreeting = "Greetings";
      if (hour < 12) timeGreeting = "Good morning";
      else if (hour < 18) timeGreeting = "Good afternoon";
      else timeGreeting = "Good evening";

      let displayName =
        typeof currentUsername !== "undefined" ? currentUsername : "";
      if (
        displayName === "Nikhil" ||
        displayName === "Nikhil080905" ||
        displayName === "nikhil080905@gmail.com"
      ) {
        displayName = "Nikky";
      }
      const name = displayName ? `, ${displayName}` : "";
      greeting.textContent = `${timeGreeting}${name}`;
    }
  }
}

function hideWelcomeScreen(instant = false) {
  const welcomeScreen = document.getElementById("welcomeScreen");
  if (welcomeScreen && welcomeScreen.style.display !== "none") {
    if (instant) {
      welcomeScreen.style.display = "none";
    } else {
      welcomeScreen.classList.add("fade-out");
      setTimeout(() => {
        welcomeScreen.style.display = "none";
      }, 500);
    }
  }
}

async function loadHistory(
  chatIdToLoad = null,
  targetMessageId = null,
  highlightTerm = null,
) {
  if (!messagesDiv) return;
  messagesDiv.innerHTML = "";
  historyLoaded = false;
  setStatus("Loading history...");

  try {
    let url = "/get_history";
    if (chatIdToLoad) {
      url += `?chat_id=${chatIdToLoad}`;
    }
    const response = await fetch(url, { credentials: "include" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();

    if (
      data.history &&
      Array.isArray(data.history) &&
      data.history.length > 0
    ) {
      hideWelcomeScreen(true);
      const ids = new Set();
      data.history.forEach((msg) => {
        if (!msg?.id || ids.has(msg.id)) {
          return;
        }
        ids.add(msg.id);
        const content = msg.message_content || "";
        const msgId = msg.id;
        const messageType = msg.message_type || "stellar";
        const analysisContext = msg.file_analysis_context || null;

        try {
          if (messageType === "user") {
            appendUserMessage(
              content,
              msgId,
              msg.attached_files || [],
              false,
              msg.timestamp,
            );
          } else {
            const isResearch = msg.is_research_output;
            const msgDiv = document.createElement("div");
            msgDiv.classList.add("message", "stellar-msg");
            if (isResearch) msgDiv.classList.add("research-output");
            msgDiv.dataset.id = msgId;

            const contentDiv = document.createElement("div");
            contentDiv.classList.add("message-content");
            msgDiv.rawMarkdownData = content;

            let htmlContent = "";
            let spec = robustParseSpecial(content, "PRESENTATION_DATA:");
            if (spec && spec.json) {
              htmlContent = marked.parse(spec.before || "");
              contentDiv.innerHTML = htmlContent;
              renderPresentationViewer(contentDiv, spec.json);
              if (spec.after && spec.after.trim()) {
                const extraDiv = document.createElement("div");
                extraDiv.innerHTML = marked.parse(spec.after);
                contentDiv.appendChild(extraDiv);
              }
            } else {
              spec = robustParseSpecial(content, "REGENERATED_SLIDE:");
              if (spec && spec.json) {
                htmlContent = marked.parse(spec.before || "");
                contentDiv.innerHTML = htmlContent;
                handleRegeneratedSlide(spec.json);
                if (spec.after && spec.after.trim()) {
                  const extraDiv = document.createElement("div");
                  extraDiv.innerHTML = marked.parse(spec.after);
                  contentDiv.appendChild(extraDiv);
                }
              } else {
                let contentToParse = wrapNakedHtmlBlocks(content || "");
                htmlContent = marked.parse(contentToParse);
                htmlContent = wrapTables(htmlContent);
                contentDiv.innerHTML = htmlContent;
              }
            }

            msgDiv.appendChild(contentDiv);
            unwrapVisuals(contentDiv);
            processCodeBlocks(contentDiv);
            processGenerativeUI(contentDiv);
            if (analysisContext) {
              addAnalysisIndicator(msgDiv, analysisContext);
            }
            addOutputCopyButton(msgDiv);

            if (isResearch) {
              createAndAppendResearchButtons(
                msgDiv,
                msgId,
                content,
                msg.visualization_html,
              );
              if (msg.html_file) {
                const btnsDiv = msgDiv.querySelector(".message-buttons");
                if (btnsDiv) {
                  const viewBtn = document.createElement("a");
                  viewBtn.classList.add("download-btn", "view-html-btn");
                  viewBtn.textContent = "View Paper";
                  viewBtn.href = `/view/${msg.html_file.split("/").pop()}`;
                  viewBtn.target = "_blank";
                  btnsDiv.appendChild(viewBtn);
                }
              }
            }
            if (messagesDiv) {
              const timeDiv = document.createElement("div");
              timeDiv.className = "message-timestamp";
              timeDiv.textContent = formatMsgTime(msg.timestamp);
              msgDiv.appendChild(timeDiv);
              messagesDiv.appendChild(msgDiv);
            }
            setTimeout(() => {
              renderMath(contentDiv);
              const analysisEl = msgDiv.querySelector(
                ".analysis-content .formatted-analysis-context",
              );
              if (analysisEl) renderMath(analysisEl);
            }, 150);
          }
        } catch (error) {
          appendStellarMessage(
            `*Error displaying history message ${msgId}*`,
            msgId + "_err",
          );
        }
      });

      const lastMessage = data.history[data.history.length - 1];
      const lastUserMessage = data.history
        .filter((m) => m.message_type === "user")
        .pop();

      if (lastUserMessage && lastUserMessage.message_content) {
        const lastStellarMessage = data.history[data.history.length - 1];
        if (lastStellarMessage && lastStellarMessage.is_research_output) {
          modeSelector.value = "stellar";
          handleModeChange();
        }
      }
      // --- FIX END ---

      if (targetMessageId) {
        setTimeout(() => {
          const targetElement = messagesDiv.querySelector(
            `.message[data-id="${targetMessageId}"]`,
          );
          if (targetElement) {
            targetElement.scrollIntoView({
              behavior: "smooth",
              block: "center",
            });
            targetElement.classList.add("highlight");
            if (highlightTerm)
              highlightTextInMessage(targetElement, highlightTerm);
            setTimeout(() => {
              targetElement.classList.remove("highlight");
              removeHighlightFromMessage(targetElement);
            }, 2500);
          }
        }, 400);
      } else {
        setTimeout(scrollToBottom, 400);
      }
    } else {
      showWelcomeScreen();
    }
    historyLoaded = true;
  } catch (error) {
    appendStellarMessage(
      `Error loading history: ${error.message}.`,
      Date.now() + "_hist_err",
    );
    setStatus("Error loading history", true);
    historyLoaded = false;
  } finally {
    updateModelSelectTheme();
    updateModelSelectWidth();
    adjustTextareaHeight();
    const idle = !isProcessing && stagedFiles.length === 0;
    if (idle) setStatus("Idle");
    else setStatus(currentStatusText);
  }
}

async function loadChatList() {
  try {
    const response = await fetch("/api/chats");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const chats = await response.json();

    chatList.innerHTML = "";
    if (chats.length > 0) {
      chats.forEach((chat) => {
        const li = document.createElement("li");
        li.className = "chat-item";
        li.dataset.chatId = chat.id;
        li.innerHTML = createChatItemHtml(chat);
        li.addEventListener("click", () => selectChat(chat.id));
        li.querySelector(".delete-chat-btn").addEventListener("click", (e) => {
          e.stopPropagation();
          showConfirmationModal(
            "Delete Chat",
            `Are you sure you want to delete the chat "${chat.name}"? This action cannot be undone.`,
            () => deleteChat(chat.id, chat.name),
          );
        });
        chatList.appendChild(li);
      });
      if (!currentChatId) {
        selectChat(chats[0].id);
      } else {
        try {
          await selectChat(currentChatId);
        } catch (selectErr) {
          console.warn(
            `Failed to select chat ${currentChatId}, falling back to default:`,
            selectErr,
          );
          if (chats[0].id !== currentChatId) {
            selectChat(chats[0].id);
          }
        }
      }
    } else {
      if (currentChatId) {
        try {
          await selectChat(currentChatId);
        } catch (selectErr) {
          await createNewChat(true);
        }
      } else {
        await createNewChat(true);
      }
    }
  } catch (error) {
    setStatus(`Error loading chats: ${error.message}`, true);
  }
}

async function createNewChat(selectNew = true) {
  setStatus("Creating new chat...");
  try {
    const response = await fetch("/api/chats/new", { method: "POST" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const newChat = {
      id: data.chat_id,
      name: "New Chat",
      created_at: new Date().toISOString(),
    };
    const li = document.createElement("li");
    li.className = "chat-item";
    li.dataset.chatId = newChat.id;
    li.innerHTML = createChatItemHtml(newChat); // UPDATED LINE
    li.addEventListener("click", () => selectChat(newChat.id));
    li.querySelector(".delete-chat-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      showConfirmationModal(
        "Delete Chat",
        `Are you sure you want to delete the chat "${newChat.name}"? This action cannot be undone.`,
        () => deleteChat(newChat.id, newChat.name),
      );
    });
    chatList.prepend(li);
    if (selectNew) {
      await selectChat(newChat.id);
      setStatus("New chat created.", false);
    }
  } catch (error) {
    setStatus(`Error creating chat: ${error.message}`, true);
  } finally {
    if (!isProcessing) setStatus("Idle");
  }
}

async function selectChat(chatId) {
  if (currentChatId === chatId && historyLoaded) {
    sidebar.classList.remove("open");
    return;
  }

  if (isProcessing && sseEventSource) {
    sseEventSource.close();
    sseEventSource = null;
  }
  cleanupStream(false, null, null);

  currentChatId = chatId;

  try {
    const response = await fetch("/api/set_active_chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId }),
    });
    if (!response.ok) {
      console.error("Failed to set active chat on backend.");
    }
  } catch (error) {
    console.error("Error setting active chat on backend:", error);
  }

  messagesDiv.innerHTML = "";
  historyLoaded = false;
  setStatus("Switching chat...");

  // Clear and hide the browser pane on chat switch
  const browserPane = document.getElementById("browserPane");
  if (browserPane) {
    browserPane.style.display = "none";
    document.body.classList.remove("browser-open");
    document.getElementById("browserTabsContainer").innerHTML = "";
    document.getElementById("browserContentContainer").innerHTML = "";
    const toggleBtn = document.getElementById("toggleBrowserPaneBtn");
    if (toggleBtn) toggleBtn.style.display = "none";
  }

  document
    .querySelectorAll(".chat-item")
    .forEach((item) => item.classList.remove("active"));
  const selectedItem = chatList.querySelector(
    `.chat-item[data-chat-id="${chatId}"]`,
  );
  if (selectedItem) selectedItem.classList.add("active");

  const targetMessageId = selectedItem?.dataset.messageId || null;
  const highlightTerm = selectedItem?.dataset.searchTerm || null;

  await loadHistory(chatId, targetMessageId, highlightTerm);
  await updateTokenCount();

  sidebar.classList.remove("open");
  if (window.innerWidth <= 768) {
    sidebar.classList.remove("locked");
  }
  setStatus(`Switched to chat.`, false);

  if (selectedItem) {
    delete selectedItem.dataset.messageId;
    delete selectedItem.dataset.searchTerm;
  }

  checkActiveStream(chatId);
}

async function checkActiveStream(chatId) {
  try {
    const res = await fetch(`/api/chats/${chatId}/active_stream`);
    const data = await res.json();
    if (data.query_id && data.mode) {
      console.log("Reconnecting to active stream:", data.query_id);
      reconnectToStream(data.query_id, data.mode, chatId);
    } else if (!isProcessing) {
      setStatus("Idle");
    }
  } catch (e) {
    console.error("Error checking active stream:", e);
    if (!isProcessing) setStatus("Idle");
  }
}

async function deleteChat(chatId, chatName) {
  if (isProcessing && currentChatId === chatId && sseEventSource) {
    sseEventSource.close();
    sseEventSource = null;
    cleanupStream(false, null, null);
  }

  setStatus(`Deleting chat "${chatName}"...`);
  try {
    const response = await fetch(`/api/chats/${chatId}/delete`, {
      method: "DELETE",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (data.success) {
      const deletedItem = chatList.querySelector(
        `.chat-item[data-chat-id="${chatId}"]`,
      );
      if (deletedItem) {
        deletedItem.remove();
      }
      if (currentChatId === chatId) {
        currentChatId = null;
        messagesDiv.innerHTML = "";
        historyLoaded = false;

        await loadChatList();
      }
      setStatus(`Chat "${chatName}" deleted.`, false);
    } else {
      throw new Error(data.message || "Failed to delete chat.");
    }
  } catch (error) {
    setStatus(`Error deleting chat: ${error.message}`, true);
  } finally {
    setTimeout(() => {
      if (!isProcessing) setStatus("Idle");
    }, 2000);
  }
}

function updateChatNameInList(chatId, newName) {
  const chatItem = chatList.querySelector(
    `.chat-item[data-chat-id="${chatId}"] span`,
  );
  if (chatItem) {
    chatItem.textContent = newName;
  }
}

async function updateTokenCount(forceUpdate = false) {
  document.querySelectorAll(".chat-item-token-counter").forEach((el) => {
    el.style.display = "none";
  });

  const activeChatItem = document.querySelector("#chatList .chat-item.active");

  if (!activeChatItem || !currentChatId) {
    return;
  }

  const counterContainer = activeChatItem.querySelector(
    ".chat-item-token-counter",
  );
  const tokenText = activeChatItem.querySelector(".token-text");
  const tokenFill = activeChatItem.querySelector(".token-bar-fill");

  if (!counterContainer || !tokenText || !tokenFill) {
    //
    return;
  }

  try {
    const url = forceUpdate
      ? `/api/chats/${currentChatId}/tokens?refresh=true`
      : `/api/chats/${currentChatId}/tokens`;
    const response = await fetch(url);
    if (!response.ok) {
      console.error("Failed to fetch token count:", response.status);
      return;
    }

    const data = await response.json();
    const tokenCount = data.token_count || 0;

    let percentage = (tokenCount / MAX_TOKEN_LIMIT) * 100;
    percentage = Math.min(100, Math.max(0, percentage));

    tokenText.textContent = `${percentage.toFixed(1)}% Used`;

    tokenFill.style.width = `${percentage}%`;

    if (percentage < 50) {
      tokenFill.style.backgroundColor = "var(--token-bar-fill-low)";
    } else if (percentage < 80) {
      tokenFill.style.backgroundColor = "var(--token-bar-fill-medium)";
    } else {
      tokenFill.style.backgroundColor = "var(--token-bar-fill-high)";
    }

    counterContainer.style.display = "block";
  } catch (error) {
    console.error("Error fetching token count:", error);
  }
}

async function checkAuthStatus() {
  try {
    const response = await fetch("/check_auth");
    const data = await response.json();
    if (data.logged_in) {
      currentUsername = data.display_name || data.username;
      if (data.pfp_url) {
        profileIcon.innerHTML = `<img src="${data.pfp_url}" alt="${currentUsername}">`;
      } else {
        profileIcon.textContent = currentUsername.charAt(0).toUpperCase();
      }
      sidebarUsername.textContent = currentUsername;
      profileUsernameDisplay.textContent = currentUsername;

      document.querySelector("header").style.display = "flex";
      document.querySelector(".header-left").style.display = "flex";
      document.querySelector(".header-right").style.display = "flex";
      chatContainer.style.display = "flex";
      inputContainer.style.display = "flex";
      sidebar.style.display = "flex";
      tokenCountBar.style.display = "block";

      if (data.role === "admin") {
        addAdminControls();
        addAgentGroupChatButton();
      }

      // Set currentChatId from URL param first, fallback to check_auth data, then null
      const urlParams = new URLSearchParams(window.location.search);
      const urlChatId = urlParams.get("chat_id");
      if (urlChatId) {
        currentChatId = parseInt(urlChatId) || null;
      } else if (!currentChatId && data.current_chat_id) {
        currentChatId = parseInt(data.current_chat_id) || null;
      }

      if (!historyLoaded) {
        loadChatList();
      }

      // Start listening to the background user state
      initGlobalEventStream();
    } else {
      window.location.href = "/";
    }
  } catch (error) {
    console.error("Auth check failed:", error);
    window.location.href = "/";
  }
}

function addAdminControls() {
  if (document.getElementById("adminWaitlistBtn")) return;
  const headerRight = document.querySelector(".header-right");
  const waitlistBtn = document.createElement("button");
  waitlistBtn.id = "adminWaitlistBtn";
  waitlistBtn.className = "header-icon-btn";
  waitlistBtn.title = "Admin Dashboard";
  waitlistBtn.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path></svg>';
  waitlistBtn.onclick = openAdminWaitlist;
  const targetBtn =
    document.getElementById("headerNewChatBtn") ||
    document.getElementById("clearHistoryBtn");
  if (targetBtn) {
    headerRight.insertBefore(waitlistBtn, targetBtn);
  } else {
    headerRight.appendChild(waitlistBtn);
  }
}

function addAgentGroupChatButton() {
  if (document.getElementById("agentGroupChatBtn")) return;
  const sidebar = document.getElementById("sidebar");
  if (!sidebar) return;

  const newChatBtn = document.getElementById("newChatBtn");
  if (!newChatBtn) return;

  const groupChatBtn = document.createElement("button");
  groupChatBtn.id = "agentGroupChatBtn";
  // Palette: changed button label to Agent Hub
  groupChatBtn.innerHTML = `
          <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
          </svg>
          Agent Hub
        `;
  groupChatBtn.onclick = () => {
    window.location.href = "/agent-group-chat";
  };

  // Insert after newChatBtn
  newChatBtn.parentNode.insertBefore(groupChatBtn, newChatBtn.nextSibling);
}

const adminWaitlistModal = document.getElementById("adminWaitlistModal");
const waitlistGrid = document.getElementById("waitlistGrid");
const closeAdminWaitlistBtn = document.getElementById("closeAdminWaitlistBtn");

const adminTabUsersBtn = document.getElementById("adminTabUsersBtn");
const adminTabKeysBtn = document.getElementById("adminTabKeysBtn");
const waitlistContent = document.getElementById("waitlistContent");
const keysContent = document.getElementById("keysContent");
const keysGrid = document.getElementById("keysGrid");

let activeAdminTab = "users";

if (adminTabUsersBtn && adminTabKeysBtn) {
  adminTabUsersBtn.onclick = () => {
    activeAdminTab = "users";
    adminTabUsersBtn.style.background = "rgba(255, 255, 255, 0.05)";
    adminTabUsersBtn.style.borderColor = "rgba(255, 255, 255, 0.1)";
    adminTabUsersBtn.style.color = "#fff";

    adminTabKeysBtn.style.background = "transparent";
    adminTabKeysBtn.style.borderColor = "transparent";
    adminTabKeysBtn.style.color = "#888";

    waitlistContent.style.display = "block";
    keysContent.style.display = "none";
    loadWaitlist();
  };

  adminTabKeysBtn.onclick = () => {
    activeAdminTab = "keys";
    adminTabKeysBtn.style.background = "rgba(255, 255, 255, 0.05)";
    adminTabKeysBtn.style.borderColor = "rgba(255, 255, 255, 0.1)";
    adminTabKeysBtn.style.color = "#fff";

    adminTabUsersBtn.style.background = "transparent";
    adminTabUsersBtn.style.borderColor = "transparent";
    adminTabUsersBtn.style.color = "#888";

    waitlistContent.style.display = "none";
    keysContent.style.display = "block";
    loadKeyHealth();
  };
}

// ── Key Health: countdown + SSE state ────────────────────────────────────────
let keyHealthCountdownInterval = null;
let keyHealthEventSource = null;

function formatKeyCountdown(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function recomputeCardBadge(card) {
  const badge = card.querySelector(".key-status-badge");
  if (!badge) return;
  const statuses = [...card.querySelectorAll(".key-model-status")];
  const hasRpd = statuses.some((el) => el.classList.contains("blocked-rpd"));
  const hasBlocked = statuses.some((el) => !el.classList.contains("active"));
  const globalCountdown = card.querySelector(".key-global-countdown");
  const hasGlobal = globalCountdown !== null;
  if (hasGlobal) return; // global block overrides — handled separately
  if (hasRpd) {
    badge.className = "key-status-badge blocked";
    badge.textContent = "Daily Quota Out";
  } else if (hasBlocked) {
    badge.className = "key-status-badge limited";
    badge.textContent = "Partially Blocked";
  } else {
    badge.className = "key-status-badge active";
    badge.textContent = "Active";
  }
}

function recoverKeyScope(card, scope) {
  if (scope === "global") {
    card.querySelector(".key-global-remaining")?.remove();
    // Only clear badge if no model blocks remain
    const anyModelBlocked = [
      ...card.querySelectorAll(".key-model-status"),
    ].some((el) => !el.classList.contains("active"));
    const badge = card.querySelector(".key-status-badge");
    if (badge && !anyModelBlocked) {
      badge.className = "key-status-badge active";
      badge.textContent = "Active";
    } else if (badge && anyModelBlocked) {
      badge.className = "key-status-badge limited";
      badge.textContent = "Partially Blocked";
    }
  } else {
    const row = card.querySelector(
      `.key-model-row[data-scope="${CSS.escape(scope)}"]`,
    );
    if (row) {
      const st = row.querySelector(".key-model-status");
      if (st) {
        st.className = "key-model-status active";
        st.textContent = "Active";
      }
      row.querySelector(".key-model-remaining")?.remove();
    }
    recomputeCardBadge(card);
  }
}

function applyKeyBlock(card, scope, blockedUntil, reason) {
  if (scope === "global") {
    const badge = card.querySelector(".key-status-badge");
    if (badge) {
      badge.className = "key-status-badge blocked";
      badge.textContent = `Global Block (${reason})`;
    }
    let globalRem = card.querySelector(".key-global-remaining");
    if (!globalRem) {
      globalRem = document.createElement("div");
      globalRem.className = "key-global-remaining";
      globalRem.style.cssText =
        "font-size:0.8rem;color:#FF2A4D;margin-top:5px;";
      card.querySelector(".key-info")?.appendChild(globalRem);
    }
    globalRem.innerHTML = `Block expires in: <span class="key-global-countdown key-countdown-el"
      data-blocked-until="${blockedUntil}" data-scope="global"></span>`;
  } else {
    const row = card.querySelector(
      `.key-model-row[data-scope="${CSS.escape(scope)}"]`,
    );
    if (!row) return;
    const st = row.querySelector(".key-model-status");
    if (st) {
      if (reason === "RPM" || reason === "OVERLOAD") {
        st.className = "key-model-status blocked-rpm";
        st.textContent = "Rate Limited (RPM)";
      } else if (reason === "RPD") {
        st.className = "key-model-status blocked-rpd";
        st.textContent = "Quota Exceeded (RPD)";
      } else {
        st.className = "key-model-status blocked-other";
        st.textContent = `Blocked (${reason})`;
      }
    }
    let cdEl = row.querySelector(".key-model-remaining");
    if (!cdEl) {
      cdEl = document.createElement("span");
      cdEl.className = "key-model-remaining key-countdown-el";
      row.querySelector(".key-model-status-wrap")?.appendChild(cdEl);
    }
    cdEl.dataset.blockedUntil = blockedUntil;
    cdEl.dataset.scope = scope;
    recomputeCardBadge(card);
  }
}

function startKeyCountdownTimer() {
  if (keyHealthCountdownInterval) clearInterval(keyHealthCountdownInterval);
  keyHealthCountdownInterval = setInterval(() => {
    const now = Date.now() / 1000;
    document
      .querySelectorAll(".key-countdown-el[data-blocked-until]")
      .forEach((el) => {
        const until = parseFloat(el.dataset.blockedUntil);
        const remaining = until - now;
        if (remaining <= 0) {
          const card = el.closest(".key-card");
          const scope = el.dataset.scope;
          if (card && scope) recoverKeyScope(card, scope);
        } else {
          el.textContent = `(${formatKeyCountdown(remaining)} left)`;
        }
      });
  }, 1000);
}

function connectKeyHealthSSE() {
  if (keyHealthEventSource) {
    keyHealthEventSource.close();
    keyHealthEventSource = null;
  }
  const es = new EventSource("/api/admin/keys/stream");
  keyHealthEventSource = es;

  es.onmessage = (e) => {
    let data;
    try {
      data = JSON.parse(e.data);
    } catch {
      return;
    }
    if (data.type === "heartbeat" || data.type === "connected") return;

    const card = document.querySelector(
      `.key-card[data-key-hash="${data.key_hash}"]`,
    );
    if (!card) return;

    if (data.type === "key_blocked") {
      applyKeyBlock(card, data.scope, data.blocked_until, data.reason);
    } else if (data.type === "key_recovered") {
      // Remove countdown element so the timer loop stops trying
      const cdEl = card.querySelector(
        `.key-countdown-el[data-scope="${data.scope}"]`,
      );
      if (cdEl) cdEl.dataset.blockedUntil = "0";
      recoverKeyScope(card, data.scope);
    }
  };

  es.onerror = () => {
    es.close();
    keyHealthEventSource = null;
    // Reconnect after 5 s if the tab is still on Keys
    setTimeout(() => {
      if (activeAdminTab === "keys") connectKeyHealthSSE();
    }, 5000);
  };
}

function stopKeyHealth() {
  if (keyHealthEventSource) {
    keyHealthEventSource.close();
    keyHealthEventSource = null;
  }
  if (keyHealthCountdownInterval) {
    clearInterval(keyHealthCountdownInterval);
    keyHealthCountdownInterval = null;
  }
}
// ─────────────────────────────────────────────────────────────────────────────

async function loadKeyHealth() {
  keysGrid.innerHTML =
    '<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: #666;">Querying key states...</div>';
  stopKeyHealth(); // clear any previous SSE / timer before rebuilding
  try {
    const response = await fetch("/api/admin/keys");
    const data = await response.json();
    keysGrid.innerHTML = "";
    if (data.length === 0) {
      keysGrid.innerHTML =
        '<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: #666;">No API keys configured.</div>';
    } else {
      const now = Date.now() / 1000;
      data.forEach((keyData) => {
        const card = document.createElement("div");
        card.className = "key-card";
        card.dataset.keyHash = keyData.key_hash || "";

        const globalBlock = keyData.blocks.global || {
          blocked: false,
          reason: null,
          remaining_seconds: 0,
          blocked_until: 0,
        };

        let badgeClass = "active",
          badgeLabel = "Active";
        let hasAnyBlock = globalBlock.blocked;
        let hasRpm = globalBlock.reason === "RPM";
        let hasRpd =
          globalBlock.reason === "RPD" || globalBlock.reason === "INVALID";

        const modelNamesMap = {
          "gemini-3.1-flash-lite": "Emerald (Flash-Lite)",
          "gemma-4-31b-it": "Lunarity (Gemma-4)",
          "gemini-3-flash-preview": "Crimson (Gemini-3)",
          "gemini-3.5-flash": "Obsidian (Gemini-3.5)",
        };
        const orderedModelIds = [
          "gemini-3.5-flash",
          "gemini-3-flash-preview",
          "gemma-4-31b-it",
          "gemini-3.1-flash-lite",
        ];

        const modelRowsHtml = [];
        orderedModelIds.forEach((modelId) => {
          const status = keyData.blocks[modelId];
          if (!status) return;
          const friendlyName = modelNamesMap[modelId] || modelId;
          let statusClass = "active",
            statusLabel = "Active";
          let cdAttr = "";
          if (status.blocked) {
            hasAnyBlock = true;
            if (status.reason === "RPM" || status.reason === "OVERLOAD") {
              statusClass = "blocked-rpm";
              statusLabel = "Rate Limited (RPM)";
              hasRpm = true;
            } else if (status.reason === "RPD") {
              statusClass = "blocked-rpd";
              statusLabel = "Quota Exceeded (RPD)";
              hasRpd = true;
            } else {
              statusClass = "blocked-other";
              statusLabel = `Blocked (${status.reason})`;
              hasRpm = true;
            }
            if (status.blocked_until > now) {
              cdAttr = `data-blocked-until="${status.blocked_until}" data-scope="${modelId}"`;
            }
          }
          const cdHtml =
            status.blocked && status.blocked_until > now
              ? `<span class="key-model-remaining key-countdown-el" ${cdAttr}>(${formatKeyCountdown(status.blocked_until - now)} left)</span>`
              : "";
          modelRowsHtml.push(`
            <div class="key-model-row key-model-item" data-scope="${modelId}">
              <span class="key-model-name">${friendlyName}</span>
              <div class="key-model-status-wrap">
                <span class="key-model-status ${statusClass}">${statusLabel}</span>
                ${cdHtml}
              </div>
            </div>`);
        });

        if (globalBlock.blocked) {
          badgeClass = "blocked";
          badgeLabel = `Global Block (${globalBlock.reason})`;
        } else if (hasRpd) {
          badgeClass = "blocked";
          badgeLabel = "Daily Quota Out";
        } else if (hasAnyBlock) {
          badgeClass = "limited";
          badgeLabel = "Partially Blocked";
        }

        let globalRemHtml = "";
        if (globalBlock.blocked && globalBlock.blocked_until > now) {
          const cdAttr = `data-blocked-until="${globalBlock.blocked_until}" data-scope="global"`;
          globalRemHtml = `<div class="key-global-remaining" style="font-size:0.8rem;color:#FF2A4D;margin-top:5px;">
            Block expires in: <span class="key-global-countdown key-countdown-el" ${cdAttr}>${formatKeyCountdown(globalBlock.blocked_until - now)} left</span>
          </div>`;
        }

        card.innerHTML = `
          <div class="key-card-header">
            <div class="key-info">
              <span class="key-name">${keyData.label}</span>
              <span class="key-masked">${keyData.masked}</span>
              ${globalRemHtml}
            </div>
            <span class="key-status-badge ${badgeClass}">${badgeLabel}</span>
          </div>
          <div class="key-models-list">
            <div style="font-size:0.75rem;color:#555;text-transform:uppercase;font-weight:600;letter-spacing:0.5px;border-bottom:1px solid rgba(255,255,255,0.03);padding-bottom:5px;margin-bottom:5px;">Model Statuses</div>
            ${modelRowsHtml.join("")}
          </div>`;
        keysGrid.appendChild(card);
      });

      startKeyCountdownTimer();
      connectKeyHealthSSE();
    }
  } catch (err) {
    keysGrid.innerHTML =
      '<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: #FF2A4D;">Critical error fetching API keys status.</div>';
  }
}

if (closeAdminWaitlistBtn) {
  closeAdminWaitlistBtn.onclick = () => {
    adminWaitlistModal.style.display = "none";
    document.body.style.overflow = "";
    stopKeyHealth(); // close SSE connection and countdown timer
  };
}

async function openAdminWaitlist() {
  adminWaitlistModal.style.display = "flex";
  document.body.style.overflow = "hidden";
  if (activeAdminTab === "keys") {
    loadKeyHealth();
  } else {
    loadWaitlist();
  }
}

function formatMsgTime(tsString) {
  if (!tsString) {
    return new Date().toLocaleString([], {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  // Handle SQLite UTC dates safely
  const d = new Date(
    tsString.includes("Z") ? tsString : tsString.replace(" ", "T") + "Z",
  );
  return isNaN(d)
    ? tsString
    : d.toLocaleString([], {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}
function formatTokens(tokens) {
  if (!tokens) return "0";
  if (tokens < 1000) return tokens;
  if (tokens < 1000000) return (tokens / 1000).toFixed(1) + "K";
  return (tokens / 1000000).toFixed(1) + "M";
}

function formatDateTime(utcStr) {
  if (!utcStr || utcStr === "Never") return utcStr;
  const date = new Date(utcStr + " UTC");
  if (isNaN(date.getTime())) return utcStr;

  const options = {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  };

  const formatter = new Intl.DateTimeFormat("en-IN", options);
  const parts = formatter.formatToParts(date);

  let day, month, year, hour, minute, dayPeriod;
  parts.forEach((p) => {
    if (p.type === "day") day = p.value;
    if (p.type === "month") month = p.value;
    if (p.type === "year") year = p.value;
    if (p.type === "hour") hour = p.value;
    if (p.type === "minute") minute = p.value;
    if (p.type === "dayPeriod") dayPeriod = p.value;
  });

  const getOrdinal = (n) => {
    const s = ["th", "st", "nd", "rd"],
      v = n % 100;
    return n + (s[(v - 20) % 10] || s[v] || s[0]);
  };

  return `${getOrdinal(parseInt(day))} ${month} ${year}, ${hour}:${minute} ${dayPeriod}`;
}

async function loadWaitlist() {
  waitlistGrid.innerHTML =
    '<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: #666;">Initializing grid...</div>';
  try {
    const response = await fetch("/api/admin/waitlist");
    const data = await response.json();
    waitlistGrid.innerHTML = "";
    if (data.length === 0) {
      waitlistGrid.innerHTML =
        '<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: #666;">No entities found.</div>';
    } else {
      data.forEach((user) => {
        const card = document.createElement("div");
        card.className = "user-card";

        const rawName = user.display_name || user.username.split("@")[0];
        const name = rawName;
        const email = user.username;
        const lastActive = formatDateTime(user.last_active || "Never");
        const chats = user.num_chats || 0;
        const projects = user.num_projects || 0;
        const tokens = formatTokens(user.total_tokens_approx);

        const isSelf = user.username === sidebarUsername.textContent; // Using sidebarUsername as it holds current user's name/email
        const isAdmin = user.role === "admin";
        const toggleHtml = isAdmin
          ? `<span class="admin-badge">Admin</span>`
          : `<label class="admin-toggle" title="${user.is_approved ? "Disable Access" : "Enable Access"}">
                                  <input type="checkbox" ${user.is_approved ? "checked" : ""} onchange="toggleUserAccess(${user.id}, this.checked)">
                                  <span class="admin-slider"></span>
                              </label>`;

        card.innerHTML = `
                              <div class="user-card-header">
                                  <div class="user-info">
                                      <span class="user-name" title="${rawName}">${name}</span>
                                      <span class="user-email">${email}</span>
                                  </div>
                                  ${toggleHtml}
                              </div>
                              <div class="user-stats">
                                  <div class="stat-item">
                                      <span class="stat-value">${chats}</span>
                                      <span class="stat-label">Chats</span>
                                  </div>
                                  <div class="stat-item">
                                      <span class="stat-value">${projects}</span>
                                      <span class="stat-label">Projects</span>
                                  </div>
                                  <div class="stat-item">
                                      <span class="stat-value">${tokens}</span>
                                      <span class="stat-label">Tokens</span>
                                  </div>
                              </div>
                              <div class="user-meta">
                                  <span>Last active: ${lastActive}</span>
                              </div>
                              ${
                                !isAdmin
                                  ? user.waitlist_form_submitted
                                    ? `
                              <div class="user-waitlist-details" style="border-top: 1px solid rgba(255, 255, 255, 0.05); padding-top: 12px; margin-top: 4px; display: flex; flex-direction: column; gap: 8px; text-align: left;">
                                  <div style="font-size: 0.8rem; line-height: 1.3;"><strong style="color: #bbb;">Role:</strong> <span style="color: #fff;">${escapeHtml(user.designation || "N/A")}</span></div>
                                  <div style="font-size: 0.8rem; line-height: 1.3;"><strong style="color: #bbb;">Source:</strong> <span style="color: #fff;">${escapeHtml(user.source || "N/A")}</span></div>
                                  <div style="font-size: 0.8rem; line-height: 1.3; display: flex; flex-direction: column; gap: 2px;">
                                      <strong style="color: #bbb;">Use Case:</strong>
                                      <p style="color: #888; margin: 0; font-size: 0.75rem; line-height: 1.4; white-space: normal; word-break: break-word;">${escapeHtml(user.use_case || "N/A")}</p>
                                  </div>
                              </div>
                              `
                                    : `
                              <div class="user-waitlist-details" style="border-top: 1px solid rgba(255, 255, 255, 0.05); padding-top: 12px; margin-top: 4px; font-size: 0.8rem; color: #666; font-style: italic; text-align: left;">
                                  No waitlist information submitted.
                              </div>
                              `
                                  : ""
                              }
                              <div class="user-actions">
                                  <button class="impersonate-btn" onclick="impersonateUser(${user.id})">Impersonate</button>
                              </div>
                          `;
        waitlistGrid.appendChild(card);
      });
    }
  } catch (err) {
    waitlistGrid.innerHTML =
      '<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: #FF2A4D;">Critical error fetching waitlist.</div>';
  }
}

window.toggleUserAccess = async function (userId, isApproved) {
  try {
    const response = await fetch("/api/admin/toggle_access", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, is_approved: isApproved }),
    });
    const data = await response.json();
    if (!data.success) {
      alert("Status update failed: " + data.error);
      loadWaitlist(); // Revert UI
    }
  } catch (err) {
    alert("Connection error");
    loadWaitlist(); // Revert UI
  }
};

window.impersonateUser = async function (userId) {
  try {
    const response = await fetch("/api/admin/impersonate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId }),
    });
    const data = await response.json();
    if (data.success) {
      window.location.reload();
    } else {
      alert("Impersonation failed: " + data.error);
    }
  } catch (err) {
    alert("Connection error");
  }
};

window.approveUser = async function (userId, username) {
  const btn = event.target;
  btn.disabled = true;
  btn.innerText = "PROVISIONING...";
  try {
    const response = await fetch("/api/admin/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, email: username }),
    });
    const data = await response.json();
    if (data.success) {
      loadWaitlist();
    } else {
      alert("Approval failed: " + data.error);
      btn.disabled = false;
      btn.innerText = "Approve User";
    }
  } catch (err) {
    alert("Connection error");
    btn.disabled = false;
    btn.innerText = "Approve User";
  }
};

async function handleLogout() {
  try {
    const response = await fetch("/logout", { method: "POST" });
    const data = await response.json();
    if (data.success) {
      window.location.href = "/";
    } else {
      setStatus(data.message || "Logout failed.", true);
    }
  } catch (error) {
    setStatus("An error occurred during logout.", true);
  }
}

async function handleChangePassword(event) {
  event.preventDefault();
  const newDisplayName = newDisplayNameInput.value.trim();
  displayModalMessage(passwordChangeMessage, "", false);

  if (!newDisplayName) {
    displayModalMessage(
      passwordChangeMessage,
      "New display name is required.",
      true,
    );
    return;
  }

  try {
    const response = await fetch("/api/user/change_display_name", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_display_name: newDisplayName }),
    });
    const data = await response.json();
    if (data.success) {
      displayModalMessage(
        passwordChangeMessage,
        "Display name changed successfully!",
        false,
      );
      newDisplayNameInput.value = "";
      checkAuthStatus(); // Refresh UI to show new name
    } else {
      displayModalMessage(
        passwordChangeMessage,
        data.message || "Failed to change name.",
        true,
      );
    }
  } catch (error) {
    displayModalMessage(passwordChangeMessage, "An error occurred.", true);
  }
}

function highlightTextInMessage(messageElement, textToHighlight) {
  const contentDiv = messageElement.querySelector(".message-content");
  if (!contentDiv || !textToHighlight) return;

  // Safe regex escaping to handle special characters in textToHighlight
  const regexSafeTerm = textToHighlight.replace(
    /[/\-\\^$*+?.()|[\]{}]/g,
    "\\$&",
  );

  // Protect user messages: perform safe direct DOM highlight manipulation
  // to avoid reading/writing innerHTML and causing XSS or HTML/style unwrap leaks.
  const isUserMsg =
    messageElement.classList.contains("user-msg") ||
    messageElement.closest(".user-msg");
  if (isUserMsg) {
    const walk = document.createTreeWalker(
      contentDiv,
      NodeFilter.SHOW_TEXT,
      null,
      false,
    );
    let node;
    const nodesToReplace = [];

    while ((node = walk.nextNode())) {
      const parentTag = node.parentNode.tagName.toLowerCase();
      if (
        parentTag === "code" ||
        parentTag === "pre" ||
        parentTag === "script" ||
        node.parentNode.classList.contains("highlighted-text")
      ) {
        continue;
      }

      const regex = new RegExp(regexSafeTerm, "gi");
      if (node.nodeValue.match(regex)) {
        nodesToReplace.push(node);
      }
    }

    nodesToReplace.forEach((node) => {
      const text = node.nodeValue;
      const regex = new RegExp(regexSafeTerm, "gi");
      const parent = node.parentNode;

      let lastIndex = 0;
      let match;
      const fragment = document.createDocumentFragment();

      while ((match = regex.exec(text)) !== null) {
        if (match.index > lastIndex) {
          fragment.appendChild(
            document.createTextNode(text.substring(lastIndex, match.index)),
          );
        }
        const span = document.createElement("span");
        span.className = "highlighted-text";
        span.textContent = match[0];
        fragment.appendChild(span);
        lastIndex = regex.lastIndex;
        if (regex.lastIndex === match.index) {
          regex.lastIndex++;
        }
      }

      if (lastIndex < text.length) {
        fragment.appendChild(
          document.createTextNode(text.substring(lastIndex)),
        );
      }

      parent.replaceChild(fragment, node);
    });
    return;
  }

  const tempDiv = document.createElement("div");
  tempDiv.innerHTML = contentDiv.innerHTML;

  const walk = document.createTreeWalker(
    tempDiv,
    NodeFilter.SHOW_TEXT,
    null,
    false,
  );
  let node;
  const nodesToReplace = [];

  while ((node = walk.nextNode())) {
    const parentTag = node.parentNode.tagName.toLowerCase();

    if (
      parentTag === "code" ||
      parentTag === "pre" ||
      parentTag === "script" ||
      node.parentNode.classList.contains("highlighted-text")
    ) {
      continue;
    }

    const regex = new RegExp(regexSafeTerm, "gi");
    if (node.nodeValue.match(regex)) {
      nodesToReplace.push(node);
    }
  }

  nodesToReplace.forEach((node) => {
    const span = document.createElement("span");
    span.innerHTML = node.nodeValue.replace(
      new RegExp(regexSafeTerm, "gi"),
      (match) => `<span class="">${match}</span>`,
    );
    node.parentNode.replaceChild(span, node);
  });

  contentDiv.innerHTML = tempDiv.innerHTML;
}
function removeHighlightFromMessage(messageElement) {
  const contentDiv = messageElement.querySelector(".message-content");
  if (!contentDiv) return;

  const highlightedSpans = contentDiv.querySelectorAll(".highlighted-text");
  highlightedSpans.forEach((span) => {
    const parent = span.parentNode;
    while (span.firstChild) {
      parent.insertBefore(span.firstChild, span);
    }
    parent.removeChild(span);
  });
}

function displayLoginMessage(message, isError) {
  const cardHeader = document.querySelector(".auth-card-header");
  if (!cardHeader) return;

  const existingMsg = cardHeader.querySelector(".auth-message");
  if (existingMsg) existingMsg.remove();

  if (!message) return;

  const messageEl = document.createElement("div");
  messageEl.textContent = message;
  messageEl.className = "auth-message";
  messageEl.classList.add(isError ? "error" : "success");

  cardHeader.appendChild(messageEl);
}

function displayModalMessage(element, message, isError) {
  element.textContent = message;
  element.classList.remove("success", "error");
  if (message) {
    element.style.display = "block";
    element.classList.add(isError ? "error" : "success");
  } else {
    element.style.display = "none";
  }
}

let isSidebarLocked = localStorage.getItem("stellar_sidebar_locked") === "true";
if (isSidebarLocked) {
  sidebar.classList.add("open");
  sidebar.classList.add("locked");
}

function toggleSidebar() {
  if (window.innerWidth <= 768) {
    const isOpen =
      sidebar.classList.contains("open") ||
      sidebar.classList.contains("locked");
    if (isOpen) {
      sidebar.classList.remove("open");
      sidebar.classList.remove("locked");
    } else {
      sidebar.classList.add("open");
      sidebar.classList.remove("locked");
    }
    return;
  }

  isSidebarLocked = !isSidebarLocked;
  localStorage.setItem("stellar_sidebar_locked", isSidebarLocked);
  if (isSidebarLocked) {
    sidebar.classList.add("open");
    sidebar.classList.add("locked");
  } else {
    sidebar.classList.remove("open");
    sidebar.classList.remove("locked");
  }
}

function showProfileModal() {
  profileModal.style.display = "flex";
  if (newDisplayNameInput) newDisplayNameInput.value = "";
  displayModalMessage(passwordChangeMessage, "", false);
}

function hideProfileModal() {
  profileModal.style.display = "none";
  hideAgentSettingsModal();
}

function showAgentSettingsModal() {
  agentSettingsModal.style.display = "flex";
  document.body.style.overflow = "hidden";
}

function hideAgentSettingsModal() {
  agentSettingsModal.style.display = "none";
  document.body.style.overflow = "";
}

if (sendBtn) sendBtn.addEventListener("click", () => handleSend());
if (chatInput) {
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  });
  chatInput.addEventListener("input", adjustTextareaHeight);
  chatInput.addEventListener("input", () => {
    toggleSendStopButtons(isProcessing);
  });
}
if (modeSelector) modeSelector.addEventListener("change", handleModeChange);
if (modelSelect) {
  modelSelect.addEventListener("change", () => {
    updateModelSelectWidth();
    updateModelSelectTheme();
    enableDisableModelOptions();
    localStorage.setItem("preferredModel", modelSelect.value);
  });
}
if (cancelEditBtn) cancelEditBtn.addEventListener("click", hideEditModal);
if (saveEditBtn) {
  saveEditBtn.addEventListener("click", () => {
    if (currentEditingMsg && currentEditingMsgId) {
      const md = editMarkdownInput.value;
      let html = "";
      try {
        html = marked.parse(md || "");
        html = wrapTables(html);
      } catch (e) {
        html = "<p>Error parsing.</p>";
      }
      const contentDiv = currentEditingMsg.querySelector(".message-content");
      if (contentDiv) {
        currentEditingMsg.rawMarkdownData = md;
        contentDiv.innerHTML = html;
        processCodeBlocks(contentDiv);
        processGenerativeUI(contentDiv);
        setTimeout(() => renderMath(contentDiv), 150);
        createAndAppendResearchButtons(
          currentEditingMsg,
          currentEditingMsgId,
          contentDiv.innerHTML,
        );
        addOutputCopyButton(currentEditingMsg);
      }
    }
    hideEditModal();
  });
}
// REPLACE the old clearHistoryBtn block with this one
if (clearHistoryBtn) {
  clearHistoryBtn.addEventListener("click", () => {
    showConfirmationModal(
      "Clear Chat History",
      "Are you sure you want to clear the entire chat history for this chat? This action cannot be undone.",
      async () => {
        if (isProcessing && sseEventSource) {
          sseEventSource.close();
          sseEventSource = null;
        }
        cleanupStream(false, null, null);

        setStatus("Clearing...");
        try {
          const r = await fetch("/clear_history", { method: "POST" });
          if (!r.ok) {
            let m = `HTTP ${r.status}`;
            try {
              m = (await r.json()).message || m;
            } catch (e) {}
            throw new Error(m);
          }
          const d = await r.json();
          if (d.status !== "Success") throw new Error(d.message || "Fail");

          lastRefinedQuery = "";
          historyLoaded = false;
          stagedFiles = [];
          updateStagedFilesUI();
          messagesDiv.innerHTML = "";

          modeSelector.value = "stellar";
          handleModeChange();

          await loadHistory();

          setStatus("History cleared");
          setTimeout(() => {
            const shouldBeIdle = !isProcessing && stagedFiles.length === 0;
            if (shouldBeIdle) setStatus("Idle");
          }, 1500);
        } catch (err) {
          appendStellarMessage(
            `Clear err: ${err.message}`,
            Date.now() + "_clr_err",
          );
          setStatus("Error clear", true);
          setTimeout(() => setStatus(currentStatusText, false), 3000);
        }
      },
    );
  });
}

const exportChatBtn = document.getElementById("exportChatBtn");
if (exportChatBtn) {
  exportChatBtn.addEventListener("click", async () => {
    if (!currentChatId) {
      alert("No active chat to export.");
      return;
    }
    setStatus("Exporting chat...");
    try {
      const url = `/get_history?chat_id=${currentChatId}`;
      const response = await fetch(url, { credentials: "include" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();

      if (!data.history || data.history.length === 0) {
        alert("No messages to export in this chat.");
        setStatus("Idle");
        return;
      }

      // Create download
      const jsonString = JSON.stringify(data.history, null, 2);
      const blob = new Blob([jsonString], { type: "application/json" });
      const downloadUrl = URL.createObjectURL(blob);

      // Get chat name if possible
      let chatName = "chat_export";
      const chatItem = document.querySelector(
        `.chat-item[data-chat-id="${currentChatId}"] span`,
      );
      if (chatItem) {
        chatName = chatItem.textContent
          .trim()
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, "_");
      }

      const a = document.createElement("a");
      a.href = downloadUrl;
      a.download = `${chatName}_${currentChatId}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(downloadUrl);
      setStatus("Exported successfully");
      setTimeout(() => {
        const shouldBeIdle = !isProcessing && stagedFiles.length === 0;
        if (shouldBeIdle) setStatus("Idle");
      }, 1500);
    } catch (err) {
      console.error("Export failed:", err);
      alert(`Failed to export chat: ${err.message}`);
      setStatus("Idle");
    }
  });
}

if (fileUploadInput) {
  fileUploadInput.addEventListener("change", handleFileUpload);
}

let sidebarHoverTimeout;

// Expand trigger area to the entire left edge of screen (Desktop only)
document.addEventListener("mousemove", (e) => {
  if (window.innerWidth <= 768) return; // Disable edge-trigger on mobile
  if (e.clientX <= 40) {
    // Using 40px for a generous edge trigger
    clearTimeout(sidebarHoverTimeout);
    sidebar.classList.add("open");
  }
});

sidebarToggleBtn.addEventListener("mouseenter", () => {
  if (window.innerWidth <= 768) return; // Disable hover trigger on mobile
  clearTimeout(sidebarHoverTimeout);
  sidebar.classList.add("open");
});

sidebarToggleBtn.addEventListener("mouseleave", () => {
  if (window.innerWidth <= 768) return; // Disable hover trigger on mobile
  if (isSidebarLocked) return;
  sidebarHoverTimeout = setTimeout(() => {
    if (sidebar.classList.contains("open")) {
      sidebar.classList.remove("open");
    }
  }, 150);
});

sidebar.addEventListener("mouseenter", () => {
  if (window.innerWidth <= 768) return; // Disable hover trigger on mobile
  clearTimeout(sidebarHoverTimeout);
});

sidebar.addEventListener("mouseleave", () => {
  if (window.innerWidth <= 768) return; // Disable hover trigger on mobile
  if (isSidebarLocked) return;
  sidebarHoverTimeout = setTimeout(() => {
    if (sidebar.classList.contains("open")) {
      sidebar.classList.remove("open");
    }
  }, 150);
});

// Keep click for mobile fallback
sidebarToggleBtn.addEventListener("click", toggleSidebar);
sidebarCloseBtn.addEventListener("click", toggleSidebar);
newChatBtn.addEventListener("click", createNewChat);

const headerTempChatBtn = document.getElementById("headerTempChatBtn");
if (headerTempChatBtn) {
  headerTempChatBtn.addEventListener("click", async () => {
    if (isProcessing && sseEventSource) {
      sseEventSource.close();
      sseEventSource = null;
    }
    cleanupStream(false, null, null);

    setStatus("Starting Incognito Session...");
    try {
      const response = await fetch("/api/chats/new_temp", {
        method: "POST",
      });
      if (!response.ok) throw new Error("Failed to create temporary chat");
      const data = await response.json();

      // Deselect active chat in the sidebar
      document
        .querySelectorAll(".chat-item")
        .forEach((item) => item.classList.remove("active"));

      // Set backend to new temp chat ID
      currentChatId = data.chat_id;

      // Clear the frontend UI
      messagesDiv.innerHTML = "";
      historyLoaded = true; // Set true so it doesn't auto-load past DB history
      hideWelcomeScreen(true);

      // Inject intro message
      const tempWelcome =
        "**Incognito Mode Enabled**.\n\nYou are now in a temporary, isolated session. This conversation is hidden from the sidebar, and will be permanently deleted when you start a new temp chat.";
      appendStellarMessage(tempWelcome, "temp-welcome-msg");

      setStatus("Incognito Mode Active", false);
      setTimeout(() => setStatus("Idle"), 2500);

      // Close sidebar on mobile
      if (window.innerWidth <= 768) {
        sidebar.classList.remove("open", "locked");
      }
    } catch (err) {
      setStatus("Error starting temp chat", true);
    }
  });
}
profileIcon.addEventListener("click", showProfileModal);
profileCloseBtn.addEventListener("click", hideProfileModal);
openAgentSettingsBtn.addEventListener("click", showAgentSettingsModal);
agentSettingsCloseBtn.addEventListener("click", hideAgentSettingsModal);

// Bind PWA profile install action
const pwaProfileInstallBtn = document.getElementById("pwaProfileInstallBtn");
if (pwaProfileInstallBtn) {
  const isStandalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    navigator.standalone;
  if (!isStandalone && localStorage.getItem("pwa_installed") !== "true") {
    pwaProfileInstallBtn.style.display = "block";
  }

  pwaProfileInstallBtn.addEventListener("click", () => {
    if (deferredPrompt) {
      deferredPrompt.prompt();
      deferredPrompt.userChoice.then((choiceResult) => {
        if (choiceResult.outcome === "accepted") {
          localStorage.setItem("pwa_installed", "true");
          pwaProfileInstallBtn.style.display = "none";
        }
        deferredPrompt = null;
      });
    } else {
      alert(
        "To install Stellar, tap your browser's menu (three dots or share button) and select 'Add to Home screen' or 'Install App'.",
      );
    }
  });
}

window.addEventListener("click", function (event) {
  if (event.target === profileModal) {
    hideProfileModal();
  }
  if (event.target === agentSettingsModal) {
    hideAgentSettingsModal();
  }
});

window.addEventListener("beforeunload", () => {
  if (typeof audioContext !== "undefined" && audioContext) stopListening(false);
});

changePasswordForm.addEventListener("submit", handleChangePassword);
logoutButtonProfile.addEventListener("click", handleLogout);
document.getElementById("logoutButton").addEventListener("click", handleLogout);
if (chatScrollToBottomBtn) {
  chatScrollToBottomBtn.addEventListener("click", scrollToBottom);
}

function toggleScrollButton() {
  if (!chatScrollToBottomBtn) return;
  const threshold = 150; // Pixels from bottom to hide the button

  let isScrollable = false;
  let distanceToBottom = 0;

  if (messagesDiv && messagesDiv.scrollHeight > messagesDiv.clientHeight) {
    isScrollable = true;
    distanceToBottom =
      messagesDiv.scrollHeight -
      messagesDiv.scrollTop -
      messagesDiv.clientHeight;
  } else if (
    document.documentElement.scrollHeight >
    document.documentElement.clientHeight
  ) {
    isScrollable = true;
    distanceToBottom =
      document.documentElement.scrollHeight -
      document.documentElement.scrollTop -
      document.documentElement.clientHeight;
  }

  if (isScrollable && distanceToBottom > threshold) {
    chatScrollToBottomBtn.classList.add("visible");
  } else {
    chatScrollToBottomBtn.classList.remove("visible");
  }
}

window.addEventListener(
  "load",
  () => {
    // Short delay to ensure custom_select.js DOMContentLoaded logic has run
    setTimeout(() => {
      if (modelSelect) {
        const savedModel = localStorage.getItem("preferredModel");
        if (savedModel) {
          const optionExists = Array.from(modelSelect.options).some(
            (opt) => opt.value === savedModel,
          );
          if (optionExists) {
            modelSelect.value = savedModel;
            // Trigger change to update custom select UI and theme
            modelSelect.dispatchEvent(new Event("change"));
          }
        }
        updateModelSelectTheme();
        updateModelSelectWidth();
      }
    }, 100);

    if (messagesDiv) {
      messagesDiv.addEventListener("scroll", toggleScrollButton);
    }
    window.addEventListener("scroll", toggleScrollButton);
    window.addEventListener("resize", toggleScrollButton);

    handleModeChange();
    checkAuthStatus();

    setTimeout(() => {
      adjustTextareaHeight();
      scrollToBottom();
    }, 300);

    const closeBtn = document.getElementById("live-preview-close-btn");
    if (closeBtn) closeBtn.addEventListener("click", hideLivePreview);
    const previewContainer = document.getElementById("live-preview-container");
    const previewHeader = document.getElementById("live-preview-header");
    if (previewContainer && previewHeader)
      makeDraggable(previewContainer, previewHeader);

    // --- FULL VOICE RECOGNITION LOGIC ---
    const voiceInputBtn = document.getElementById("voiceInputBtn");
    const SpeechRecognition =
      window.SpeechRecognition || window.webkitSpeechRecognition;
    const voiceOverlay = document.getElementById("voiceOverlay");
    const canvas = document.getElementById("voiceWaveCanvas");
    const canvasCtx = canvas.getContext("2d");
    const cancelVoiceBtn = document.getElementById("cancelVoiceBtn");
    const confirmVoiceBtn = document.getElementById("confirmVoiceBtn");
    const voicePrompt = document.querySelector(".voice-prompt");

    let recognition;
    let audioContext, analyser, source, animationFrameId;
    let smoothedDataArray;
    const smoothingFactor = 0.8;

    if (SpeechRecognition && voiceInputBtn) {
      recognition = new SpeechRecognition();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.lang = "en-US";

      const resizeCanvas = () => {
        const dpr = window.devicePixelRatio || 1;
        canvas.width = window.innerWidth * dpr;
        canvas.height = window.innerHeight * dpr;
        canvasCtx.scale(dpr, dpr);
      };

      let cachedColorStart = null;
      let cachedColorEnd = null;

      const draw = () => {
        if (!analyser) return;
        animationFrameId = requestAnimationFrame(draw);
        const bufferLength = analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);
        analyser.getByteFrequencyData(dataArray);
        for (let i = 0; i < bufferLength; i++) {
          smoothedDataArray[i] =
            smoothedDataArray[i] * smoothingFactor +
            dataArray[i] * (1 - smoothingFactor);
        }
        canvasCtx.clearRect(0, 0, canvas.width, canvas.height);

        if (!cachedColorStart) {
          cachedColorStart =
            getComputedStyle(document.body)
              .getPropertyValue("--model-color-start")
              .trim() || "#a78bfa";
          cachedColorEnd =
            getComputedStyle(document.body)
              .getPropertyValue("--model-color-end")
              .trim() || "#7b61ff";
        }

        const gradient = canvasCtx.createLinearGradient(0, 0, canvas.width, 0);
        gradient.addColorStop(0.2, cachedColorStart);
        gradient.addColorStop(0.8, cachedColorEnd);
        canvasCtx.strokeStyle = gradient;
        canvasCtx.lineWidth = 4;
        const barWidth = 6;
        const barSpacing = 8;
        const numBars = Math.floor(bufferLength / 3);
        const activeBarsData = [];
        const threshold = 5;
        for (let i = 0; i < numBars; i++) {
          if (i < numBars / 2) {
            const dataIndex = Math.floor((i / (numBars / 2)) * bufferLength);
            if (smoothedDataArray[dataIndex] > threshold) {
              const v = smoothedDataArray[dataIndex] / 128.0;
              const barHeight = v * (canvas.height / 10);
              activeBarsData.push(barHeight < 2 ? 2 : barHeight);
            }
          }
        }
        const totalActiveWidth =
          activeBarsData.length * (barWidth + barSpacing);
        let x = (canvas.width - totalActiveWidth) / 2;
        const yPos = canvas.height / 2;
        canvasCtx.beginPath();
        for (const barHeight of activeBarsData) {
          canvasCtx.moveTo(x, yPos - barHeight);
          canvasCtx.lineTo(x, yPos + barHeight);
          x += barWidth + barSpacing;
        }
        canvasCtx.stroke();
      };

      const startListening = async () => {
        if (isProcessing) {
          setStatus("Cannot use voice while processing a request.", true);
          setTimeout(() => setStatus(currentStatusText, false), 3000);
          return;
        }
        if (audioContext) return;
        cachedColorStart = null;
        cachedColorEnd = null;
        try {
          const stream = await navigator.mediaDevices.getUserMedia({
            audio: true,
          });
          audioContext = new (
            window.AudioContext || window.webkitAudioContext
          )();
          analyser = audioContext.createAnalyser();
          analyser.fftSize = 512;
          analyser.smoothingTimeConstant = 0.88;
          smoothedDataArray = new Array(analyser.frequencyBinCount).fill(0);
          source = audioContext.createMediaStreamSource(stream);
          source.connect(analyser);
          resizeCanvas();
          window.addEventListener("resize", resizeCanvas);
          voiceOverlay.classList.add("visible");
          canvas.classList.add("visible");
          voicePrompt.textContent = "Listening...";
          recognition.start();
          draw();
        } catch (err) {
          console.error("Microphone access denied:", err);
          alert(
            "Microphone access was denied. Please allow it in your browser settings.",
          );
          stopListening(false);
        }
      };

      const stopListening = (shouldSend = false) => {
        if (!audioContext) return;
        recognition.stop();
        cancelAnimationFrame(animationFrameId);
        if (source && source.mediaStream) {
          source.mediaStream.getTracks().forEach((track) => track.stop());
        }
        if (audioContext.state !== "closed") {
          audioContext.close();
        }
        audioContext = null;
        analyser = null;
        voiceOverlay.classList.remove("visible");
        canvas.classList.remove("visible");
        window.removeEventListener("resize", resizeCanvas);
        const finalTranscript = chatInput.value.trim();
        if (shouldSend && finalTranscript) {
          handleSend();
        } else {
          chatInput.value = "";
        }
      };

      recognition.onresult = (event) => {
        let interim_transcript = "";
        let final_transcript = "";
        for (let i = event.resultIndex; i < event.results.length; ++i) {
          if (event.results[i].isFinal) {
            final_transcript += event.results[i][0].transcript;
          } else {
            interim_transcript += event.results[i][0].transcript;
          }
        }
        chatInput.value = final_transcript;
        voicePrompt.textContent = final_transcript + interim_transcript;
      };

      recognition.onerror = (event) => {
        console.error("Speech Recognition Error:", event.error);
        stopListening(false);
      };

      voiceInputBtn.addEventListener("click", startListening);
      cancelVoiceBtn.addEventListener("click", () => stopListening(false));
      confirmVoiceBtn.addEventListener("click", () => stopListening(true));
    } else {
      if (voiceInputBtn) voiceInputBtn.style.display = "none";
    }
  },
  { once: true },
);

document.body.addEventListener(
  "dragenter",
  (e) => {
    e.preventDefault();
    e.stopPropagation();

    const isModalInput = e.target.closest(
      "#editModalBackdrop, #regenerateModalBackdrop, #profileModal",
    );
    if (!isModalInput && inputContainer) {
      inputContainer.classList.add("active-drop");
    }
  },
  false,
);

document.body.addEventListener(
  "dragover",
  (e) => {
    e.preventDefault();
    e.stopPropagation();
    const isModalInput = e.target.closest(
      "#editModalBackdrop, #regenerateModalBackdrop, #profileModal",
    );
    if (!isModalInput && inputContainer) {
      if (!inputContainer.classList.contains("active-drop")) {
        inputContainer.classList.add("active-drop");
      }
    } else {
      if (inputContainer) inputContainer.classList.remove("active-drop");
    }
  },
  false,
);

document.body.addEventListener(
  "dragleave",
  (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (
      inputContainer &&
      (!e.relatedTarget || !inputContainer.contains(e.relatedTarget))
    ) {
      inputContainer.classList.remove("active-drop");
    }
  },
  false,
);

document.body.addEventListener(
  "drop",
  (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (inputContainer) inputContainer.classList.remove("active-drop");

    const isModalInput = e.target.closest(
      "#editModalBackdrop, #regenerateModalBackdrop, #profileModal",
    );
    if (isModalInput) return;

    const droppedFiles = e.dataTransfer?.files;
    if (droppedFiles && droppedFiles.length > 0) {
      handleFileUpload(droppedFiles);
    }
  },
  false,
);

if (chatInput) {
  chatInput.addEventListener("paste", async (e) => {
    const pastedFiles = [];

    if (e.clipboardData?.files?.length > 0) {
      pastedFiles.push(...e.clipboardData.files);
    } else if (navigator.clipboard?.read) {
      try {
        const clipboardItems = await navigator.clipboard.read();
        for (const item of clipboardItems) {
          for (const type of item.types) {
            if (type.startsWith("image/")) {
              const blob = await item.getType(type);
              const extension = type.split("/")[1] || "png";
              const fileName = `pasted-image-${Date.now()}.${extension}`;
              pastedFiles.push(new File([blob], fileName, { type }));
            }
          }
        }
      } catch (err) {
        if (err.name === "NotAllowedError") {
        } else {
          console.error("Async clipboard read failed:", err);
        }
      }
    }

    if (pastedFiles.length > 0) {
      e.preventDefault();
      e.stopPropagation();
      handleFileUpload(pastedFiles);
    }
  });
}

function showLivePreview(url) {
  const container = document.getElementById("live-preview-container");
  const iframe = document.getElementById("live-preview-iframe");
  const title = document.getElementById("live-preview-title");
  const fallback = document.getElementById("live-preview-fallback");
  const fallbackLink = fallback.querySelector("a");

  if (!container || !iframe || !title || !fallback || !fallbackLink) return;

  clearTimeout(livePreviewTimer);
  iframe.style.display = "block";
  fallback.style.display = "none";

  title.textContent = `Scraping: ${url}`;
  fallbackLink.href = url;

  iframe.src = "about:blank";
  setTimeout(() => {
    iframe.src = url;
  }, 50);

  container.style.display = "flex";

  iframe.onload = () => {
    try {
      const x = iframe.contentWindow.document;
    } catch (e) {
      iframe.style.display = "none";
      fallback.style.display = "flex";
    }
  };

  livePreviewTimer = setTimeout(() => {
    hideLivePreview();
  }, 20000);
}

function hideLivePreview() {
  const container = document.getElementById("live-preview-container");
  if (container) {
    container.style.display = "none";
  }
  clearTimeout(livePreviewTimer);
}
function processScrapingQueue() {
  clearTimeout(scrapingTimerId);
  scrapingTimerId = null;

  if (scrapingQueue.length > 0) {
    const urlToDisplay = scrapingQueue.shift();
    showLivePreview(urlToDisplay);

    scrapingTimerId = setTimeout(() => {
      processScrapingQueue();
    }, SCRAPING_DISPLAY_DELAY);
  } else {
    hideLivePreview();
  }
}

function makeDraggable(element, handle) {
  let pos1 = 0,
    pos2 = 0,
    pos3 = 0,
    pos4 = 0;
  handle.onmousedown = dragMouseDown;

  function dragMouseDown(e) {
    e = e || window.event;
    e.preventDefault();
    pos3 = e.clientX;
    pos4 = e.clientY;
    document.onmouseup = closeDragElement;
    document.onmousemove = elementDrag;
  }

  function elementDrag(e) {
    e = e || window.event;
    e.preventDefault();
    pos1 = pos3 - e.clientX;
    pos2 = pos4 - e.clientY;
    pos3 = e.clientX;
    pos4 = e.clientY;
    element.style.top = element.offsetTop - pos2 + "px";
    element.style.left = element.offsetLeft - pos1 + "px";
  }

  function closeDragElement() {
    document.onmouseup = null;
    document.onmousemove = null;
  }
}

if (chatSearchInput) {
  chatSearchInput.addEventListener("input", (e) => {
    if (e.target.value.trim() === "") {
      chatSearchInput.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter" }),
      );
    }
  });

  chatSearchInput.addEventListener("keydown", async (e) => {
    if (e.key === "Enter") {
      const searchTerm = chatSearchInput.value.toLowerCase().trim();
      currentSearchTerm = searchTerm;
      const chatItems = document.querySelectorAll("#chatList .chat-item");

      if (searchTerm === "") {
        chatItems.forEach((item) => {
          item.style.display = "flex";
          const snippetDiv = item.querySelector(".snippet-preview");
          if (snippetDiv) {
            snippetDiv.textContent = "";
            snippetDiv.style.display = "none";
          }
          delete item.dataset.messageId;
          delete item.dataset.searchTerm;
        });
        currentSearchTerm = "";
        return;
      }

      try {
        const response = await fetch(
          `/api/chats/search_messages?search_term=${encodeURIComponent(searchTerm)}`,
        );
        if (!response.ok) {
          console.error("Failed to fetch search results:", response.statusText);
          return;
        }
        const data = await response.json();
        const matchingChatResults = data.results || {};

        chatItems.forEach((item) => {
          const chatId = item.dataset.chatId;
          const chatNameSpan = item.querySelector(".chat-name");
          const snippetDiv = item.querySelector(".snippet-preview");

          const chatName = chatNameSpan?.textContent.toLowerCase() || "";
          const result = matchingChatResults[chatId];

          if (result) {
            item.style.display = "flex";
            item.dataset.messageId = result.message_id;
            item.dataset.searchTerm = searchTerm;
            if (snippetDiv) {
              if (result.snippet) {
                // Protect snippet previews of user messages from client-side unwrap and HTML/CSS execution leaks
                const escapedSnippet = escapeHtml(result.snippet);
                const escapedSearchTerm = escapeHtml(searchTerm);
                // Safe regex escaping to handle special characters in the search term
                const regexSafeSearchTerm = escapedSearchTerm.replace(
                  /[/\-\\^$*+?.()|[\]{}]/g,
                  "\\$&",
                );
                snippetDiv.innerHTML = escapedSnippet.replace(
                  new RegExp(regexSafeSearchTerm, "gi"),
                  (match) => `<span class="highlighted-text">${match}</span>`,
                );
                snippetDiv.style.display = "block";
              } else {
                snippetDiv.textContent = "";
                snippetDiv.style.display = "none";
              }
            }
          } else if (chatName.includes(searchTerm)) {
            item.style.display = "flex";
            if (snippetDiv) {
              snippetDiv.textContent = "";
              snippetDiv.style.display = "none";
            }
            delete item.dataset.messageId;
            delete item.dataset.searchTerm;
          } else {
            item.style.display = "none";
            if (snippetDiv) {
              snippetDiv.textContent = "";
              snippetDiv.style.display = "none";
            }
            delete item.dataset.messageId;
            delete item.dataset.searchTerm;
          }
        });
      } catch (error) {
        console.error("Error during chat search:", error);
        chatItems.forEach((item) => {
          item.style.display = "flex";
          const snippetDiv = item.querySelector(".snippet-preview");
          if (snippetDiv) {
            snippetDiv.textContent = "";
            snippetDiv.style.display = "none";
          }
          delete item.dataset.messageId;
          delete item.dataset.searchTerm;
        });
      }
    }
  });
}

function createChatItemHtml(chat) {
  return `
          <div class="chat-item-main-content">
              <span class="chat-name" title="${escapeHtml(chat.name)}">${escapeHtml(chat.name)}</span>
              <div class="snippet-preview"></div>
              <div class="chat-item-token-counter">
                  <span class="token-text"></span>
                  <div class="token-bar-track">
                      <div class="token-bar-fill"></div>
                  </div>
              </div>
          </div>
          <button class="delete-chat-btn" title="Delete chat"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg></button>
      `;
}

async function handleStopGeneration() {
  console.log("Stop button clicked.");
  if (!isProcessing) {
    toggleSendStopButtons(false);
    return;
  }

  let idToStop = null;
  let placeholderId = null;

  if (currentStreamQueryId) {
    idToStop = currentStreamQueryId;
    placeholderId = document.querySelector(".placeholder-message")?.dataset.id;
  }

  if (!idToStop) {
    console.warn("Stop clicked, but no active process ID found.");
    cleanupStream(true, "Stopped by user.", null);
    return;
  }

  setStatus("Stopping...", false);
  cleanupStream(true, "Stopped by user.", placeholderId);

  try {
    await fetch("/api/stop_generation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query_id: idToStop,
        chat_id: currentChatId,
      }),
    });
    console.log(`Sent stop signal for ID: ${idToStop}`);
  } catch (err) {
    console.error("Failed to send stop signal to backend:", err);
  }
}
if (stopBtn) stopBtn.addEventListener("click", handleStopGeneration);

// ==== BROWSER PANE RESIZER LOGIC ====
document.addEventListener("DOMContentLoaded", () => {
  const resizer = document.getElementById("paneResizer");
  const browserPane = document.getElementById("browserPane");
  const chatPane = document.getElementById("chatPane");
  let isResizing = false;

  if (resizer && browserPane && chatPane) {
    resizer.addEventListener("mousedown", (e) => {
      e.preventDefault();
      isResizing = true;
      document.body.style.cursor = "col-resize";
      browserPane.style.transition = "none"; // Disable animation during drag
      // Remove max-width restriction
      browserPane.style.maxWidth = "none";
      browserPane.style.flex = "none";
      // Prevent iframe from intercepting mouse events during drag
      browserPane.style.pointerEvents = "none";

      // Prevent text selection while dragging
      document.body.style.userSelect = "none";
    });

    document.addEventListener("mousemove", (e) => {
      if (!isResizing) return;

      // Calculate new width (from right edge)
      // The browser window width minus the mouse X position
      let newWidth = window.innerWidth - e.clientX;

      // Enforce min and max widths
      if (newWidth < 300) newWidth = 300; // Min width 300px
      if (newWidth > window.innerWidth - 300)
        newWidth = window.innerWidth - 300; // Leave 300px for chatPane

      browserPane.style.width = `${newWidth}px`;

      const header = document.querySelector("header");
      if (header && document.body.classList.contains("browser-open")) {
        header.style.width = `calc(100% - ${newWidth}px)`;
      }
    });

    document.addEventListener("mouseup", () => {
      if (isResizing) {
        isResizing = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        // Re-enable iframe mouse events
        browserPane.style.pointerEvents = "auto";
      }
    });

    // Observer to toggle resizer visibility whenever browserPane visibility changes
    const observer = new MutationObserver((mutations) => {
      mutations.forEach((mutation) => {
        if (
          mutation.type === "attributes" &&
          mutation.attributeName === "style"
        ) {
          const display = window.getComputedStyle(browserPane).display;
          resizer.style.display = display === "none" ? "none" : "block";

          const header = document.querySelector("header");
          if (header) {
            if (display === "none") {
              header.style.width = "";
            } else if (browserPane.style.width) {
              header.style.width = `calc(100% - ${browserPane.style.width})`;
            }
          }
        }
      });
    });

    observer.observe(browserPane, { attributes: true });

    // Handle window resize to enforce constraints and handle mobile transition layout
    window.addEventListener("resize", () => {
      if (window.innerWidth <= 768) {
        resizer.style.display = "none";
        const header = document.querySelector("header");
        if (header) {
          header.style.width = "";
        }
      } else {
        const display = window.getComputedStyle(browserPane).display;
        if (display !== "none") {
          resizer.style.display = "block";
          const header = document.querySelector("header");
          if (header) {
            if (browserPane.style.width) {
              header.style.width = `calc(100% - ${browserPane.style.width})`;
            } else {
              header.style.width = "50%";
            }
          }
          // Enforce 300px min width for chatPane and browserPane on desktop
          let currentWidth = parseFloat(browserPane.style.width);
          if (currentWidth) {
            const maxWidth = window.innerWidth - 300;
            if (currentWidth > maxWidth) {
              currentWidth = Math.max(300, maxWidth);
              browserPane.style.width = `${currentWidth}px`;
              if (header) {
                header.style.width = `calc(100% - ${currentWidth}px)`;
              }
            }
          }
        }
      }
    });
  }
});

// Toggle .has-content class on input container to prevent minimization
window.updateHasContent = function () {
  const inputContainer = document.getElementById("inputContainer");
  const chatInput = document.getElementById("chatInput");
  const stagedFilesContainer = document.getElementById("stagedFilesContainer");
  if (!inputContainer || !chatInput || !stagedFilesContainer) return;
  const hasText = chatInput.value.length > 0;
  const hasFiles = stagedFilesContainer.children.length > 0;
  if (hasText || hasFiles) {
    inputContainer.classList.add("has-content");
  } else {
    inputContainer.classList.remove("has-content");
  }
};

(function () {
  const chatInput = document.getElementById("chatInput");
  const stagedFilesContainer = document.getElementById("stagedFilesContainer");
  if (chatInput) {
    chatInput.addEventListener("input", window.updateHasContent);
  }

  if (stagedFilesContainer) {
    const observer = new MutationObserver(window.updateHasContent);
    observer.observe(stagedFilesContainer, { childList: true });
  }
})();

// Close sidebar on mobile when clicking outside of it
document.addEventListener("click", (e) => {
  if (window.innerWidth <= 768) {
    if (
      sidebar &&
      (sidebar.classList.contains("open") ||
        sidebar.classList.contains("locked"))
    ) {
      if (!sidebar.contains(e.target) && !sidebarToggleBtn.contains(e.target)) {
        sidebar.classList.remove("open");
        sidebar.classList.remove("locked");
      }
    }
  }
});
