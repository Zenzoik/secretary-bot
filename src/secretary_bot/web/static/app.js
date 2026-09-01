(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  const state = { bootstrap: null, contacts: [], selectedContact: null, activeView: "overview" };
  const titles = {
    overview: "Огляд", schedule: "Розклад", contacts: "Контакти",
    templates: "Шаблони", classifier: "Класифікація", summary: "Самарі", logs: "Журнал",
  };
  const actions = ["replied", "dry_run", "skipped_schedule", "skipped_excluded", "skipped_owner_replied", "skipped_window_limit", "skipped_kill_switch", "skipped_inactive", "skipped_unsupported_content", "error"];
  const actionLabels = {
    replied: "Відповів", dry_run: "Прев’ю", skipped_schedule: "Поза розкладом",
    skipped_excluded: "Виключено", skipped_owner_replied: "Власник відповів",
    skipped_window_limit: "Ліміт вікна", skipped_kill_switch: "Вимкнено",
    skipped_inactive: "Неактивне", skipped_unsupported_content: "Непідтримуване", error: "Помилка",
  };
  const timezones = ["Europe/Kyiv", "Europe/Prague", "Europe/Warsaw", "Europe/Berlin", "UTC"];
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  tg?.ready();
  tg?.expand();
  tg?.setHeaderColor?.("#101925");
  tg?.setBackgroundColor?.("#0d141f");

  function authHeaders() {
    const headers = { "Content-Type": "application/json" };
    if (tg?.initData) headers["X-Telegram-Init-Data"] = tg.initData;
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

  function formatDate(value) {
    if (!value) return "—";
    return new Intl.DateTimeFormat("uk-UA", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }

  async function submit(form, callback) {
    const button = $("button[type=submit]", form);
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Зберігаємо…";
    try {
      await callback();
      toast("Збережено");
      tg?.HapticFeedback?.notificationOccurred?.("success");
    } catch (error) {
      toast(error.message, true);
      tg?.HapticFeedback?.notificationOccurred?.("error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
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

  function navigate(view) {
    if (!titles[view]) return;
    state.activeView = view;
    $$("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
    $$("[data-view-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.viewPanel === view));
    $("#page-title").textContent = titles[view];
    history.replaceState(null, "", `#${view}`);
    if (view === "contacts") loadContacts();
    if (view === "logs") loadLogs();
  }

  function renderStatus() {
    const { connection, delivery } = state.bootstrap;
    const live = !connection.dry_run && connection.is_active && !connection.kill_switch;
    const rights = connection.rights || {};
    $("#connection-pill").className = `connection-pill ${live ? "live" : connection.is_active ? "" : "off"}`;
    $("#connection-pill span:last-child").textContent = live ? "Live" : connection.dry_run ? "Dry-run" : "Зупинено";
    $("#status-grid").innerHTML = [
      ["Режим", live ? "Live" : connection.dry_run ? "Dry-run" : "Зупинено", live ? "Відповіді активні" : "Перевірте стан"],
      ["Відправник", delivery.sender_identity === "bot" ? "Секретар" : "Власник", delivery.sender_identity === "bot" ? "З видимим підписом" : "Без підпису"],
      ["Затримка", delivery.sender_identity === "bot" ? `${delivery.bot_delay_seconds}–${Math.min(delivery.delay_max_seconds, 60)} с` : `${delivery.delay_min_seconds}–${delivery.delay_max_seconds} с`, "Випадковий інтервал"],
      ["Права", rights.can_reply ? (rights.can_read_messages ? "Відповідь + читання" : "Тільки відповідь") : "Немає відповіді", rights.can_reply ? "Telegram Business" : "Потрібна увага"],
    ].map(([label, value, note], index) => `<article class="status-card ${index === 3 && !rights.can_reply ? "attention" : ""}"><small>${label}</small><strong>${value}</strong><span>${note}</span></article>`).join("");
  }

  function fillDelivery() {
    const form = $("#delivery-form");
    const data = state.bootstrap.delivery;
    $(`input[name=sender_identity][value=${data.sender_identity}]`, form).checked = true;
    ["delay_min_seconds", "delay_max_seconds", "bot_delay_seconds"].forEach((name) => { form.elements[name].value = data[name]; });
    form.elements.mark_read.checked = data.mark_read;
    renderDelayRanges();
  }

  function renderDelayRanges() {
    const form = $("#delivery-form");
    const ownerMin = form.elements.delay_min_seconds.value;
    const botMin = form.elements.bot_delay_seconds.value;
    const maximum = form.elements.delay_max_seconds.value;
    $("#bot-delay-range").textContent = botMin && maximum ? `${botMin}–${maximum} с` : "—";
    $("#owner-delay-range").textContent = ownerMin && maximum ? `${ownerMin}–${maximum} с` : "—";
  }

  function createWindow(container, data = { weekday_mask: 127, time_from: "22:00", time_to: "08:00", is_active: true }) {
    const node = $("#window-template").content.firstElementChild.cloneNode(true);
    $(".weekday-mask", node).value = String(data.weekday_mask);
    $(".time-from", node).value = data.time_from.slice(0, 5);
    $(".time-to", node).value = data.time_to.slice(0, 5);
    $(".is-active", node).checked = data.is_active;
    $(".remove-window", node).addEventListener("click", () => node.remove());
    container.append(node);
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
  }

  function fillTemplates() {
    const form = $("#templates-form");
    form.elements.off_hours_default.value = state.bootstrap.templates.off_hours_default;
    form.elements.money_priority.value = state.bootstrap.templates.money_priority;
  }

  function fillClassifier() {
    const form = $("#classifier-form");
    const data = state.bootstrap.classifier;
    $("#direction-list").innerHTML = data.directions.map((direction) => `
      <article class="direction-card" data-code="${direction.code}">
        <label>Назва<input class="direction-label" maxlength="80" value="${escapeHtml(direction.label)}" required></label>
        <label>Опис<input class="direction-description" maxlength="500" value="${escapeHtml(direction.description)}" required></label>
        <label class="keywords">Ключові слова, через кому<input class="direction-keywords" value="${escapeHtml(direction.keywords.join(", "))}"></label>
        <label class="switch-row"><span><strong>Напрямок активний</strong><small>${direction.code}</small></span><input class="direction-active" type="checkbox" role="switch" ${direction.is_active ? "checked" : ""} ${direction.code === "general" ? "disabled" : ""}></label>
      </article>`).join("");
    form.elements.system_prompt.value = data.system_prompt;
    form.elements.model.value = data.model;
    form.elements.confidence_min.value = data.confidence_min;
  }

  function fillSummary() {
    const form = $("#summary-form");
    form.elements.summary_time.value = state.bootstrap.summary.summary_time;
    form.elements.summary_channel_id.value = state.bootstrap.summary.summary_channel_id ?? "";
  }

  async function loadContacts() {
    const search = $("#contact-search").value.trim();
    try {
      const result = await api(`/api/v1/contacts?search=${encodeURIComponent(search)}`);
      state.contacts = result.items;
      renderContacts();
    } catch (error) { toast(error.message, true); }
  }

  function renderContacts() {
    const list = $("#contact-list");
    if (!state.contacts.length) {
      list.innerHTML = '<div class="empty-row">Контакти з’являться після першого вхідного повідомлення.</div>';
      return;
    }
    list.innerHTML = state.contacts.map((contact) => `<button type="button" class="contact-item ${state.selectedContact?.contact_id === contact.contact_id ? "active" : ""}" data-contact-id="${contact.contact_id}"><strong>${escapeHtml(contact.contact_name || `Контакт ${contact.contact_id}`)}</strong><small>${formatDate(contact.last_incoming_at)} · ${contact.auto_reply_count} відповідей</small></button>`).join("");
    $$(".contact-item", list).forEach((button) => button.addEventListener("click", () => selectContact(Number(button.dataset.contactId))));
  }

  function selectContact(contactId) {
    state.selectedContact = state.contacts.find((contact) => contact.contact_id === contactId);
    if (!state.selectedContact) return;
    renderContacts();
    const form = $("#contact-form");
    form.classList.remove("empty");
    $("#contact-empty").classList.add("hidden");
    $("#contact-fields").classList.remove("hidden");
    $("#contact-title").textContent = state.selectedContact.contact_name || `Контакт ${contactId}`;
    $("#contact-meta").textContent = `ID ${contactId} · останнє повідомлення ${formatDate(state.selectedContact.last_incoming_at)}`;
    $(`input[name=exclusion][value=${state.selectedContact.exclusion}]`, form).checked = true;
    form.elements.exclusion_until.value = state.selectedContact.exclusion_until ? new Date(state.selectedContact.exclusion_until).toISOString().slice(0, 16) : "";
    const windows = $("#contact-windows");
    windows.innerHTML = "";
    state.selectedContact.windows.forEach((window) => createWindow(windows, window));
  }

  async function loadLogs() {
    const form = $("#log-filter");
    const params = new URLSearchParams();
    if (form.elements.contact_id.value) params.set("contact_id", form.elements.contact_id.value);
    if (form.elements.action.value) params.set("action", form.elements.action.value);
    try {
      const result = await api(`/api/v1/logs?${params}`);
      $("#log-rows").innerHTML = result.items.length ? result.items.map((row) => `<tr><td>${formatDate(row.occurred_at)}</td><td>${row.contact_id}</td><td>${escapeHtml(actionLabels[row.action] || row.action)}</td><td>${escapeHtml(row.category || "—")}</td><td>${escapeHtml(row.error_code || row.template_code || "—")}</td></tr>`).join("") : '<tr><td class="empty-row" colspan="5">За вибраними фільтрами записів немає.</td></tr>';
    } catch (error) { toast(error.message, true); }
  }

  function bindEvents() {
    $$("[data-view]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
    ["delay_min_seconds", "delay_max_seconds", "bot_delay_seconds"].forEach((name) => {
      $("#delivery-form").elements[name].addEventListener("input", renderDelayRanges);
    });
    $("#delivery-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      state.bootstrap.delivery = await api("/api/v1/delivery", { method: "PUT", body: JSON.stringify({ sender_identity: form.elements.sender_identity.value, delay_min_seconds: Number(form.elements.delay_min_seconds.value), delay_max_seconds: Number(form.elements.delay_max_seconds.value), bot_delay_seconds: Number(form.elements.bot_delay_seconds.value), mark_read: form.elements.mark_read.checked }) });
      renderStatus();
    }); });
    $("#add-schedule-window").addEventListener("click", () => createWindow($("#schedule-windows")));
    $("#schedule-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const windows = windowsPayload($("#schedule-windows"));
      if (!windows.length) throw new Error("Додайте хоча б одне вікно");
      state.bootstrap.schedule = await api("/api/v1/schedule", { method: "PUT", body: JSON.stringify({ timezone: event.currentTarget.elements.timezone.value, windows }) });
    }); });
    let searchTimer;
    $("#contact-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadContacts, 250); });
    $("#add-contact-window").addEventListener("click", () => createWindow($("#contact-windows")));
    $("#contact-form").addEventListener("submit", (event) => { event.preventDefault(); if (!state.selectedContact) return; submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      const exclusion = form.elements.exclusion.value;
      const rawUntil = form.elements.exclusion_until.value;
      const saved = await api(`/api/v1/contacts/${state.selectedContact.contact_id}`, { method: "PUT", body: JSON.stringify({ exclusion, exclusion_until: exclusion === "until" && rawUntil ? new Date(rawUntil).toISOString() : null, windows: windowsPayload($("#contact-windows")) }) });
      const index = state.contacts.findIndex((item) => item.contact_id === saved.contact_id);
      state.contacts[index] = saved;
      state.selectedContact = saved;
      selectContact(saved.contact_id);
    }); });
    $("#templates-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      state.bootstrap.templates = await api("/api/v1/templates", { method: "PUT", body: JSON.stringify({ off_hours_default: form.elements.off_hours_default.value, money_priority: form.elements.money_priority.value }) });
      fillTemplates();
    }); });
    $("#classifier-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      const directions = $$(".direction-card").map((card) => ({ code: card.dataset.code, label: $(".direction-label", card).value, description: $(".direction-description", card).value, keywords: $(".direction-keywords", card).value.split(",").map((item) => item.trim()).filter(Boolean), is_active: card.dataset.code === "general" || $(".direction-active", card).checked }));
      state.bootstrap.classifier = await api("/api/v1/classifier", { method: "PUT", body: JSON.stringify({ directions, system_prompt: form.elements.system_prompt.value, model: form.elements.model.value, confidence_min: form.elements.confidence_min.value }) });
      fillClassifier();
    }); });
    $("#summary-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      state.bootstrap.summary = await api("/api/v1/summary", { method: "PUT", body: JSON.stringify({ summary_time: form.elements.summary_time.value, summary_channel_id: form.elements.summary_channel_id.value ? Number(form.elements.summary_channel_id.value) : null }) });
    }); });
    $("#log-filter").elements.action.innerHTML += actions.map((action) => `<option value="${action}">${escapeHtml(actionLabels[action] || action)}</option>`).join("");
    $("#log-filter").addEventListener("submit", (event) => { event.preventDefault(); loadLogs(); });
    $$(".browser-link-action").forEach((button) => button.addEventListener("click", async () => { try { const result = await api("/api/v1/auth/browser-link", { method: "POST" }); await copyText(result.url); toast("Одноразове посилання скопійовано"); } catch (error) { toast(error.message, true); } }));
    $("#logout").addEventListener("click", async () => { await api("/api/v1/auth/logout", { method: "POST" }); location.reload(); });
  }

  async function init() {
    bindEvents();
    try {
      state.bootstrap = await api("/api/v1/bootstrap");
    } catch (error) {
      $("#loading-state").classList.add("hidden");
      $("#auth-state").classList.remove("hidden");
      $("#app").setAttribute("aria-busy", "false");
      return;
    }
    $("#loading-state").classList.add("hidden");
    $("#views").classList.remove("hidden");
    if (!tg?.initData) $("#logout").classList.remove("hidden");
    renderStatus(); fillDelivery(); fillSchedule(); fillTemplates(); fillClassifier(); fillSummary();
    const requested = location.hash.slice(1);
    navigate(titles[requested] ? requested : "overview");
    $("#app").setAttribute("aria-busy", "false");
  }

  init();
})();
