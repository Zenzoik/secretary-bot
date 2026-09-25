(() => {
  "use strict";

  const ui = window.SecretaryUI;
  // Telegram passes signed initData in the launch hash. Keep a copy before
  // navigation replaces that hash with the selected panel. This also lets the
  // panel authenticate when telegram-web-app.js is slow or unavailable.
  const launchInitData = new URLSearchParams(location.hash.slice(1)).get("tgWebAppData") || "";
  let tg = window.Telegram?.WebApp;
  let configuredTelegram = null;
  const state = { bootstrap: null, contacts: [], logContacts: [], selectedContact: null, analytics: null, activeView: "overview", trail: [] };
  const titles = {
    overview: "Головна", contacts: "Контакти", classifier: "Шаблони відповідей", more: "Ще",
    schedule: "Розклад", delivery: "Доставка", summary: "Щоденний підсумок", escalation: "Платні звернення",
    analytics: "Аналітика", logs: "Історія дій", check: "Перевірити відповідь", users: "Користувачі",
  };
  // The four tabs; every other view is a page opened from one of them.
  const tabs = ["overview", "contacts", "classifier", "more"];
  const actions = ["replied", "dry_run", "skipped_schedule", "skipped_excluded", "skipped_unconfigured", "skipped_owner_replied", "skipped_window_limit", "skipped_kill_switch", "skipped_inactive", "skipped_unsupported_content", "error"];
  const actionLabels = {
    replied: "Відповів", dry_run: "Прев’ю", skipped_schedule: "Поза розкладом",
    skipped_excluded: "Виключено", skipped_unconfigured: "Новий контакт, не налаштований", skipped_owner_replied: "Власник відповів",
    skipped_window_limit: "Ліміт вікна", skipped_kill_switch: "Вимкнено",
    skipped_inactive: "Неактивне", skipped_unsupported_content: "Непідтримуване", error: "Помилка",
  };
  const timezones = ["Europe/Kyiv", "Europe/Prague", "Europe/Warsaw", "Europe/Berlin", "UTC"];
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  function applyTheme() {
    document.documentElement.dataset.theme = tg?.colorScheme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  }

  function configureTelegram() {
    const current = window.Telegram?.WebApp;
    if (current && current !== configuredTelegram) {
      tg = current;
      configuredTelegram = current;
      tg.ready();
      tg.expand();
      tg.setHeaderColor?.("bg_color");
      tg.setBackgroundColor?.(tg.themeParams?.bg_color || "#0d141f");
      tg.onEvent?.("themeChanged", applyTheme);
      tg.BackButton?.onClick?.(() => goBack());
      if (state.bootstrap) renderBackButton();
    }
    applyTheme();
  }

  // The official SDK is an enhancement, not a parser-blocking dependency.
  // Some Telegram Desktop networks stall telegram.org while the tunnel itself
  // remains reachable; the local UI and signed launch data must still work.
  $("#telegram-web-app-sdk")?.addEventListener("load", configureTelegram);
  configureTelegram();

  function authHeaders() {
    const headers = { "Content-Type": "application/json" };
    const initData = tg?.initData || launchInitData;
    if (initData) headers["X-Telegram-Init-Data"] = initData;
    return headers;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { credentials: "same-origin", ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
    if (response.status === 204) return null;
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(payload.detail) ? payload.detail.map((item) => item.msg).join(". ") : payload.detail;
      const error = new Error(detail || "Не вдалося виконати дію");
      error.status = response.status;
      error.details = Array.isArray(payload.detail) ? payload.detail : [];
      throw error;
    }
    return payload;
  }

  function toast(message, error = false) {
    const node = $("#toast");
    node.textContent = message;
    node.classList.toggle("error", error);
    node.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => node.classList.remove("show"), 3000);
  }

  // Every time in the panel is the bot's own clock: the owner needs to read the
  // log against the schedule that produced it, not against the device timezone.
  function formatDate(value) {
    if (!value) return "—";
    return new Intl.DateTimeFormat("uk-UA", {
      dateStyle: "short", timeStyle: "short",
      timeZone: state.bootstrap?.schedule?.timezone || undefined,
    }).format(new Date(value));
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }

  function contactName(contact) {
    return contact.contact_label || contact.contact_name || (contact.contact_username ? `@${contact.contact_username}` : "Контакт без імені");
  }

  const categoryLabels = { general: "Звичайне звернення", money: "Питання про оплату", unknown: "Без типу" };
  const templateLabels = { off_hours_default: "Звичайна відповідь", money_priority: "Питання про оплату" };
  const errorLabels = {
    DELIVERY_UNCERTAIN: "Результат невідомий: перевірте чат перед повтором",
    BUSINESS_CHAT_INACTIVE: "Вікно відповіді закрите",
    BUSINESS_CONNECTION_INVALID: "Перевірте підключення бота",
    STALE_REPLY: "Запізнілу відповідь скасовано після простою",
    NOTIFICATION_FAILED: "Сповіщення про платне звернення не доставлено",
  };

  async function submit(form, callback) {
    if (form.dataset.dirty !== "true" && !form.classList.contains("needs-save")) return;
    const button = $("button[type=submit]", form);
    const original = button.textContent;
    $$(".field-error", form).forEach(node => node.remove());
    $$("[aria-invalid]", form).forEach(node => node.removeAttribute("aria-invalid"));
    button.disabled = true;
    button.textContent = "Зберігаємо…";
    try {
      const saved = await callback();
      if (saved === false) return;
      markClean(form);
      toast("Збережено");
      tg?.HapticFeedback?.notificationOccurred?.("success");
    } catch (error) {
      const summary = document.createElement("p");
      summary.className = "field-error";
      summary.setAttribute("role", "alert");
      summary.textContent = error.message;
      form.append(summary);
      for (const detail of error.details || []) {
        const field = form.elements[detail.loc?.[1]];
        if (field?.setAttribute) {
          field.setAttribute("aria-invalid", "true");
          const note = document.createElement("span"); note.className = "field-error"; note.textContent = detail.msg;
          note.id = `${form.id}-${field.name}-error`; field.setAttribute("aria-describedby", note.id); field.after(note);
        }
      }
      toast(error.message, true);
      tg?.HapticFeedback?.notificationOccurred?.("error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  // A form is unsaved only while its values differ from what was loaded or last
  // saved: switching something off and back on again leaves nothing to save.
  const baselines = new WeakMap();

  function formSnapshot(form) {
    return JSON.stringify($$("input, select, textarea", form)
      .filter((field) => !field.closest("[data-no-dirty]") && !["button", "submit"].includes(field.type))
      .map((field) => (field.type === "checkbox" || field.type === "radio" ? field.checked : field.value)));
  }

  function markClean(form) {
    baselines.set(form, formSnapshot(form));
    setDirty(form, false);
  }

  function refreshDirty(form) {
    if (!baselines.has(form)) return;
    setDirty(form, formSnapshot(form) !== baselines.get(form));
  }

  function setDirty(form, dirty) {
    form.dataset.dirty = String(dirty);
    let badge = $(".dirty-note", form);
    if (!badge) { badge = document.createElement("p"); badge.className = "dirty-note muted"; badge.setAttribute("role", "status"); form.append(badge); }
    badge.textContent = dirty ? "Є незбережені зміни" : "";
    let reset = $(".discard-changes", form);
    if (!reset && form.id !== "preview-form") {
      reset = document.createElement("button"); reset.type = "button"; reset.className = "secondary discard-changes"; reset.textContent = "Скасувати";
      reset.addEventListener("click", () => {
        if (!window.confirm("Відкинути незбережені зміни цієї форми?")) return;
        setDirty(form, false);
        const fill = {"delivery-form":fillDelivery,"escalation-form":fillEscalation,"schedule-form":fillSchedule,"classifier-form":fillClassifier,"summary-form":fillSummary,"contact-form":() => state.selectedContact && fillContactForm(state.selectedContact)}[form.id];
        fill?.(); $$(".field-error", form).forEach(node => node.remove());
      });
      const actions = $$(".form-actions", form).at(-1);
      (actions || form).append(reset);
    }
    reset?.classList.toggle("hidden", !dirty);
    const anyDirty = $$("form[data-dirty=true]").length > 0;
    if (anyDirty) tg?.enableClosingConfirmation?.(); else tg?.disableClosingConfirmation?.();
  }

  async function refreshStatus() {
    if (!state.bootstrap || document.hidden) return;
    try {
      const fresh = await api("/api/v1/bootstrap");
      state.bootstrap.connection = fresh.connection;
      state.bootstrap.status = fresh.status;
      renderStatus();
      if (state.activeView === "overview") loadContactStats();
    } catch (error) { toast(error.status === 401 ? "Сеанс завершено. Відкрийте панель через бота." : "Не вдалося оновити стан. Спробуйте ще раз.", true); }
  }

  async function copyText(value) {
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(value);
    const field = document.createElement("textarea");
    field.value = value;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
    field.select();
    document.execCommand("copy");
    field.remove();
  }

  // How a view was reached: a tab starts over, a link inside a page remembers
  // where it came from so that Back returns there.
  function navigate(view, { via } = {}) {
    if (!titles[view]) return;
    if (via === "tab") state.trail = [];
    if (via === "link" && view !== state.activeView) state.trail.push(state.activeView);
    state.activeView = view;
    // A page belongs to the tab it was opened from; a deep link to one belongs to "Ще".
    const tab = tabs.includes(view) ? view : [...state.trail].reverse().find((item) => tabs.includes(item)) || "more";
    $$("#navigation [data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === tab));
    $$("[data-view-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.viewPanel === view));
    $("#page-title").textContent = titles[view];
    history.replaceState(null, "", `#${view}`);
    // A kept scroll offset would open the next view halfway down.
    window.scrollTo({ top: 0 });
    renderBackButton();
    if (view === "overview") loadContactStats();
    if (view === "contacts") loadContacts();
    if (view === "analytics") loadAnalytics();
    if (view === "logs") { renderDiagnostics(); loadLogs(); }
    if (view === "users") loadUsers();
    if (view === "check") { state.previewContact = state.selectedContact; renderPreviewScope(); }
  }

  // On wide screens the contact list and the open card sit side by side.
  const wideLayout = () => Boolean(window.matchMedia?.("(min-width: 900px)").matches);

  function contactOpenAlone() {
    return state.activeView === "contacts" && Boolean(state.selectedContact) && !wideLayout();
  }

  function canGoBack() {
    return contactOpenAlone() || state.trail.length > 0 || !tabs.includes(state.activeView);
  }

  function renderBackButton() {
    const visible = canGoBack();
    $("#back-button").classList.toggle("hidden", !visible);
    if (state.activeView === "contacts") $("#page-title").textContent = contactOpenAlone() ? contactName(state.selectedContact) : titles.contacts;
    if (visible) tg?.BackButton?.show?.(); else tg?.BackButton?.hide?.();
  }

  function goBack() {
    if (contactOpenAlone()) { closeContact(); return; }
    if (state.trail.length) navigate(state.trail.pop());
    else if (!tabs.includes(state.activeView)) navigate("more");
  }

  async function loadUsers() {
    const container = $("#access-users");
    container.textContent = "Завантаження…";
    try {
      const { users } = await api("/api/v1/access/users");
      const labels = { pending: "Очікує підтвердження", active: "Доступ активний", revoked: "Доступ відкликано" };
      container.innerHTML = users.map(user => `<div class="access-user">
        <div><strong>${escapeHtml(user.display_name || (user.username ? `@${user.username}` : `ID ${user.user_id}`))}</strong>
          <small>ID ${user.user_id} · ${escapeHtml(labels[user.status] || user.status)}${user.role === "master" ? " · Майстер" : ""}</small></div>
        <div class="access-user-actions">${user.role === "master" ? "" : user.status === "pending"
          ? `<button class="primary" data-access-action="approve" data-user-id="${user.user_id}" type="button">Підтвердити</button><button class="danger-button" data-access-action="revoke" data-user-id="${user.user_id}" type="button">Відхилити</button>`
          : user.status === "active" ? `<button class="danger-button" data-access-action="revoke" data-user-id="${user.user_id}" type="button">Відкликати</button>` : ""}</div>
      </div>`).join("") || "Поки немає користувачів.";
    } catch (error) { container.textContent = error.message; toast(error.message, true); }
  }

  function localTime(value) {
    return new Intl.DateTimeFormat("uk-UA", { hour: "2-digit", minute: "2-digit", timeZone: state.bootstrap?.schedule?.timezone || undefined }).format(new Date(value));
  }

  function startsAt(value) {
    const zone = state.bootstrap?.schedule?.timezone || undefined;
    const day = (date) => new Intl.DateTimeFormat("uk-UA", { dateStyle: "short", timeZone: zone }).format(date);
    return day(new Date(value)) === day(new Date()) ? `о ${localTime(value)}` : formatDate(value);
  }

  // A whole-day window ends at midnight, but the next day may be one too: count
  // the whole days that follow, so a 24/7 schedule does not claim to end tonight.
  function windowEndNote(value) {
    const zone = state.bootstrap?.schedule?.timezone || undefined;
    const end = new Date(value);
    const clock = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: zone }).format(end);
    const wholeDays = (state.bootstrap?.schedule?.windows || []).filter((window) => window.is_active && isWholeDay(window))
      .reduce((mask, window) => mask | window.weekday_mask, 0);
    if (clock !== "00:00" || !wholeDays) return `До ${localTime(value)}`;
    const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    let day = weekdays.indexOf(new Intl.DateTimeFormat("en-US", { weekday: "short", timeZone: zone }).format(end));
    let covered = 0;
    while (covered < 7 && wholeDays & (1 << day)) { covered += 1; day = (day + 1) % 7; }
    if (covered >= 7) return "Цілодобово";
    if (!covered) return `До ${localTime(value)}`;
    // Midday of the last covered day, so a DST shift cannot move it to another date.
    const last = new Date(end.getTime() + (covered - 0.5) * 86400000);
    return `До кінця ${new Intl.DateTimeFormat("uk-UA", { day: "numeric", month: "long", timeZone: zone }).format(last)}`;
  }

  function currentMode(connection) {
    if (connection.kill_switch) return "off";
    return connection.dry_run ? "test" : "live";
  }

  function scheduleSummary(windows) {
    const active = (windows || []).filter((window) => window.is_active);
    if (!active.length) return "Не налаштовано";
    const [first] = active;
    const hours = isWholeDay(first) ? "цілодобово" : `${first.time_from.slice(0, 5)}–${first.time_to.slice(0, 5)}`;
    const text = `${hours}, ${(weekdayLabels[first.weekday_mask] || "обрані дні").toLowerCase()}`;
    return active.length > 1 ? `${text} +${active.length - 1}` : text;
  }

  function renderStatus() {
    const { connection, delivery } = state.bootstrap;
    const current = state.bootstrap.status || {};
    const mode = currentMode(connection);
    const inactive = current.code === "inactive";
    const paused = current.code === "paused";
    const [title, note] = {
      inactive: ["Немає підключення", "Перевірте бота в Chat Automation"],
      stopped: ["Вимкнено", "Клієнти не отримують відповідей"],
      paused: [`Пауза до ${current.muted_until ? localTime(current.muted_until) : "—"}`, ""],
      outside_schedule: ["Чекає розкладу", current.next_start ? `Почне ${startsAt(current.next_start)}` : ""],
      dry_run: ["Тестовий режим", current.window_end ? windowEndNote(current.window_end) : ""],
      live: ["Відповідає клієнтам", current.window_end ? windowEndNote(current.window_end) : ""],
    }[current.code] || [current.label || "Перевіряємо стан", ""];
    $("#operating-title").textContent = title;
    $("#operating-note").textContent = note;
    $("#status-dot").dataset.state = inactive ? "off" : mode === "off" ? "off" : paused ? "paused" : mode;
    $$("[data-mode]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
      // Without the reply right only going live is impossible; off and test still work.
      button.disabled = Boolean(state.controlBusy) || (inactive && button.dataset.mode === "live");
    });
    $(".mode-switch").setAttribute("aria-busy", String(Boolean(state.controlBusy)));
    if (state.activeView === "logs") renderDiagnostics();
    $("#mode-hint").textContent = mode === "test" ? "Клієнти нічого не отримують, відповіді приходять вам." : "";
    const pause = $("#pause-toggle");
    pause.classList.toggle("hidden", mode === "off");
    pause.disabled = Boolean(state.controlBusy);
    pause.dataset.control = paused ? "resume" : "pause";
    pause.textContent = paused ? "Зняти паузу" : "Пауза на 1 год";
    renderAttention();
    const schedule = scheduleSummary(state.bootstrap.schedule?.windows);
    $("#home-schedule").textContent = schedule;
    $("#more-schedule").textContent = schedule;
    $("#more-delivery").textContent = delivery.sender_identity === "bot" ? "Секретар" : "Від вас";
  }

  function renderAttention() {
    const current = state.bootstrap.status || {};
    const rights = state.bootstrap.connection.rights || {};
    const items = [];
    if (!rights.can_reply) items.push('<div class="list-row warn"><span>Немає права відповідати</span><small>Chat Automation</small></div>');
    if (current.failed_notifications > 0) items.push(`<div class="list-row warn"><span>Сповіщення не доставлено</span><small>${current.failed_notifications}</small><button class="chip-button" id="retry-notifications" type="button">Повторити</button></div>`);
    if (current.summary_status === "error") items.push('<button class="list-row warn" type="button" data-view="summary"><span>Підсумок не надіслано</span></button>');
    if (current.uncertain_deliveries > 0) items.push(`<div class="list-row warn"><span>Перевірте надсилання в чатах</span><small>${current.uncertain_deliveries}</small></div>`);
    const container = $("#attention");
    container.innerHTML = items.join("");
    container.classList.toggle("hidden", !items.length);
  }

  function renderDiagnostics() {
    const current = state.bootstrap.status || {};
    const rights = state.bootstrap.connection.rights || {};
    const summary = { none: "ще не було", pending: "готується", delivered: "надіслано", error: "помилка" }[current.summary_status] || "—";
    $("#operating-history").innerHTML = [
      ["Остання відповідь", formatDate(current.last_reply_at)],
      ["Остання помилка", errorLabels[current.last_error] || current.last_error || "немає"],
      ["Підсумок", summary],
      ["Сповіщення в черзі", current.pending_notifications || 0],
      ["Права", rights.can_reply ? (rights.can_read_messages ? "відповідь і читання" : "лише відповідь") : "немає"],
    ].map(([label, value]) => `<div><span>${label}</span><b>${escapeHtml(value)}</b></div>`).join("");
  }

  async function loadContactStats() {
    // The home screen stays usable without the counts.
    try {
      state.contactStats = await api("/api/v1/contacts/stats");
      renderContactStats();
    } catch { /* keep the last known counts */ }
  }

  // Only the groups that have someone in them; new contacts need the owner.
  function renderContactStats() {
    const stats = state.contactStats || {};
    const cells = [
      ["new", "Нові"], ["active", "Активні"], ["paused", "На паузі"], ["never", "Без відповіді"],
    ].filter(([key]) => Number(stats[key]) > 0);
    const block = $("#contact-stats");
    block.innerHTML = cells.map(([key, label]) => `<button type="button" class="stat ${key === "new" ? "warn" : ""}" data-view="contacts" data-contact-list><strong>${Number(stats[key])}</strong><small>${label}</small></button>`).join("");
    block.classList.toggle("hidden", !cells.length);
  }

  function fillDelivery() {
    const form = $("#delivery-form");
    const data = state.bootstrap.delivery;
    $(`input[name=sender_identity][value=${data.sender_identity}]`, form).checked = true;
    ["delay_min_seconds", "delay_max_seconds", "bot_delay_seconds"].forEach((name) => { form.elements[name].value = data[name]; });
    form.elements.mark_read.checked = data.mark_read;
    form.elements.max_auto_replies_per_window.value = data.max_auto_replies_per_window || 0;
    renderDelayRanges();
    markClean(form);
  }

  function renderPreviewScope() {
    const contact = state.previewContact;
    $("#preview-scope").textContent = contact
      ? `Для контакту: ${contactName(contact)}. Нічого не надсилається.`
      : "За основним розкладом. Нічого не надсилається.";
    $("#preview-clear-contact").classList.toggle("hidden", !contact);
  }

  function fillEscalation() {
    const form = $("#escalation-form");
    const data = state.bootstrap.escalation;
    form.elements.enabled.checked = data.enabled;
    form.elements.price_amount.value = data.price_amount;
    form.elements.currency.value = data.currency;
    form.elements.offer_text.value = data.offer_text;
    form.elements.confirm_text.value = data.confirm_text;
    form.elements.decline_text.value = data.decline_text;
    $("#escalation-badge").textContent = data.enabled ? `${data.price_amount} ${data.currency}` : "Вимкнено";
    markClean(form);
  }

  function renderDelayRanges() {
    const form = $("#delivery-form");
    const ownerMin = form.elements.delay_min_seconds.value;
    const botMin = form.elements.bot_delay_seconds.value;
    const maximum = form.elements.delay_max_seconds.value;
    $("#bot-delay-range").textContent = botMin && maximum ? `${botMin}–${ui.botMaximum(maximum)} с` : "—";
    $("#owner-delay-range").textContent = ownerMin && maximum ? `${ownerMin}–${maximum} с` : "—";
    // Only the chosen sender's minimum matters; the other stays saved but out of the way.
    const bot = form.elements.sender_identity.value === "bot";
    $$(".delay-bot", form).forEach((node) => node.classList.toggle("hidden", !bot));
    $$(".delay-owner", form).forEach((node) => node.classList.toggle("hidden", bot));
    // A hidden field must never block saving with a message nobody can see.
    form.elements.bot_delay_seconds.disabled = !bot;
    form.elements.delay_min_seconds.disabled = bot;
  }

  // 00:00–00:00 is the server's explicit "whole day".
  const WHOLE_DAY = "00:00";
  const isWholeDay = (window) => window.time_from.slice(0, 5) === WHOLE_DAY && window.time_to.slice(0, 5) === WHOLE_DAY;

  function createWindow(container, data = { weekday_mask: 127, time_from: "22:00", time_to: "08:00", is_active: true }) {
    const node = $("#window-template").content.firstElementChild.cloneNode(true);
    $(".weekday-mask", node).value = String(data.weekday_mask);
    $(".time-from", node).value = data.time_from.slice(0, 5);
    $(".time-to", node).value = data.time_to.slice(0, 5);
    $(".is-active", node).checked = data.is_active;
    const allDay = $(".all-day-toggle", node);
    allDay.checked = isWholeDay(data);
    node.classList.toggle("is-all-day", allDay.checked);
    allDay.addEventListener("change", () => {
      const from = $(".time-from", node), to = $(".time-to", node);
      if (allDay.checked) {
        // Remember the hours, so unticking brings them back unchanged.
        node.dataset.hours = `${from.value}-${to.value}`;
        from.value = WHOLE_DAY; to.value = WHOLE_DAY;
      } else {
        const [previousFrom, previousTo] = (node.dataset.hours || "").split("-");
        const restore = previousFrom && !(previousFrom === WHOLE_DAY && previousTo === WHOLE_DAY);
        from.value = restore ? previousFrom : "22:00"; to.value = restore ? previousTo : "08:00";
      }
      node.classList.toggle("is-all-day", allDay.checked);
    });
    $(".remove-window", node).addEventListener("click", () => {
      if (!window.confirm("Видалити цей інтервал? Зміна набуде чинності після збереження.")) return;
      node.remove();
      refreshDirty(container.closest("form"));
      if (container.id === "contact-windows") renderContactScheduleEditor();
    });
    container.append(node);
  }

  const weekdayLabels = {
    127: "Щодня", 31: "Будні", 96: "Вихідні", 1: "Понеділок", 2: "Вівторок",
    4: "Середа", 8: "Четвер", 16: "П’ятниця", 32: "Субота", 64: "Неділя",
  };

  function renderContactScheduleEditor() {
    const container = $("#contact-windows");
    const hasPersonalSchedule = $$(".window-row", container).length > 0;
    const inheritedWindows = state.bootstrap?.schedule?.windows || [];
    $("#contact-schedule-source").textContent = hasPersonalSchedule ? "Особливий" : "Основний";
    $("#add-contact-window").textContent = hasPersonalSchedule ? "+ Інтервал" : "Змінити";
    $("#add-contact-window").classList.toggle("edit-icon", !hasPersonalSchedule);
    $("#reset-contact-windows").classList.toggle("hidden", !hasPersonalSchedule);
    const mainAlwaysOn = inheritedWindows.some((window) => window.is_active && window.weekday_mask === 127 && isWholeDay(window));
    $("#contact-all-day").classList.toggle("hidden", hasPersonalSchedule || mainAlwaysOn);
    container.classList.toggle("hidden", !hasPersonalSchedule);
    const preview = $("#contact-schedule-preview");
    preview.classList.toggle("hidden", hasPersonalSchedule);
    const activeInheritedWindows = inheritedWindows.filter((window) => window.is_active);
    preview.innerHTML = activeInheritedWindows.length
      ? activeInheritedWindows.map((window) => `<div><span>${escapeHtml(weekdayLabels[window.weekday_mask] || "Обрані дні")}</span><b>${isWholeDay(window) ? "Цілодобово" : `${escapeHtml(window.time_from.slice(0, 5))}–${escapeHtml(window.time_to.slice(0, 5))}`}</b></div>`).join("")
      : '<div><span>Основний розклад не налаштовано</span></div>';
  }

  function windowsPayload(container) {
    return $$(".window-row", container).map((row) => ({
      weekday_mask: Number($(".weekday-mask", row).value),
      time_from: $(".time-from", row).value,
      time_to: $(".time-to", row).value,
      is_active: $(".is-active", row).checked,
    }));
  }

  function fillSchedule() {
    const data = state.bootstrap.schedule;
    const select = $("#timezone-select");
    select.innerHTML = [...new Set([...timezones, data.timezone])].map((zone) => `<option value="${escapeHtml(zone)}">${escapeHtml(zone)}</option>`).join("");
    select.value = data.timezone;
    const container = $("#schedule-windows");
    container.innerHTML = "";
    data.windows.forEach((window) => createWindow(container, window));
    markClean($("#schedule-form"));
  }

  function directionsPayload() {
    return $$(".direction-card").map((card) => ({
      code: card.dataset.code, label: $(".direction-label", card).value,
      description: $(".direction-description", card).value,
      keywords: $(".direction-keywords", card).value.split(",").map((v) => v.trim()).filter(Boolean),
      is_active: card.dataset.code === "general" || $(".direction-active", card).checked,
      reply_template: $(".direction-template", card).value,
    }));
  }

  function classifierPayload() {
    const form = $("#classifier-form");
    return { directions: directionsPayload(), system_prompt: form.elements.system_prompt.value,
      model: form.elements.model.value, confidence_min: form.elements.confidence_min.value };
  }

  function renderDirections(directions) {
    $("#direction-list").innerHTML = directions.map(directionCard).join("");
    $$(".direction-card").forEach(renderDirectionHead);
  }

  function directionCard(direction) {
    {
      const custom = !["general", "money"].includes(direction.code);
      const toggle = direction.code === "general"
        ? ""
        : `<input class="direction-active switch" type="checkbox" role="switch" ${direction.is_active ? "checked" : ""}>`;
      return `
        <article class="card direction-card" data-code="${escapeHtml(direction.code)}">
          <header class="direction-head"><strong class="direction-title"></strong>${toggle}</header>
          <label class="direction-reply"><span class="visually-hidden">Відповідь клієнту</span><textarea class="direction-template" maxlength="2000" rows="3" placeholder="Що бот відповість клієнту" required>${escapeHtml(direction.reply_template || "")}</textarea></label>
          <details class="direction-more" ${direction.label ? "" : "open"}>
            <summary>${direction.code === "general" ? "Назва" : "Назва й опис"}</summary>
            <label>Назва<input class="direction-label" maxlength="80" value="${escapeHtml(direction.label)}" required></label>
            <label>Коли застосовувати<textarea class="direction-description" maxlength="500" rows="2" required>${escapeHtml(direction.description)}</textarea></label>
            <label class="keywords">Ключові слова<input class="direction-keywords" value="${escapeHtml(direction.keywords.join(", "))}"><small>Через кому. Потрібні, лише коли ШІ недоступний.</small></label>
            ${custom ? '<button type="button" class="chip-button danger remove-direction">Видалити тип</button>' : ""}
          </details>
        </article>`;
    }
  }

  function renderDirectionHead(card) {
    $(".direction-title", card).textContent = $(".direction-label", card).value.trim() || "Новий тип";
    const toggle = $(".direction-active", card);
    if (!toggle) return;
    toggle.setAttribute("aria-label", `Тип «${$(".direction-title", card).textContent}»`);
    card.classList.toggle("is-off", !toggle.checked);
  }

  function validateClassifierForm(form) {
    const blank = $$(".direction-label, .direction-description, .direction-template", form)
      .find((field) => !field.value.trim());
    const invalid = blank || $$('input, textarea, select', form).find((field) => !field.checkValidity());
    if (!invalid) return true;
    const details = invalid.closest("details");
    if (details) details.open = true;
    if (!blank) form.reportValidity();
    invalid?.scrollIntoView({behavior: "smooth", block: "center"});
    invalid?.focus({preventScroll: true});
    const card = invalid.closest(".direction-card");
    const onlyReplyMissing = invalid.matches(".direction-template") && card
      && $$(".direction-label, .direction-description", card).every((field) => field.value.trim());
    const message = onlyReplyMissing
      ? "Додайте відповідь клієнту для цього типу."
      : "Заповніть назву, опис і відповідь для типу.";
    toast(message, true);
    tg?.HapticFeedback?.notificationOccurred?.("error");
    return false;
  }

  // The AI instruction is stale only while the types differ from the ones it was
  // generated for; saving then regenerates it first. Undoing a change undoes that.
  function typesSnapshot() {
    return JSON.stringify(directionsPayload().map((d) => [d.code, d.label.trim(), d.description.trim(), d.is_active]));
  }

  function rememberRules() {
    state.rulesTypes = typesSnapshot();
    state.rulesPrompt = $("#classifier-form").elements.system_prompt.value;
    refreshClassifierRules();
  }

  function refreshClassifierRules() {
    const form = $("#classifier-form");
    form.dataset.promptStale = String(typesSnapshot() !== state.rulesTypes);
    form.dataset.promptEdited = String(form.elements.system_prompt.value !== state.rulesPrompt);
  }

  function fillClassifier() {
    const form = $("#classifier-form");
    const data = state.bootstrap.classifier;
    data.directions.forEach((d) => { categoryLabels[d.code] = d.label; });
    renderDirections(data.directions);
    form.elements.system_prompt.value = data.system_prompt;
    form.elements.model.value = data.model;
    form.elements.confidence_min.value = data.confidence_min;
    rememberRules();
    markClean(form);
  }

  function fillSummary() {
    const form = $("#summary-form");
    const data = state.bootstrap.summary;
    const unsaved = form.dataset.dirty === "true" ? {time:form.elements.summary_time.value, retention:form.elements.message_retention_enabled.checked} : null;
    form.elements.summary_time.value = data.summary_time;
    form.elements.summary_channel_id.value = data.summary_channel_id ?? "";
    form.elements.message_retention_enabled.checked = data.message_retention_enabled;
    $("#retention-badge").textContent = data.message_retention_enabled ? data.summary_time.slice(0, 5) : "Вимкнено";
    $("#retention-stats").textContent = data.message_retention_enabled
      ? `Збережено повідомлень: ${data.retained_message_count}`
      : "";
    const connected = Boolean(data.summary_channel_id);
    $("#summary-channel-state").textContent = connected
      ? data.summary_channel_title || "Telegram-канал"
      : "Чат із ботом";
    $("#disconnect-summary-channel").classList.toggle("hidden", !connected);
    $("#choose-summary-channel").classList.toggle("hidden", !tg?.requestChat);
    if (!tg?.requestChat) $("#summary-channel-fallback").open = true;
    markClean(form);
    // A channel connected meanwhile must not discard the owner's other edits.
    if (unsaved) { form.elements.summary_time.value = unsaved.time; form.elements.message_retention_enabled.checked = unsaved.retention; refreshDirty(form); }
  }

  const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

  async function pollChannelRequest(requestId) {
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await wait(600 + attempt * 120);
      const result = await api(`/api/v1/summary/channel-request/${requestId}`);
      if (result.status === "connected") {
        state.bootstrap.summary = result.summary;
        fillSummary();
        toast("Канал підключено");
        tg?.HapticFeedback?.notificationOccurred?.("success");
        return;
      }
      if (result.status === "error") throw new Error(result.error || "Не вдалося підключити канал");
    }
    throw new Error("Telegram ще обробляє вибір. Оновіть панель за кілька секунд.");
  }

  async function withBusyButton(button, label, callback) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = label;
    try { await callback(); }
    catch (error) {
      toast(error.message, true);
      tg?.HapticFeedback?.notificationOccurred?.("error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  let contactRequest = 0;
  async function loadContacts(append = false) {
    const search = $("#contact-search").value.trim();
    const request = ++contactRequest;
    try {
      const result = await api(`/api/v1/contacts?search=${encodeURIComponent(search)}&offset=${append ? state.contacts.length : 0}`);
      if (request !== contactRequest) return;
      state.contacts = append ? [...state.contacts, ...result.items] : result.items;
      $("#more-contacts").classList.toggle("hidden", !result.has_more);
      renderContacts();
    } catch (error) { toast(error.message, true); }
  }

  function renderContacts() {
    const list = $("#contact-list");
    if (!state.contacts.length) {
      list.innerHTML = `<div class="empty-row">${$("#contact-search").value.trim() ? "Нічого не знайдено" : "Контакт з’явиться після першого повідомлення в чаті"}</div>`;
      return;
    }
    list.innerHTML = state.contacts.map((contact) => `<button type="button" class="contact-item ${state.selectedContact?.contact_id === contact.contact_id ? "active" : ""} ${contact.configured ? "" : "needs-setup"}" data-contact-id="${contact.contact_id}"><strong>${escapeHtml(contactName(contact))}</strong><small>${escapeHtml(contactState(contact))}</small></button>`).join("");
    $$(".contact-item", list).forEach((button) => button.addEventListener("click", () => selectContact(Number(button.dataset.contactId))));
  }

  function contactState(contact) {
    if (!contact.configured) return "Не налаштовано";
    if (contact.exclusion === "forever") return "Не відповідати";
    if (contact.exclusion === "until") return contact.exclusion_until ? `Пауза до ${formatDate(contact.exclusion_until)}` : "Пауза";
    return contact.windows?.length ? "Особливий розклад" : "За розкладом";
  }

  function closeContact() {
    const editor = $("#contact-form");
    if (editor.dataset.dirty === "true" && !window.confirm("Відкинути незбережені зміни контакту?")) return;
    setDirty(editor, false);
    state.selectedContact = null;
    $("#contact-layout").classList.remove("editing");
    editor.classList.add("empty");
    $("#contact-empty").classList.remove("hidden");
    $("#contact-fields").classList.add("hidden");
    renderContacts();
    renderBackButton();
  }

  function selectContact(contactId, saved = false) {
    const editor = $("#contact-form");
    if (!saved && editor.dataset.dirty === "true" && !window.confirm("Відкинути незбережені зміни контакту?")) return;
    const contact = state.contacts.find((item) => item.contact_id === contactId);
    if (!contact) return;
    state.selectedContact = contact;
    renderContacts();
    fillContactForm(contact);
    $("#contact-layout").classList.add("editing");
    window.scrollTo({ top: 0 });
    renderBackButton();
  }

  // The selected card is kept as its own snapshot: a later search may drop it
  // from the visible list, and discarding edits must still restore it.
  function renderContactMeta(contact) {
    $("#contact-meta").textContent = `Останнє повідомлення ${formatDate(contact.last_incoming_at)}`;
    $("#exclusion-zone").textContent = `(${state.bootstrap.schedule.timezone})`;
  }

  function fillContactForm(contact) {
    const form = $("#contact-form");
    setDirty(form, false);
    form.classList.remove("empty");
    $("#contact-empty").classList.add("hidden");
    $("#contact-fields").classList.remove("hidden");
    $("#contact-title").textContent = contactName(contact);
    $("#contact-setup-note").classList.toggle("hidden", Boolean(contact.configured));
    // A new contact is reviewed by saving it, so its save button shows even without edits.
    form.classList.toggle("needs-save", !contact.configured);
    renderContactMeta(contact);
    $(`input[name=exclusion][value=${contact.exclusion}]`, form).checked = true;
    form.dataset.exclusionOriginal = contact.exclusion_until || "";
    form.elements.exclusion_until.value = contact.exclusion_until
      ? ui.zonedDateTime(contact.exclusion_until, state.bootstrap.schedule.timezone)
      : "";
    const windows = $("#contact-windows");
    windows.innerHTML = "";
    contact.windows.forEach((window) => createWindow(windows, window));
    renderContactScheduleEditor();
    renderExclusionUntil();
    markClean(form);
  }

  function renderExclusionUntil() {
    const form = $("#contact-form");
    $(".exclusion-until", form).classList.toggle("hidden", form.elements.exclusion.value !== "until");
    // A contact that is never answered has no use for a schedule.
    $(".contact-schedule", form).classList.toggle("hidden", form.elements.exclusion.value === "forever");
  }

  async function loadLogs(append = false) {
    const form = $("#log-filter");
    if (!state.logContacts.length) {
      let offset = 0, more = true;
      while (more) {
        const page = await api(`/api/v1/contacts?offset=${offset}`);
        state.logContacts.push(...page.items);
        more = page.has_more; offset = page.next_offset;
      }
      const selected = form.elements.contact_id.value;
      form.elements.contact_id.innerHTML = '<option value="">Усі контакти</option>' + state.logContacts.map((contact) => `<option value="${contact.contact_id}">${escapeHtml(contactName(contact))}</option>`).join("");
      form.elements.contact_id.value = selected;
    }
    const params = new URLSearchParams();
    if (form.elements.contact_id.value) params.set("contact_id", form.elements.contact_id.value);
    if (form.elements.action.value) params.set("action", form.elements.action.value);
    try {
      params.set("offset", append ? state.logOffset || 0 : 0);
      const result = await api(`/api/v1/logs?${params}`);
      state.logOffset = result.next_offset;
      $("#log-timezone").textContent = `Час за ${state.bootstrap.schedule.timezone}`;
      $("#more-logs").classList.toggle("hidden", !result.has_more);
      const rows = result.items.length ? result.items.map((row) => `<tr><td>${formatDate(row.occurred_at)}</td><td>${escapeHtml(row.contact_label)}</td><td>${escapeHtml(actionLabels[row.action] || row.action)}</td><td>${escapeHtml(categoryLabels[row.category] || "—")}</td><td>${escapeHtml(errorLabels[row.error_code] || row.error_code || templateLabels[row.template_code] || "—")}</td></tr>`).join("") : '<tr><td class="empty-row" colspan="5">За вибраними фільтрами записів немає.</td></tr>';
      if (append) $("#log-rows").insertAdjacentHTML("beforeend", rows); else $("#log-rows").innerHTML = rows;
      labelTables();
    } catch (error) { toast(error.message, true); }
  }

  function formatAmounts(amounts) {
    const entries = Object.entries(amounts || {});
    return entries.length ? entries.map(([currency, amount]) => `${amount} ${currency}`).join(", ") : "—";
  }

  function formatCategories(categories) {
    const entries = Object.entries(categories || {});
    return entries.length ? entries.map(([code, count]) => `${categoryLabels[code] || code}: ${count}`).join(" · ") : "—";
  }

  function fillMonthOptions(referenceMonth) {
    const select = $("#analytics-month");
    const previous = select.value;
    const [year, month] = referenceMonth.split("-").map(Number);
    const options = [];
    for (let offset = 0; offset < 24; offset += 1) {
      const point = new Date(Date.UTC(year, month - 1 - offset, 1));
      const value = `${point.getUTCFullYear()}-${String(point.getUTCMonth() + 1).padStart(2, "0")}`;
      const label = new Intl.DateTimeFormat("uk-UA", { month: "long", year: "numeric", timeZone: "UTC" }).format(point);
      options.push(`<option value="${value}">${escapeHtml(label)}</option>`);
    }
    select.innerHTML = options.join("");
    select.value = previous && options.some((option) => option.includes(`value="${previous}"`)) ? previous : referenceMonth;
    const inTelegram = Boolean(tg?.initData);
    $("#pdf-export-note").textContent = inTelegram
      ? "Відкриється в браузері"
      : "Збережеться в завантаження";
    $("#download-monthly-pdf").textContent = inTelegram ? "Відкрити" : "Завантажити";
  }

  function renderAnalytics() {
    const data = state.analytics;
    if (!data) return;
    const totals = data.totals;
    const form = $("#analytics-filter");
    form.elements.date_from.value = data.period.date_from;
    form.elements.date_to.value = data.period.date_to;
    $("#analytics-timezone").textContent = `Час за ${data.period.timezone}`;
    $("#analytics-totals").innerHTML = [
      ["Контакти", totals.contacts],
      ["Повідомлення", totals.messages],
      ["Звичайні звернення", totals.ordinary_requests],
      ["Платні звернення", totals.paid_requests],
      ["Питання", `${totals.questions_asked} / ${totals.questions_closed}`],
      ["Нараховано", formatAmounts(totals.paid_amounts)],
    ].map(([label, value]) => `<article><small>${label}</small><strong>${escapeHtml(value)}</strong></article>`).join("");
    $("#analytics-rows").innerHTML = data.items.length ? data.items.map((item) => {
      const name = contactName(item);
      const categories = Object.keys(item.request_categories).length ? item.request_categories : item.categories;
      return `<tr><td><strong>${escapeHtml(name)}</strong></td><td>${item.messages}</td><td>${item.message_directions.in || 0} / ${item.message_directions.out || 0}</td><td>${item.ordinary_requests}</td><td>${item.paid_requests}</td><td>${escapeHtml(formatAmounts(item.paid_amounts))}</td><td>${escapeHtml(formatCategories(categories))}</td><td>${item.questions_asked} / ${item.questions_closed}</td></tr>`;
    }).join("") : '<tr><td class="empty-row" colspan="8">За вибраний період контактів немає.</td></tr>';
    labelTables();
  }

  async function loadAnalytics() {
    const form = $("#analytics-filter");
    const params = new URLSearchParams();
    if (form.elements.date_from.value) params.set("date_from", form.elements.date_from.value);
    if (form.elements.date_to.value) params.set("date_to", form.elements.date_to.value);
    try {
      state.analytics = await api(`/api/v1/analytics?${params}`);
      renderAnalytics();
      if (!$("#analytics-month").options.length) fillMonthOptions(state.analytics.period.date_to.slice(0, 7));
    } catch (error) { toast(error.message, true); }
  }

  async function downloadMonthlyPdf() {
    const month = $("#analytics-month").value;
    if (!month) throw new Error("Оберіть місяць");
    if (tg?.initData) {
      const result = await api(`/api/v1/analytics/monthly-link?month=${encodeURIComponent(month)}`, { method: "POST" });
      tg.openLink(result.url);
      return "opened";
    }
    const response = await fetch(`/api/v1/analytics/monthly.pdf?month=${encodeURIComponent(month)}`, {
      credentials: "same-origin",
      headers: authHeaders(),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || "Не вдалося сформувати PDF");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `personal-secretary-${month}.pdf`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return "downloaded";
  }

  function labelTables() {
    $$("table").forEach(table => {
      const labels = $$("thead th", table).map(node => node.textContent);
      $$("tbody tr", table).forEach(row => $$("td", row).forEach((cell, index) => { cell.dataset.label = labels[index] || ""; }));
    });
  }

  function bindEvents() {
    $("#preview-form").addEventListener("submit", event => {
      event.preventDefault(); const form = event.currentTarget;
      withBusyButton($("button[type=submit]", form), "Перевіряємо…", async () => {
        const result = await api("/api/v1/preview", {method:"POST", body:JSON.stringify({text:form.elements.text.value, contact_id:state.previewContact?.contact_id || null})});
        const draftNote = state.previewContact && $("#contact-form").dataset.dirty === "true" ? "Незбережені зміни контакту не враховано. " : "";
        const decision = result.decision === "allowed" ? (result.dry_run ? "Лише прев’ю вам" : "Бот відповість") : actionLabels[result.decision] || result.decision;
        const templateNote = result.forced_template ? "шаблон персональний для контакту" : `тип «${categoryLabels[result.category] || result.category}»`;
        $("#preview-result").textContent = `${draftNote}${decision}: ${templateNote}. ${result.text}`;
      });
    });
    $("#preview-clear-contact").addEventListener("click", () => { state.previewContact = null; renderPreviewScope(); });
    $("#attention").addEventListener("click", (event) => {
      const button = event.target.closest("#retry-notifications");
      if (!button) return;
      withBusyButton(button, "Повторюємо…", async () => {
        const fresh = await api("/api/v1/notifications/retry", {method:"POST"});
        state.bootstrap.connection = fresh.connection; state.bootstrap.status = fresh.status;
        renderStatus(); toast("Сповіщення поставлено в чергу повторно");
      });
    });
    $("#retry-load").addEventListener("click", () => location.reload());
    $("#more-contacts").addEventListener("click", () => loadContacts(true));
    $("#more-logs").addEventListener("click", () => loadLogs(true));
    $$("form").filter(form => !form.id.endsWith("filter") && form.id !== "preview-form").forEach(form => {
      const track = (event) => { if (!event.target.closest?.("[data-no-dirty]")) refreshDirty(form); };
      form.addEventListener("input", track);
      form.addEventListener("change", track);
    });
    window.addEventListener("beforeunload", event => {
      if ($$("form[data-dirty=true]").length) { event.preventDefault(); event.returnValue = ""; }
    });
    document.addEventListener("visibilitychange", refreshStatus);
    window.addEventListener("focus", refreshStatus);
    const control = async (action) => {
      const fresh = await api("/api/v1/control", {method:"POST", body:JSON.stringify({action, confirmed:action === "live"})});
      state.bootstrap.connection = fresh.connection; state.bootstrap.status = fresh.status;
    };
    // One change at a time: the labels come from the server state, so they are
    // re-rendered afterwards instead of being restored like a plain busy button.
    async function changeMode(steps, message) {
      if (state.controlBusy) return;
      state.controlBusy = true;
      renderStatus();
      try {
        for (const action of steps) await control(action);
        toast(message);
      } catch (error) {
        toast(error.message, true);
        tg?.HapticFeedback?.notificationOccurred?.("error");
      } finally {
        state.controlBusy = false;
        renderStatus();
      }
    }
    $$("[data-mode]").forEach((button) => button.addEventListener("click", () => {
      const mode = button.dataset.mode;
      const { connection } = state.bootstrap;
      if (state.controlBusy || mode === currentMode(connection)) return;
      if (mode === "live" && !window.confirm("Секретар почне надсилати клієнтам реальні відповіді за розкладом. Увімкнути?")) return;
      // Leaving "off" also lifts the kill switch; a pause is cleared with it.
      const steps = mode === "off" ? ["stop"] : [mode === "live" ? "live" : "dry_run", ...(connection.kill_switch ? ["resume"] : [])];
      changeMode(steps, "Режим змінено");
    }));
    $("#pause-toggle").addEventListener("click", (event) => {
      const action = event.currentTarget.dataset.control;
      changeMode([action], action === "pause" ? "Пауза на 1 годину" : "Паузу знято");
    });
    // Rows that open a view can be rendered later (the attention list), so listen once.
    document.addEventListener("click", (event) => {
      const target = event.target.closest("[data-view]");
      if (!target || target.disabled) return;
      // "Нові контакти" promises the list: a card left open earlier is closed,
      // unless it has unsaved edits, which are kept.
      if (target.hasAttribute("data-contact-list") && state.selectedContact && $("#contact-form").dataset.dirty !== "true") closeContact();
      navigate(target.dataset.view, { via: target.closest("#navigation") ? "tab" : "link" });
    });
    $("#back-button").addEventListener("click", goBack);
    window.matchMedia?.("(min-width: 900px)")?.addEventListener?.("change", () => { if (state.bootstrap) renderBackButton(); });
    $("#create-invite").addEventListener("click", (event) => withBusyButton(event.currentTarget, "Створюємо…", async () => {
      const result = await api("/api/v1/access/invites", { method: "POST" });
      $("#invite-url").value = result.url;
      $("#invite-result").classList.remove("hidden");
      toast("Посилання створено");
    }));
    $("#copy-invite").addEventListener("click", async () => {
      try { await copyText($("#invite-url").value); toast("Посилання скопійовано"); }
      catch (error) { toast(error.message, true); }
    });
    $("#refresh-users").addEventListener("click", loadUsers);
    $("#access-users").addEventListener("click", (event) => {
      const button = event.target.closest("[data-access-action]");
      if (!button) return;
      const action = button.dataset.accessAction;
      if (action === "revoke" && !window.confirm("Відкликати доступ цього користувача?")) return;
      withBusyButton(button, "Змінюємо…", async () => {
        const result = await api(`/api/v1/access/users/${button.dataset.userId}/${action}`, { method: "POST" });
        toast(action === "approve" ? (result.notified ? "Доступ підтверджено, користувача сповіщено" : "Доступ підтверджено. Не вдалося сповістити користувача") : "Доступ відкликано", action === "approve" && !result.notified);
        await loadUsers();
      });
    });
    ["delay_min_seconds", "delay_max_seconds", "bot_delay_seconds"].forEach((name) => {
      $("#delivery-form").elements[name].addEventListener("input", renderDelayRanges);
    });
    $$("#delivery-form [name=sender_identity]").forEach((input) => input.addEventListener("change", renderDelayRanges));
    $("#delivery-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      const rawLimit = Number(form.elements.max_auto_replies_per_window.value);
      const maximum = Number(form.elements.delay_max_seconds.value);
      // The hidden sender's minimum is kept, but clamped so the server accepts it.
      const botMinimum = Math.max(1, Math.min(Number(form.elements.bot_delay_seconds.value) || 1, Math.min(maximum, 60)));
      const ownerMinimum = Math.max(0, Math.min(Number(form.elements.delay_min_seconds.value) || 0, maximum));
      const bot = form.elements.sender_identity.value === "bot";
      state.bootstrap.delivery = await api("/api/v1/delivery", { method: "PUT", body: JSON.stringify({ sender_identity: form.elements.sender_identity.value, delay_min_seconds: bot ? ownerMinimum : Number(form.elements.delay_min_seconds.value), delay_max_seconds: maximum, bot_delay_seconds: bot ? Number(form.elements.bot_delay_seconds.value) : botMinimum, mark_read: form.elements.mark_read.checked, max_auto_replies_per_window: rawLimit || null }) });
      fillDelivery();
      renderStatus();
    }); });
    $("#escalation-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      state.bootstrap.escalation = await api("/api/v1/escalation", { method: "PUT", body: JSON.stringify({ enabled: form.elements.enabled.checked, price_amount: form.elements.price_amount.value, currency: form.elements.currency.value.trim().toUpperCase(), offer_text: form.elements.offer_text.value, confirm_text: form.elements.confirm_text.value, decline_text: form.elements.decline_text.value }) });
      fillEscalation();
    }); });
    $("#add-schedule-window").addEventListener("click", () => { createWindow($("#schedule-windows")); refreshDirty($("#schedule-form")); });
    $("#schedule-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const windows = windowsPayload($("#schedule-windows"));
      if (!windows.length) throw new Error("Додайте хоча б одне вікно");
      state.bootstrap.schedule = await api("/api/v1/schedule", { method: "PUT", body: JSON.stringify({ timezone: event.currentTarget.elements.timezone.value, windows }) });
      if (state.selectedContact) {
        renderContactMeta(state.selectedContact);
        renderContactScheduleEditor();
      }
      await refreshStatus();
      renderStatus();
    }); });
    let searchTimer;
    $("#contact-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadContacts, 250); });
    $("#contact-form").addEventListener("change", (event) => { if (event.target.name === "exclusion") renderExclusionUntil(); });
    $("#add-contact-window").addEventListener("click", () => {
      const container = $("#contact-windows");
      if (!$(".window-row", container)) {
        const inheritedWindows = state.bootstrap.schedule.windows.filter((window) => window.is_active);
        (inheritedWindows.length ? inheritedWindows : [undefined]).forEach((window) => createWindow(container, window));
      } else {
        createWindow(container);
      }
      renderContactScheduleEditor();
      refreshDirty($("#contact-form"));
    });
    // A one-tap personal schedule: this contact is answered around the clock.
    $("#contact-all-day").addEventListener("click", () => {
      const container = $("#contact-windows");
      container.innerHTML = "";
      createWindow(container, { weekday_mask: 127, time_from: WHOLE_DAY, time_to: WHOLE_DAY, is_active: true });
      renderContactScheduleEditor();
      refreshDirty($("#contact-form"));
    });
    $("#reset-contact-windows").addEventListener("click", () => {
      if (!window.confirm("Повернути основний розклад? Зміна набуде чинності після збереження.")) return;
      $("#contact-windows").innerHTML = "";
      renderContactScheduleEditor();
      refreshDirty($("#contact-form"));
    });
    $("#contact-form").addEventListener("submit", (event) => { event.preventDefault(); if (!state.selectedContact) return; submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      const exclusion = form.elements.exclusion.value;
      const rawUntil = form.elements.exclusion_until.value;
      const saved = await api(`/api/v1/contacts/${state.selectedContact.contact_id}`, { method: "PUT", body: JSON.stringify({ exclusion, exclusion_until: exclusion === "until" ? ui.resolveDateTime(rawUntil, form.dataset.exclusionOriginal || null, state.bootstrap.schedule.timezone) : null, windows: windowsPayload($("#contact-windows")) }) });
      const index = state.contacts.findIndex((item) => item.contact_id === saved.contact_id);
      if (index >= 0) state.contacts[index] = saved;
      state.selectedContact = saved;
      renderContacts();
      fillContactForm(saved);
      renderBackButton();
      loadContactStats();
    }); });
    $("#add-direction").addEventListener("click", () => {
      if ($$(".direction-card").length >= 30) { toast("Можна додати до 30 типів"); return; }
      // Appended on its own, so the other cards keep their open sections and text as typed.
      $("#direction-list").insertAdjacentHTML("beforeend", directionCard({code: `type_${crypto.randomUUID().replaceAll("-", "")}`, label: "", description: "", keywords: [], reply_template: "", is_active: true}));
      const card = $$(".direction-card").at(-1);
      renderDirectionHead(card);
      $(".direction-template", card).focus();
      card.scrollIntoView({behavior: "smooth", block: "center"});
      refreshClassifierRules();
      refreshDirty($("#classifier-form"));
    });
    $("#direction-list").addEventListener("click", (event) => {
      if (!event.target.closest(".remove-direction")) return;
      event.target.closest(".direction-card").remove();
      refreshClassifierRules();
      refreshDirty($("#classifier-form"));
    });
    $("#classifier-form").elements.system_prompt.addEventListener("input", refreshClassifierRules);
    const onTypeEdit = (event) => {
      if (event.target.matches(".direction-label, .direction-active")) renderDirectionHead(event.target.closest(".direction-card"));
      refreshClassifierRules();
    };
    $("#direction-list").addEventListener("input", onTypeEdit);
    $("#direction-list").addEventListener("change", onTypeEdit);
    // Changed types need a fresh AI instruction; saving produces it first, so the
    // owner never has to run a separate generation step.
    async function regenerateRules(form) {
      const payload = classifierPayload();
      const snapshot = JSON.stringify(payload);
      const result = await api("/api/v1/classifier/expand", {method: "POST", body: JSON.stringify(payload)});
      const keywordsByCode = new Map((result.directions || []).map((d) => [d.code, d.keywords || []]));
      if (keywordsByCode.size !== payload.directions.length || payload.directions.some((d) => !keywordsByCode.has(d.code))) {
        throw new Error("ШІ повернув неповний результат.");
      }
      // inert is missing in older WebViews: never apply rules to types edited meanwhile.
      if (JSON.stringify(classifierPayload()) !== snapshot) {
        const error = new Error("Типи змінилися під час оновлення правил. Збережіть ще раз.");
        error.abort = true;
        throw error;
      }
      $$(".direction-card").forEach((card) => {
        $(".direction-keywords", card).value = keywordsByCode.get(card.dataset.code).join(", ");
      });
      form.elements.system_prompt.value = result.system_prompt;
      rememberRules();
    }
    $("#classifier-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      if (!validateClassifierForm(form)) return;
      // The form is locked while the slow AI call and the save run, so what is
      // saved is exactly what the instruction was generated for.
      form.inert = true;
      form.setAttribute("aria-busy", "true");
      submit(form, async () => {
        if (form.dataset.promptStale === "true" && form.dataset.promptEdited === "true"
          && !window.confirm("Типи змінилися, тому інструкцію ШІ буде оновлено, а ваші ручні правки в ній замінено. Зберегти?")) return false;
        if (form.dataset.promptStale === "true") {
          $("#save-classifier").textContent = "Оновлюємо правила…";
          try { await regenerateRules(form); }
          catch (error) {
            if (error.abort) throw error;
            if (!window.confirm(`${error.message} Зберегти без оновлення правил ШІ?`)) return false;
          }
          $("#save-classifier").textContent = "Зберігаємо…";
        }
        state.bootstrap.classifier = await api("/api/v1/classifier", { method: "PUT", body: JSON.stringify(classifierPayload()) });
        fillClassifier();
      }).finally(() => { form.inert = false; form.removeAttribute("aria-busy"); });
    });
    $("#summary-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      const enableRetention = form.elements.message_retention_enabled.checked;
      if (enableRetention && !state.bootstrap.summary.message_retention_enabled) {
        const accepted = window.confirm("Увімкнути зашифроване зберігання текстів повідомлень на строк до 48 годин для формування добового підсумку?");
        if (!accepted) {
          form.elements.message_retention_enabled.checked = false;
          refreshDirty(form);
          toast("Зберігання залишилось вимкненим");
          return false;
        }
      }
      try {
        state.bootstrap.summary = await api("/api/v1/summary", { method: "PUT", body: JSON.stringify({ summary_time: form.elements.summary_time.value, summary_channel_id: form.elements.summary_channel_id.value ? Number(form.elements.summary_channel_id.value) : null, message_retention_enabled: enableRetention }) });
      } catch (error) {
        form.elements.message_retention_enabled.checked = state.bootstrap.summary.message_retention_enabled;
        refreshDirty(form);
        throw error;
      }
      fillSummary();
    }); });
    $("#choose-summary-channel").addEventListener("click", (event) => withBusyButton(event.currentTarget, "Відкриваємо Telegram…", async () => {
      if (!tg?.requestChat) throw new Error("Відкрийте Mini App у Telegram або скористайтеся посиланням нижче");
      const request = await api("/api/v1/summary/channel-request", { method: "POST" });
      const sent = await new Promise((resolve) => tg.requestChat(request.prepared_id, resolve));
      if (!sent) {
        toast("Вибір каналу скасовано");
        return;
      }
      toast("Перевіряємо доступ до каналу…");
      await pollChannelRequest(request.request_id);
    }));
    $("#connect-summary-channel-link").addEventListener("click", (event) => withBusyButton(event.currentTarget, "Перевіряємо…", async () => {
      const input = $("#summary-channel-reference");
      const reference = input.value.trim();
      if (!reference) throw new Error("Вставте посилання на допис або @username каналу");
      state.bootstrap.summary = await api("/api/v1/summary/channel", { method: "POST", body: JSON.stringify({ reference }) });
      input.value = "";
      fillSummary();
      toast("Канал перевірено й підключено");
      tg?.HapticFeedback?.notificationOccurred?.("success");
    }));
    $("#disconnect-summary-channel").addEventListener("click", (event) => withBusyButton(event.currentTarget, "Відключаємо…", async () => {
      const accepted = window.confirm("Відключити канал? Наступні підсумки надходитимуть в особистий чат із ботом.");
      if (!accepted) return;
      state.bootstrap.summary = await api("/api/v1/summary/channel", { method: "DELETE" });
      fillSummary();
      toast("Канал відключено");
    }));
    $("#log-filter").elements.action.innerHTML += actions.map((action) => `<option value="${action}">${escapeHtml(actionLabels[action] || action)}</option>`).join("");
    $("#log-filter").addEventListener("submit", (event) => { event.preventDefault(); loadLogs(); });
    $("#analytics-filter").addEventListener("submit", (event) => { event.preventDefault(); loadAnalytics(); });
    $("#download-monthly-pdf").addEventListener("click", (event) => withBusyButton(event.currentTarget, "Формуємо PDF…", async () => {
      const result = await downloadMonthlyPdf();
      toast(result === "opened" ? "PDF відкрито у браузері" : "PDF збережено у завантаження браузера");
    }));
    $$(".browser-link-action").forEach((button) => button.addEventListener("click", async () => { try { const result = await api("/api/v1/auth/browser-link", { method: "POST" }); await copyText(result.url); toast("Одноразове посилання скопійовано"); } catch (error) { toast(error.message, true); } }));
    $("#logout").addEventListener("click", async () => { await api("/api/v1/auth/logout", { method: "POST" }); location.reload(); });
  }

  async function init() {
    bindEvents();
    try {
      state.bootstrap = await api("/api/v1/bootstrap");
    } catch (error) {
      $("#loading-state").classList.add("hidden");
      if (error.status === 401 || error.status === 403) $("#auth-state").classList.remove("hidden");
      else {
        $("#load-error").classList.remove("hidden");
        if (error.status === 409) $("#load-error h2").textContent = "Завершіть підключення";
        $("#load-error-message").textContent = error.status === 409
          ? "Підключіть бота в Chat Automation. Після підключення бот проведе вас через решту налаштувань."
          : "Перевірте інтернет-з’єднання та повторіть завантаження.";
      }
      $("#app").setAttribute("aria-busy", "false");
      return;
    }
    $("#loading-state").classList.add("hidden");
    $("#views").classList.remove("hidden");
    $("#users-nav").classList.toggle("hidden", state.bootstrap.user.role !== "master");
    if (!tg?.initData) $("#logout").classList.remove("hidden");
    fillSchedule(); renderStatus(); fillDelivery(); fillEscalation(); fillClassifier(); fillSummary();
    // Reply templates now live on the request types; keep old #templates links working.
    const requested = location.hash.slice(1) === "templates" ? "classifier" : location.hash.slice(1);
    navigate(titles[requested] ? requested : "overview");
    $("#app").setAttribute("aria-busy", "false");
  }

  init();
})();
